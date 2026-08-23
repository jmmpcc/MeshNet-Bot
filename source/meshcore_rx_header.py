#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Normalización aislada del encabezado visible de mensajes RX MeshCore.

El broker conserva metadatos de canal local para compatibilidad con el perfil
histórico Meshtastic A + MeshCore B. Cuando MeshCore es el nodo A, esos datos
son internos y no deben duplicar ni ensuciar la referencia visible del canal
MeshCore en Telegram.
"""
from __future__ import annotations

import re

from radio_profile import (
    PROFILE_MESHCORE_A_MESHTASTIC_B,
    PROFILE_MESHCORE_ONLY,
    normalize_radio_profile,
)


_PRIMARY_MESHCORE_RE = re.compile(
    r"\(MeshCore canal (?P<ref>mc:\d+(?: \([^)]+\))?)$"
)
_LOCAL_MARKER = " · canal local "


def normalize_meshcore_rx_header(text: object, radio_profile: object) -> object:
    """Corrige únicamente el encabezado RX MeshCore que llega a Telegram.

    Reglas:
        - ``meshcore_only`` y ``meshcore_a_meshtastic_embedded_b`` muestran solo
          la referencia MeshCore principal, porque MeshCore es el nodo A.
        - Perfiles históricos/legacy conservan ``canal local`` para no alterar
          su información útil, pero eliminan una referencia ``mc:X`` repetida.
        - DM, mensajes no MeshCore y cualquier texto que no coincida exactamente
          con la forma conocida se devuelven sin cambios.

    La función es deliberadamente pura e idempotente: no toca el paquete, el
    broker, el mapeo de canales ni el contenido del mensaje recibido.
    """
    if not isinstance(text, str) or not text.startswith("📩 "):
        return text

    first_line, separator, remainder = text.partition("\n")
    if "(MeshCore canal mc:" not in first_line or _LOCAL_MARKER not in first_line:
        return text
    if not first_line.endswith("):"):
        return text

    prefix, local_with_suffix = first_line.split(_LOCAL_MARKER, 1)
    match = _PRIMARY_MESHCORE_RE.search(prefix)
    if match is None:
        return text

    # Quita únicamente el cierre del encabezado. El contenido del canal local
    # puede contener paréntesis, asteriscos u otros caracteres y se conserva.
    local_part = local_with_suffix[:-2]
    primary_ref = match.group("ref")
    duplicate_suffix = f" · {primary_ref}"
    if local_part.endswith(duplicate_suffix):
        local_part = local_part[: -len(duplicate_suffix)]

    profile = normalize_radio_profile(radio_profile)
    if profile in {PROFILE_MESHCORE_ONLY, PROFILE_MESHCORE_A_MESHTASTIC_B}:
        normalized_first_line = f"{prefix}):"
    else:
        normalized_first_line = f"{prefix}{_LOCAL_MARKER}{local_part}):"

    if not separator:
        return normalized_first_line
    return f"{normalized_first_line}{separator}{remainder}"


__all__ = ["normalize_meshcore_rx_header"]
