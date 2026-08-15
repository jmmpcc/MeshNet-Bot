#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Comando Telegram para administrar la pasarela interna de canales del broker."""
from __future__ import annotations

import json
import os
import socket
from html import escape


_TRANSPORT_ALIASES = {
    "meshcore": "meshcore",
    "mc": "meshcore",
    "meshtastic": "meshtastic",
    "mesh": "meshtastic",
    "mt": "meshtastic",
}


def _admin_ids() -> set[int]:
    """Lee ADMIN_IDS con el mismo formato CSV/; usado por Telegram_Bot_Broker.py."""
    return {
        int(x)
        for x in (os.getenv("ADMIN_IDS") or "").replace(";", ",").split(",")
        if x.strip().isdigit()
    }


def _ctrl_target() -> tuple[str, int]:
    """Resuelve host/puerto del control CHANNEL_GATEWAY dentro del broker."""
    host = (
        os.getenv("CHANNEL_GATEWAY_CTRL_HOST")
        or os.getenv("BROKER_CTRL_HOST")
        or os.getenv("BROKER_HOST")
        or "127.0.0.1"
    ).strip()
    try:
        default_port = int(os.getenv("BROKER_CTRL_PORT", "8766") or 8766) + 1
    except Exception:
        default_port = 8767
    try:
        port = int(os.getenv("CHANNEL_GATEWAY_CTRL_PORT", str(default_port)) or default_port)
    except Exception:
        port = default_port
    return host, port


def _radio_profile_context() -> dict:
    """
    Resuelve el perfil de radio usando el resolvedor común del proyecto.

    Devuelve siempre un diccionario con:
        profile: nombre canónico/legacy.
        valid: si el perfil es conocido.
        transports: transportes que el gateway puede aceptar en este perfil.
        node_a_transport / node_b_transport: distribución física/lógica actual.
        embedded_bridge_enabled: si existe nodo B embebido.

    Esta función no modifica el entorno ni abre conexiones de radio.
    """
    try:
        from radio_profile import resolve_radio_profile

        caps = resolve_radio_profile(env=os.environ, strict=False)
        transports: list[str] = []
        if bool(getattr(caps, "meshtastic_enabled", False)):
            transports.append("meshtastic")
        if bool(getattr(caps, "meshcore_enabled", False)):
            transports.append("meshcore")
        return {
            "profile": str(getattr(caps, "profile", "legacy") or "legacy"),
            "valid": bool(getattr(caps, "valid", False)),
            "legacy_mode": bool(getattr(caps, "legacy_mode", False)),
            "transports": tuple(transports),
            "node_a_transport": getattr(caps, "node_a_transport", None),
            "node_b_transport": getattr(caps, "node_b_transport", None),
            "embedded_bridge_enabled": bool(getattr(caps, "embedded_bridge_enabled", False)),
        }
    except Exception:
        # Fallback conservador: una función nueva no debe asumir una topología si
        # el resolvedor común no está disponible.
        return {
            "profile": (os.getenv("RADIO_PROFILE") or "legacy").strip() or "legacy",
            "valid": False,
            "legacy_mode": True,
            "transports": (),
            "node_a_transport": None,
            "node_b_transport": None,
            "embedded_bridge_enabled": False,
        }


def _normalize_transport(value: object) -> str | None:
    """Normaliza aliases de transporte admitidos por el comando."""
    token = str(value or "").strip().lower()
    return _TRANSPORT_ALIASES.get(token)


def _resolve_transport_for_command(args: list[str], profile_ctx: dict) -> tuple[str | None, int]:
    """
    Resuelve el transporte de una orden add/del y el índice donde empiezan los canales.

    Reglas:
      - Un único transporte válido: se permite omitirlo.
      - Dos transportes válidos: es obligatorio indicar meshcore/meshtastic.
      - Transporte incompatible con el perfil: se rechaza.
      - Perfil legacy/desconocido: no se adivina el transporte.

    Retorna:
        (transport, channel_arg_index)

    Ejemplos:
        meshcore_only + ["add", "0", "2"] -> ("meshcore", 1)
        combinado + ["add", "meshcore", "0", "2"] -> ("meshcore", 2)
    """
    allowed = tuple(profile_ctx.get("transports") or ())
    if not allowed:
        return None, -1

    explicit = _normalize_transport(args[1]) if len(args) >= 2 else None
    if explicit:
        if explicit not in allowed:
            return None, -2
        return explicit, 2

    if len(allowed) == 1:
        return allowed[0], 1

    return None, -3


