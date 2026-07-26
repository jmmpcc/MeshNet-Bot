from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import DATA_DIR, atomic_write_json
from .models import Event, utc_now


CURRENT_FILE = DATA_DIR / "current.json"
STATE_FILE = DATA_DIR / "state.json"
HISTORY_FILE = DATA_DIR / "history.jsonl"
CACHE_DIR = DATA_DIR / "cache"


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def load_current() -> dict[str, Event]:
    payload = load_json(CURRENT_FILE, {"events": []})
    return {item["event_id"]: Event.from_dict(item) for item in payload.get("events", [])}


def save_current(events: dict[str, Event]) -> None:
    atomic_write_json(CURRENT_FILE, {
        "updated_at": utc_now(),
        "events": [event.to_dict() for event in sorted(events.values(), key=lambda item: item.event_id)],
    })


def load_state() -> dict[str, Any]:
    return load_json(STATE_FILE, {"sources": {}})


def save_state(state: dict[str, Any]) -> None:
    atomic_write_json(STATE_FILE, state)


def append_history(change: str, event: Event) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    record = {"recorded_at": utc_now(), "change": change, "event": event.to_dict()}
    with HISTORY_FILE.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def read_history(limit: int = 100) -> list[dict[str, Any]]:
    if not HISTORY_FILE.exists():
        return []
    lines = HISTORY_FILE.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines[-max(1, limit):]]


def cache_paths(source_id: str) -> tuple[Path, Path]:
    safe = "".join(char if char.isalnum() or char in "-_" else "_" for char in source_id)
    return CACHE_DIR / f"{safe}.body", CACHE_DIR / f"{safe}.json"
