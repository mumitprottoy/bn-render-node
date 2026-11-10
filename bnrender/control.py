"""The pause flag (`state/paused`). Its presence means paused, which survives reboots. It counts the pauses: the first
lets the unit in progress finish, the second ends it at once."""

import dataclasses
import json
import os
import time

from .settings import Settings


@dataclasses.dataclass
class PauseState:
    count: int
    since: float


def read_pause(settings: Settings) -> PauseState | None:
    try:
        text = settings.pause_file.read_text(encoding='utf-8')
    except FileNotFoundError:
        return None
    except OSError:
        return PauseState(count=1, since=time.time())
    try:
        raw = json.loads(text) if text.strip() else dict()
    except ValueError:
        raw = dict()
    count = raw.get('count') if isinstance(raw, dict) else None
    since = raw.get('since') if isinstance(raw, dict) else None
    return PauseState(
        count=count if isinstance(count, int) and count > 0 else 1,
        since=float(since) if isinstance(since, (int, float)) else settings.pause_file.stat().st_mtime,
    )


def request_pause(settings: Settings) -> PauseState:
    current = read_pause(settings)
    state = PauseState(count=1, since=time.time()) if current is None else PauseState(count=current.count + 1,
                                                                                      since=current.since)
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    temporary = settings.pause_file.with_name('paused.tmp')
    temporary.write_text(json.dumps(dataclasses.asdict(state)), encoding='utf-8')
    os.replace(temporary, settings.pause_file)
    return state


def clear_pause(settings: Settings) -> bool:
    try:
        settings.pause_file.unlink()
    except FileNotFoundError:
        return False
    return True
