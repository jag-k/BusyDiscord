import os
import unittest

from unittest.mock import AsyncMock, MagicMock, patch

from busydiscord.main import demo, main


class CommandLineTests(unittest.TestCase):
    def test_test_mode_uses_cli_then_environment_then_usb_without_discord_login(self):
        cases = [
            (["--host", "127.0.0.1:8080"], {"BUSYBAR_ADDR": "env.local"}, "127.0.0.1:8080"),
            ([], {"BUSYBAR_ADDR": "env.local"}, "env.local"),
            ([], {}, "10.0.4.20"),
        ]
        for arguments, environment, expected_host in cases:
            with self.subTest(expected_host=expected_host):
                context = MagicMock()
                bar = object()
                context.__aenter__ = AsyncMock(return_value=bar)
                context.__aexit__ = AsyncMock(return_value=False)
                with (
                    patch.dict(os.environ, environment, clear=True),
                    patch("busydiscord.main.load_dotenv"),
                    patch("busydiscord.main.prepare_token") as prepare,
                    patch("busydiscord.main.make_bar", return_value=context) as make_bar,
                    patch("busydiscord.main.demo", new_callable=AsyncMock) as render,
                    patch("busydiscord.main.output"),
                    self.assertRaises(SystemExit) as exit_status,
                ):
                    main([*arguments, "--test"])
                self.assertEqual(exit_status.exception.code, 0)
                prepare.assert_not_called()
                make_bar.assert_called_once_with(expected_host, None)
                render.assert_awaited_once_with(bar, single_frame=True)


class SingleFrameTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_frame_releases_screen(self):
        screen = AsyncMock()
        with (
            patch("busydiscord.main.BarDisplay", return_value=screen),
            patch("busydiscord.main.asyncio.sleep", new_callable=AsyncMock),
            patch("busydiscord.main.output"),
        ):
            await demo(object(), single_frame=True)
        screen.update.assert_awaited_once()
        screen.close.assert_awaited_once()
