#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Persistencia mínima de la intención de escucha del bot de Telegram.

Solo se conserva configuración estable (chat, enabled y canal). El estado de
runtime (Task, StreamWriter, contadores y timestamps de proceso) se reconstruye
al arrancar y nunca se serializa.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional

_LOG = logging.getLogger(__name__)
_LOCK = threading.RLock()
_STATE_VERSION = 1
_STATE_FILENAME = "telegram_listen_state.json"


def state_path() -> Path:
    """Devuelve la ruta persistente bajo BOT_DATA_DIR."""
    data_dir = Path(os.getenv("BOT_DATA_DIR", "/app/bot_data")).expanduser()
    return data_dir / _STATE_FILENAME


def _empty_state() -> dict[str, Any]:
    return {"version": _STATE_VERSION, "listeners": {}}


def load_state(path: Optional[Path] = None) -> dict[str, Any]:
    """Carga y valida el estado; cualquier corrupción degrada a estado vacío."""
    target = Path(path) if path is not None else state_path()
    if not target.exists():
        return _empty_state()

    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:
        _LOG.warning("No se pudo leer %s: %s", target, exc)
        return _empty_state()

    if not isinstance(raw, dict) or raw.get("version") != _STATE_VERSION:
        _LOG.warning("Estado de escucha no compatible en %s", target)
        return _empty_state()

    listeners = raw.get("listeners")
    if not isinstance(listeners, dict):
        _LOG.warning("Estado de escucha sin listeners válidos en %s", target)
        return _empty_state()

    clean: dict[str, dict[str, Any]] = {}
    for raw_chat_id, raw_spec in listeners.items():
        try:
            chat_id = int(raw_chat_id)
        except (TypeError, ValueError):
            continue
        if not isinstance(raw_spec, dict):
            continue

        enabled = raw_spec.get("enabled")
        if not isinstance(enabled, bool):
            continue

        raw_channel = raw_spec.get("channel")
        if raw_channel is None:
            channel = None
        else:
            try:
                channel = int(raw_channel)
            except (TypeError, ValueError):
                continue

        clean[str(chat_id)] = {
            "enabled": enabled,
            "channel": channel,
        }

    return {"version": _STATE_VERSION, "listeners": clean}


def _write_state_atomic(state: dict[str, Any], path: Optional[Path] = None) -> None:
    """Escribe JSON mediante temporal + os.replace para evitar estados parciales."""
    target = Path(path) if path is not None else state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    payload = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, target)


def set_listener(
    chat_id: int,
    *,
    enabled: bool,
    channel: Optional[int],
    path: Optional[Path] = None,
) -> dict[str, Any]:
    """Actualiza una preferencia conservando las de los demás chats."""
    normalized_chat_id = int(chat_id)
    normalized_channel = None if channel is None else int(channel)

    with _LOCK:
        state = load_state(path)
        listeners = state.setdefault("listeners", {})
        listeners[str(normalized_chat_id)] = {
            "enabled": bool(enabled),
            "channel": normalized_channel,
        }
        _write_state_atomic(state, path)
        return state
