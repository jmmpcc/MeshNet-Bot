#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Launcher fino que normaliza encabezados RX y delega en Channel Gateway.

No sustituye ninguna función del broker ni del bot. Solo intercepta el texto que
ExtBot va a enviar a Telegram y aplica una transformación idempotente cuando el
encabezado coincide exactamente con un RX MeshCore conocido.
"""
from __future__ import annotations

import os
from typing import Any

from telegram.ext import ExtBot

import Telegram_Bot_ChannelGateway as channel_gateway_launcher
from meshcore_rx_header import normalize_meshcore_rx_header


def _install_meshcore_rx_header_normalizer() -> None:
    """Envuelve ``ExtBot.send_message`` sin alterar el resto del bot.

    El wrapper solo modifica el argumento ``text`` cuando el normalizador reconoce
    el encabezado RX MeshCore. Todos los demás argumentos y llamadas se delegan
    exactamente al método original.
    """
    current_send_message = ExtBot.send_message
    if getattr(current_send_message, "_meshnet_rx_header_normalizer", False):
        return

    original_send_message = current_send_message

    async def send_message_with_rx_header_normalizer(
        self: ExtBot,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        profile = os.getenv("RADIO_PROFILE", "")

        if "text" in kwargs:
            kwargs["text"] = normalize_meshcore_rx_header(kwargs["text"], profile)
        elif len(args) >= 2:
            mutable_args = list(args)
            mutable_args[1] = normalize_meshcore_rx_header(mutable_args[1], profile)
            args = tuple(mutable_args)

        return await original_send_message(self, *args, **kwargs)

    send_message_with_rx_header_normalizer._meshnet_rx_header_normalizer = True
    ExtBot.send_message = send_message_with_rx_header_normalizer


def main() -> None:
    """Instala la normalización visual y conserva el launcher existente."""
    _install_meshcore_rx_header_normalizer()
    channel_gateway_launcher.main()


if __name__ == "__main__":
    main()
