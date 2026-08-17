"""Pruebas de la autenticación por sesiones de MeshNet Mobile API v7.0.58."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from tools.MobileAPI.mobile_api_v7058 import app
from tools.MobileAPI.mobile_auth import (
    AuthIdentity,
    authenticate_user,
    hash_password,
    issue_session,
    refresh_session,
    set_user,
    verify_access_token,
    verify_password,
)


def _auth_environment(tmp_path: Path) -> dict[str, str]:
    """Construye rutas aisladas para que las pruebas nunca usen credenciales reales."""
    return {
        "MESHNET_MOBILE_API_TOKEN": "legacy-test-token",
        "MESHNET_MOBILE_AUTH_USERS_FILE": str(tmp_path / "mobile_users.json"),
        "MESHNET_MOBILE_AUTH_DB": str(tmp_path / "mobile_auth.db"),
    }


def test_password_hash_is_salted_and_verifiable() -> None:
    """scrypt debe verificar la clave correcta sin guardar el texto original."""
    first = hash_password("Clave-de-prueba-2026")
    second = hash_password("Clave-de-prueba-2026")

    assert first["algorithm"] == "scrypt"
    assert first["hash"] != second["hash"]
    assert first["salt"] != second["salt"]
    assert "Clave-de-prueba-2026" not in str(first)
    assert verify_password("Clave-de-prueba-2026", first) is True
    assert verify_password("incorrecta", first) is False


def test_user_store_authenticates_without_plaintext_password(tmp_path: Path) -> None:
    """El almacén local debe devolver identidad y no incluir la contraseña en claro."""
    environment = _auth_environment(tmp_path)
    with patch.dict(os.environ, environment, clear=False):
        set_user("operador", "Secreta-1234", role="operator")
        identity = authenticate_user("operador", "Secreta-1234")
        file_content = Path(environment["MESHNET_MOBILE_AUTH_USERS_FILE"]).read_text(encoding="utf-8")

    assert identity == AuthIdentity(username="operador", role="operator")
    assert "Secreta-1234" not in file_content


def test_login_session_can_use_existing_protected_endpoint(tmp_path: Path) -> None:
    """Un access token nuevo debe atravesar la API histórica sin cambiar /capabilities."""
    environment = _auth_environment(tmp_path)
    with patch.dict(os.environ, environment, clear=False):
        set_user("jmmol", "Prueba-Segura-2026", role="viewer")
        client = TestClient(app)

        login = client.post(
            "/api/v1/auth/login",
            json={"username": "jmmol", "password": "Prueba-Segura-2026"},
        )
        assert login.status_code == 200
        login_data = login.json()
        access_token = login_data["access_token"]

        capabilities = client.get(
            "/api/v1/capabilities",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    assert login_data["ok"] is True
    assert login_data["username"] == "jmmol"
    assert login_data["role"] == "viewer"
    assert login_data["token_type"] == "bearer"
    assert capabilities.status_code == 200
    assert capabilities.json()["mode"] == "read_only"


def test_legacy_bearer_remains_valid_in_v7058(tmp_path: Path) -> None:
    """La nueva capa no debe romper el token fijo utilizado por clientes actuales."""
    environment = _auth_environment(tmp_path)
    with patch.dict(os.environ, environment, clear=False):
        client = TestClient(app)
        response = client.get(
            "/api/v1/capabilities",
            headers={"Authorization": "Bearer legacy-test-token"},
        )

    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_current_view_reuses_complete_control_panel_snapshot_with_session(tmp_path: Path) -> None:
    """La ruta móvil debe exponer la instantánea completa sin aplicar el límite histórico de 200.

    Se usa una incidencia forestal de Huesca porque este caso reproduce la diferencia observada
    entre Control Panel y Android: el selector móvil debe recibir provincia y categoría desde la
    misma instantánea ``load_current`` que consume el Control Panel.
    """
    environment = _auth_environment(tmp_path)
    current = {
        "firms:huesca-1": SimpleNamespace(
            event_id="firms:huesca-1",
            title="Detección térmica satelital",
            description="Incendio forestal de prueba",
            source="nasa_firms",
            category="wildfire",
            status="active",
            severity="high",
            municipality="Jaca",
            province="Huesca",
            road="",
            kilometre=None,
            latitude=42.57,
            longitude=-0.55,
            started_at="2026-08-17T09:00:00+00:00",
            updated_at="2026-08-17T10:00:00+00:00",
            last_seen="2026-08-17T11:00:00+00:00",
        ),
    }

    with patch.dict(os.environ, environment, clear=False), patch(
        "tools.MobileAPI.mobile_api_v7058.load_current",
        return_value=current,
    ):
        set_user("jmmol", "Prueba-Segura-2026", role="viewer")
        client = TestClient(app)
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "jmmol", "password": "Prueba-Segura-2026"},
        ).json()
        response = client.get(
            "/api/v1/emergencies/current-view",
            headers={"Authorization": f"Bearer {login['access_token']}"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["total"] == 1
    assert payload["provinces"] == ["Huesca"]
    assert payload["categories"] == ["wildfire"]
    assert payload["events"][0]["event_id"] == "firms:huesca-1"
    assert payload["events"][0]["province"] == "Huesca"
    assert payload["events"][0]["category"] == "wildfire"


def test_current_view_rejects_invalid_bearer(tmp_path: Path) -> None:
    """La ruta nueva debe conservar la protección de la Mobile API y no quedar pública."""
    environment = _auth_environment(tmp_path)
    with patch.dict(os.environ, environment, clear=False):
        client = TestClient(app)
        response = client.get(
            "/api/v1/emergencies/current-view",
            headers={"Authorization": "Bearer token-invalido"},
        )

    assert response.status_code == 401
    assert response.json()["detail"] == "Token Bearer no válido"


def test_invalid_login_does_not_issue_tokens(tmp_path: Path) -> None:
    """Usuario/contraseña erróneos deben producir 401 sin distinguir el motivo."""
    environment = _auth_environment(tmp_path)
    with patch.dict(os.environ, environment, clear=False):
        set_user("jmmol", "Correcta-2026")
        client = TestClient(app)
        response = client.post(
            "/api/v1/auth/login",
            json={"username": "jmmol", "password": "incorrecta"},
        )

    assert response.status_code == 401
    assert response.json()["detail"] == "Usuario o contraseña no válidos"


def test_refresh_rotates_previous_family(tmp_path: Path) -> None:
    """Un refresh utilizado debe invalidar access y refresh de la familia anterior."""
    environment = _auth_environment(tmp_path)
    with patch.dict(os.environ, environment, clear=False):
        original = issue_session(AuthIdentity(username="jmmol", role="viewer"))
        refreshed = refresh_session(original["refresh_token"])

        assert refreshed is not None
        assert verify_access_token(original["access_token"]) is None
        assert refresh_session(original["refresh_token"]) is None
        assert verify_access_token(refreshed["access_token"]) == AuthIdentity(
            username="jmmol",
            role="viewer",
        )


def test_auth_me_and_logout_revoke_session(tmp_path: Path) -> None:
    """/me identifica la sesión y logout deja de aceptar su access token."""
    environment = _auth_environment(tmp_path)
    with patch.dict(os.environ, environment, clear=False):
        set_user("jmmol", "Prueba-Segura-2026", role="admin")
        client = TestClient(app)
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "jmmol", "password": "Prueba-Segura-2026"},
        ).json()

        me = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {login['access_token']}"},
        )
        logout = client.post(
            "/api/v1/auth/logout",
            json={"token": login["refresh_token"]},
        )
        after_logout = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {login['access_token']}"},
        )

    assert me.status_code == 200
    assert me.json()["username"] == "jmmol"
    assert me.json()["role"] == "admin"
    assert logout.status_code == 200
    assert logout.json()["revoked"] is True
    assert after_logout.status_code == 401
