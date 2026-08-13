#!/usr/bin/env python3
"""Extensión v7.0.54 de MeshNet Mobile API.

Esta capa se monta sobre la API v1 existente sin modificar su implementación A1.
Su objetivo es mantener la versión publicada sincronizada con MeshNet-Bot y
exponer capacidades para que MeshNet-Mobile pueda adaptar la interfaz sin
suponer funciones que el servidor todavía no permite.

Ejecución:
    python3 -m uvicorn tools.MobileAPI.mobile_api_v7054:app --host 0.0.0.0 --port 8791

Compatibilidad:
    - reutiliza exactamente la autenticación Bearer de mobile_api.py;
    - conserva todos los endpoints A1 existentes;
    - no añade operaciones mutantes;
    - no modifica ControlPanel, WebAdmin, dispatchers ni radio.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from tools.MobileAPI import mobile_api as base


REPO_DIR = Path(__file__).resolve().parents[2]
CHANGELOG_DIR = REPO_DIR / "docs"
VERSION_RE = re.compile(r"^CHANGELOG_v(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?\.md$")


def _version_key(path: Path) -> tuple[int, int, int, int]:
    """Convierte un nombre CHANGELOG_vX.Y.Z[.N].md en una clave comparable.

    Parámetros:
        path: fichero de changelog candidato.

    Retorno:
        Tupla numérica de cuatro componentes. Si el nombre no corresponde al
        formato esperado devuelve (-1, -1, -1, -1).
    """
    match = VERSION_RE.match(path.name)
    if not match:
        return (-1, -1, -1, -1)
    major, minor, patch, hotfix = match.groups()
    return (int(major), int(minor), int(patch), int(hotfix or 0))


def detect_meshnet_version() -> str:
    """Obtiene la versión que debe publicar la API móvil.

    Orden de resolución:
        1. MESHNET_BOT_VERSION, si está configurada explícitamente.
        2. Changelog con versión numérica más alta en docs/.
        3. v0.0.0 como fallback seguro.

    No lee secretos ni modifica archivos.
    """
    configured = os.getenv("MESHNET_BOT_VERSION", "").strip()
    if configured:
        return configured if configured.startswith("v") else f"v{configured}"

    candidates = [
        path
        for path in CHANGELOG_DIR.glob("CHANGELOG_v*.md")
        if _version_key(path) >= (0, 0, 0, 0)
    ]
    if not candidates:
        return "v0.0.0"

    latest = max(candidates, key=_version_key)
    return latest.stem.removeprefix("CHANGELOG_")


# Los handlers originales consultan base.MESHNET_VERSION en tiempo de ejecución,
# por lo que actualizar este valor mantiene /health y /system/overview coherentes
# sin duplicar ni reescribir esos endpoints.
base.MESHNET_VERSION = detect_meshnet_version()
base.app.version = "1.1.0"
app = base.app


@app.get("/api/v1/capabilities")
def capabilities() -> dict[str, Any]:
    """Publica qué funciones puede utilizar de forma segura MeshNet-Mobile.

    La app Android debe usar este endpoint para activar u ocultar funciones. De
    este modo una versión móvil nueva puede seguir conectándose a servidores más
    antiguos sin intentar operaciones inexistentes.
    """
    return {
        "ok": True,
        "api_version": base.API_VERSION,
        "meshnet_version": base.MESHNET_VERSION,
        "mode": "read_only",
        "features": {
            "system_overview": True,
            "services_read": True,
            "messages_audit_read": True,
            "emergencies_overview": True,
            "emergencies_read": True,
            "emergencies_coordinates": True,
            "meshcore_nodes": False,
            "meshtastic_nodes": False,
            "message_send": False,
            "service_control": False,
            "configuration_write": False,
        },
        "backend": {
            "delivery_audit": True,
            "emergency_secondary_category_matrix": True,
            "firms_nearest_population": True,
            "lightweight_node_geolocation": True,
        },
    }
