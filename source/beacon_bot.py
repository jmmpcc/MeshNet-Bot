#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Balizas periódicas de usuario para Meshtastic y MeshCore.

Este módulo es una extensión aislada del bot Telegram. No modifica las funciones
históricas de envío: reutiliza el servidor de control del broker, que ya es el
propietario de las rutas TX SEND_TEXT (Meshtastic) y MESHCORE_SEND (MeshCore).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import time
from dataclasses import dataclass, field
from typing import Any

from radio_profile import resolve_radio_profile

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_MIN_INTERVAL_MINUTES = 1
_MAX_INTERVAL_MINUTES = 24 * 60
_MIN_DURATION_HOURS = 1
_MAX_DURATION_HOURS = 168
_MIN_CHANNEL = 0
_MAX_CHANNEL_MESHTASTIC = 7
_MAX_CHANNEL_MESHCORE = 40
_NUMBER_MODE_ON = "num"
_NUMBER_MODE_OFF = "nonum"


@dataclass(slots=True)
class BeaconSpec:
    """Describe una baliza activa y su estado de ejecución.

    Parámetros:
        transport: ``meshtastic`` o ``meshcore``.
        interval_minutes: periodo entre transmisiones, en minutos.
        max_hours: duración máxima de la baliza, en horas.
        name: identificador único dentro del transporte.
        channel: canal lógico de transmisión.
        text: texto base configurado por el administrador.
        numbered: si True, antepone ``1.``, ``2.``, etc. a cada TX confirmado.
        sequence: último número confirmado por el broker para esta baliza.
        created_monotonic: instante monotónico de creación.
        task: tarea asyncio que ejecuta la baliza.

    La clase se crea desde :func:`_start_beacon` y no abre conexiones por sí sola.
    """

    transport: str
    interval_minutes: int
    max_hours: int
    name: str
    channel: int
    text: str
    numbered: bool = False
    sequence: int = 0
    created_monotonic: float = field(default_factory=time.monotonic)
    task: asyncio.Task | None = field(default=None, repr=False)

    @property
    def key(self) -> tuple[str, str]:
        """Devuelve la clave normalizada usada para evitar nombres duplicados."""
        return self.transport, self.name.casefold()

    @property
    def expires_monotonic(self) -> float:
        """Calcula el instante monotónico en el que la baliza debe finalizar."""
        return self.created_monotonic + (self.max_hours * 3600)


_ACTIVE_BEACONS: dict[tuple[str, str], BeaconSpec] = {}
_LOCK = asyncio.Lock()


def _admin_ids() -> set[int]:
    """Lee ``ADMIN_IDS`` con el formato CSV/semicolon usado por el bot principal."""
    return {
        int(value)
        for value in (os.getenv("ADMIN_IDS") or "").replace(";", ",").split(",")
        if value.strip().isdigit()
    }


def _is_admin(update: Any) -> bool:
    """Comprueba si el usuario efectivo de Telegram pertenece a ``ADMIN_IDS``."""
    user = getattr(update, "effective_user", None)
    try:
        return int(getattr(user, "id", 0) or 0) in _admin_ids()
    except Exception:
        return False


def _available_transports() -> set[str]:
    """Devuelve los transportes habilitados por el ``RADIO_PROFILE`` actual.

    Se llama antes de crear una baliza para impedir que ``/baliza`` intente usar
    Meshtastic en ``meshcore_only`` y viceversa. Reutiliza el resolvedor común del
    proyecto y no modifica ninguna variable de entorno.
    """
    try:
        caps = resolve_radio_profile(env=os.environ, strict=False)
        available: set[str] = set()
        if bool(getattr(caps, "meshtastic_enabled", False)):
            available.add("meshtastic")
        if bool(getattr(caps, "meshcore_enabled", False)):
            available.add("meshcore")
        return available
    except Exception:
        return set()


def _broker_ctrl_target() -> tuple[str, int]:
    """Resuelve el host y puerto del BacklogServer/control del broker.

    Prioridad de host: ``BROKER_CTRL_HOST`` -> ``BROKER_HOST`` -> localhost.
    El puerto por defecto es ``BROKER_PORT + 1``, igual que en el bot existente.
    """
    host = (
        os.getenv("BROKER_CTRL_HOST")
        or os.getenv("BROKER_HOST")
        or "127.0.0.1"
    ).strip()
    try:
        default_port = int(os.getenv("BROKER_PORT", "8765") or 8765) + 1
    except Exception:
        default_port = 8766
    try:
        port = int(os.getenv("BROKER_CTRL_PORT", str(default_port)) or default_port)
    except Exception:
        port = default_port
    return host, port


