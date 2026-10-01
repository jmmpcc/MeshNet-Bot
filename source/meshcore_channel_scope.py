#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Aplicación segura de flood scope por canal MeshCore.

Este módulo mantiene la asociación entre ``channel_idx`` y flood scope fuera del
broker principal para no alterar su cola, fragmentación, reintentos ni rutas RX.

Configuración:
    MESHCORE_CHANNEL_SCOPE_MAP="5:#zaragoza,2:#otra-region"

Las claves son índices nativos de canal MeshCore. Los valores se pasan sin
transformación a ``meshcore_py.commands.set_flood_scope``.
"""
from __future__ import annotations

import os
import re
from typing import Any, Mapping


_ENV_NAME = "MESHCORE_CHANNEL_SCOPE_MAP"
_PATCH_MARKER = "_meshnet_channel_scope_runtime"
_MC_CHANNEL_RE = re.compile(r"\bmc:(?P<idx>\d+)\b")
_TX_CHANNEL_RE = re.compile(r"channel_idx\):\s*<b>(?P<idx>\d+)</b>", re.IGNORECASE)


def parse_meshcore_channel_scope_map(raw: object) -> dict[int, str]:
    """Convierte la variable de entorno de scopes a ``{channel_idx: scope}``.

    Uso:
        scopes = parse_meshcore_channel_scope_map("5:#zaragoza,2:#huesca")

    Parámetros:
        raw:
            Texto CSV. Cada entrada admite ``<idx>:<scope>`` o
            ``<idx>=<scope>``. Las entradas inválidas se ignoran para conservar
            un arranque tolerante a configuraciones antiguas.

    Funcionalidad:
        - Mantiene exactamente el scope indicado; no añade ni elimina ``#``.
        - Acepta ``0`` para volver al scope por defecto del nodo y ``*`` para
          forzar unscoped, según la semántica de MeshCore.
        - La última entrada válida de un mismo canal prevalece.
    """
    text = str(raw or "").strip().strip('"').strip("'")
    if not text:
        return {}

    out: dict[int, str] = {}
    for item in text.split(","):
        token = (item or "").strip()
        if not token:
            continue

        if "=" in token:
            left, right = token.split("=", 1)
        elif ":" in token:
            left, right = token.split(":", 1)
        else:
            continue

        try:
            channel_idx = int(left.strip())
        except (TypeError, ValueError):
            continue
        scope = right.strip()
        if channel_idx < 0 or not scope:
            continue
        out[channel_idx] = scope

    return out


def configured_scope_for_channel(channel_idx: object, env: Mapping[str, str] | None = None) -> str | None:
    """Devuelve el scope configurado para un canal MeshCore, si existe.

    Si el canal no está en el mapa devuelve ``None``. Esto permite diferenciar
    entre "sin configuración específica en MeshNet" y un valor explícito como
    ``0`` o ``*``.
    """
    try:
        idx = int(channel_idx)
    except (TypeError, ValueError):
        return None
    source = os.environ if env is None else env
    return parse_meshcore_channel_scope_map(source.get(_ENV_NAME, "")).get(idx)


def _event_is_error(result: Any) -> bool:
    """Detecta de forma compatible una respuesta ERROR de ``meshcore_py``."""
    event_type = getattr(result, "type", None)
    if event_type is None:
        return False
    name = str(getattr(event_type, "name", "") or "").upper()
    value = str(getattr(event_type, "value", "") or "").lower()
    text = str(event_type).lower()
    return name == "ERROR" or value == "command_error" or text.endswith(".error") or "command_error" in text


def _install_on_instance(mc: Any, scope_map: Mapping[int, str]) -> Any:
    """Envuelve ``commands.send_chan_msg`` de una instancia MeshCore.

    Antes de cada TX de canal establece el flood scope correspondiente. Cuando
    no existe un scope específico para el canal, envía ``0`` para limpiar
    cualquier override previo y volver al default scope del nodo. Si la orden de
    scope falla, el mensaje NO se transmite: se evita una fuga accidental como
    paquete unscoped.
    """
    commands = getattr(mc, "commands", None)
    if commands is None:
        return mc
    current = getattr(commands, "send_chan_msg", None)
    set_scope = getattr(commands, "set_flood_scope", None)
    if not callable(current) or not callable(set_scope):
        return mc
    if getattr(current, _PATCH_MARKER, False):
        return mc

    original_send_chan_msg = current

    async def send_chan_msg_scoped(chan: int, msg: str, timestamp: int | None = None):
        try:
            channel_idx = int(chan)
        except (TypeError, ValueError):
            return await original_send_chan_msg(chan, msg, timestamp)

        selected_scope = scope_map.get(channel_idx)
        scope_command = selected_scope if selected_scope is not None else "0"
        scope_result = await set_scope(scope_command)
        if _event_is_error(scope_result):
            return scope_result

        return await original_send_chan_msg(channel_idx, msg, timestamp)

    setattr(send_chan_msg_scoped, _PATCH_MARKER, True)
    commands.send_chan_msg = send_chan_msg_scoped
    return mc


def install_meshcore_channel_scope_runtime(
    meshcore_cls: Any,
    *,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Instala el scope por canal sobre los constructores públicos de MeshCore.

    Uso:
        from meshcore import MeshCore
        install_meshcore_channel_scope_runtime(MeshCore)

    Parámetros:
        meshcore_cls:
            Clase ``meshcore.MeshCore`` ya importada.
        env:
            Entorno opcional para pruebas. En producción usa ``os.environ``.

    Funcionalidad:
        - No hace nada si ``MESHCORE_CHANNEL_SCOPE_MAP`` está vacío.
        - Intercepta únicamente las conexiones creadas por ``create_serial``,
          ``create_tcp`` o ``create_ble``.
        - No modifica DM, RX, reintentos, fragmentación ni otras órdenes.
        - Es idempotente: una segunda instalación no duplica wrappers.
    """
    source = os.environ if env is None else env
    scope_map = parse_meshcore_channel_scope_map(source.get(_ENV_NAME, ""))
    if not scope_map:
        return False
    if getattr(meshcore_cls, _PATCH_MARKER, False):
        return True

    patched_any = False
    for creator_name in ("create_serial", "create_tcp", "create_ble"):
        original_creator = getattr(meshcore_cls, creator_name, None)
        if not callable(original_creator):
            continue

        async def creator_with_scope(*args: Any, __creator=original_creator, **kwargs: Any):
            mc = await __creator(*args, **kwargs)
            return _install_on_instance(mc, scope_map)

        setattr(creator_with_scope, _PATCH_MARKER, True)
        setattr(meshcore_cls, creator_name, staticmethod(creator_with_scope))
        patched_any = True

    if patched_any:
        setattr(meshcore_cls, _PATCH_MARKER, True)
    return patched_any


