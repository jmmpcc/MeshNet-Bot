#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Launcher fino que normaliza encabezados RX y delega en Channel Gateway.

No sustituye ninguna función del broker ni del bot. Solo intercepta el texto que
ExtBot va a enviar a Telegram y aplica una transformación idempotente cuando el
encabezado coincide exactamente con un RX MeshCore conocido. La misma envoltura
puede añadir el scope a una confirmación TX cuando ese valor es conocido de forma
explícita por el comando que acaba de enviarla.
"""
from __future__ import annotations

import os
from typing import Any

from telegram.ext import ExtBot

import Telegram_Bot_ChannelGateway as channel_gateway_launcher
from meshcore_channel_scope import annotate_meshcore_scope
from meshcore_rx_header import normalize_meshcore_rx_header


def _install_meshcore_rx_header_normalizer() -> None:
    """Envuelve ``ExtBot.send_message`` sin alterar el resto del bot.

    El wrapper mantiene primero la normalización RX MeshCore existente. Después
    añade ``Scope TX`` solo a la confirmación de un ``/enviar_mc --scope`` en el
    que conocemos el valor solicitado. No se añade scope a RX porque la versión
    actual de ``meshcore_py`` no lo expone en ``CHANNEL_MSG_RECV`` y no debemos
    presentar un valor inferido como si fuera recibido por radio.

    Todos los demás argumentos y llamadas se delegan exactamente al método
    original.
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
            text = normalize_meshcore_rx_header(kwargs["text"], profile)
            kwargs["text"] = annotate_meshcore_scope(text)
        elif len(args) >= 2:
            mutable_args = list(args)
            text = normalize_meshcore_rx_header(mutable_args[1], profile)
            mutable_args[1] = annotate_meshcore_scope(text)
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
