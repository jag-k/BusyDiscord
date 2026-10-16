import json
import unittest

from unittest.mock import AsyncMock

import httpx2

from busylib import AsyncBusyBar
from busylib.exceptions import BusyBarAPIError
from busylib.types import DisplayElements

from busydiscord.bar_display import (
    APP_NAME,
    ELEMENT_TIMEOUT,
    GLYPH_FILES,
    ICON_FILES,
    BarDisplay,
    display_payload,
)


class DisplayTests(unittest.TestCase):
    def test_display_prefers_server_duration(self):
        payload = display_payload({"status": "in_voice", "observed_seconds": 5, "elapsed_seconds": 125})
        elements = {e["id"]: e for e in payload["elements"]}
        self.assertEqual(elements["front-elapsed"]["type"], "countdown")
        self.assertEqual(elements["front-elapsed"]["direction"], "time_since")
        self.assertEqual(elements["front-elapsed"]["timestamp"], elements["back-elapsed"]["timestamp"])

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
                self.assertEqual(elements["front-elapsed"].direction, "time_since")
                expected_color = {"MIC": "#33FF66FF", "MUTE": "#FF3333FF", "DEAF": "#FFB000FF"}[expected]
                self.assertEqual(elements["front-elapsed"].color, expected_color)
                self.assertEqual(elements["back-elapsed"].color, "#FFFFFFFF")
                self.assertEqual(elements["back-mic"].text, "MICROPHONE: " + ("ON" if expected == "MIC" else "OFF"))
                self.assertEqual(elements["back-audio"].text, "AUDIO: " + ("OFF" if expected == "DEAF" else "ON"))
                for flag, reason in [
                    ("mute", "Server muted"),
                    ("suppress", "Stage suppressed"),
                    ("self_mute", "You muted"),
                ]:
                    if flags.get(flag):
                        self.assertIn(reason, elements["back-mic-reason"].text)
                for flag, reason in [("deaf", "Server deafened"), ("self_deaf", "You deafened")]:
                    if flags.get(flag):
                        self.assertIn(reason, elements["back-audio-reason"].text)
                self.assertNotIn("front-microphone", elements)
                self.assertNotIn("front-headphones", elements)
                self.assertEqual(elements["front-clock"].stock_path, "shared/images/clock_5x5.image")
                self.assertEqual(elements["front-clock"].x, 18)
                self.assertEqual(elements["front-elapsed"].x, 24)
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
                self.assertTrue(all(e.timeout == ELEMENT_TIMEOUT for e in model.elements))
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
        bar.display_clear.assert_not_awaited()
        elements = {e["id"]: e for e in bar.display_draw.call_args.args[0]["elements"]}
        self.assertEqual(elements["zzz-status-strike"]["section"], "hide")
        self.assertEqual(elements["zz-status-glyph"]["section"], "hide_mic")

    async def test_native_counter_advances_without_any_device_requests(self):
        bar = AsyncMock()
        display = BarDisplay(bar)
        state = {"status": "in_voice", "timer_started_at": 1700000000, "elapsed_seconds": 1}
        await display.update(state)
        bar.reset_mock()
        for elapsed in (2, 5, 30, 60, 3600):
            await display.update({**state, "elapsed_seconds": elapsed, "observed_seconds": elapsed})
        self.assertEqual(bar.mock_calls, [])
        elements = {e["id"]: e for e in display.last_payload["elements"]}
        for id in ("front-elapsed", "back-elapsed"):
            self.assertEqual(elements[id]["timestamp"], "1700000000")
            self.assertEqual(elements[id]["direction"], "time_since")
            self.assertEqual(elements[id]["timeout"], 0)


class StateChangeTests(unittest.IsolatedAsyncioTestCase):
    async def test_dismissed_screen_returns_only_after_a_state_change(self):
        bar = AsyncMock()
        display = BarDisplay(bar)
        state = {"status": "in_voice", "timer_started_at": 1700000000}
        await display.update(state)
        # Simulate closing the screen independently, as the physical Back button does.
        await bar.display_clear(application_name=APP_NAME)
        bar.reset_mock()
        for elapsed in (1, 10, 100):
            await display.update({**state, "elapsed_seconds": elapsed})
        self.assertEqual(bar.mock_calls, [])
        await display.update({**state, "self_mute": True, "elapsed_seconds": 101})
        bar.display_draw.assert_awaited_once()
        self.assertEqual(display.last_payload["elements"][-1]["section"], "show")

    async def test_priority_rejection_is_not_retried_until_state_changes(self):
        bar = AsyncMock()
        display = BarDisplay(bar)
        state = {"status": "in_voice", "timer_started_at": 1700000000}
        bar.display_draw.side_effect = BusyBarAPIError("Low priority", status_code=409)
        with self.assertRaises(BusyBarAPIError):
            await display.update(state)
        for elapsed in (1, 10, 60):
            await display.update({**state, "elapsed_seconds": elapsed})
        self.assertEqual(bar.display_draw.await_count, 1)
        with self.assertRaises(BusyBarAPIError):
            await display.update({**state, "self_mute": True})
        self.assertEqual(bar.display_draw.await_count, 2)

    async def test_stale_elements_after_dismissal_do_not_block_new_state(self):
        bar = AsyncMock()
        display = BarDisplay(bar)
        state = {"status": "in_voice", "timer_started_at": 1700000000, "self_mute": True}
        await display.update(state)
        bar.display_clear.side_effect = BusyBarAPIError("Bad Request", status_code=400)
        await display.update({"status": "unknown"})
        self.assertEqual(bar.display_draw.await_count, 2)
        self.assertEqual(display.last_state["status"], "unknown")

    async def test_leave_voice_clears_once_without_background_requests(self):
        bar = AsyncMock()
        display = BarDisplay(bar)
        await display.update({"status": "in_voice"})
        await display.update({"status": "outside_voice"})
        await display.update({"status": "outside_voice"})
        await display.close()
        bar.display_clear.assert_awaited_once_with(application_name=APP_NAME)
