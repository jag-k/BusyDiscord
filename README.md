# BusyDiscord

Display Discord voice activity on [BUSY Bar](https://busy.app/): the current voice
channel, voice session duration, and microphone and audio status.

BusyDiscord connects through `discord.py-self`. It does not require a Discord
client running on the same computer or a bot installed in each server.

> **Discord account policy:** Automating user accounts violates
> [Discord's rules](https://support.discord.com/hc/en-us/articles/115002192352-Automated-User-Accounts-Self-Bots)
> and may result in account termination. BusyDiscord is an unofficial integration.

## Features

- Front display with the Discord logo, channel name, session timer, and mute/deafen indicators.
- Independent animated microphone/headphone glyphs and mute strike on the front display.
- Rear display with detailed microphone and audio status.
- Support for self mute, server mute, deafen, and Stage suppression.
- Automatic reconnection and persistence of server-issued token replacements.
- Screen cleanup when leaving voice or stopping the application.
- Local execution and Docker Compose deployment.

## Requirements

- Python 3.14 or later and [uv](https://docs.astral.sh/uv/), or Docker with Compose.
- A BUSY Bar reachable over USB or Wi-Fi.
- A Discord user account token. Password and QR-code login are not currently supported.

## Getting started

Run these commands from the repository checkout:

```sh
uv sync --locked
cp .env.example .env
uv run busydiscord --login
uv run busydiscord
```

The `--login` command prompts for a Discord token without displaying the input and
stores it in `.state/discord-token.json`. Run it again to replace the saved token.

Configure the BUSY Bar connection in `.env`:

```dotenv
BUSYBAR_ADDR=10.0.4.20
BUSYBAR_TOKEN=
```

| Setting | Description |
| --- | --- |
| `BUSYBAR_ADDR` | Device IP address, `host:port`, or URL. Defaults to `10.0.4.20` for USB. |
| `BUSYBAR_TOKEN` | HTTP API password configured in the device's web interface for Wi-Fi access. Not required over USB. |

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
  Device connection failures are retried automatically. Higher-priority applications
  retain control of the BUSY Bar display.
- **Channel names:** Emoji are removed because they are not supported by the device fonts.
- **Manual dismissal:** The display refreshes once per second. When its elements
  are removed by exiting the screen or another app taking over, BusyDiscord detects
  the loss using a transparent presence element and waits 10 seconds before drawing
  again. Discord monitoring continues during this pause, and the next frame uses
  the latest state. The API does not distinguish manual dismissal from takeover.
- **Screen cleanup:** Display elements expire after 10 seconds without renewal.
  Normal shutdown releases the application's screen immediately when the device is reachable.

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
