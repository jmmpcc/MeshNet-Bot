#!/usr/bin/env python3
"""Launcher compatible de Emergencias con scope MeshCore opcional por TX.

Reutiliza íntegramente el CLI y el motor de ``emergencias_guardia``. Solo añade
el campo ``scope`` a los ``MESHCORE_SEND`` de canal cuando existe la variable
``EMERGENCIAS_MESHCORE_SCOPE``.

Uso::

    EMERGENCIAS_MESHCORE_SCOPE=#Utebo \
        python3 emergencias_guardia_scoped.py check --notify-changes

DGT, FIRMS y cualquier otra fuente siguen pasando por los filtros, rutas,
fragmentación, deduplicación, retries, auditoría y salidas APRS existentes. Los
TX Meshtastic y los mensajes que no sean de canal MeshCore no se modifican.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any


REPO_DIR = Path(__file__).resolve().parents[2]
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

from shared.meshcore_optional_scope import add_optional_channel_scope
from emergencias import notifier


_ORIGINAL_BROKER_REQUEST = notifier.broker_request


def _scoped_broker_request(
    config: dict[str, Any],
    command: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Añade el scope configurado solo al TX MeshCore de canal.

    ``notifier.broker_request`` conserva su firma original. El diccionario de
    configuración, el socket, timeout y tratamiento de errores siguen siendo los
    del módulo histórico.
    """
    scoped_params = add_optional_channel_scope(
        command,
        params,
        os.getenv("EMERGENCIAS_MESHCORE_SCOPE", ""),
    )
    return _ORIGINAL_BROKER_REQUEST(config, command, scoped_params)


def install_scope_wrapper() -> None:
    """Instala una sola vez el wrapper sobre el notifier existente."""
    if notifier.broker_request is not _scoped_broker_request:
        notifier.broker_request = _scoped_broker_request


def main() -> int:
    """Ejecuta el CLI original después de activar el scope opcional."""
    install_scope_wrapper()
    from emergencias.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
