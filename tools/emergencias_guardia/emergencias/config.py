from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .models import VALID_CATEGORIES


APP_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.getenv("EMERGENCIAS_DATA_DIR", APP_DIR / "data")).resolve()
CONFIG_FILE = Path(os.getenv("EMERGENCIAS_CONFIG_FILE", DATA_DIR / "config.json")).resolve()

DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": 1,
    "api": {"host": "127.0.0.1", "port": 8789, "max_body_bytes": 65536},
    "fetch": {
        "timeout_seconds": 15,
        "max_response_bytes": 10_000_000,
        "resolve_after_missing_fetches": 2,
        "user_agent": "MeshNet-Emergencias/0.1",
    },
    "filters": {"minimum_severity": "low", "categories": sorted(VALID_CATEGORIES)},
    "areas": [],
    "sources": {
        "dgt_datex": {
            "type": "datex2", "enabled": False, "url": "",
            "verification": "official", "default_province": "",
        },
        "municipal_json": {
            "type": "json", "enabled": False, "url": "",
            "verification": "official", "default_province": "Zaragoza",
            "records_path": "", "mapping": {},
        },
    },
}


def _merge(default: Any, supplied: Any) -> Any:
    if isinstance(default, dict) and isinstance(supplied, dict):
        return {key: _merge(value, supplied.get(key, value)) for key, value in default.items()} | {
            key: value for key, value in supplied.items() if key not in default
        }
    return supplied


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def load_config(create: bool = True) -> dict[str, Any]:
    if not CONFIG_FILE.exists():
        if create:
            atomic_write_json(CONFIG_FILE, DEFAULT_CONFIG)
        return json.loads(json.dumps(DEFAULT_CONFIG))
    supplied = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    return _merge(DEFAULT_CONFIG, supplied)


def save_config(config: dict[str, Any]) -> None:
    atomic_write_json(CONFIG_FILE, config)
