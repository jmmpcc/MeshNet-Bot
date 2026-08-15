#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Launcher transparente que añade /channel_gateway al bot existente."""
from __future__ import annotations

import atexit

import Telegram_Bot_Broker as bot
from channel_gateway_bot import channel_gateway_cmd
from telegram.ext import CommandHandler
from tcpinterface_persistent import TCPInterfacePool


def _install_command_without_touching_original() -> None:
    """Envuelve build_application() y añade únicamente los handlers del gateway."""
    original_build_application = bot.build_application

    def build_application_with_channel_gateway():
        app = original_build_application()
        app.add_handler(CommandHandler("channel_gateway", channel_gateway_cmd))
        app.add_handler(CommandHandler("pasarela_canales", channel_gateway_cmd))
        return app

    bot.build_application = build_application_with_channel_gateway


def main() -> None:
    """Instala la extensión y delega el arranque completo al bot original."""
    _install_command_without_touching_original()
    atexit.register(TCPInterfacePool.shutdown)
    bot.main()


if __name__ == "__main__":
    main()
