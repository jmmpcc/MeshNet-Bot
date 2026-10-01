#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Integración Telegram de flood scope por transmisión MeshCore."""
from __future__ import annotations

import json
import socket
from typing import Any, Awaitable, Callable

from telegram.ext import CommandHandler

from meshcore_channel_scope import (
    bot_tx_scope,
    current_bot_tx_scope,
    extract_scope_modifier,
)


_PATCH_MARKER = "_meshnet_enviar_mc_scope"


def _scope_help_text() -> str:
    """Texto de ayuda reutilizable para /ayuda y errores de sintaxis."""
    return (
        "MeshCore - scope por transmisión\n"
        "• Añade --scope <región> a /enviar_mc para limitar únicamente ese TX.\n"
        "• Ej.: /enviar_mc ch5 --scope #zaragoza Hola\n"
        "• Ej.: /enviar_mc ambos ch5 --scope #zaragoza aprs broadcast Aviso\n"
        "• --scope 0 usa el scope por defecto del nodo.\n"
        "• --scope * fuerza un TX sin scope.\n"
        "• Si omites --scope, /enviar_mc conserva su funcionamiento histórico.\n"
        "• El scope es independiente del channel_idx y no modifica la configuración permanente del canal.\n"
        "• En TX el bot muestra Scope TX. En RX no se muestra un scope inventado: meshcore_py no lo expone aún en CHANNEL_MSG_RECV."
    )


def contextual_help() -> str:
    """Devuelve la ayuda contextual que se añade a /ayuda."""
    return _scope_help_text()


def _send_scoped_via_broker(
    bot_module: Any,
    channel_idx: int,
    text: str,
    scope: str,
    timeout: float = 3.0,
) -> dict:
    """Envía MESHCORE_SEND incluyendo ``params.scope``.

    Se usa exclusivamente cuando /enviar_mc contiene ``--scope``. Si no existe
    modificador, el wrapper delega en ``_send_via_broker_meshcore`` original.
    """
    host = str(getattr(bot_module, "BROKER_CTRL_HOST", "127.0.0.1") or "127.0.0.1")
    port = int(getattr(bot_module, "BROKER_CTRL_PORT", 8766) or 8766)
    request = {
        "cmd": "MESHCORE_SEND",
        "params": {
            "kind": "chan",
            "channel_idx": int(channel_idx),
            "text": str(text),
            "scope": str(scope),
        },
    }

    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
            reader = sock.makefile("rb")
            raw = reader.readline()
        if not raw:
            return {"ok": False, "error": "empty response", "scope": scope}
        response = json.loads(raw.decode("utf-8", errors="replace"))
        if not isinstance(response, dict):
            return {"ok": False, "error": "invalid response", "scope": scope}
        response.setdefault("scope", scope)
        return response
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "scope": scope,
        }


def install_enviar_mc_scope_support(app: Any, bot_module: Any) -> bool:
    """Añade ``--scope`` al /enviar_mc existente sin reescribir su handler.

    Funcionamiento:
        1. Localiza el CommandHandler histórico de ``/enviar_mc``.
        2. Conserva su callback, grupo y toda su lógica de transporte/APRS.
        3. Extrae únicamente ``--scope`` de ``context.args``.
        4. Mientras se ejecuta el callback original, la función interna de TX
           MeshCore añade ``params.scope`` al broker mediante un ContextVar.
        5. Restaura siempre ``context.args`` al terminar.

    Si no se indica ``--scope`` se delega exactamente en la función histórica.
    """
    if bool(getattr(app, _PATCH_MARKER, False)):
        return True

    original_handler: CommandHandler | None = None
    original_callback: Callable[[Any, Any], Awaitable[Any]] | None = None
    original_group = 0

    for group, handlers in list(getattr(app, "handlers", {}).items()):
        for handler in list(handlers):
            if not isinstance(handler, CommandHandler):
                continue
            commands = set(getattr(handler, "commands", ()) or ())
            if "enviar_mc" not in commands:
                continue
            original_handler = handler
            original_callback = handler.callback
            original_group = group
            break
        if original_handler is not None:
            break

    if original_handler is None or original_callback is None:
        return False

    historical_send = getattr(bot_module, "_send_via_broker_meshcore", None)
    if not callable(historical_send):
        return False

    app.remove_handler(original_handler, group=original_group)

    async def enviar_mc_with_scope(update: Any, context: Any) -> Any:
        """Despacha al handler original añadiendo un scope opcional por TX."""
        old_args = list(getattr(context, "args", None) or [])
        cleaned_args, scope, error = extract_scope_modifier(old_args)
        if error:
            message = getattr(update, "effective_message", None)
            if message is not None:
                await message.reply_text(f"{error}\n\n{_scope_help_text()}")
            return None

        # Sin modificador se conserva el envío histórico. Cuando además no hay
        # argumentos, se añade después el bloque de ayuda de scope para que
        # /enviar_mc documente la nueva opción sin sustituir su ayuda existente.
        if scope is None:
            result = await original_callback(update, context)
            if not old_args:
                message = getattr(update, "effective_message", None)
                if message is not None:
                    await message.reply_text(_scope_help_text())
            return result

        context.args = cleaned_args
        try:
            with bot_tx_scope(scope):
                return await original_callback(update, context)
        finally:
            context.args = old_args

    # Wrapper permanente y concurrency-safe. ``asyncio.to_thread`` copia el
    # ContextVar del comando actual, por lo que dos usuarios pueden emitir a la
    # vez con scopes distintos sin sobrescribir una función global temporal.
    def send_meshcore_dispatch(
        channel_idx: int, text: str, timeout: float = 3.0
    ) -> dict:
        active_scope = current_bot_tx_scope()
        if active_scope is None:
            return historical_send(channel_idx, text, timeout)
        return _send_scoped_via_broker(
            bot_module,
            int(channel_idx),
            str(text),
            active_scope,
            timeout,
        )

    setattr(send_meshcore_dispatch, _PATCH_MARKER, True)
    setattr(bot_module, "_send_via_broker_meshcore", send_meshcore_dispatch)
    setattr(enviar_mc_with_scope, _PATCH_MARKER, True)
    app.add_handler(CommandHandler("enviar_mc", enviar_mc_with_scope), group=original_group)
    setattr(app, _PATCH_MARKER, True)
    return True


__all__ = ["contextual_help", "install_enviar_mc_scope_support"]
