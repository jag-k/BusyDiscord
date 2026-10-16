import asyncio
import re
import time

from http import HTTPStatus
from importlib.resources import files

from busylib import AsyncBusyBar
from busylib.exceptions import BusyBarAPIError


APP_NAME = "BusyDiscord"
LEASE_SECONDS = 10
LOGO_FILE = "discord.png"
GLYPH_FILES = {
    section: f"status-glyph-{section}.anim"
    for section in (
        "mic",
        "headphones",
        "to_mic",
        "to_headphones",
        "show_mic",
        "show_headphones",
        "hide_mic",
        "hide_headphones",
    )
}
STRIKE_FILES = {section: f"status-strike-{section}.anim" for section in ("clear", "hold", "show", "hide")}
ICON_FILES = (LOGO_FILE, *GLYPH_FILES.values(), *STRIKE_FILES.values())
TEXT_LEFT = 18
REFRESH_SECONDS = 1
RETURN_DELAY_SECONDS = 10
PRESENCE_ELEMENT_ID = "screen-presence"


def channel_text(value):
    # Device fonts don't include Discord emoji. Keep letters, numbers and punctuation.
    text = "".join(c for c in value if c.isalnum() or c in " -_#./")
    return re.sub(r"\s+", " ", text).strip()[:120] or "Voice"


def duration(seconds):
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes:02}:{seconds:02}"


def display_payload(state, previous=None):
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
    timer = duration(state.get("elapsed_seconds", state.get("observed_seconds", 0)))

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

    overlay_elements = []
    old_active = previous is not None and previous.get("status") == "in_voice"
    old_deaf = bool(previous and (previous.get("self_deaf") or previous.get("deaf")))
    old_muted = bool(
        previous and (previous.get("self_mute") or previous.get("mute") or old_deaf or previous.get("suppress"))
    )
    if active and (muted or (old_active and old_muted)):
        glyph_section = "headphones" if deaf else "mic"
        if old_active:
            if not muted:
                glyph_section = "hide_headphones" if old_deaf else "hide_mic"
            elif not old_muted:
                glyph_section = "show_headphones" if deaf else "show_mic"
            elif deaf != old_deaf:
                glyph_section = "to_headphones" if deaf else "to_mic"
        strike_section = "hold" if muted else "clear"
        if old_active and muted != old_muted:
            strike_section = "show" if muted else "hide"

        def animation(id, path, section):
            return {
                "id": id,
                "type": "animation",
                "display": "front",
                "path": path,
                "section": section,
                "loop": False,
                "x": 7,
                "y": 7,
                "align": "top_left",
                "timeout": LEASE_SECONDS,
            }

        overlay_elements = [
            animation("zz-status-glyph", GLYPH_FILES[glyph_section], glyph_section),
            animation("zzz-status-strike", STRIKE_FILES[strike_section], strike_section),
        ]

    return {
        "application_name": APP_NAME,
        "priority": 50,
        "elements": [
            {
                "id": PRESENCE_ELEMENT_ID,
                "type": "rectangle",
                "display": "front",
                "x": 0,
                "y": 0,
                "width": 1,
                "height": 1,
                "fill": "solid",
                "fill_colors": ["#00000000"],
                "border_width": 0,
                "timeout": LEASE_SECONDS,
            },
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
        self.last_state = None
        self.last_sent = 0
        self.owns_screen = False
        self.ready = False
        self.unavailable = False
        self.resume_at = 0

    async def screen_still_open(self):
        # There is no HTTP exit event. A scoped DELETE checks a transparent marker
        # without opening the canvas. The next draw restores it. Firmware 27.5
        # returns a generic 400 if the canvas/marker is gone or another app owns it.
        try:
            await self.bar.display_clear(application_name=APP_NAME, element_ids=[PRESENCE_ELEMENT_ID])
        except BusyBarAPIError as exc:
            if exc.status_code == HTTPStatus.BAD_REQUEST:
                return False
            raise
        return True

    async def update(self, state):
        payload = display_payload(state, self.last_state if self.owns_screen else None)
        if payload is None and not self.owns_screen:
            return
        now = time.monotonic()
        if now < self.resume_at:
            return
        if payload == self.last_payload and now - self.last_sent < REFRESH_SECONDS:
            return
        if (
            payload is not None
            and self.owns_screen
            and now - self.last_sent < LEASE_SECONDS
            and not await self.screen_still_open()
        ):
            self.owns_screen = False
            self.last_payload = None
            self.resume_at = now + RETURN_DELAY_SECONDS
            self.report({"busybar": "screen_released", "return_in_seconds": RETURN_DELAY_SECONDS})
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
        self.last_state = dict(state)
        self.last_sent = now
        if self.unavailable:
            self.report({"busybar": "reconnected"})
        self.unavailable = False

    async def run(self, snapshot):
        while True:
            try:
                await self.update(snapshot())
            except BusyBarAPIError as exc:
                if exc.status_code != HTTPStatus.CONFLICT:
                    self.unavailable = True
                    self.ready = False
                    self.report({"busybar": "unavailable", "error_type": type(exc).__name__})
                    await asyncio.sleep(3)
                else:
                    # A higher-priority screen is normal, not a lost connection.
                    self.owns_screen = False
                    self.last_payload = None
            except Exception as exc:
                if not self.unavailable:
                    self.report({"busybar": "unavailable", "error_type": type(exc).__name__})
                self.unavailable = True
                self.ready = False
                await asyncio.sleep(3)
            await asyncio.sleep(REFRESH_SECONDS)

    async def close(self):
        if self.owns_screen:
            try:
                await self.bar.display_clear(application_name=APP_NAME)
            except Exception:
                # The lease also expires if a disconnected device cannot be cleared.
                self.report({"busybar": "clear_failed", "expires_in_seconds": LEASE_SECONDS})


def make_bar(addr, token):
    return AsyncBusyBar(addr, token=token, timeout=3, max_retries=0)
