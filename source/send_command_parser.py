#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Normalización ligera de argumentos para los flujos de ``/enviar``."""

from __future__ import annotations

import re
from typing import Iterable

_CHANNEL_TOKEN = re.compile(r"^canal\s+(-?\d+)$", re.IGNORECASE)
_FORCED_CHANNEL_TOKEN = re.compile(r"^forzado\s+canal\s+(-?\d+)$", re.IGNORECASE)


def normalize_send_args(args: Iterable[object] | None) -> list[str]:
    """Expande el destino de ForceReply sin alterar aliases con espacios.

    Telegram entrega ``canal 2`` como dos argumentos al ejecutar directamente
    ``/enviar canal 2 texto``, pero el diálogo guiado lo guarda como una única
    respuesta. Este helper iguala ambas representaciones de manera conservadora.
    """
    tokens = [str(value).strip() for value in (args or ()) if str(value).strip()]
    if not tokens:
        return []
    match = _FORCED_CHANNEL_TOKEN.fullmatch(tokens[0])
    if match:
        return ["forzado", "canal", match.group(1), *tokens[1:]]
    match = _CHANNEL_TOKEN.fullmatch(tokens[0])
    if match:
        return ["canal", match.group(1), *tokens[1:]]
    return tokens