def _format_contextual_help(profile_ctx: dict) -> str:
    """Genera ayuda del comando mostrando exclusivamente opciones válidas para RADIO_PROFILE."""
    profile = str(profile_ctx.get("profile") or "legacy")
    allowed = tuple(profile_ctx.get("transports") or ())
    node_a = profile_ctx.get("node_a_transport")
    node_b = profile_ctx.get("node_b_transport")

    lines = [
        "Pasarela interna de canales",
        f"Perfil: {profile}",
    ]

    if node_a:
        lines.append(f"Nodo A: {str(node_a).capitalize()}")
    if node_b:
        suffix = " embebido" if profile_ctx.get("embedded_bridge_enabled") else ""
        lines.append(f"Nodo B: {str(node_b).capitalize()}{suffix}")

    lines.extend([
        "",
        "Comandos disponibles:",
        "/channel_gateway status",
        "/channel_gateway list",
        "/channel_gateway on",
        "/channel_gateway off",
    ])

    if len(allowed) == 1:
        transport = allowed[0]
        lines.extend([
            "",
            f"Transporte válido: {transport}",
            "El transporte puede omitirse porque el perfil solo admite uno:",
            "/channel_gateway add <origen> <destino> [both]",
            "/channel_gateway del <origen> <destino> [both]",
            "",
            "También se admite la forma explícita:",
            f"/channel_gateway add {transport} <origen> <destino> [both]",
            f"/channel_gateway del {transport} <origen> <destino> [both]",
        ])
    elif len(allowed) > 1:
        lines.extend([
            "",
            "Transportes válidos para este perfil: " + ", ".join(allowed),
            "En perfiles combinados es obligatorio indicar el transporte:",
        ])
        for transport in allowed:
            lines.append(f"/channel_gateway add {transport} <origen> <destino> [both]")
            lines.append(f"/channel_gateway del {transport} <origen> <destino> [both]")
    else:
        lines.extend([
            "",
            "No se puede determinar de forma segura un transporte válido.",
            "Define un RADIO_PROFILE canónico antes de crear o eliminar reglas.",
        ])

    lines.extend([
        "",
        "/channel_gateway clear",
        "",
        "both = crea/elimina las dos direcciones.",
    ])
    return "\n".join(lines)


