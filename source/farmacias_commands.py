#!/usr/bin/env python3
"""Comandos de farmacias procesados directamente por MeshNet-Broker.

Este módulo no conoce las radios. Recibe un mensaje normalizado, consulta la
aplicación local de farmacias y devuelve los textos que el broker debe encolar
como DM por la misma red de origen.
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
from typing import Callable, Iterable


def _env_bool(name: str, default: str = "0") -> bool:
    return str(os.getenv(name, default) or default).strip().lower() in {
        "1", "true", "yes", "on", "si", "sí", "y"
    }


def _normalized_command(text: str) -> str:
    value = unicodedata.normalize("NFKC", str(text or "")).strip()
    return " ".join(value.split())


@dataclass(frozen=True)
class FarmaciasCommandContext:
    """Datos mínimos de una solicitud recibida por el broker."""

    network: str
    source_id: str
    text: str
    channel: int | None
    is_direct: bool
    packet_id: str | int | None = None


class SlidingWindowRateLimiter:
    """Limitador persistente por ``red + contacto`` con ventana deslizante."""

    def __init__(self) -> None:
        self.limit = max(1, int(os.getenv("FARMACIAS_MAX_REQUESTS_PER_HOUR", "5")))
        self.window = max(60, int(os.getenv("FARMACIAS_RATE_LIMIT_WINDOW_SECONDS", "3600")))
        self.duplicate_window = max(1, int(os.getenv("FARMACIAS_DUPLICATE_WINDOW_SECONDS", "20")))
        self.save_interval = max(10, int(os.getenv("FARMACIAS_RATE_LIMIT_SAVE_SECONDS", "60")))
        default_path = os.getenv("BOT_DATA_DIR", "/app/bot_data")
        self.path = Path(os.getenv(
            "FARMACIAS_RATE_LIMIT_FILE",
            str(Path(default_path) / "farmacias_rate_limit.json"),
        ))
        self._entries: dict[str, deque[float]] = defaultdict(deque)
        self._duplicates: dict[str, float] = {}
        self._lock = threading.RLock()
        self._last_save = 0.0
        self._load()

    def _key(self, network: str, source_id: str) -> str:
        return f"{str(network).lower()}:{str(source_id).strip()}"

    def _duplicate_key(self, ctx: FarmaciasCommandContext) -> str:
        packet = str(ctx.packet_id or "").strip()
        if packet:
            return f"{self._key(ctx.network, ctx.source_id)}:pkt:{packet}"
        bucket = int(time.time() // self.duplicate_window)
        return f"{self._key(ctx.network, ctx.source_id)}:txt:{_normalized_command(ctx.text).casefold()}:{bucket}"

    def _prune(self, now: float) -> None:
        cutoff = now - self.window
        for key in list(self._entries):
            q = self._entries[key]
            while q and q[0] <= cutoff:
                q.popleft()
            if not q:
                self._entries.pop(key, None)
        dup_cutoff = now - self.duplicate_window
        self._duplicates = {k: ts for k, ts in self._duplicates.items() if ts > dup_cutoff}

    def check_and_record(self, ctx: FarmaciasCommandContext) -> tuple[bool, int, bool]:
        """Devuelve ``(permitida, espera_segundos, duplicada)`` y registra atómicamente."""
        now = time.time()
        with self._lock:
            self._prune(now)
            dup_key = self._duplicate_key(ctx)
            if dup_key in self._duplicates:
                return False, 0, True
            self._duplicates[dup_key] = now

            key = self._key(ctx.network, ctx.source_id)
            q = self._entries[key]
            if len(q) >= self.limit:
                retry = max(1, int(math.ceil(self.window - (now - q[0]))))
                self._save_if_due(now)
                return False, retry, False
            q.append(now)
            self._save_if_due(now)
            return True, 0, False

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            entries = raw.get("entries", {}) if isinstance(raw, dict) else {}
            now = time.time()
            cutoff = now - self.window
            for key, values in entries.items():
                valid = [float(v) for v in values if float(v) > cutoff]
                if valid:
                    self._entries[str(key)] = deque(sorted(valid))
        except FileNotFoundError:
            return
        except Exception as exc:
            print(f"[farmacias] rate-limit load WARN: {type(exc).__name__}: {exc}", flush=True)

    def _save_if_due(self, now: float) -> None:
        if now - self._last_save < self.save_interval:
            return
        self._last_save = now
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            payload = {"version": 1, "saved_at": now, "entries": {k: list(v) for k, v in self._entries.items()}}
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception as exc:
            print(f"[farmacias] rate-limit save WARN: {type(exc).__name__}: {exc}", flush=True)


class FarmaciasServiceClient:
    """Cliente HTTP local, sin dependencias externas, para la app de farmacias."""

    def __init__(self) -> None:
        self.url = os.getenv("FARMACIAS_SERVICE_URL", "http://host.docker.internal:8788/query").strip()
        self.timeout = max(0.5, float(os.getenv("FARMACIAS_SERVICE_TIMEOUT_SECONDS", "3")))

    def query(self, ctx: FarmaciasCommandContext) -> list[str]:
        payload = json.dumps({
            "text": _normalized_command(ctx.text),
            "network": ctx.network,
            "source_id": ctx.source_id,
            "channel": ctx.channel,
            "is_direct": ctx.is_direct,
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8", errors="replace"))
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise RuntimeError(f"servicio no disponible: {type(exc).__name__}: {exc}") from exc
        if not isinstance(data, dict) or not data.get("recognized", False):
            return []
        messages = data.get("messages") or []
        return [str(msg).strip() for msg in messages if str(msg).strip()]


_LIMITER = SlidingWindowRateLimiter()
_CLIENT = FarmaciasServiceClient()


def is_farmacias_command(text: str) -> bool:
    """Reconoce ``farma`` como palabra completa, sin capturar textos ordinarios."""
    cmd = _normalized_command(text).casefold()
    return cmd == "farma" or cmd.startswith("farma ")


def is_allowed_origin(ctx: FarmaciasCommandContext) -> bool:
    """Permite todos los DM y solo el canal FARMACIA configurado en mensajes públicos."""
    if ctx.is_direct:
        return True
    try:
        expected = int(os.getenv(
            "FARMACIAS_MESHCORE_CHANNEL" if ctx.network == "meshcore" else "FARMACIAS_MESHTASTIC_CHANNEL",
            "-1",
        ))
        return ctx.channel is not None and int(ctx.channel) == expected
    except Exception:
        return False


def handle_farmacias_command(
    ctx: FarmaciasCommandContext,
    enqueue_direct: Callable[[str], None],
) -> bool:
    """Procesa una orden ``farma`` y consume siempre el mensaje reconocido.

    ``enqueue_direct`` debe encolar un único texto como DM al contacto de origen.
    El broker aporta un callback distinto para MeshCore y Meshtastic, de modo que
    esta función no puede seleccionar accidentalmente una red incorrecta.
    """
    if not _env_bool("FARMACIAS_COMMAND_ENABLED", "1") or not is_farmacias_command(ctx.text):
        return False
    if not is_allowed_origin(ctx):
        return False

    allowed, retry_after, duplicate = _LIMITER.check_and_record(ctx)
    if duplicate:
        return True
    if not allowed:
        minutes = max(1, int(math.ceil(retry_after / 60)))
        enqueue_direct(f"Límite alcanzado: 5 consultas por hora. Disponible en {minutes} min.")
        return True

    try:
        messages = _CLIENT.query(ctx)
        if not messages:
            messages = ["Consulta de farmacias sin resultados. Usa: farma"]
    except Exception as exc:
        print(f"[farmacias] query WARN: {type(exc).__name__}: {exc}", flush=True)
        messages = ["Servicio de farmacias no disponible temporalmente."]

    max_messages = max(1, int(os.getenv("FARMACIAS_DM_MAX_MESSAGES_PER_RESPONSE", "6")))
    for message in messages[:max_messages]:
        enqueue_direct(message)
    if len(messages) > max_messages:
        enqueue_direct(f"Respuesta truncada a {max_messages} mensajes.")
    return True