def _broker_rpc(cmd: str, params: dict[str, Any], timeout: float = 8.0) -> dict[str, Any]:
    """Envía una orden JSONL al control del broker y devuelve su respuesta.

    Parámetros:
        cmd: comando de control, actualmente ``SEND_TEXT`` o ``MESHCORE_SEND``.
        params: parámetros que ya entiende el broker estable.
        timeout: timeout de socket en segundos.

    Esta función no abre conexiones directas con los nodos de radio; preserva la
    arquitectura actual donde el broker es el único propietario del TX.
    """
    host, port = _broker_ctrl_target()
    request = {"cmd": str(cmd), "params": dict(params)}
    data = (json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(data)
            buffer = b""
            while b"\n" not in buffer and len(buffer) < 65536:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buffer += chunk
        raw = buffer.split(b"\n", 1)[0].decode("utf-8", errors="replace").strip()
        if not raw:
            return {"ok": False, "error": "empty_response"}
        response = json.loads(raw)
        if not isinstance(response, dict):
            return {"ok": False, "error": "invalid_response"}
        return response
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _text_for_next_transmission(spec: BeaconSpec) -> str:
    """Construye el texto de la próxima transmisión sin alterar el contador.

    Para una baliza sin numerar devuelve exactamente ``spec.text``. Para una
    baliza numerada utiliza el siguiente número todavía no confirmado. El contador
    solo se consolida en :func:`_send_beacon` cuando el broker devuelve ``ok=True``.
    """
    if not spec.numbered:
        return spec.text
    return f"{spec.sequence + 1}. {spec.text}"


def _send_beacon_sync(spec: BeaconSpec) -> dict[str, Any]:
    """Transmite una emisión de baliza reutilizando las rutas TX existentes.

    Meshtastic utiliza ``SEND_TEXT`` con broadcast y sin ACK. MeshCore utiliza
    ``MESHCORE_SEND`` por ``channel_idx``. Si la baliza es numerada se envía el
    siguiente ordinal; si no lo es, se conserva exactamente el texto configurado.
    """
    outgoing_text = _text_for_next_transmission(spec)
    if spec.transport == "meshtastic":
        return _broker_rpc(
            "SEND_TEXT",
            {
                "ch": int(spec.channel),
                "text": outgoing_text,
                "destination": None,
                "require_ack": False,
                "origin": "telegram_beacon",
                "meta": {"beacon": spec.name},
            },
        )
    if spec.transport == "meshcore":
        return _broker_rpc(
            "MESHCORE_SEND",
            {
                "channel_idx": int(spec.channel),
                "text": outgoing_text,
                "max_retries": 0,
            },
        )
    return {"ok": False, "error": "unsupported_transport"}


async def _send_beacon(spec: BeaconSpec) -> dict[str, Any]:
    """Envía una baliza fuera del event loop y consolida su contador si procede.

    Una numeración solo avanza cuando el broker confirma ``ok=True``. Así, un fallo
    local de conexión/control no provoca saltos visibles del tipo 1, 3, 4.
    """
    result = await asyncio.to_thread(_send_beacon_sync, spec)
    if spec.numbered and isinstance(result, dict) and result.get("ok"):
        spec.sequence += 1
    return result


async def _beacon_worker(spec: BeaconSpec) -> None:
    """Ejecuta una baliza hasta alcanzar su duración máxima o ser cancelada.

    La primera transmisión se realiza inmediatamente. Las posteriores respetan
    ``interval_minutes``. Si un TX puntual falla, la baliza permanece activa y
    reintenta únicamente en el siguiente periodo; no introduce bucles de reintento
    adicionales que puedan saturar la malla.
    """
    try:
        while True:
            now = time.monotonic()
            if now >= spec.expires_monotonic:
                return

            await _send_beacon(spec)

            remaining = spec.expires_monotonic - time.monotonic()
            if remaining <= 0:
                return
            sleep_seconds = min(float(spec.interval_minutes * 60), remaining)
            await asyncio.sleep(sleep_seconds)
    except asyncio.CancelledError:
        raise
    finally:
        async with _LOCK:
            current = _ACTIVE_BEACONS.get(spec.key)
            if current is spec:
                _ACTIVE_BEACONS.pop(spec.key, None)


def _validate_definition(
    args: list[str],
    transport: str = "meshtastic",
) -> tuple[int, int, str, int, bool, str] | str:
    """Valida los argumentos comunes de ``/baliza`` y ``/baliza_mc``.

    Sintaxis:
        ``<minutos> <horas> <nombre> <canal> [num|nonum] <texto...>``

    Rango de canal por transporte:
        - Meshtastic: 0..7.
        - MeshCore: 0..40.

    Compatibilidad:
        ``transport`` es opcional y por defecto ``meshtastic`` para que las
        llamadas históricas directas a este helper conserven el límite 0..7.
        Si no aparece ``num`` o ``nonum`` justo después del canal, todo lo que
        sigue se considera texto y la baliza queda sin numerar.

    Retorna:
        ``(intervalo, horas, nombre, canal, numerada, texto)`` o un mensaje de error.
    """
    if len(args) < 5:
        return "Faltan parámetros."
    try:
        interval = int(args[0])
    except Exception:
        return "El intervalo debe ser un número entero de minutos."
    try:
        max_hours = int(args[1])
    except Exception:
        return "La duración máxima debe ser un número entero de horas."

    name = str(args[2] or "").strip()
    try:
        channel = int(args[3])
    except Exception:
        return "El canal debe ser un número entero."

    numbered = False
    text_start = 4
    mode = str(args[4] or "").strip().lower()
    if mode == _NUMBER_MODE_ON:
        numbered = True
        text_start = 5
    elif mode == _NUMBER_MODE_OFF:
        numbered = False
        text_start = 5

    text = " ".join(str(x) for x in args[text_start:]).strip()

    if not (_MIN_INTERVAL_MINUTES <= interval <= _MAX_INTERVAL_MINUTES):
        return f"El intervalo debe estar entre {_MIN_INTERVAL_MINUTES} y {_MAX_INTERVAL_MINUTES} minutos."
    if not (_MIN_DURATION_HOURS <= max_hours <= _MAX_DURATION_HOURS):
        return f"La duración máxima debe estar entre {_MIN_DURATION_HOURS} y {_MAX_DURATION_HOURS} horas."
    if not _NAME_RE.fullmatch(name):
        return "El nombre debe tener 1-32 caracteres: letras, números, guion o guion bajo."

    transport_norm = str(transport or "meshtastic").strip().lower()
    max_channel = (
        _MAX_CHANNEL_MESHCORE
        if transport_norm == "meshcore"
        else _MAX_CHANNEL_MESHTASTIC
    )
    if not (_MIN_CHANNEL <= channel <= max_channel):
        return f"El canal {transport_norm} debe estar entre {_MIN_CHANNEL} y {max_channel}."
    if not text:
        return "El texto de la baliza no puede estar vacío."

    return interval, max_hours, name, channel, numbered, text


def _usage(transport: str) -> str:
    """Genera la ayuda compacta correspondiente al transporte solicitado."""
    if transport == "meshcore":
        return (
            "Uso:\n"
            "/baliza_mc <minutos> <horas> <nombre> <canal 0-40> [num|nonum] <texto>\n"
            "num = 1. texto, 2. texto, 3. texto...\n"
            "nonum o argumento omitido = texto sin numerar\n"
            "/balizas_mc\n"
            "/parar_baliza_mc <nombre>"
        )
    return (
        "Uso:\n"
        "/baliza <minutos> <horas> <nombre> <canal 0-7> [num|nonum] <texto>\n"
        "num = 1. texto, 2. texto, 3. texto...\n"
        "nonum o argumento omitido = texto sin numerar\n"
        "/balizas\n"
        "/parar_baliza <nombre>"
    )


async def _start_beacon(update: Any, context: Any, transport: str) -> None:
    """Crea una nueva baliza validada y lanza su tarea periódica.

    Solo los administradores pueden crear balizas. El transporte debe estar
    habilitado por el perfil de radio. El nombre es único por transporte. El modo
    de numeración es opcional y por defecto permanece desactivado.
    """
    message = getattr(update, "effective_message", None)
    if message is None:
        return
    if not _is_admin(update):
        await message.reply_text("Solo disponible para administradores.")
        return

    available = _available_transports()
    if transport not in available:
        await message.reply_text(
            f"{transport.capitalize()} no está disponible con el RADIO_PROFILE actual."
        )
        return

    args = [str(x) for x in (getattr(context, "args", None) or [])]
    parsed = _validate_definition(args, transport=transport)
    if isinstance(parsed, str):
        await message.reply_text(parsed + "\n\n" + _usage(transport))
        return

    interval, max_hours, name, channel, numbered, text = parsed
    spec = BeaconSpec(
        transport=transport,
        interval_minutes=interval,
        max_hours=max_hours,
        name=name,
        channel=channel,
        text=text,
        numbered=numbered,
    )

    duplicate = False
    async with _LOCK:
        existing = _ACTIVE_BEACONS.get(spec.key)
        if existing is not None and existing.task is not None and not existing.task.done():
            duplicate = True
        else:
            _ACTIVE_BEACONS[spec.key] = spec
            spec.task = asyncio.create_task(
                _beacon_worker(spec),
                name=f"meshnet-beacon:{transport}:{name}",
            )

    if duplicate:
        await message.reply_text(
            f"Ya existe una baliza {transport} activa con el nombre '{name}'."
        )
        return

    await message.reply_text(
        f"Baliza '{name}' activada.\n"
        f"Red: {transport}\n"
        f"Canal: {channel}\n"
        f"Cada: {interval} min\n"
        f"Máximo: {max_hours} h\n"
        f"Numeración: {'SI' if numbered else 'NO'}"
    )


async def _stop_beacon(update: Any, context: Any, transport: str) -> None:
    """Detiene por nombre una baliza activa del transporte indicado."""
    message = getattr(update, "effective_message", None)
    if message is None:
        return
    if not _is_admin(update):
        await message.reply_text("Solo disponible para administradores.")
        return

    args = [str(x).strip() for x in (getattr(context, "args", None) or []) if str(x).strip()]
    if len(args) != 1:
        await message.reply_text(_usage(transport))
        return
    name = args[0]
    key = (transport, name.casefold())

    async with _LOCK:
        spec = _ACTIVE_BEACONS.get(key)
        if spec is None or spec.task is None or spec.task.done():
            spec = None
        else:
            task = spec.task
            _ACTIVE_BEACONS.pop(key, None)
            task.cancel()

    if spec is None:
        await message.reply_text(f"No existe una baliza {transport} activa llamada '{name}'.")
        return

    await message.reply_text(f"Baliza '{spec.name}' detenida.")


async def _list_beacons(update: Any, transport: str) -> None:
    """Lista las balizas activas de un transporte con estado de numeración."""
    message = getattr(update, "effective_message", None)
    if message is None:
        return
    if not _is_admin(update):
        await message.reply_text("Solo disponible para administradores.")
        return

    async with _LOCK:
        specs = [
            spec for spec in _ACTIVE_BEACONS.values()
            if spec.transport == transport and spec.task is not None and not spec.task.done()
        ]

    if not specs:
        await message.reply_text(f"No hay balizas {transport} activas.")
        return

    lines = [f"Balizas {transport} activas:"]
    now = time.monotonic()
    for spec in sorted(specs, key=lambda item: item.name.casefold()):
        remaining_minutes = max(0, int((spec.expires_monotonic - now + 59) // 60))
        numbering = f"num (último {spec.sequence})" if spec.numbered else "nonum"
        lines.append(
            f"- {spec.name}: CH{spec.channel}, cada {spec.interval_minutes} min, "
            f"{numbering}, restan ~{remaining_minutes} min"
        )
    await message.reply_text("\n".join(lines))


async def baliza_cmd(update: Any, context: Any) -> None:
    """Handler de ``/baliza`` para crear una baliza Meshtastic."""
    await _start_beacon(update, context, "meshtastic")


async def baliza_mc_cmd(update: Any, context: Any) -> None:
    """Handler de ``/baliza_mc`` para crear una baliza MeshCore."""
    await _start_beacon(update, context, "meshcore")


async def parar_baliza_cmd(update: Any, context: Any) -> None:
    """Handler de ``/parar_baliza <nombre>`` para Meshtastic."""
    await _stop_beacon(update, context, "meshtastic")


async def parar_baliza_mc_cmd(update: Any, context: Any) -> None:
    """Handler de ``/parar_baliza_mc <nombre>`` para MeshCore."""
    await _stop_beacon(update, context, "meshcore")


async def balizas_cmd(update: Any, context: Any) -> None:
    """Handler de ``/balizas`` para listar balizas Meshtastic activas."""
    del context
    await _list_beacons(update, "meshtastic")


async def balizas_mc_cmd(update: Any, context: Any) -> None:
    """Handler de ``/balizas_mc`` para listar balizas MeshCore activas."""
    del context
    await _list_beacons(update, "meshcore")


def contextual_help() -> str:
    """Devuelve ayuda de balizas filtrada por el perfil de radio actual."""
    available = _available_transports()
    lines = ["Balizas periódicas:"]
    if "meshtastic" in available:
        lines.extend([
            "/baliza <min> <horas> <nombre> <canal 0-7> [num|nonum] <texto>",
            "/balizas",
            "/parar_baliza <nombre>",
        ])
    if "meshcore" in available:
        lines.extend([
            "/baliza_mc <min> <horas> <nombre> <canal 0-40> [num|nonum] <texto>",
            "/balizas_mc",
            "/parar_baliza_mc <nombre>",
        ])
    if len(lines) == 1:
        lines.append("No hay transporte de radio habilitado por RADIO_PROFILE.")
    else:
        lines.extend([
            "num = numeración automática 1., 2., 3....",
            "nonum o sin argumento = texto original sin numerar.",
        ])
    return "\n".join(lines)
