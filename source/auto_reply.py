"""Configuración y reglas compartidas para respuestas automáticas de radio."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Callable

DEFAULT_TEMPLATE = "Recibido, {message}"


class AutoReply:
    """Carga la configuración en caliente y evita bucles/duplicados recientes."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._signature: tuple[int, int, int] | None = None
        self._config: dict = {}
        self._sent: dict[tuple[str, int, str], float] = {}
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            stat = self.path.stat()
            signature = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
            if signature != self._signature:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self._config = raw if isinstance(raw, dict) else {}
                self._signature = signature
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
            self._config = {}
            self._signature = None
        return self._config

    def reply_for(self, transport: str, channel: int, message: str) -> str | None:
        message = " ".join(str(message or "").split()).strip()
        if not message:
            return None
        with self._lock:
            config = self._load()
            route = config.get(transport, {})
            channels = route.get("channels", []) if isinstance(route, dict) else []
            if not config.get("enabled", False) or channel not in channels:
                return None
            template = str(config.get("template") or DEFAULT_TEMPLATE)
            prefix = template.split("{message}", 1)[0]
            if prefix and message.casefold().startswith(prefix.casefold()):
                return None
            key = (transport, channel, message)
            now = time.monotonic()
            self._sent = {item: ts for item, ts in self._sent.items() if now - ts < 120}
            if key in self._sent:
                return None
            reply = template.replace("{message}", message).strip()
            if not reply or len(reply) > 500:
                return None
            self._sent[key] = now
            return reply

    def send(self, transport: str, channel: int, message: str, sender: Callable[[str], object]) -> bool:
        reply = self.reply_for(transport, channel, message)
        if reply is None:
            return False
        sender(reply)
        return True
