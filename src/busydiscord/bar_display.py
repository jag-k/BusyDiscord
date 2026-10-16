import asyncio
import os
import re
import time

from http import HTTPStatus
from importlib.resources import files

from busylib import AsyncBusyBar
from busylib.exceptions import BusyBarAPIError


APP_NAME = "BusyDiscord"
DEFAULT_PRIORITY = 60
MAX_PRIORITY = 100
ELEMENT_TIMEOUT = 0
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
BACK_ICON_FILES = tuple(f"back-{icon}-{state}.png" for icon in ("microphone", "headphones") for state in ("on", "off"))
ICON_FILES = (LOGO_FILE, *GLYPH_FILES.values(), *STRIKE_FILES.values(), *BACK_ICON_FILES)
TEXT_LEFT = 18
REFRESH_SECONDS = 1


def channel_text(value):
    # Device fonts don't include Discord emoji. Keep letters, numbers and punctuation.
    text = "".join(c for c in value if c.isalnum() or c in " -_#./")
    return re.sub(r"\s+", " ", text).strip()[:120] or "Voice"


def display_payload(state, previous=None, *, priority=DEFAULT_PRIORITY):
    if state.get("status") == "outside_voice":
        return None
    active = state.get("status") == "in_voice"
    deaf = bool(state.get("self_deaf") or state.get("deaf"))
    muted = bool(state.get("self_mute") or state.get("mute") or deaf or state.get("suppress"))
    status_color = "#FFB000FF" if deaf else "#FF3333FF" if muted else "#33FF66FF"
    name = channel_text(state.get("channel_name") or "Voice") if active else "Discord"
    timestamp = state.get("timer_started_at") or state.get("voice_start_time")
    if active and timestamp is None:
        timestamp = int(time.time()) - int(state.get("elapsed_seconds", state.get("observed_seconds", 0)))

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
            "timeout": ELEMENT_TIMEOUT,
        }

    def counter(id, display, x, y):
        return {
            "id": id,
            "type": "countdown",
            "display": display,
            "timestamp": str(int(timestamp)),
            "direction": "time_since",
            "show_hours": "when_non_zero",
            "color": status_color if display == "front" else "#FFFFFFFF",
            "x": x,
            "y": y,
            "align": "top_left",
            "timeout": ELEMENT_TIMEOUT,
        }

    status_elements = []
    timer_element = text("front-status", "front", "WAITING", 8, "tiny", tint="#FFB000FF")
    back_timer = text("back-timer", "back", "WAITING", 30, "extra_large")
    if active:
        # Separate IDs avoid changing an existing text element's type on firmware.
        timer_element = counter("front-elapsed", "front", 24, 8)
        back_timer = counter("back-elapsed", "back", 12, 29)
        status_elements.append(
            {
                "id": "front-clock",
                "type": "image",
                "display": "front",
                "stock_path": "shared/images/clock_5x5.image",
                "x": TEXT_LEFT,
                "y": 10,
                "align": "top_left",
                "timeout": ELEMENT_TIMEOUT,
            }
        )

    back_details = []
    if active:
        mic_reasons = [
            reason
            for flag, reason in [
                (state.get("self_mute"), "You muted"),
                (state.get("mute"), "Server muted"),
                (state.get("suppress"), "Stage suppressed"),
                (deaf, "Audio disabled"),
            ]
            if flag
        ]
        audio_reasons = [
            reason
            for flag, reason in [
                (state.get("self_deaf"), "You deafened"),
                (state.get("deaf"), "Server deafened"),
            ]
            if flag
        ]
        for id, icon, off, title, detail, y in [
            ("back-mic", "microphone", muted, "MICROPHONE", "; ".join(mic_reasons) or "Ready to speak", 41),
            ("back-audio", "headphones", deaf, "AUDIO", "; ".join(audio_reasons) or "Listening", 59),
        ]:
            back_details.append(
                {
                    "id": id + "-icon",
                    "type": "image",
                    "display": "back",
                    "path": f"back-{icon}-{'off' if off else 'on'}.png",
                    "x": 2,
                    "y": y + 1,
                    "align": "top_left",
                    "timeout": ELEMENT_TIMEOUT,
                }
            )
            heading = text(id, "back", f"{title}: {'OFF' if off else 'ON'}", y, width=139)
            heading["x"] = 19
            explanation = text(id + "-reason", "back", detail, y + 9, "tiny", width=139)
            explanation["x"] = 19
            back_details.extend((heading, explanation))
        back_details.append(
            {
                "id": "back-clock",
                "type": "image",
                "display": "back",
                "stock_path": "shared/images/clock_5x5.image",
                "x": 2,
                "y": 31,
                "align": "top_left",
                "timeout": ELEMENT_TIMEOUT,
            }
        )
    else:
        back_details = [
            text("back-mic", "back", "Status unavailable", 56),
            text("back-audio", "back", "Connecting to Discord", 68),
        ]

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
                "timeout": ELEMENT_TIMEOUT,
            }

        overlay_elements = [
            animation("zz-status-glyph", GLYPH_FILES[glyph_section], glyph_section),
            animation("zzz-status-strike", STRIKE_FILES[strike_section], strike_section),
        ]

    return {
        "application_name": APP_NAME,
        "priority": priority,
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
                "timeout": ELEMENT_TIMEOUT,
            },
            {
                "id": "front-logo",
                "type": "image",
                "display": "front",
                "path": LOGO_FILE,
                "x": 2,
                "y": 3,
                "align": "top_left",
                "timeout": ELEMENT_TIMEOUT,
            },
            text("front-channel", "front", name, 0, "small"),
            timer_element,
            *status_elements,
            text("back-title", "back", "DISCORD VOICE", 0),
            text("back-channel", "back", name, 12, "normal"),
            back_timer,
            *back_details,
            *overlay_elements,
        ],
    }


