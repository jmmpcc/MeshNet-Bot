#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Adaptador de transporte para comandos BBS recibidos desde MeshCore.

El motor :mod:`bbs_server` es independiente de la radio. Este módulo conserva
la política ya usada por Meshtastic (DM, canal público multi-BBS y
``BBS_DM_ONLY``) y devuelve respuestas con un destino explícito, sin enviar ni
abrir conexiones por su cuenta.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional


@dataclass(frozen=True)
class BbsReply:
    """Respuesta BBS lista para el transporte que recibió el comando."""

    text: str
    direct: bool
    channel: int


def _looks_like_callsign(value: str) -> bool:
    token = str(value or "").strip().upper()
    return (
        3 <= len(token) <= 16
        and any(char.isalpha() for char in token)
        and any(char.isdigit() for char in token)
        and all(char.isalnum() or char in "-/" for char in token)
    )


def _clean_chunks(chunks: Optional[Iterable[object]]) -> tuple[str, ...]:
    return tuple(text for item in (chunks or ()) if (text := str(item or "").strip()))


def handle_bbs_transport_command(
    *,
    engine: object,
    text: str,
    source_id: str,
    channel: Optional[int],
    is_direct: bool,
    bbs_callsign: str,
    allowed_channels: set[int],
    dm_channel: int = 0,
    dm_only: bool = True,
    dm_init_hint: bool = True,
) -> Optional[tuple[BbsReply, ...]]:
    """Procesa un ``#BBS`` aplicando la misma política del receptor Meshtastic.

    ``None`` significa que el texto no es BBS. Una tupla vacía significa que sí
    lo era, pero iba dirigido a otra BBS o llegó por un canal no autorizado.
    De este modo el receptor puede ocultar los comandos de control sin impedir
    que el resto del tráfico continúe su flujo normal.
    """

    command = str(text or "").strip()
    if not command.upper().startswith("#BBS"):
        return None

    callsign = str(bbs_callsign or "").strip().upper()
    sender = str(source_id or "").strip()
    if not callsign or not sender:
        return ()

    try:
        rx_channel = int(channel) if channel is not None else int(dm_channel)
    except (TypeError, ValueError):
        rx_channel = int(dm_channel)

    parts = command.split(maxsplit=2)
    text_for_bbs = command
    bbs_channel = rx_channel

    if not is_direct:
        if rx_channel not in allowed_channels:
            return ()
        if len(parts) < 2:
            if not (dm_only and dm_init_hint):
                return ()
            hint = (
                "BBS: sintaxis obligatoria en canal (multi-BBS).\n"
                f"Usa: #BBS {callsign} <COMANDO>\n"
                "Responderé por DM.\nEn DM puedes iniciar con: #BBS"
            )
            return (BbsReply(hint, True, int(dm_channel)),)

        target = str(parts[1] or "").strip().upper()
        if target != callsign:
            if dm_only and dm_init_hint and not _looks_like_callsign(target):
                hint = (
                    "BBS: sintaxis obligatoria en canal (multi-BBS).\n"
                    f"Usa: #BBS {callsign} <COMANDO>\n"
                    "Responderé por DM.\nEn DM puedes iniciar con: #BBS"
                )
                return (BbsReply(hint, True, int(dm_channel)),)
            return ()

        if dm_only:
            text_for_bbs = "#BBS" if len(parts) == 2 else "#BBS " + str(parts[2] or "").strip()
            bbs_channel = int(dm_channel)
    elif len(parts) >= 2 and _looks_like_callsign(parts[1]):
        target = str(parts[1] or "").strip().upper()
        if target != callsign:
            return ()
        text_for_bbs = "#BBS" if len(parts) == 2 else "#BBS " + str(parts[2] or "").strip()
        bbs_channel = int(dm_channel)
    else:
        bbs_channel = int(dm_channel)

    chunks = _clean_chunks(
        engine.handle_text(from_id=sender, ch=int(bbs_channel), text=text_for_bbs)
    )
    reply_direct = bool(is_direct or dm_only)
    reply_channel = int(dm_channel) if reply_direct else rx_channel
    return tuple(BbsReply(chunk, reply_direct, reply_channel) for chunk in chunks)


__all__ = ["BbsReply", "handle_bbs_transport_command"]
