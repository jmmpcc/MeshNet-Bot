from tools.MobileAPI import mobile_api
from tools.emergencias_guardia.emergencias.models import Event


def _event(
    event_id: str,
    *,
    source: str,
    severity: str,
    status: str = "active",
    municipality: str = "",
    latitude: float | None = None,
    longitude: float | None = None,
    updated_at: str = "2026-08-13T08:00:00+00:00",
) -> Event:
    """Crea un evento mínimo y válido para probar el contrato móvil."""
    return Event(
        event_id=event_id,
        source=source,
        source_event_id=event_id,
        category="public_safety",
        severity=severity,
        status=status,
        title=f"Evento {event_id}",
        municipality=municipality,
        latitude=latitude,
        longitude=longitude,
        updated_at=updated_at,
    )


def test_emergency_events_snapshot_filters_and_summarizes(monkeypatch):
    """Los filtros no alteran eventos y el resumen refleja solo los visibles lógicos."""
    events = {
        "a": _event(
            "a",
            source="dgt_datex",
            severity="high",
            municipality="Zaragoza",
            latitude=41.65,
            longitude=-0.88,
            updated_at="2026-08-13T09:00:00+00:00",
        ),
        "b": _event(
            "b",
            source="dgt_datex",
            severity="medium",
            municipality="Huesca",
            updated_at="2026-08-13T08:00:00+00:00",
        ),
        "c": _event(
            "c",
            source="ign_rss",
            severity="high",
            municipality="Zaragoza",
            latitude=41.70,
            longitude=-0.90,
            updated_at="2026-08-13T10:00:00+00:00",
        ),
    }
    monkeypatch.setattr(mobile_api, "load_current", lambda: events)

    result = mobile_api._emergency_events_snapshot(
        source="dgt_datex",
        query="zaragoza",
        limit=200,
    )

    assert result["ok"] is True
    assert [item["event_id"] for item in result["events"]] == ["a"]
    assert result["summary"]["total"] == 1
    assert result["summary"]["with_coordinates"] == 1
    assert result["summary"]["severity"]["high"] == 1
    assert result["has_more"] is False


def test_emergency_events_snapshot_orders_newest_and_applies_limit(monkeypatch):
    """La lista móvil prioriza la incidencia actualizada más recientemente."""
    events = {
        "old": _event(
            "old",
            source="ign_rss",
            severity="medium",
            updated_at="2026-08-13T07:00:00+00:00",
        ),
        "new": _event(
            "new",
            source="ign_rss",
            severity="critical",
            latitude=40.0,
            longitude=-1.0,
            updated_at="2026-08-13T11:00:00+00:00",
        ),
    }
    monkeypatch.setattr(mobile_api, "load_current", lambda: events)

    result = mobile_api._emergency_events_snapshot(limit=1)

    assert [item["event_id"] for item in result["events"]] == ["new"]
    assert result["summary"]["total"] == 2
    assert result["summary"]["with_coordinates"] == 1
    assert result["summary"]["severity"]["critical"] == 1
    assert result["has_more"] is True
