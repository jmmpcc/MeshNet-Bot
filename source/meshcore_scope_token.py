#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Parser de sintaxis de scope por transmisión para /enviar_mc.

Este módulo añade una sintaxis robusta sin guiones para Telegram:

    scope#utebo

La forma histórica ``--scope`` se mantiene por compatibilidad. El parser no
modifica el motor RF ni la semántica interna del scope; únicamente transforma
los argumentos del comando antes de delegarlos al handler existente.
"""
from __future__ import annotations

from typing import Iterable

from meshcore_channel_scope import extract_scope_modifier as _legacy_extract_scope_modifier
from meshcore_channel_scope import normalize_scope


def extract_scope_modifier(args: Iterable[object]) -> tuple[list[str], str | None, str | None]:
    """Extrae el scope opcional de ``/enviar_mc`` sin alterar el resto del comando.

    Sintaxis recomendada:
        ``scope#utebo``
        ``scope#zaragoza``

    Valores especiales:
        ``scope#0``  -> default scope del nodo
        ``scope#*``  -> TX explícitamente unscoped

    Compatibilidad mantenida:
        ``--scope #utebo``
        ``--scope=#utebo``

    Parámetros:
        args: secuencia original de ``context.args`` de Telegram.

    Devuelve:
        ``(args_limpios, scope_normalizado, error)``.

    Seguridad:
        - Solo admite un modificador de scope por transmisión.
        - ``scope#`` sin valor devuelve error y no transmite.
        - Si no existe ``scope#...`` delega al parser histórico, por lo que no
          cambia el comportamiento ya validado de ``--scope``.
    """
    tokens = [str(value) for value in args]
    cleaned: list[str] = []
    found: str | None = None

    for token in tokens:
        low = token.casefold()
        if not low.startswith("scope#"):
            cleaned.append(token)
            continue

        raw_value = token[len("scope#"):].strip()
        if not raw_value:
            return tokens, None, "Falta el valor después de scope#."
        if found is not None:
            return tokens, None, "Solo puede indicarse un scope por envío."

        normalized = normalize_scope(raw_value)
        if normalized is None:
            return tokens, None, "El valor de scope# está vacío."
        found = normalized

    if found is not None:
        # Evita mezclar sintaxis nueva y antigua en el mismo TX.
        legacy_clean, legacy_scope, legacy_error = _legacy_extract_scope_modifier(cleaned)
        if legacy_error:
            return tokens, None, legacy_error
        if legacy_scope is not None:
            return tokens, None, "Solo puede indicarse un scope por envío."
        return legacy_clean, found, None

    return _legacy_extract_scope_modifier(tokens)


__all__ = ["extract_scope_modifier"]
