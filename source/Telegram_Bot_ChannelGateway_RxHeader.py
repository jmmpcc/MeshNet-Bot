#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Launcher fino que normaliza encabezados RX y delega en Channel Gateway.

No sustituye ninguna función del broker ni del bot. Solo intercepta el texto que
ExtBot va a enviar a Telegram y aplica transformaciones visuales idempotentes
sobre mensajes MeshCore conocidos.
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

    Uso:
        _install_meshcore_rx_header_normalizer()

    Parámetros:
        Ninguno.

    Funcionalidad:
        - Normaliza primero el encabezado RX MeshCore ya existente.
        - Añade después información de scope únicamente cuando el canal figura
          en ``MESHCORE_CHANNEL_SCOPE_MAP``.
        - En RX identifica el dato como configuración del canal, no como scope
          real recibido, porque ``meshcore_py`` aún no lo entrega en el evento.
        - En la respuesta TX de ``/enviar_mc`` muestra el mismo scope que aplica
          el runtime del broker antes de ``send_chan_msg``.
        - Todos los demás argumentos y llamadas se delegan sin cambios.
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