def channel_gateway_rpc(cmd: str, params: dict | None = None, timeout: float = 3.0) -> dict:
    """
    Ejecuta una orden JSONL contra el servidor de control del gateway.

    No abre conexión al nodo de radio; únicamente al socket de control del
    proceso broker.
    """
    host, port = _ctrl_target()
    req = {
        "cmd": str(cmd or "").upper(),
        "params": dict(params or {}),
    }
    token = (os.getenv("CHANNEL_GATEWAY_CTRL_TOKEN") or "").strip()
    if token:
        req["token"] = token

    data = (json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(data)
            buf = b""
            while b"\n" not in buf and len(buf) < 65536:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
        raw = buf.split(b"\n", 1)[0].decode("utf-8", errors="replace").strip()
        return json.loads(raw or "{}")
    except Exception as exc:
        return {"ok": False, "error": f"control_unavailable: {type(exc).__name__}: {exc}"}


def _format_status(data: dict) -> str:
    """Formatea el estado multi-radio en texto compacto para Telegram."""
    enabled = "ACTIVADA" if data.get("enabled") else "DESACTIVADA"
    rules = data.get("rules") or []
    profile_ctx = _radio_profile_context()
    lines = [
        "Pasarela interna de canales",
        f"Estado: {enabled}",
        f"Perfil: {profile_ctx.get('profile')}",
        "",
        "Reglas:",
    ]
    if rules:
        for item in rules:
            try:
                transport = str(item.get("transport") or "").strip().lower()
                prefix = f"{transport}: " if transport else ""
                active = item.get("active_for_profile")
                suffix = " [inactiva por perfil]" if active is False else ""
                lines.append(
                    f"{prefix}CH{int(item['source'])} -> CH{int(item['destination'])}{suffix}"
                )
            except Exception:
                continue
    else:
        lines.append("(sin reglas)")

    stats = data.get("stats") or {}
    if isinstance(stats, dict):
        lines.extend([
            "",
            f"Reenviados: {int(stats.get('forwarded', 0) or 0)}",
            f"Eco bloqueado: {int(stats.get('echo_suppressed', 0) or 0)}",
            f"Duplicados RX: {int(stats.get('duplicate_rx', 0) or 0)}",
            f"Rate-limit: {int(stats.get('rate_limited', 0) or 0)}",
        ])
    return "\n".join(lines)


async def channel_gateway_cmd(update, context) -> None:
    """
    Administra el gateway desde Telegram con validación contextual por RADIO_PROFILE.

    Uso general:
        /channel_gateway
        /channel_gateway status|list
        /channel_gateway on|off
        /channel_gateway add [transporte] ORIGEN DESTINO [both]
        /channel_gateway del [transporte] ORIGEN DESTINO [both]
        /channel_gateway clear

    Comportamiento sin parámetros:
        Muestra exclusivamente la sintaxis válida para el perfil activo.

    Reglas de transporte:
        - meshcore_only: transporte implícito MeshCore; "meshtastic" se rechaza.
        - perfiles combinados: obliga a indicar meshcore o meshtastic.
        - legacy/desconocido: no permite crear/eliminar reglas ambiguas.

    Alias registrado por el launcher:
        /pasarela_canales

    Consulta de estado/ayuda: disponible para cualquier usuario del bot.
    Cambios: solo usuarios incluidos en ADMIN_IDS.
    """
    message = update.effective_message
    user = update.effective_user
    args = [str(x).strip() for x in (getattr(context, "args", None) or []) if str(x).strip()]
    profile_ctx = _radio_profile_context()

    # Sin parámetros: ayuda contextual, como protección frente a argumentos que
    # no pertenecen al RADIO_PROFILE activo.
    if not args:
        await message.reply_text(_format_contextual_help(profile_ctx))
        return

    action = args[0].lower()
    read_only = action in {"status", "list", "estado", "listar", "help", "ayuda"}
    if not read_only:
        uid = int(getattr(user, "id", 0) or 0)
        if uid not in _admin_ids():
            await message.reply_text("Solo disponible para administradores.")
            return

    if action in {"help", "ayuda"}:
        await message.reply_text(_format_contextual_help(profile_ctx))
        return

    if action in {"status", "list", "estado", "listar"}:
        result = channel_gateway_rpc("CHANNEL_GATEWAY_STATUS")
    elif action in {"on", "activar", "enable"}:
        result = channel_gateway_rpc("CHANNEL_GATEWAY_ON")
    elif action in {"off", "desactivar", "disable"}:
        result = channel_gateway_rpc("CHANNEL_GATEWAY_OFF")
    elif action in {"clear", "limpiar"}:
        result = channel_gateway_rpc("CHANNEL_GATEWAY_CLEAR")
    elif action in {"add", "añadir", "anadir", "del", "delete", "borrar", "remove"}:
        transport, channel_index = _resolve_transport_for_command(args, profile_ctx)

        if channel_index == -1:
            await message.reply_text(
                "No se puede determinar un transporte válido para el perfil actual.\n\n"
                + _format_contextual_help(profile_ctx)
            )
            return
        if channel_index == -2:
            allowed = ", ".join(profile_ctx.get("transports") or ()) or "ninguno"
            await message.reply_text(
                f"Transporte no válido para RADIO_PROFILE={profile_ctx.get('profile')}. "
                f"Válidos: {allowed}."
            )
            return
        if channel_index == -3:
            await message.reply_text(
                "Este perfil tiene más de un transporte activo. Debes indicar meshcore o meshtastic.\n\n"
                + _format_contextual_help(profile_ctx)
            )
            return

        if transport is None or len(args) < channel_index + 2:
            await message.reply_text(_format_contextual_help(profile_ctx))
            return

        try:
            src = int(args[channel_index])
            dst = int(args[channel_index + 1])
        except Exception:
            await message.reply_text("Origen y destino deben ser números de canal.")
            return

        if src < 0 or dst < 0:
            await message.reply_text("Origen y destino deben ser canales >= 0.")
            return
        if src == dst:
            await message.reply_text("Origen y destino deben ser canales distintos.")
            return

        both_index = channel_index + 2
        both = len(args) > both_index and args[both_index].lower() in {
            "both", "bidireccional", "bi", "2way"
        }

        params = {
            "transport": transport,
            "source": src,
            "destination": dst,
            "both": both,
        }
        if action in {"add", "añadir", "anadir"}:
            result = channel_gateway_rpc("CHANNEL_GATEWAY_ADD", params)
        else:
            result = channel_gateway_rpc("CHANNEL_GATEWAY_DEL", params)
    else:
        await message.reply_text(_format_contextual_help(profile_ctx))
        return

    if not isinstance(result, dict) or not result.get("ok"):
        err = escape(str((result or {}).get("error") or "error desconocido"))
        await message.reply_text(f"Error de pasarela: {err}")
        return

    await message.reply_text(_format_status(result))
