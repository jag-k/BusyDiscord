import time


class VoiceTracker:
    def __init__(self):
        self.user_id = None
        self.state = None
        self.started = None
        self.observed_started_at = None
        self.channels = {}
        self.voice_starts = {}

    def update(self, state, now):
        if not self.user_id or state.get("user_id") != self.user_id:
            return False
        current = {
            key: state.get(key)
            for key in ("channel_id", "guild_id", "self_mute", "self_deaf", "mute", "deaf", "suppress")
        }
        # Do not treat an unrelated/stale guild leave as leaving the active call.
        if (
            self.state
            and self.state.get("channel_id")
            and not current["channel_id"]
            and current["guild_id"] != self.state.get("guild_id")
        ):
            return False
        if current == self.state:
            return False
        if current["channel_id"]:
            if not self.state or not self.state.get("channel_id"):
                self.started = now
                self.observed_started_at = int(time.time())
        else:
            self.started = None
        self.state = current
        return True

    def update_channel_info(self, event, data):
        if event == "CHANNEL_INFO":
            channels = data.get("channels", [])
        elif event == "VOICE_CHANNEL_START_TIME_UPDATE":
            channels = [data]
        else:
            return False
        changed = False
        for channel in channels:
            if "voice_start_time" not in channel:
                continue
            key = (str(data.get("guild_id")), str(channel["id"]))
            value = channel["voice_start_time"]
            if value is None:
                self.voice_starts.pop(key, None)
            elif type(value) is int and value > 0:
                self.voice_starts[key] = value
            else:
                continue
            if self.state and key == (self.state.get("guild_id"), self.state.get("channel_id")):
                changed = True
        return changed

    def dispatch(self, event, data, now):
        changed = False
        if event == "READY":
            self.user_id = data["user"]["id"]
            self.state = None
            self.started = None
            self.channels.clear()
            self.voice_starts.clear()
        if event == "VOICE_STATE_UPDATE":
            return self.update(data, now)
        if event in ("READY", "GUILD_CREATE", "CALL_CREATE", "CALL_UPDATE"):
            containers = [data]
            if event == "READY":
                containers += data.get("guilds", []) + data.get("calls", [])
            for container in containers:
                for channel in container.get("channels", []) + container.get("private_channels", []):
                    if channel.get("name"):
                        self.channels[channel["id"]] = channel["name"]
                for voice_state in container.get("voice_states", []):
                    state = dict(voice_state)
                    if event == "GUILD_CREATE" or (event == "READY" and container in data.get("guilds", [])):
                        state.setdefault("guild_id", container.get("id"))
                    changed |= self.update(state, now)
        if event in ("CHANNEL_CREATE", "CHANNEL_UPDATE") and data.get("name"):
            self.channels[data["id"]] = data["name"]
        return changed

    def snapshot(self, now, wall_now=None):
        if self.state is None:
            return {"status": "unknown", "reason": "waiting_for_own_voice_state"}
        if not self.state["channel_id"]:
            return {"status": "outside_voice"}
        observed = max(0, int(now - self.started))
        voice_start = self.voice_starts.get((self.state.get("guild_id"), self.state["channel_id"]))
        elapsed = (
            max(0, int((time.time() if wall_now is None else wall_now) - voice_start)) if voice_start else observed
        )
        return {
            "status": "in_voice",
            **self.state,
            "channel_name": self.channels.get(self.state["channel_id"]),
            "observed_seconds": observed,
            "voice_start_time": voice_start,
            "timer_started_at": voice_start or self.observed_started_at,
            "elapsed_seconds": elapsed,
            "timer_source": "discord" if voice_start else "observed",
        }
