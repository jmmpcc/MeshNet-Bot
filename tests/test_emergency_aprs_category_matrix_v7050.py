from __future__ import annotations

from pathlib import Path

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


def test_controlpanel_exposes_two_secondary_matrix_columns():
    """La matriz visual debe mantener Mesh y añadir sólo APRS-IS/APRS RF."""
    source = Path(web_admin.__file__).read_text(encoding="utf-8")
    assert "EMERGENCIAS_APRSIS_CATEGORIES" in source
    assert "EMERGENCIAS_APRS_RF_CATEGORIES" in source
    assert "secondary_transports" in source
    assert "secondary-aprsis" in web_admin.DASHBOARD
    assert "secondary-aprs-rf" in web_admin.DASHBOARD
    assert "APRS-IS" in web_admin.DASHBOARD
    assert "APRS RF" in web_admin.DASHBOARD
    assert "UI 2 · v7.0.50" in web_admin.DASHBOARD