class BarDisplay:
    def __init__(self, bar, report=print):
        self.priority = int(os.environ.get("BUSYBAR_PRIORITY") or DEFAULT_PRIORITY)
        if not 1 <= self.priority <= MAX_PRIORITY:
            raise ValueError("BUSYBAR_PRIORITY must be an integer from 1 to 100")
        self.bar = bar
        self.report = report
        self.last_payload = None
        self.last_state = None
        self.observed_state = None
        self.owns_screen = False
        self.ready = False
        self.unavailable = False

    async def update(self, state):
        state = dict(state)
        if state.get("status") == "in_voice" and not state.get("timer_started_at"):
            previous = self.observed_state or {}
            state["timer_started_at"] = (
                state.get("voice_start_time")
                or (previous.get("timer_started_at") if previous.get("status") == "in_voice" else None)
                or int(time.time()) - int(state.get("elapsed_seconds", state.get("observed_seconds", 0)))
            )
        # Only semantic changes cause HTTP requests. The native counter advances locally.
        visible = {key: value for key, value in state.items() if key not in ("observed_seconds", "elapsed_seconds")}
        previous = {
            key: value
            for key, value in (self.observed_state or {}).items()
            if key not in ("observed_seconds", "elapsed_seconds")
        }
        if visible == previous:
            return
        # Remember even failed attempts: a dismissed/blocked screen stays dismissed
        # until the next state change, without background redraw or retry traffic.
        self.observed_state = state
        if state.get("status") == "outside_voice" and not self.owns_screen:
            self.last_state = state
            return
        payload = display_payload(state, self.last_state if self.owns_screen else None, priority=self.priority)
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
                    try:
                        await self.bar.display_clear(application_name=APP_NAME, element_ids=sorted(removed_ids))
                    except BusyBarAPIError as exc:
                        if exc.status_code != HTTPStatus.BAD_REQUEST:
                            raise
                        # Manual dismissal already removed these elements.
            await self.bar.display_draw(payload)
            self.owns_screen = True
        self.last_payload = payload
        self.last_state = dict(state)
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
                self.report({"busybar": "clear_failed"})


def make_bar(addr, token):
    return AsyncBusyBar(addr, token=token, timeout=3, max_retries=0)
