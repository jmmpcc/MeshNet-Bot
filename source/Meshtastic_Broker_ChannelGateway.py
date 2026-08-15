#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Launcher transparente: instala CHANNEL_GATEWAY y ejecuta el broker original."""
from __future__ import annotations

import os
import runpy
import sys

from channel_gateway import start_channel_gateway_runtime


def main() -> None:
    """
    Arranca el runtime del gateway dentro de este proceso y delega en el broker
    original sin modificar sus argumentos ni su código.
    """
    if len(sys.argv) < 2:
        raise SystemExit("Uso: Meshtastic_Broker_ChannelGateway.py <broker.py> [args...]")

    broker_script = sys.argv[1]
    broker_args = sys.argv[2:]
    if not os.path.isfile(broker_script):
        raise SystemExit(f"Broker no encontrado: {broker_script}")

    start_channel_gateway_runtime()
    sys.argv = [broker_script, *broker_args]
    runpy.run_path(broker_script, run_name="__main__")


if __name__ == "__main__":
    main()
