"""Pruebas de contrato de MeshNet Mobile API v1 (fase A1)."""

from __future__ import annotations

import os
from unittest.mock import patch

from fastapi.testclient import TestClient

from tools.MobileAPI.mobile_api import app


def test_health_is_public_and_versioned() -> None:
    """Health debe funcionar sin token y publicar el contrato v1."""
    with patch.dict(os.environ, {"MESHNET_MOBILE_API_TOKEN": ""}, clear=False):
        client = TestClient(app)
        response = client.get("/api/v1/health")

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["service"] == "meshnet-mobile-api"
    assert data["api_version"] == "1"
    assert data["authentication"] == "bearer"


def test_protected_endpoint_fails_closed_without_configured_token() -> None:
    """Sin token configurado la API nunca debe quedar abierta accidentalmente."""
    with patch.dict(os.environ, {"MESHNET_MOBILE_API_TOKEN": ""}, clear=False):
        client = TestClient(app)
        response = client.get("/api/v1/services")

    assert response.status_code == 503
    assert "MESHNET_MOBILE_API_TOKEN" in response.json()["detail"]


def test_protected_endpoint_rejects_invalid_bearer() -> None:
    """Un token incorrecto debe producir 401 y no ejecutar el endpoint."""
    with patch.dict(os.environ, {"MESHNET_MOBILE_API_TOKEN": "correct-token"}, clear=False):
        client = TestClient(app)
        response = client.get(
            "/api/v1/services",
            headers={"Authorization": "Bearer wrong-token"},
        )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_nodes_contract_is_stable_and_read_only() -> None:
    """A1 publica ambos contratos de nodos sin inventar datos."""
    with patch.dict(os.environ, {"MESHNET_MOBILE_API_TOKEN": "test-token"}, clear=False):
        client = TestClient(app)
        headers = {"Authorization": "Bearer test-token"}

        meshcore = client.get("/api/v1/nodes/meshcore", headers=headers)
        meshtastic = client.get("/api/v1/nodes/meshtastic", headers=headers)

    assert meshcore.status_code == 200
    assert meshtastic.status_code == 200
    assert meshcore.json()["transport"] == "meshcore"
    assert meshtastic.json()["transport"] == "meshtastic"
    assert meshcore.json()["available"] is False
    assert meshtastic.json()["available"] is False