def annotate_meshcore_scope(text: object, *, env: Mapping[str, str] | None = None) -> object:
    """Añade información visual de scope a mensajes Telegram MeshCore conocidos.

    RX:
        Inserta ``scope canal configurado: ...`` en el encabezado. No afirma que
        sea el scope real del paquete recibido, porque ``meshcore_py`` todavía
        no lo expone en ``CHANNEL_MSG_RECV``.

    TX de ``/enviar_mc``:
        Inserta ``Scope TX: ...`` en la respuesta porque el mismo mapa es el que
        aplica el runtime justo antes de ``send_chan_msg``.

    Cualquier otro texto se devuelve sin cambios. La función es idempotente.
    """
    if not isinstance(text, str):
        return text
    source = os.environ if env is None else env
    scope_map = parse_meshcore_channel_scope_map(source.get(_ENV_NAME, ""))
    if not scope_map:
        return text

    if text.startswith("📩 ") and "MeshCore canal mc:" in text:
        first_line, sep, rest = text.partition("\n")
        if "scope canal configurado:" in first_line:
            return text
        match = _MC_CHANNEL_RE.search(first_line)
        if match and first_line.endswith("):"):
            scope = scope_map.get(int(match.group("idx")))
            if scope is not None:
                first_line = f"{first_line[:-2]} · scope canal configurado: {scope}):"
                return first_line if not sep else f"{first_line}{sep}{rest}"

    if text.startswith("Envío MeshCore\n") and "Scope TX:" not in text:
        match = _TX_CHANNEL_RE.search(text)
        if match:
            scope = scope_map.get(int(match.group("idx")))
            if scope is not None:
                lines = text.splitlines()
                insert_at = next(
                    (i for i, line in enumerate(lines) if line.startswith("Resultado MeshCore:")),
                    len(lines),
                )
                safe_scope = (
                    scope.replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                )
                lines.insert(insert_at, f"Scope TX: <b>{safe_scope}</b>")
                return "\n".join(lines)

    return text


__all__ = [
    "annotate_meshcore_scope",
    "configured_scope_for_channel",
    "install_meshcore_channel_scope_runtime",
    "parse_meshcore_channel_scope_map",
]
