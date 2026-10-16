import json
import unittest

from unittest.mock import AsyncMock, patch

import httpx2

from busylib import AsyncBusyBar
from busylib.exceptions import BusyBarAPIError
from busylib.types import DisplayElements

from busydiscord.bar_display import APP_NAME, GLYPH_FILES, ICON_FILES, PRESENCE_ELEMENT_ID, BarDisplay, display_payload


class DisplayTests(unittest.TestCase):
    def test_display_prefers_server_duration(self):
        payload = display_payload({"status": "in_voice", "observed_seconds": 5, "elapsed_seconds": 125})
        elements = {e["id"]: e for e in payload["elements"]}
        self.assertEqual(elements["front-status"]["text"], "MIC 02:05")
        self.assertEqual(elements["back-timer"]["text"], "02:05")

    def test_muting_and_deafening_control_front_and_back(self):
        for flags, expected in [
            ({}, "MIC"),
            ({"self_mute": True}, "MUTE"),
            ({"mute": True}, "MUTE"),
            ({"self_deaf": True}, "DEAF"),
            ({"deaf": True}, "DEAF"),
            ({"suppress": True}, "MUTE"),
        ]:
            with self.subTest(flags=flags):
                payload = display_payload(
                    {"status": "in_voice", "channel_name": "🐺• 2", "observed_seconds": 3661, **flags}
                )
                model = DisplayElements.model_validate(payload)
                elements = {e.id: e for e in model.elements}
                self.assertEqual(elements["front-channel"].text, "2")
                self.assertEqual(elements["front-status"].text, expected + " 1:01:01")
                self.assertEqual(elements["front-logo"].type, "image")
                self.assertEqual(elements["front-channel"].x, 18)
                self.assertEqual(elements["front-channel"].width, 54)
                self.assertEqual(elements["0-logo-background"].radius, 3)
                if expected == "MIC":
                    self.assertNotIn("zz-status-glyph", elements)
                    self.assertNotIn("zzz-status-strike", elements)
                    self.assertNotIn("z-status-background", elements)
                else:
                    self.assertEqual(
                        elements["zz-status-glyph"].path, GLYPH_FILES["headphones" if expected == "DEAF" else "mic"]
                    )
                    self.assertEqual(elements["zzz-status-strike"].section, "hold")
                self.assertTrue(all(e.timeout == 10 for e in model.elements))
        self.assertIsNone(display_payload({"status": "outside_voice"}))

    def test_norm_mute_deafen_norm_has_independent_transitions(self):
        normal = {"status": "in_voice"}
        muted = {**normal, "self_mute": True}
        deaf = {**muted, "self_deaf": True}
        for before, after, icon, strike in [
            (normal, muted, "show_mic", "show"),
            (muted, deaf, "to_headphones", "hold"),
            (deaf, muted, "to_mic", "hold"),
            (deaf, normal, "hide_headphones", "hide"),
            (muted, muted, "mic", "hold"),
            (muted, normal, "hide_mic", "hide"),
            (normal, deaf, "show_headphones", "show"),
            (None, deaf, "headphones", "hold"),
        ]:
            with self.subTest(before=before, after=after):
                elements = {e["id"]: e for e in display_payload(after, before)["elements"]}
                self.assertEqual(elements["zz-status-glyph"]["section"], icon)
                self.assertEqual(elements["zzz-status-strike"]["section"], strike)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_busylib_http_payload_and_scoped_cleanup(self):
        requests = []

        def handle(request):
            requests.append(request)
            if request.url.path == "/api/version":
                return httpx2.Response(200, json={"api_semver": "27.5.0"})
            return httpx2.Response(200, json={"result": "success"})

        async with AsyncBusyBar(
            "bar.local", token="test-only-token", transport=httpx2.MockTransport(handle), compatibility_mode="none"
        ) as bar:
            display = BarDisplay(bar)
            await display.update({"status": "in_voice", "channel_name": "General", "observed_seconds": 5})
            await display.update({"status": "outside_voice"})
            await display.close()
        draw_index = 1 + len(ICON_FILES)
        self.assertEqual([r.method for r in requests], ["GET", *["POST"] * (len(ICON_FILES) + 1), "DELETE"])
        for request in requests[1:draw_index]:
            self.assertEqual(request.url.path, "/api/assets/upload")
            self.assertTrue(request.content.startswith((b"\x89PNG", b"bicycle0")))
        self.assertEqual(requests[draw_index].url.path, "/api/display/draw")
        self.assertEqual(requests[draw_index].headers["X-API-Token"], "test-only-token")
        self.assertEqual(json.loads(requests[draw_index].content)["application_name"], APP_NAME)
        self.assertEqual(requests[draw_index + 1].url.params["application_name"], APP_NAME)

    async def test_unmuting_animates_strike_without_clearing_channel_or_logo(self):
        bar = AsyncMock()
        display = BarDisplay(bar)
        state = {"status": "in_voice", "observed_seconds": 5}
        await display.update({**state, "self_mute": True})
        await display.update(state)
        bar.display_clear.assert_awaited_once_with(application_name=APP_NAME, element_ids=[PRESENCE_ELEMENT_ID])
        elements = {e["id"]: e for e in bar.display_draw.call_args.args[0]["elements"]}
        self.assertEqual(elements["zzz-status-strike"]["section"], "hide")
        self.assertEqual(elements["zz-status-glyph"]["section"], "hide_mic")

    async def test_unchanged_status_is_refreshed_before_expiry(self):
        bar = AsyncMock()
        display = BarDisplay(bar)
        state = {"status": "unknown"}
        with patch("busydiscord.bar_display.time.monotonic", side_effect=[1, 1.5, 2]):
            await display.update(state)
            await display.update(state)
            await display.update(state)
        self.assertEqual(bar.display_draw.await_count, 2)


