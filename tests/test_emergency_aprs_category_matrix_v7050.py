from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from tools.ControlPanel import aprs_category_matrix as matrix
from tools.ControlPanel import web_admin
from tools.emergencias_guardia.emergencias import emergency_dispatcher as dispatcher
from tools.emergencias_guardia.emergencias.models import Event


def event(category: str = "road_closed", severity: str = "high") -> Event:
    """Crea un evento oficial mínimo para probar las autorizaciones APRS."""
    return Event(
        event_id=f"TEST-{category}",
        source="test",
        source_event_id=f"TEST-{category}",
        category=category,
        severity=severity,
        status="active",
        verification="official",
        title="Prueba de matriz APRS",
    )


def enable_aprs(monkeypatch) -> None:
    """Activa las autorizaciones generales sin abrir ningún transporte real."""
    monkeypatch.setenv("APPS_APRS_ENABLED", "1")
    monkeypatch.setenv("APPS_APRS_ALLOWED_SOURCES", "emergencias")
    monkeypatch.setenv("EMERGENCIAS_APRS_ENABLED", "1")
    monkeypatch.setenv("EMERGENCIAS_APRS_RF_ENABLED", "1")
    monkeypatch.setenv("APRSIS_PUSH_ENABLED", "1")
    monkeypatch.setenv("APRSIS_EMERGENCY_BULLETIN_ENABLED", "1")
    monkeypatch.setenv("EMERGENCIAS_APRS_RF_MIN_LEVEL", "high")
    monkeypatch.setenv("APRSIS_EMERGENCY_BULLETIN_MIN_LEVEL", "high")


def test_missing_category_variables_preserve_legacy_behavior(monkeypatch):
    """Desplegar v7.0.50 sin guardar la matriz no cambia ninguna elegibilidad."""
    monkeypatch.delenv("EMERGENCIAS_APRS_RF_CATEGORIES", raising=False)
    monkeypatch.delenv("EMERGENCIAS_APRSIS_CATEGORIES", raising=False)
    current = event("road_closed")

    assert dispatcher._secondary_category_allowed(
        "EMERGENCIAS_APRS_RF_CATEGORIES", current
    ) is True
    assert dispatcher._secondary_category_allowed(
        "EMERGENCIAS_APRSIS_CATEGORIES", current
    ) is True


def test_empty_category_variable_blocks_that_secondary_transport(monkeypatch):
    """Una columna completamente desmarcada bloquea sólo su salida secundaria."""
    monkeypatch.setenv("EMERGENCIAS_APRS_RF_CATEGORIES", "")
    assert dispatcher._secondary_category_allowed(
        "EMERGENCIAS_APRS_RF_CATEGORIES", event("road_closed")
    ) is False


def test_category_lists_are_independent(monkeypatch):
    """APRS-IS y APRS RF pueden autorizar categorías diferentes."""
    current = event("traffic_collision")
    monkeypatch.setenv("EMERGENCIAS_APRSIS_CATEGORIES", "traffic_collision,road_closed")
    monkeypatch.setenv("EMERGENCIAS_APRS_RF_CATEGORIES", "road_closed")

    assert dispatcher._secondary_category_allowed(
        "EMERGENCIAS_APRSIS_CATEGORIES", current
    ) is True
    assert dispatcher._secondary_category_allowed(
        "EMERGENCIAS_APRS_RF_CATEGORIES", current
    ) is False


def test_aprs_rf_rejects_category_before_contacting_gateway(monkeypatch):
    """Una categoría no autorizada no llega al preview ni al gateway RF."""
    enable_aprs(monkeypatch)
    monkeypatch.setenv("EMERGENCIAS_APRS_RF_CATEGORIES", "wildfire")

    result = dispatcher._send_aprs_rf(event("road_closed"), "Corte de carretera")

    assert result == {"ok": True, "sent": False, "reason": "category_not_allowed"}


def test_aprsis_rejects_category_before_contacting_gateway(monkeypatch):
    """Una categoría no autorizada no genera petición de boletín APRS-IS."""
    enable_aprs(monkeypatch)
    monkeypatch.setenv("EMERGENCIAS_APRSIS_CATEGORIES", "wildfire")

    result = dispatcher._send_aprsis_bulletin(event("road_closed"), "Corte de carretera")

    assert result == {"ok": True, "sent": False, "reason": "category_not_allowed"}


def test_minimum_level_remains_an_independent_safety_barrier(monkeypatch):
    """Autorizar una categoría no rebaja los MIN_LEVEL históricos."""
    enable_aprs(monkeypatch)
    monkeypatch.setenv("EMERGENCIAS_APRS_RF_CATEGORIES", "road_closed")
    monkeypatch.setenv("EMERGENCIAS_APRSIS_CATEGORIES", "road_closed")

    medium = event("road_closed", severity="medium")
    assert dispatcher._send_aprs_rf(medium, "Corte") == {
        "ok": True,
        "sent": False,
        "reason": "severity_below_threshold",
    }
    assert dispatcher._send_aprsis_bulletin(medium, "Corte") == {
        "ok": True,
        "sent": False,
        "reason": "severity_below_threshold",
    }


