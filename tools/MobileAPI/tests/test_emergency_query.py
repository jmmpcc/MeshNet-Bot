"""Pruebas de la consulta dinámica de Emergencias para MeshNet-Mobile."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from tools.MobileAPI.mobile_api_v7054 import _filter_emergency_events
from tools.MobileAPI.mobile_api_v7058 import app


def _event(
    event_id: str,
    *,
    province: str,
    category: str,
    severity: str = "high",
    last_seen: str = "",
    updated_at: str = "",
    started_at: str = "",
) -> SimpleNamespace:
    """Crea una incidencia mínima compatible con ``build_emergency_snapshot``."""
    return SimpleNamespace(
        event_id=event_id,
        title=f"Incidencia {event_id}",
        description="",
        source="test",
        category=category,
        status="active",
        severity=severity,
        municipality="",
        province=province,
        road="",
        kilometre=None,
        latitude=42.0,
        longitude=-0.5,
        started_at=started_at,
        updated_at=updated_at,
        last_seen=last_seen,
    )


def test_filter_applies_time_and_dimensions_before_pagination() -> None:
    """24 h + provincia + categoría opera sobre la colección completa."""
    now = datetime(2026, 8, 17, 18, 0, tzinfo=timezone.utc)
    events = [
        _event("recent-huesca", province="Huesca", category="wildfire", last_seen="2026-08-17T17:00:00Z"),
        _event("old-huesca", province="Huesca", category="wildfire", last_seen="2026-08-15T17:00:00Z"),
        _event("recent-zaragoza", province="Zaragoza", category="wildfire", last_seen="2026-08-17T17:00:00Z"),
    ]

    filtered = _filter_emergency_events(
        events,
        hours=24,
        province="huesca",
        severity="",
        category="WILDFIRE",
        now=now,
    )

    assert [event.event_id for event in filtered] == ["recent-huesca"]


def test_query_defaults_to_24_hours_and_keeps_global_facets() -> None:
    """La ruta devuelve sólo recientes, manteniendo provincias/tipos globales."""
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(hours=2)).isoformat()
    old = (now - timedelta(hours=48)).isoformat()
    current = {
        "recent-huesca": _event("recent-huesca", province="Huesca", category="wildfire", last_seen=recent),
        "old-huesca": _event("old-huesca", province="Huesca", category="wildfire", last_seen=old),
        "road-zaragoza": _event("road-zaragoza", province="Zaragoza", category="road_closed", last_seen=recent),
    }

    with patch.dict(os.environ, {"MESHNET_MOBILE_API_TOKEN": "legacy-test-token"}, clear=False), patch(
        "tools.MobileAPI.mobile_api_v7054.load_current",
        return_value=current,
    ):
        response = TestClient(app).get(
            "/api/v1/emergencies/query",
            params={"province": "Huesca", "category": "wildfire"},
            headers={"Authorization": "Bearer legacy-test-token"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["filters"]["hours"] == 24
    assert payload["total"] == 1
    assert payload["returned"] == 1
    assert payload["has_more"] is False
    assert payload["events"][0]["event_id"] == "recent-huesca"
    assert payload["provinces"] == ["Huesca", "Zaragoza"]
    assert payload["categories"] == ["road_closed", "wildfire"]


def test_query_hours_zero_supports_todo_and_paginates_after_filtering() -> None:
    """hours=0 incluye históricos y pagina sólo el subconjunto seleccionado."""
    current = {
        "huesca-1": _event("huesca-1", province="Huesca", category="wildfire", started_at="2026-08-10T10:00:00Z"),
        "huesca-2": _event("huesca-2", province="Huesca", category="wildfire", started_at="2026-08-11T10:00:00Z"),
        "zaragoza": _event("zaragoza", province="Zaragoza", category="wildfire", started_at="2026-08-12T10:00:00Z"),
    }

    with patch.dict(os.environ, {"MESHNET_MOBILE_API_TOKEN": "legacy-test-token"}, clear=False), patch(
        "tools.MobileAPI.mobile_api_v7054.load_current",
        return_value=current,
    ):
        response = TestClient(app).get(
            "/api/v1/emergencies/query",
            params={"hours": 0, "province": "Huesca", "category": "wildfire", "limit": 1, "offset": 0},
            headers={"Authorization": "Bearer legacy-test-token"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert payload["returned"] == 1
    assert payload["limit"] == 1
    assert payload["offset"] == 0
    assert payload["has_more"] is True
    assert payload["filters"]["hours"] == 0
    assert payload["filters"]["province"] == "Huesca"
    assert payload["filters"]["category"] == "wildfire"
