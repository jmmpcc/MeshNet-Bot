#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Comando Telegram para administrar la pasarela interna de canales del broker."""
from __future__ import annotations

import json
import os
import socket
from html import escape


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
    """Formatea el estado en texto compacto para Telegram."""
    enabled = "ACTIVADA" if data.get("enabled") else "DESACTIVADA"
    rules = data.get("rules") or []
    lines = [
        "Pasarela interna de canales",
        f"Estado: {enabled}",
        "",
        "Reglas:",
    ]
    if rules:
        for item in rules:
            try:
                lines.append(f"CH{int(item['source'])} -> CH{int(item['destination'])}")
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
    Administra el gateway desde Telegram.

    Uso:
        /channel_gateway
        /channel_gateway on|off|list
        /channel_gateway add ORIGEN DESTINO [both]
        /channel_gateway del ORIGEN DESTINO [both]
        /channel_gateway clear

    Alias registrado por el launcher:
        /pasarela_canales

    Consulta de estado: disponible para cualquier usuario del bot.
    Cambios: solo usuarios incluidos en ADMIN_IDS.
    """
    message = update.effective_message
    user = update.effective_user
    args = [str(x).strip() for x in (getattr(context, "args", None) or []) if str(x).strip()]

    action = (args[0].lower() if args else "status")
    read_only = action in {"status", "list", "estado", "listar"}
    if not read_only:
        uid = int(getattr(user, "id", 0) or 0)
        if uid not in _admin_ids():
            await message.reply_text("Solo disponible para administradores.")
            return

    if action in {"status", "list", "estado", "listar"}:
        result = channel_gateway_rpc("CHANNEL_GATEWAY_STATUS")
    elif action in {"on", "activar", "enable"}:
        result = channel_gateway_rpc("CHANNEL_GATEWAY_ON")
    elif action in {"off", "desactivar", "disable"}:
        result = channel_gateway_rpc("CHANNEL_GATEWAY_OFF")
    elif action in {"clear", "limpiar"}:
        result = channel_gateway_rpc("CHANNEL_GATEWAY_CLEAR")
    elif action in {"add", "añadir", "anadir"}:
        if len(args) < 3:
            await message.reply_text("Uso: /channel_gateway add <origen> <destino> [both]")
            return
        try:
            src, dst = int(args[1]), int(args[2])
        except Exception:
            await message.reply_text("Origen y destino deben ser números de canal.")
            return
        both = len(args) >= 4 and args[3].lower() in {"both", "bidireccional", "bi", "2way"}
        result = channel_gateway_rpc(
            "CHANNEL_GATEWAY_ADD",
            {"source": src, "destination": dst, "both": both},
        )
    elif action in {"del", "delete", "borrar", "remove"}:
        if len(args) < 3:
            await message.reply_text("Uso: /channel_gateway del <origen> <destino> [both]")
            return
        try:
            src, dst = int(args[1]), int(args[2])
        except Exception:
            await message.reply_text("Origen y destino deben ser números de canal.")
            return
        both = len(args) >= 4 and args[3].lower() in {"both", "bidireccional", "bi", "2way"}
        result = channel_gateway_rpc(
            "CHANNEL_GATEWAY_DEL",
            {"source": src, "destination": dst, "both": both},
        )
    else:
        await message.reply_text(
            "Uso:\n"
            "/channel_gateway\n"
            "/channel_gateway on|off\n"
            "/channel_gateway add <origen> <destino> [both]\n"
            "/channel_gateway del <origen> <destino> [both]\n"
            "/channel_gateway clear"
        )
        return

    if not isinstance(result, dict) or not result.get("ok"):
        err = escape(str((result or {}).get("error") or "error desconocido"))
        await message.reply_text(f"Error de pasarela: {err}")
        return

    await message.reply_text(_format_status(result))
