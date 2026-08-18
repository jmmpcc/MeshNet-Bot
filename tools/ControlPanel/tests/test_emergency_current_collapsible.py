from datetime import datetime, timezone
from types import SimpleNamespace

from tools.ControlPanel.emergency_current_collapsible import (
    _emergency_current_collapsible_script,
    build_windowed_emergency_snapshot,
)


def _event(**overrides):
    """Crea una incidencia mínima compatible con la instantánea del Control Panel."""
    values = {
        "event_id": "evt-1",
        "title": "Incidencia de prueba",
        "description": "Descripción",
        "source": "dgt_datex",
        "category": "road_closed",
        "status": "active",
        "severity": "high",
        "municipality": "Zaragoza",
        "province": "Zaragoza",
        "road": "A-2",
        "kilometre": 10.5,
        "latitude": 41.65,
        "longitude": -0.88,
        "started_at": "2026-08-18T08:00:00+00:00",
        "updated_at": "2026-08-18T08:00:00+00:00",
        "last_seen": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_default_window_filters_events_older_than_24_hours():
    """La carga inicial no devuelve incidencias fuera de las últimas 24 horas."""
    now = datetime(2026, 8, 18, 8, 30, tzinfo=timezone.utc)

    snapshot = build_windowed_emergency_snapshot(
        [
            _event(event_id="recent", updated_at="2026-08-18T08:00:00+00:00"),
            _event(event_id="old", updated_at="2026-08-16T08:00:00+00:00"),
        ],
        now=now,
    )

    assert snapshot["total"] == 1
    assert [event["event_id"] for event in snapshot["events"]] == ["recent"]


def test_zero_hours_preserves_historical_all_option():
    """El valor cero mantiene disponible la vista completa bajo petición explícita."""
    snapshot = build_windowed_emergency_snapshot(
        [
            _event(event_id="recent"),
            _event(event_id="old", updated_at="2020-01-01T00:00:00+00:00"),
        ],
        hours=0,
    )

    assert snapshot["total"] == 2


def test_recent_update_keeps_incident_even_when_started_at_is_old():
    """Una incidencia antigua pero actualizada recientemente sigue siendo visible."""
    now = datetime(2026, 8, 18, 8, 30, tzinfo=timezone.utc)

    snapshot = build_windowed_emergency_snapshot(
        [
            _event(
                event_id="updated",
                started_at="2026-08-10T08:00:00+00:00",
                updated_at="2026-08-18T08:15:00+00:00",
            )
        ],
        hours=24,
        now=now,
    )

    assert snapshot["total"] == 1
    assert snapshot["events"][0]["event_id"] == "updated"


def test_missing_or_invalid_dates_are_not_hidden_for_compatibility():
    """Fuentes antiguas sin fecha válida no desaparecen accidentalmente del panel."""
    now = datetime(2026, 8, 18, 8, 30, tzinfo=timezone.utc)

    snapshot = build_windowed_emergency_snapshot(
        [
            _event(event_id="missing", updated_at="", started_at="", last_seen=""),
            _event(event_id="invalid", updated_at="sin-fecha", started_at="", last_seen=""),
        ],
        hours=24,
        now=now,
    )

    assert {event["event_id"] for event in snapshot["events"]} == {"missing", "invalid"}


def test_script_reuses_existing_panel_and_defaults_to_24_hours():
    """La mejora conserva IDs históricos, añade periodo y replica el plegado de Mensajes."""
    script = _emergency_current_collapsible_script()

    assert "const DEFAULT_HOURS = '24';" in script
    assert "select.id = 'emergency-window-hours';" in script
    assert "Últimas 24 horas" in script
    assert "Últimas 48 horas" in script
    assert "Últimos 7 días" in script
    assert "#emergency-province-refresh" in script
    assert "/api/emergencias/current-view-window" in script
    assert "document.createElement('details')" in script
    assert "state.textContent = 'DESPLEGAR'" in script
    assert "details.open ? 'OCULTAR' : 'DESPLEGAR'" in script
