#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MeshNet-Bot v7.0.55 — launcher del Channel Gateway integrado en broker."""
from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path


def _load_environment_before_gateway() -> None:
    """
    Carga el .env antes de inicializar Channel Gateway cuando el proceso no
    recibe ya las variables mediante Docker Compose/systemd.

    Uso:
        _load_environment_before_gateway()

    Parámetros:
        Ninguno.

    Funcionalidad:
        - Respeta variables ya presentes con ``override=False``.
        - Busca primero ENV_FILE/DOTENV_PATH y después /app/.env y ./.env.
        - Si python-dotenv no está disponible, no altera el arranque: el broker
          original conserva su propio mecanismo de carga y funcionamiento.
    """
    try:
        from dotenv import load_dotenv  # type: ignore
    except Exception:
        return

    candidates: list[Path] = []
    explicit = (os.getenv("ENV_FILE") or os.getenv("DOTENV_PATH") or "").strip()
    if explicit:
        candidates.append(Path(explicit).expanduser())
    candidates.extend([Path("/app/.env"), Path.cwd() / ".env"])

    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            if path.exists():
                load_dotenv(dotenv_path=str(path), override=False)
        except Exception:
            # El launcher nunca debe impedir que el broker original arranque por
            # un fallo opcional de lectura del .env.
            continue


def _install_meshcore_scope_runtime_if_configured() -> None:
    """Instala el flood scope por canal sin modificar el broker principal.

    Uso:
        _install_meshcore_scope_runtime_if_configured()

    Parámetros:
        Ninguno. Lee ``MESHCORE_CHANNEL_SCOPE_MAP`` desde el entorno ya cargado.

    Funcionalidad:
        - Si no hay mapa configurado, no cambia absolutamente nada.
        - Si MeshCore no está disponible, deja que el broker gestione su propio
          diagnóstico de dependencia como hasta ahora.
        - Cuando existe mapa, cada ``send_chan_msg`` aplica primero el scope del
          ``channel_idx``; canales no mapeados vuelven al default scope del nodo.
        - Si ``set_flood_scope`` falla, ese TX no se emite sin scope por error.
    """
    if not (os.getenv("MESHCORE_CHANNEL_SCOPE_MAP") or "").strip():
        return

    try:
        from meshcore import MeshCore  # type: ignore
        from meshcore_channel_scope import install_meshcore_channel_scope_runtime

        installed = install_meshcore_channel_scope_runtime(MeshCore)
        if installed:
            print(
                "[meshcore-scope] runtime por channel_idx habilitado mediante "
                "MESHCORE_CHANNEL_SCOPE_MAP",
                flush=True,
            )
    except Exception as exc:
        # No se aborta el broker por una capa opcional. El fallo queda visible
        # para diagnóstico y el runtime principal mantiene su comportamiento.
        print(
            f"[meshcore-scope] WARN: no se pudo instalar runtime: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )


def main() -> None:
    """
    Arranca el runtime del gateway dentro de este proceso y delega en el broker
    original sin modificar sus argumentos ni su código.

    Uso:
        python Meshtastic_Broker_ChannelGateway.py Meshtastic_Broker.py [args]

    Funcionalidad:
        1. Resuelve el script original recibido como primer argumento.
        2. Precarga variables de entorno de forma compatible con el broker.
        3. Instala, solo si está configurado, el scope MeshCore por canal.
        4. Instala Channel Gateway en el mismo proceso.
        5. Ejecuta el broker original como ``__main__`` con sus argumentos.
    """
    if len(sys.argv) < 2:
        raise SystemExit("Uso: Meshtastic_Broker_ChannelGateway.py <broker.py> [args...]")

    broker_script = sys.argv[1]
    broker_args = sys.argv[2:]
    if not os.path.isfile(broker_script):
        raise SystemExit(f"Broker no encontrado: {broker_script}")

    _load_environment_before_gateway()
    _install_meshcore_scope_runtime_if_configured()

    # Import diferido: garantiza que channel_gateway lea las variables una vez
    # precargado el entorno en ejecuciones manuales fuera de Docker Compose.
    from channel_gateway import start_channel_gateway_runtime

    start_channel_gateway_runtime()
    sys.argv = [broker_script, *broker_args]
    runpy.run_path(broker_script, run_name="__main__")


if __name__ == "__main__":
    main()
