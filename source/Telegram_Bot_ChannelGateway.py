#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Launcher transparente que añade extensiones al bot existente sin modificarlo."""
from __future__ import annotations

import atexit
from typing import Any, Awaitable, Callable

import Telegram_Bot_Broker as bot
import beacon_bot
from beacon_bot import (
    baliza_cmd,
    baliza_mc_cmd,
    balizas_cmd,
    balizas_mc_cmd,
    contextual_help,
    parar_baliza_cmd,
    parar_baliza_mc_cmd,
)
from channel_gateway_bot import channel_gateway_cmd
from auto_reply_bot import auto_reply_cmd, contextual_help as auto_reply_contextual_help
from meshcore_scope_bot import (
    contextual_help as meshcore_scope_contextual_help,
    install_enviar_mc_scope_support,
)
from telegram import BotCommand, BotCommandScopeChat, BotCommandScopeDefault
from telegram.ext import CommandHandler
from tcpinterface_persistent import TCPInterfacePool


async def _augment_bot_commands_for_scope(app: Any, scope: Any) -> None:
    """Añade las extensiones visibles al menú oficial de Telegram para un scope.

    Funcionalidad:
        - Lee primero los comandos ya publicados por el bot principal.
        - Mantiene su orden y descripciones salvo cuando una descripción necesita
          indicar funcionalidad añadida sin perder la histórica.
        - Añade ``/autorespuesta`` sin sustituir comandos previos.
        - Añade únicamente las balizas cuyo transporte está habilitado por
          ``RADIO_PROFILE``.
        - Amplía la descripción de ``/enviar_mc`` para anunciar ``--scope``.
        - Evita comandos duplicados.

    Parámetros:
        app: ``telegram.ext.Application`` ya inicializada.
        scope: ``BotCommandScopeDefault`` o ``BotCommandScopeChat``.
    """
    commands = list(await app.bot.get_my_commands(scope=scope) or [])
    available = beacon_bot._available_transports()

    def upsert(command: str, description: str) -> None:
        for index, item in enumerate(commands):
            if item.command == command:
                commands[index] = BotCommand(command, description)
                return
        commands.append(BotCommand(command, description))

    upsert("autorespuesta", "Administrar autorespuesta por canal")

    if "meshtastic" in available:
        upsert("baliza", "Baliza Meshtastic periódica por nombre")
        upsert("balizas", "Listar balizas Meshtastic activas")
        upsert("parar_baliza", "Detener baliza Meshtastic por nombre o meteorológica por ID")

    if "meshcore" in available:
        upsert("enviar_mc", "Enviar MeshCore/APRS; --scope opcional por TX")
        upsert("baliza_mc", "Baliza MeshCore periódica por nombre")
        upsert("balizas_mc", "Listar balizas MeshCore activas")
        upsert("parar_baliza_mc", "Detener baliza MeshCore por nombre")

    await app.bot.set_my_commands(commands, scope=scope)


def _install_visual_extensions() -> None:
    """Integra extensiones en menú ``/`` y ``/ayuda`` sin reescribir el bot principal."""
    original_set_bot_menu = bot.set_bot_menu
    original_ayuda = bot.ayuda

    async def set_bot_menu_with_extensions(app: Any) -> None:
        """Publica primero el menú histórico y después añade extensiones visibles."""
        await original_set_bot_menu(app)
        await _augment_bot_commands_for_scope(app, BotCommandScopeDefault())
        for admin_id in bot.ADMIN_IDS:
            try:
                await _augment_bot_commands_for_scope(
                    app,
                    BotCommandScopeChat(chat_id=admin_id),
                )
            except Exception as exc:
                bot.log(f"❗ set_my_commands extensiones admin {admin_id}: {exc}")

    async def ayuda_with_extensions(update: Any, context: Any) -> None:
        """Conserva ``/ayuda`` y añade bloques contextuales externos."""
        await original_ayuda(update, context)
        message = getattr(update, "effective_message", None)
        if message is not None:
            await message.reply_text(meshcore_scope_contextual_help())
            await message.reply_text(contextual_help())
            await message.reply_text(auto_reply_contextual_help())

    bot.set_bot_menu = set_bot_menu_with_extensions
    bot.ayuda = ayuda_with_extensions


def _replace_parar_baliza_handler(app: Any) -> None:
    """Unifica ``/parar_baliza`` sin romper la baliza meteorológica histórica."""
    original_callback: Callable[[Any, Any], Awaitable[Any]] | None = None
    original_group = 0
    original_handler: CommandHandler | None = None

    for group, handlers in list(app.handlers.items()):
        for handler in list(handlers):
            if not isinstance(handler, CommandHandler):
                continue
            commands = set(getattr(handler, "commands", ()) or ())
            if "parar_baliza" not in commands:
                continue
            original_callback = handler.callback
            original_group = group
            original_handler = handler
            break
        if original_handler is not None:
            break

    if original_handler is not None:
        app.remove_handler(original_handler, group=original_group)

    async def parar_baliza_unificada(update: Any, context: Any) -> None:
        """Despacha por nombre a Meshtastic o por ID a la baliza meteorológica."""
        args = [
            str(value).strip()
            for value in (getattr(context, "args", None) or [])
            if str(value).strip()
        ]
        if len(args) == 1:
            key = ("meshtastic", args[0].casefold())
            spec = beacon_bot._ACTIVE_BEACONS.get(key)
            if spec is not None and spec.task is not None and not spec.task.done():
                await parar_baliza_cmd(update, context)
                return

        if original_callback is not None:
            await original_callback(update, context)
            return

        await parar_baliza_cmd(update, context)

    app.add_handler(
        CommandHandler("parar_baliza", parar_baliza_unificada),
        group=original_group,
    )


def _install_command_without_touching_original() -> None:
    """Envuelve ``build_application()`` y añade únicamente handlers externos."""
    original_build_application = bot.build_application

    def build_application_with_channel_gateway():
        """Construye la app original y registra las extensiones sin duplicarlas."""
        app = original_build_application()

        # Se envuelve el handler histórico, conservando su parser, APRS y UI.
        # Únicamente se extrae --scope y se añade params.scope a MESHCORE_SEND.
        install_enviar_mc_scope_support(app, bot)

        app.add_handler(CommandHandler("channel_gateway", channel_gateway_cmd))
        app.add_handler(CommandHandler("pasarela_canales", channel_gateway_cmd))
        app.add_handler(CommandHandler("autorespuesta", auto_reply_cmd))
        app.add_handler(CommandHandler("baliza", baliza_cmd))
        app.add_handler(CommandHandler("baliza_mc", baliza_mc_cmd))
        app.add_handler(CommandHandler("balizas", balizas_cmd))
        app.add_handler(CommandHandler("balizas_mc", balizas_mc_cmd))
        app.add_handler(CommandHandler("parar_baliza_mc", parar_baliza_mc_cmd))

        _replace_parar_baliza_handler(app)
        return app

    bot.build_application = build_application_with_channel_gateway


def main() -> None:
    """Instala extensiones visuales/funcionales y delega al bot original."""
    _install_visual_extensions()
    _install_command_without_touching_original()
    atexit.register(TCPInterfacePool.shutdown)
    bot.main()


if __name__ == "__main__":
    main()