def test_dashboard_transform_exposes_two_secondary_matrix_columns():
    """La extensión visual mantiene Mesh y añade únicamente APRS-IS/APRS RF."""
    source = Path(matrix.__file__).read_text(encoding="utf-8")
    dashboard = matrix.transform_dashboard(web_admin.DASHBOARD)

    assert "EMERGENCIAS_APRSIS_CATEGORIES" in source
    assert "EMERGENCIAS_APRS_RF_CATEGORIES" in source
    assert "secondary_transports" in source
    assert "secondary-aprsis" in dashboard
    assert "secondary-aprs-rf" in dashboard
    assert "APRS-IS" in dashboard
    assert "APRS RF" in dashboard
    assert "UI 2 · v7.0.50" in dashboard


def test_controlpanel_filters_persist_independent_aprs_columns(tmp_path, monkeypatch):
    """GET/PUT reutilizan la matriz histórica y guardan solo las listas APRS nuevas."""
    env_file = tmp_path / "emergencias.env"
    monkeypatch.setattr(web_admin, "EMERGENCIAS_ENV_FILE", env_file)

    current_rules = {
        "low": [],
        "medium": ["traffic_collision"],
        "high": ["road_closed", "traffic_collision"],
        "critical": ["road_closed", "traffic_collision"],
    }

    def fake_execute(action):
        argv = list(action.argv)
        if action.id != "emergency_filters":
            return {"ok": True, "returncode": 0, "stdout": "", "stderr": "", "data": {}}
        if argv[-2:] == ["filters", "show"]:
            return {
                "ok": True,
                "returncode": 0,
                "stdout": "",
                "stderr": "",
                "data": {
                    "rules": {key: list(value) for key, value in current_rules.items()},
                    "categories": [{"name": name} for name in sorted(web_admin.EMERGENCY_CATEGORIES)],
                },
            }
        if "--rules-json" in argv:
            index = argv.index("--rules-json")
            supplied = json.loads(argv[index + 1])
            current_rules.clear()
            current_rules.update({key: list(value) for key, value in supplied.items()})
            return {
                "ok": True,
                "returncode": 0,
                "stdout": "",
                "stderr": "",
                "data": {"rules": supplied, "note": "Matriz actualizada"},
            }
        raise AssertionError(f"acción de filtros inesperada: {argv}")

    monkeypatch.setattr(web_admin, "execute_action", fake_execute)
    registry = web_admin.ToolRegistry(tmp_path / "state.json", web_admin.DEFAULT_TOOLS)
    registry.set_enabled("emergencias_guardia", True)
    app = matrix.apply_aprs_category_matrix(web_admin.create_app(registry))
    client = TestClient(app)

    # Verificación end-to-end del HTML real que recibirá el navegador, no sólo
    # de la función de transformación aislada.
    page = client.get("/")
    assert page.status_code == 200
    assert "UI 2 · v7.0.50" in page.text
    assert "secondary-aprsis" in page.text
    assert "secondary-aprs-rf" in page.text
    assert "APRS-IS" in page.text
    assert "APRS RF" in page.text

    # Sin variables nuevas la respuesta refleja el comportamiento histórico:
    # todas las categorías siguen autorizadas para ambas salidas.
    before = client.get("/api/emergencias/filters")
    assert before.status_code == 200
    assert set(before.json()["secondary_transports"]["aprsis"]) == web_admin.EMERGENCY_CATEGORIES
    assert set(before.json()["secondary_transports"]["aprs_rf"]) == web_admin.EMERGENCY_CATEGORIES

    payload = {
        "rules": current_rules,
        "secondary_transports": {
            "aprsis": ["traffic_collision", "road_closed"],
            "aprs_rf": ["road_closed"],
        },
    }
    saved = client.put("/api/emergencias/filters", json=payload)
    assert saved.status_code == 200
    assert saved.json()["secondary_transports"] == {
        "aprsis": ["road_closed", "traffic_collision"],
        "aprs_rf": ["road_closed"],
    }
    text = env_file.read_text(encoding="utf-8")
    assert "EMERGENCIAS_APRSIS_CATEGORIES=road_closed,traffic_collision" in text
    assert "EMERGENCIAS_APRS_RF_CATEGORIES=road_closed" in text

    after = client.get("/api/emergencias/filters")
    assert after.status_code == 200
    assert after.json()["secondary_transports"] == saved.json()["secondary_transports"]
