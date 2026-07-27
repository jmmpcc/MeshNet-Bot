#!/usr/bin/env python3
"""Comandos de emergencias procesados directamente por MeshNet-Broker.

El módulo no abre conexiones de radio. Recibe una solicitud normalizada,
consulta la API local de ``emergencias_guardia`` y entrega al broker los textos
que debe encolar como DM por la misma red de origen.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


def _env_bool(name: str, default: str = "0") -> bool:
    return str(os.getenv(name, default) or default).strip().lower() in {
        "1", "true", "yes", "on", "si", "sí", "y",
    }


def _normalized_command(text: str) -> str:
    value = unicodedata.normalize("NFKC", str(text or "")).strip()
    return " ".join(value.split())


@dataclass(frozen=True)
class EmergenciasCommandContext:
    """Datos mínimos de una consulta recibida por el broker."""

    network: str
    source_id: str
    text: str
    channel: int | None
    is_direct: bool
    packet_id: str | int | None = None


class SlidingWindowRateLimiter:
    """Limitador persistente por red y contacto, separado de Farmacias."""

    def __init__(self) -> None:
        self.limit = max(1, int(os.getenv("EMERGENCIAS_MAX_REQUESTS_PER_HOUR", "5")))
        self.window = max(60, int(os.getenv("EMERGENCIAS_RATE_LIMIT_WINDOW_SECONDS", "3600")))
        self.duplicate_window = max(1, int(os.getenv("EMERGENCIAS_DUPLICATE_WINDOW_SECONDS", "20")))
        self.save_interval = max(10, int(os.getenv("EMERGENCIAS_RATE_LIMIT_SAVE_SECONDS", "60")))
        default_path = os.getenv("BOT_DATA_DIR", "/app/bot_data")
        self.path = Path(os.getenv(
            "EMERGENCIAS_RATE_LIMIT_FILE",
            str(Path(default_path) / "emergencias_rate_limit.json"),
        ))
        self._entries: dict[str, deque[float]] = defaultdict(deque)
        self._duplicates: dict[str, float] = {}
        self._lock = threading.RLock()
        self._last_save = 0.0
        self._load()

    def _key(self, network: str, source_id: str) -> str:
        return f"{str(network).lower()}:{str(source_id).strip()}"

    def _duplicate_key(self, ctx: EmergenciasCommandContext) -> str:
        packet = str(ctx.packet_id or "").strip()
        if packet:
            return f"{self._key(ctx.network, ctx.source_id)}:pkt:{packet}"
        bucket = int(time.time() // self.duplicate_window)
        command = _normalized_command(ctx.text).casefold()
        return f"{self._key(ctx.network, ctx.source_id)}:txt:{command}:{bucket}"

    def _prune(self, now: float) -> None:
        cutoff = now - self.window
        for key in list(self._entries):
            queue = self._entries[key]
            while queue and queue[0] <= cutoff:
                queue.popleft()
            if not queue:
                self._entries.pop(key, None)
        duplicate_cutoff = now - self.duplicate_window
        self._duplicates = {
            key: timestamp
            for key, timestamp in self._duplicates.items()
            if timestamp > duplicate_cutoff
        }

    def check_and_record(self, ctx: EmergenciasCommandContext) -> tuple[bool, int, bool]:
        now = time.time()
        with self._lock:
            self._prune(now)
            duplicate_key = self._duplicate_key(ctx)
            if duplicate_key in self._duplicates:
                return False, 0, True
            self._duplicates[duplicate_key] = now
            key = self._key(ctx.network, ctx.source_id)
            queue = self._entries[key]
            if len(queue) >= self.limit:
                retry = max(1, int(math.ceil(self.window - (now - queue[0]))))
                self._save_if_due(now)
                return False, retry, False
            queue.append(now)
            self._save_if_due(now)
            return True, 0, False

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            entries = raw.get("entries", {}) if isinstance(raw, dict) else {}
            cutoff = time.time() - self.window
            for key, values in entries.items():
                valid = [float(value) for value in values if float(value) > cutoff]
                if valid:
                    self._entries[str(key)] = deque(sorted(valid))
        except FileNotFoundError:
            return
        except Exception as exc:
            print(f"[emergencias] rate-limit load WARN: {type(exc).__name__}: {exc}", flush=True)

    def _save_if_due(self, now: float) -> None:
        if now - self._last_save < self.save_interval:
            return
        self._last_save = now
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            payload = {
                "version": 1,
                "saved_at": now,
                "entries": {key: list(values) for key, values in self._entries.items()},
            }
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        except Exception as exc:
            print(f"[emergencias] rate-limit save WARN: {type(exc).__name__}: {exc}", flush=True)


class EmergenciasServiceClient:
    """Cliente HTTP local sin dependencias externas."""

    def __init__(self) -> None:
        self.url = os.getenv(
            "EMERGENCIAS_SERVICE_URL",
            "http://host.docker.internal:8789/query",
        ).strip()
        self.timeout = max(
            0.5,
            float(os.getenv("EMERGENCIAS_SERVICE_TIMEOUT_SECONDS", "3")),
        )

    def query(self, ctx: EmergenciasCommandContext) -> list[str]:
        max_bytes = max(
            80,
            min(int(os.getenv("EMERGENCIAS_MAX_TEXT_BYTES", "140")), 500),
        )
        event_limit = max(
            1,
            min(int(os.getenv("EMERGENCIAS_MAX_EVENTS_PER_QUERY", "5")), 20),
        )
        payload = json.dumps({
            "text": _normalized_command(ctx.text),
            "network": ctx.network,
            "source_id": ctx.source_id,
            "channel": ctx.channel,
            "is_direct": ctx.is_direct,
            "max_bytes": max_bytes,
            "limit": event_limit,
        }, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8", errors="replace"))
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise RuntimeError(
                f"servicio no disponible: {type(exc).__name__}: {exc}"
            ) from exc
        if not isinstance(data, dict) or not data.get("recognized", False):
            return []
        messages = data.get("messages") or []
        return [str(message).strip() for message in messages if str(message).strip()]


_LIMITER = SlidingWindowRateLimiter()
_CLIENT = EmergenciasServiceClient()


def is_emergencias_command(text: str) -> bool:
    """Reconoce el comando como palabra completa."""
    command = _normalized_command(text).casefold()
    prefixes = ("emergencia", "emergencias", "emerg")
    return any(command == prefix or command.startswith(prefix + " ") for prefix in prefixes)


def is_allowed_origin(ctx: EmergenciasCommandContext) -> bool:
    """Permite DM y, si se configura, el canal público de Emergencias."""
    if not _env_bool("EMERGENCIAS_COMMAND_ENABLED", "0"):
        return False
    if ctx.is_direct:
        return True
    variable = (
        "EMERGENCIAS_MESHCORE_CHANNEL"
        if ctx.network == "meshcore"
        else "EMERGENCIAS_MESHTASTIC_CHANNEL"
    )
    try:
        expected = int(os.getenv(variable, "-1"))
        return expected >= 0 and ctx.channel is not None and int(ctx.channel) == expected
    except (TypeError, ValueError):
        return False


def handle_emergencias_command(
    ctx: EmergenciasCommandContext,
    enqueue_direct: Callable[[str], None],
) -> bool:
    """Procesa una consulta y encola su respuesta como DM."""
    if not _env_bool("EMERGENCIAS_COMMAND_ENABLED", "0"):
        return False
    if not is_emergencias_command(ctx.text) or not is_allowed_origin(ctx):
        return False

    allowed, retry_after, duplicate = _LIMITER.check_and_record(ctx)
    if duplicate:
        return True
    if not allowed:
        minutes = max(1, int(math.ceil(retry_after / 60)))
        enqueue_direct(
            f"Límite de emergencias alcanzado. Disponible en {minutes} min."
        )
        return True

    normalized = _normalized_command(ctx.text)
    print(
        f"[emergencias] request network={ctx.network} source={ctx.source_id} "
        f"direct={ctx.is_direct} channel={ctx.channel} raw={ctx.text!r} "
        f"normalized={normalized!r} packet_id={ctx.packet_id}",
        flush=True,
    )
    try:
        messages = _CLIENT.query(ctx)
        if not messages:
            messages = ["Sin emergencias activas para esa consulta."]
    except Exception as exc:
        print(f"[emergencias] query WARN: {type(exc).__name__}: {exc}", flush=True)
        messages = ["Servicio de emergencias no disponible temporalmente."]

    maximum = max(
        1,
        int(os.getenv("EMERGENCIAS_DM_MAX_MESSAGES_PER_RESPONSE", "4")),
    )
    if len(messages) > maximum:
        selected = (
            messages[:max(0, maximum - 1)]
            + [f"Respuesta truncada a {maximum} mensajes."]
        )
    else:
        selected = messages
    delay = max(
        0.0,
        float(os.getenv("EMERGENCIAS_DM_INTER_MESSAGE_DELAY_SECONDS", "1")),
    )
    print(
        f"[emergencias] response normalized={normalized!r} "
        f"parts_generated={len(messages)} parts_enqueued={len(selected)} "
        f"complete={len(messages) <= maximum}",
        flush=True,
    )
    for index, message in enumerate(selected):
        enqueue_direct(message)
        if delay > 0 and index + 1 < len(selected):
            time.sleep(delay)
    return True
