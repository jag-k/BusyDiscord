import asyncio
import time

from .bar_display import BarDisplay
from .voice_state import VoiceTracker


class SourceError(Exception):
    """A credential-free explanation that is safe to show in CLI output."""


class VoiceSource:
    def __init__(self, report):
        self.report = report
        self.tracker = VoiceTracker()
        self.connected = False
        self.authenticated = False

    def snapshot(self):
        if not self.connected:
            return {"status": "unknown", "reason": "disconnected"}
        return self.tracker.snapshot(time.monotonic())

    async def run(self):
        raise NotImplementedError


async def observe_source(source, seconds=None, bar=None):
    task = asyncio.create_task(source.run())
    display = BarDisplay(bar, source.report) if bar else None
    display_task = asyncio.create_task(display.run(source.snapshot)) if display else None
    try:
        done, _ = await asyncio.wait({task}, timeout=seconds)
        if done:
            await task
            return 1
        if not source.authenticated:
            source.report({"connection": "authentication_not_confirmed", "voice_status": "unknown"})
        return 0 if source.authenticated else 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if display_task:
            display_task.cancel()
            await asyncio.gather(display_task, return_exceptions=True)
        if display:
            await display.close()
