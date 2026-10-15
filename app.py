"""BusyDiscord: show your Discord voice channel, call time and mute state on BUSY Bar.

Run with `uv run python app.py --host 10.0.4.20` (USB) or provide a Wi-Fi/emulator
host with --host. Use --test for one sample frame without a Discord connection.
Discord credentials are entered with --login and stored locally in .state.
"""

from busydiscord.main import main


if __name__ == "__main__":
    main()
