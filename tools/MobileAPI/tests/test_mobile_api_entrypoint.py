"""Pruebas del punto de entrada estable de MeshNet Mobile API.

Estas pruebas protegen la separación entre el nombre operativo permanente usado por
systemd y las capas internas versionadas de la Mobile API.
"""

from __future__ import annotations

from pathlib import Path

from tools.MobileAPI import mobile_api_entrypoint, mobile_api_v7058


MOBILE_API_DIR = Path(__file__).resolve().parents[1]
SERVICE_FILE = MOBILE_API_DIR / "systemd" / "meshnet-mobile-api.service"


def test_stable_entrypoint_exports_current_validated_app() -> None:
    """El entrypoint fijo debe exportar exactamente la aplicación vigente.

    No se crea una FastAPI nueva ni se duplican rutas/middlewares. La identidad del
    objeto garantiza que systemd ejecuta la misma app ya definida en v7.0.58.
    """
    assert mobile_api_entrypoint.app is mobile_api_v7058.app


def test_systemd_uses_stable_entrypoint() -> None:
    """El servicio no debe volver a depender de nombres de módulo versionados."""
    service = SERVICE_FILE.read_text(encoding="utf-8")

    assert "tools.MobileAPI.mobile_api_entrypoint:app" in service
    assert "tools.MobileAPI.mobile_api_v7054:app" not in service
    assert "tools.MobileAPI.mobile_api_v7058:app" not in service


def test_versioned_layers_remain_available_for_compatibility() -> None:
    """Las capas históricas siguen presentes mientras formen la cadena validada."""
    assert (MOBILE_API_DIR / "mobile_api.py").is_file()
    assert (MOBILE_API_DIR / "mobile_api_v7054.py").is_file()
    assert (MOBILE_API_DIR / "mobile_api_v7058.py").is_file()
