#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Launcher transparente que añade extensiones al bot existente sin modificarlo."""
from __future__ import annotations

import atexit

import Telegram_Bot_Broker as bot
from beacon_bot import (
    baliza_cmd,
    baliza_mc_cmd,
    balizas_cmd,
    balizas_mc_cmd,
    parar_baliza_cmd,
    parar_baliza_mc_cmd,
)
from channel_gateway_bot import channel_gateway_cmd
from telegram.ext import CommandHandler
from tcpinterface_persistent import TCPInterfacePool


def _install_command_without_touching_original() -> None:
    """Envuelve ``build_application()`` y añade únicamente handlers externos.

    Se llama una sola vez desde :func:`main`. Mantiene intacta la construcción
    histórica de ``Telegram_Bot_Broker.py`` y registra después las extensiones
    Channel Gateway y balizas periódicas. Ninguna función de envío existente se
    sustituye: las balizas delegan el TX al control del broker.
    """
    original_build_application = bot.build_application

    def build_application_with_channel_gateway():
        """Construye la app original y registra los comandos de las extensiones."""
        app = original_build_application()

        # Extensión ya existente: pasarela interna entre canales.
        app.add_handler(CommandHandler("channel_gateway", channel_gateway_cmd))
        app.add_handler(CommandHandler("pasarela_canales", channel_gateway_cmd))

        # Nueva extensión: balizas periódicas independientes por transporte.
        app.add_handler(CommandHandler("baliza", baliza_cmd))
        app.add_handler(CommandHandler("baliza_mc", baliza_mc_cmd))
        app.add_handler(CommandHandler("balizas", balizas_cmd))
        app.add_handler(CommandHandler("balizas_mc", balizas_mc_cmd))
        app.add_handler(CommandHandler("parar_baliza", parar_baliza_cmd))
        app.add_handler(CommandHandler("parar_baliza_mc", parar_baliza_mc_cmd))
        return app

    bot.build_application = build_application_with_channel_gateway


def main() -> None:
    """Instala las extensiones y delega el arranque completo al bot original."""
    _install_command_without_touching_original()
    atexit.register(TCPInterfacePool.shutdown)
    bot.main()


if __name__ == "__main__":
    main()
