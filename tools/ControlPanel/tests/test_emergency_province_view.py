from types import SimpleNamespace

from tools.ControlPanel.emergency_province_view import build_emergency_snapshot


def _event(**overrides):
    values = {
        "event_id": "evt-1",
        "title": "Incidencia de prueba",
        "description": "Descripción",
        "source": "dgt_datex",
        "status": "active",
        "severity": "high",
        "municipality": "Zaragoza",
        "province": "Zaragoza",
        "road": "A-2",
        "kilometre": 10.5,
        "latitude": 41.65,
        "longitude": -0.88,
        "updated_at": "2026-08-14T10:00:00+00:00",
        "last_seen": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_snapshot_exposes_only_view_fields_and_provinces():
    """La vista conserva los campos necesarios y genera provincias dinámicamente."""
    snapshot = build_emergency_snapshot([
        _event(event_id="z", province="Zaragoza"),
        _event(event_id="h", province="Huesca"),
        _event(event_id="none", province=""),
    ])

    assert snapshot["ok"] is True
    assert snapshot["total"] == 3
    assert snapshot["provinces"] == ["Huesca", "Zaragoza"]
    assert snapshot["events"][0]["title"] == "Incidencia de prueba"
    assert snapshot["events"][0]["latitude"] == 41.65


def test_snapshot_orders_newest_first_and_falls_back_to_last_seen():
    """El orden visual usa updated_at y, si falta, last_seen sin alterar los eventos."""
    snapshot = build_emergency_snapshot([
        _event(event_id="old", updated_at="2026-08-14T08:00:00+00:00"),
        _event(
            event_id="new",
            updated_at="",
            last_seen="2026-08-14T11:00:00+00:00",
        ),
    ])

    assert [event["event_id"] for event in snapshot["events"]] == ["new", "old"]
