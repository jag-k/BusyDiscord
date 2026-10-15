import json
import unittest

from unittest.mock import AsyncMock, patch

import httpx2

from busylib import AsyncBusyBar
from busylib.types import DisplayElements

from busydiscord.bar_display import APP_NAME, BarDisplay, display_payload


class DisplayTests(unittest.TestCase):
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
                    self.assertNotIn("zz-status-icon", elements)
                else:
                    expected_icon = "headphones-off.png" if expected == "DEAF" else "mic-off.png"
                    self.assertEqual(elements["zz-status-icon"].path, expected_icon)
                self.assertTrue(all(e.timeout == 10 for e in model.elements))
        self.assertIsNone(display_payload({"status": "outside_voice"}))


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
        self.assertEqual([r.method for r in requests], ["GET", "POST", "POST", "POST", "POST", "DELETE"])
        for request in requests[1:4]:
            self.assertEqual(request.url.path, "/api/assets/upload")
            self.assertTrue(request.content.startswith(b"\x89PNG"))
        self.assertEqual(requests[4].url.path, "/api/display/draw")
        self.assertEqual(requests[4].headers["X-API-Token"], "test-only-token")
        self.assertEqual(json.loads(requests[4].content)["application_name"], APP_NAME)
        self.assertEqual(requests[5].url.params["application_name"], APP_NAME)

    async def test_unmuting_removes_overlay_without_clearing_channel_or_logo(self):
        bar = AsyncMock()
        display = BarDisplay(bar)
        state = {"status": "in_voice", "observed_seconds": 5}
        await display.update({**state, "self_mute": True})
        await display.update(state)
        bar.display_clear.assert_awaited_once_with(
            application_name=APP_NAME,
            element_ids=["z-status-background", "zz-status-icon"],
        )

    async def test_unchanged_status_is_refreshed_before_expiry(self):
        bar = AsyncMock()
        display = BarDisplay(bar)
        state = {"status": "unknown"}
        with patch("busydiscord.bar_display.time.monotonic", side_effect=[1, 2, 6]):
            await display.update(state)
            await display.update(state)
            await display.update(state)
        self.assertEqual(bar.display_draw.await_count, 2)
