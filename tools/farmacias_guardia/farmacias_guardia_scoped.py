#!/usr/bin/env python3
"""Launcher compatible de Farmacias con scope MeshCore opcional por TX.

Este launcher reutiliza íntegramente ``farmacias_guardia.py``. No sustituye su
lógica de descarga, filtros, fragmentación, temporizadores, DM, APRS ni auditoría.
Únicamente intercepta las peticiones al broker y añade el campo ``scope`` a los
``MESHCORE_SEND`` de tipo ``chan`` cuando está configurada la variable
``FARMACIAS_MESHCORE_SCOPE``.

Uso::

    FARMACIAS_MESHCORE_SCOPE=#Utebo \
        python3 farmacias_guardia_scoped.py send --force

Si ``FARMACIAS_MESHCORE_SCOPE`` está vacía o no existe, el payload enviado al
broker mantiene exactamente la estructura histórica.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
REPO_DIR = BASE_DIR.parents[1]
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import farmacias_guardia as app
from shared.meshcore_optional_scope import add_optional_channel_scope


_ORIGINAL_BROKER_REQUEST = app.broker_request


def _scoped_broker_request(command: str, params: dict[str, Any]) -> dict[str, Any]:
    """Conserva ``broker_request`` y añade scope solo a TX MeshCore de canal.

    La función se instala como reemplazo local de ``app.broker_request``. Los
    mensajes directos ``kind=contact`` no se modifican, por lo que las respuestas
    de consultas ``farma`` continúan siendo DM exactamente como antes.
    """
    scoped_params = add_optional_channel_scope(
        command,
        params,
        os.getenv("FARMACIAS_MESHCORE_SCOPE", ""),
    )
    return _ORIGINAL_BROKER_REQUEST(command, scoped_params)


def install_scope_wrapper() -> None:
    """Instala una sola vez el wrapper de scope sobre la aplicación existente."""
    if app.broker_request is not _scoped_broker_request:
        app.broker_request = _scoped_broker_request


def main() -> int:
    """Ejecuta el CLI histórico después de instalar el scope opcional."""
    install_scope_wrapper()
    return app.main()


if __name__ == "__main__":
    raise SystemExit(main())
