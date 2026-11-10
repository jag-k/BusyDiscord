# BusyDiscord

Display Discord voice activity on [BUSY Bar](https://busy.app/): the current voice
channel, voice session duration, and microphone and audio status.

BusyDiscord supports a local Discord desktop connection, a Discord bot, or a
user account token. Choose the source with `--mode` or `DISCORD_MODE` in `.env`.

> **Discord account policy:** Automating user accounts violates
> [Discord's rules](https://support.discord.com/hc/en-us/articles/115002192352-Automated-User-Accounts-Self-Bots)
> and may result in account termination. This applies to `user-token` mode.
> It offers the broadest coverage but carries the highest account risk of the
> three modes. The likelihood of enforcement is unknown; do not assume it is low.
> BusyDiscord is an unofficial integration.

## Features

- Front display with the Discord logo, channel name, and clock/session timer on the left.
- Native BUSY Bar elapsed-time counters and a built-in clock icon.
- Independent animated microphone/headphone glyphs and mute strike on the front display.
- Rear display with 13×13 microphone/headphone icons and explanations for self/server mute, deafen, and Stage suppression.
- Support for self mute, server mute, deafen, and Stage suppression.
- Automatic reconnection and persistence of server-issued token replacements.
- Screen cleanup when leaving voice or stopping the application.
- Local execution and Docker Compose deployment.

## Requirements

- Python 3.14 or later and [uv](https://docs.astral.sh/uv/), or Docker with Compose.
- A BUSY Bar reachable over USB or Wi-Fi, running firmware 1.3.1 or later.
- Discord credentials for the selected mode, described below.

## Discord modes

| Mode | Runs on | Credentials | Coverage |
| --- | --- | --- | --- |
| `rpc` | The same PC as Discord desktop | Application Client ID, Client Secret, and local authorization | The local client's selected voice channel and voice state |
| `bot` | Any PC or server | Bot Token and the user ID to monitor | Voice channels on servers where the bot is installed; no personal/group calls |
| `user-token` (default) | Any PC or server | Discord user account token | Existing account-based integration, including personal/group calls |

For account safety, prefer `bot` where its server coverage is sufficient, or
`rpc` where your application has the required access. `user-token` is the most
versatile option, but Discord prohibits this type of user-account automation;
it is the least safe option for your account. Keeping it as the default preserves
existing installations and is not a safety recommendation.

All modes use the same BUSY Bar display. RPC starts the elapsed timer when a call
is first observed. Bot mode also falls back to observation, using Discord's voice-session
time when supplied. RPC reconnects and resumed bot sessions preserve the observed
timer while the process remains running. They cannot recover a personal join time
from before startup. `user-token` retains the existing Discord-provided voice-session timer.

### Local RPC

Create a developer application in the [Discord Developer Portal](https://discord.com/developers/applications).
Set `DISCORD_MODE=rpc`, `DISCORD_CLIENT_ID`, and `DISCORD_CLIENT_SECRET` in `.env`.
Then, with Discord desktop running under the same OS user:

```sh
uv run busydiscord --mode rpc --login
uv run busydiscord --mode rpc
```

Accept the authorization prompt in Discord. Tokens are stored separately in
`.state/discord-rpc.json` and refreshed automatically when reconnecting.
The connection uses native IPC on Windows, macOS, and Linux, with read-only voice
queries once per second. BUSY Bar redraws still happen only on state changes.
`DISCORD_RPC_PATH` can override the detected socket/pipe path.

Discord restricts the `rpc` and `rpc.voice.read` scopes and may require approval
or developer tester access for your application. Merely creating a Rich Presence
application does not guarantee voice access. See [RPC](https://docs.discord.com/developers/topics/rpc)
and [OAuth2 scopes](https://docs.discord.com/developers/topics/oauth2).
Run this mode natively; the Compose configuration does not expose desktop IPC.

### Bot

Create an application and bot in the Developer Portal and install it on every
server you want to monitor. Set `DISCORD_MODE=bot` and `DISCORD_USER_ID` in `.env`.
To copy your user ID, enable Developer Mode in Discord and use **Copy User ID**
on your profile. Enter the bot's token with:

```sh
uv run busydiscord --mode bot --login
uv run busydiscord --mode bot
```

The token is saved in `.state/discord-bot-token.json`. `DISCORD_BOT_TOKEN` is also
accepted for initial unattended setup. `--user-id` overrides `DISCORD_USER_ID`.
Only the `GUILDS` and `GUILD_VOICE_STATES` intents are requested; privileged
member, presence, and message-content intents are unnecessary. The bot does not
join voice or read messages. Outside voice means outside the servers visible to
the bot; activity in personal calls or other servers is unavailable to this mode.
The existing Compose deployment supports bot mode with the same `.env` and state volume.

### User account token

This is the existing mode and remains the default. Password and QR-code login
are not supported. Its token remains in `.state/discord-token.json`.
It works without a local desktop client or a bot installed in each server, making
it the most versatile mode. However, Discord prohibits self-bots and may terminate
the account. There is no reliable estimate of the ban probability for BusyDiscord,
so this mode should not be treated as low risk even if it appears to work normally.

## Getting started

Run these commands from the repository checkout:

```sh
uv sync --locked
cp .env.example .env
uv run busydiscord --login
uv run busydiscord
```

In `user-token` mode, `--login` prompts for a Discord token without displaying the input and
stores it in `.state/discord-token.json`. Run it again to replace the saved token.

Configure the BUSY Bar connection in `.env`:

```dotenv
BUSYBAR_ADDR=10.0.4.20
BUSYBAR_TOKEN=
BUSYBAR_PRIORITY=60
```

| Setting | Description |
| --- | --- |
| `BUSYBAR_ADDR` | Device IP address, `host:port`, or URL. Defaults to `10.0.4.20` for USB. |
| `BUSYBAR_TOKEN` | HTTP API password configured in the device's web interface for Wi-Fi access. Not required over USB. |
| `BUSYBAR_PRIORITY` | Display priority from 1 to 100. Defaults to 60; higher values can override lower-priority screens. |

Use `--host` to override the configured address:

```sh
uv run busydiscord --host 192.168.0.140
```

Press Ctrl+C to stop. Run only one instance per state directory and device.

## Usage

```sh
uv run busydiscord --help
```

| Option | Behavior |
| --- | --- |
| `--host HOST` | Override the BUSY Bar address; accepts an IP address, `host:port`, or URL. |
| `--login` | Save or replace the Discord token using a hidden terminal prompt. |
| `--demo` | Cycle through three sample display states without connecting to Discord. |
| `--test` | Display one sample frame, release the screen, and exit without connecting to Discord. |
| `--console-only` | Monitor Discord without drawing on BUSY Bar. |
| `--seconds N` | Stop a monitoring session after the specified number of seconds. |

For example, to monitor Discord for 30 seconds without a device:

```sh
uv run busydiscord --console-only --seconds 30
```

Connection diagnostics are available through `uv run python scripts/probe.py`.

## Docker

The default Compose configuration uses the published image from
`ghcr.io/jag-k/busydiscord`. A source checkout and Python installation are not required.
Download `compose.yaml` and `.env.example` into a deployment directory:

```sh
mkdir busydiscord && cd busydiscord
curl -fsSLO https://raw.githubusercontent.com/jag-k/BusyDiscord/main/compose.yaml
curl -fsSL https://raw.githubusercontent.com/jag-k/BusyDiscord/main/.env.example -o .env
```

Set `BUSYBAR_ADDR` and `BUSYBAR_TOKEN` in `.env`, then start the service:

```sh
docker compose pull
docker compose run --rm busydiscord --login
docker compose up -d
docker compose logs -f
```

The login command prompts for the Discord token interactively. Compose stores it
in `./.state`, preserving it across updates and container replacement. Skip login
if this directory already contains a saved token. Run only one instance using it.

Update to the latest published image:

```sh
docker compose pull
docker compose up -d --remove-orphans
```

To pin a release, add `BUSYDISCORD_VERSION=0.1.0` to `.env`. Stop the service with
`docker compose down`; the state directory is retained.

For development, build from a source checkout using the additional configuration:

```sh
docker compose -f compose.yaml -f compose.build.yaml up -d --build
```

For Docker Desktop, a Wi-Fi connection to BUSY Bar is recommended. Access to the
USB address from the container depends on the host's routing configuration.

## Behavior and limitations

- **Session duration:** Uses Discord's `voice_start_time` for the active guild voice
  channel, including when BusyDiscord starts during an existing session. This is
  the channel's session start, which may precede your own arrival. Moving channels
  selects the destination channel's timer. If Discord does not provide a start
  time (including private calls), the timer falls back to locally observed time.
  Logs identify the source as `timer_source: discord` or `observed`; `elapsed_seconds`
  is the displayed duration and `observed_seconds` remains the local observation time.
- **Connection loss:** The display shows `WAITING` while Discord is unavailable.
  A failed update is retried on the next state change. Higher-priority applications
  retain control of the BUSY Bar display.
- **Channel names:** Emoji are removed because they are not supported by the device fonts.
- **Native timer:** Both displays use `countdown` with `direction: time_since`.
  BUSY Bar updates the seconds itself from `timer_started_at`; the service sends
  display changes only when voice/channel/mute/deafen state or the session start changes.
- **Manual dismissal:** Closing the display leaves it closed until the next state
  change. There are no screen probes, keepalives, or automatic revival attempts.
- **Screen cleanup:** Elements have no expiry. Normal shutdown or leaving voice
  clears the application's screen; a forced process kill may leave the last screen visible.


## Credentials and logging

Discord credentials are stored in `.state`, independently of the BUSY Bar settings
in `.env`. Server-issued Discord token replacements are saved automatically for
subsequent runs. A revoked session or password change may require a new token.

The token file is stored unencrypted and created with `0600` permissions on
macOS and Linux. Both `.state` and `.env` are excluded from Git and the Docker
build context. Do not share the state directory.

JSON logs include the account's voice state and channel name/ID. Tokens and raw
Gateway payloads are not logged; library debug logging is disabled.

## Development

Install the locked dependencies, then run the project checks:

```sh
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run ssort --check src scripts tests app.py
uv run ty check
uv run python -m unittest discover -s tests -v
uv build --no-sources
```

EditorConfig and pre-commit configuration are included. GitHub Actions runs the
checks and builds container images for amd64 and arm64. GHCR publication is
configured for `main` and version tags matching `v*`.

Display assets are generated from SVG sources with `sh scripts/render-icons.sh`,
which requires `rsvg-convert` and `ffmpeg`. Rendering dependencies are not needed at runtime:
BUSY Bar draws the interface using native `text`, `image`, `animation`, and `rectangle` elements.

## busybar-apps compatibility

The repository includes a thin `app.py` entry point alongside the Python package:

```sh
uv run python app.py --host 127.0.0.1:8080 --test
```

The `--host` and `--test` options follow the
[busybar-apps contribution conventions](https://github.com/maxswinkels/busybar-apps/blob/main/CONTRIBUTING.md).
The project is not yet packaged as a gallery submission. Before submitting it:

- Include the package and assets in the app directory, with a manifest and `requirements.txt`.
- Align `application_name` with the gallery slug (`busydiscord`; currently `BusyDiscord`).
- Provide a 720×160 hardware or emulator preview and run the gallery checker.
- Select a project license and explain the need for `discord.py-self` and
  `python-dotenv`, which require individual review by the gallery maintainers.