class ExitTests(unittest.IsolatedAsyncioTestCase):
    async def test_exit_waits_ten_seconds_and_returns_with_latest_state(self):
        bar = AsyncMock()
        display = BarDisplay(bar, report=lambda value: None)
        state = {"status": "in_voice", "elapsed_seconds": 10}
        with patch("busydiscord.bar_display.time.monotonic", return_value=1):
            await display.update(state)
        bar.display_clear.side_effect = BusyBarAPIError("Bad Request", status_code=400)
        with patch("busydiscord.bar_display.time.monotonic", return_value=2):
            await display.update({**state, "elapsed_seconds": 11})
        bar.display_clear.assert_awaited_once_with(application_name=APP_NAME, element_ids=[PRESENCE_ELEMENT_ID])
        self.assertEqual(display.resume_at, 12)
        self.assertFalse(display.owns_screen)
        bar.display_clear.side_effect = None
        for now in (3, 7, 11.99):
            with patch("busydiscord.bar_display.time.monotonic", return_value=now):
                await display.update({**state, "elapsed_seconds": 20, "self_mute": True})
        self.assertEqual(bar.display_draw.await_count, 1)
        self.assertEqual(bar.display_clear.await_count, 1)
        with patch("busydiscord.bar_display.time.monotonic", return_value=12):
            await display.update({**state, "elapsed_seconds": 21, "self_deaf": True})
        payload = bar.display_draw.call_args.args[0]
        status = next(e for e in payload["elements"] if e["id"] == "front-status")
        self.assertEqual(status["text"], "DEAF 00:21")
        self.assertTrue(display.owns_screen)

    async def test_leaving_voice_during_pause_does_not_restore_screen(self):
        bar = AsyncMock()
        display = BarDisplay(bar, report=lambda value: None)
        with patch("busydiscord.bar_display.time.monotonic", return_value=1):
            await display.update({"status": "in_voice"})
        bar.display_clear.side_effect = BusyBarAPIError("Bad Request", status_code=400)
        with patch("busydiscord.bar_display.time.monotonic", return_value=2):
            await display.update({"status": "in_voice", "self_mute": True})
        with patch("busydiscord.bar_display.time.monotonic", return_value=20):
            await display.update({"status": "outside_voice"})
        self.assertEqual(bar.display_draw.await_count, 1)
        await display.close()
        self.assertEqual(bar.display_clear.await_count, 1)

    async def test_network_error_is_not_treated_as_manual_exit(self):
        display = BarDisplay(AsyncMock())
        display.bar.display_clear.side_effect = BusyBarAPIError("Unauthorized", status_code=403)
        with self.assertRaises(BusyBarAPIError):
            await display.screen_still_open()
        self.assertEqual(display.resume_at, 0)

    async def test_expired_lease_is_redrawn_without_manual_exit_delay(self):
        bar = AsyncMock()
        display = BarDisplay(bar)
        with patch("busydiscord.bar_display.time.monotonic", return_value=1):
            await display.update({"status": "in_voice"})
        with patch("busydiscord.bar_display.time.monotonic", return_value=12):
            await display.update({"status": "in_voice", "elapsed_seconds": 11})
        self.assertEqual(bar.display_draw.await_count, 2)
        bar.display_clear.assert_not_awaited()
        self.assertEqual(display.resume_at, 0)
