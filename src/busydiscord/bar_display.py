import asyncio
import re
import time

from importlib.resources import files

from busylib import AsyncBusyBar


APP_NAME = "BusyDiscord"
LEASE_SECONDS = 10
LOGO_FILE = "discord.png"
MIC_OFF_FILE = "mic-off.png"
HEADPHONES_OFF_FILE = "headphones-off.png"
ICON_FILES = (LOGO_FILE, MIC_OFF_FILE, HEADPHONES_OFF_FILE)
TEXT_LEFT = 18
REFRESH_SECONDS = 4


def channel_text(value):
    # Device fonts don't include Discord emoji. Keep letters, numbers and punctuation.
    text = "".join(c for c in value if c.isalnum() or c in " -_#./")
    return re.sub(r"\s+", " ", text).strip()[:120] or "Voice"


def duration(seconds):
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes:02}:{seconds:02}"


def display_payload(state):
    if state.get("status") == "outside_voice":
        return None
    active = state.get("status") == "in_voice"
    deaf = bool(state.get("self_deaf") or state.get("deaf"))
    muted = bool(state.get("self_mute") or state.get("mute") or deaf or state.get("suppress"))
    label = "DEAF" if deaf else "MUTE" if muted else "MIC"
    color = "#FFB000FF" if deaf else "#FF3333FF" if muted else "#33FF66FF"
    if not active:
        label, color = "WAITING", "#FFB000FF"
    name = channel_text(state.get("channel_name") or "Voice") if active else "Discord"
    timer = duration(state.get("observed_seconds", 0))

    def text(id, display, value, y, font="small", width=None, align="top_left", tint="#FFFFFFFF"):
        return {
            "id": id,
            "type": "text",
            "display": display,
            "text": value,
            "x": TEXT_LEFT if display == "front" else 1,
            "y": y,
            "font": font,
            "color": tint,
            "align": align,
            "width": width or (72 - TEXT_LEFT if display == "front" else 158),
            "scroll_rate": 800,
            "scroll_start_delay": 800,
            "scroll_repeat_delay": 1500,
            "timeout": LEASE_SECONDS,
        }

    overlay = HEADPHONES_OFF_FILE if deaf else MIC_OFF_FILE if muted else None
    overlay_elements = []
    if active and overlay:
        overlay_elements = [
            {
                "id": "z-status-background",
                "type": "rectangle",
                "display": "front",
                "x": 7,
                "y": 7,
                "width": 9,
                "height": 9,
                "radius": 2,
                "fill": "solid",
                "fill_colors": ["#23272AFF"],
                "border_width": 0,
                "timeout": LEASE_SECONDS,
            },
            {
                "id": "zz-status-icon",
                "type": "image",
                "display": "front",
                "path": overlay,
                "x": 7,
                "y": 7,
                "align": "top_left",
                "timeout": LEASE_SECONDS,
            },
        ]

    return {
        "application_name": APP_NAME,
        "priority": 50,
        "elements": [
            {
                "id": "0-logo-background",
                "type": "rectangle",
                "display": "front",
                "x": 0,
                "y": 0,
                "width": 16,
                "height": 16,
                "radius": 3,
                "fill": "solid",
                "fill_colors": ["#5865F2FF"],
                "border_width": 0,
                "timeout": LEASE_SECONDS,
            },
            {
                "id": "front-logo",
                "type": "image",
                "display": "front",
                "path": LOGO_FILE,
                "x": 2,
                "y": 3,
                "align": "top_left",
                "timeout": LEASE_SECONDS,
            },
            text("front-channel", "front", name, 0, "small"),
            text("front-status", "front", f"{label} {timer}" if active else label, 8, "tiny", tint=color),
            text("back-title", "back", "DISCORD VOICE", 0),
            text("back-channel", "back", name, 14, "normal"),
            text("back-timer", "back", timer if active else "WAITING", 30, "extra_large"),
            text("back-mic", "back", f"MIC: {'OFF' if muted else 'ON'}" if active else "Status unavailable", 56),
            text("back-audio", "back", f"AUDIO: {'OFF' if deaf else 'ON'}" if active else "Connecting to Discord", 68),
            *overlay_elements,
        ],
    }


class BarDisplay:
    def __init__(self, bar, report=print):
        self.bar = bar
        self.report = report
        self.last_payload = None
        self.last_sent = 0
        self.owns_screen = False
        self.ready = False
        self.unavailable = False

    async def update(self, state):
        payload = display_payload(state)
        if payload is None and not self.owns_screen:
            return
        now = time.monotonic()
        if payload == self.last_payload and now - self.last_sent < REFRESH_SECONDS:
            return
        if not self.ready:
            await self.bar.version()
            for filename in ICON_FILES:
                await self.bar.assets_upload(
                    APP_NAME,
                    filename,
                    files("busydiscord").joinpath("assets", filename).read_bytes(),
                )
            self.ready = True
        if payload is None:
            await self.bar.display_clear(application_name=APP_NAME)
            self.owns_screen = False
        else:
            if self.last_payload:
                previous_ids = {element["id"] for element in self.last_payload["elements"]}
                current_ids = {element["id"] for element in payload["elements"]}
                removed_ids = previous_ids - current_ids
                if removed_ids:
                    await self.bar.display_clear(application_name=APP_NAME, element_ids=sorted(removed_ids))
            await self.bar.display_draw(payload)
            self.owns_screen = True
        self.last_payload = payload
        self.last_sent = now
        if self.unavailable:
            self.report({"busybar": "reconnected"})
        self.unavailable = False

    async def run(self, snapshot):
        while True:
            try:
                await self.update(snapshot())
            except Exception as exc:
                if not self.unavailable:
                    self.report({"busybar": "unavailable", "error_type": type(exc).__name__})
                self.unavailable = True
                self.ready = False
                await asyncio.sleep(3)
            await asyncio.sleep(0.25)

    async def close(self):
        if self.owns_screen:
            try:
                await self.bar.display_clear(application_name=APP_NAME)
            except Exception:
                # The lease also expires if a disconnected device cannot be cleared.
                self.report({"busybar": "clear_failed", "expires_in_seconds": LEASE_SECONDS})


def make_bar(addr, token):
    return AsyncBusyBar(addr, token=token, timeout=3, max_retries=0)
