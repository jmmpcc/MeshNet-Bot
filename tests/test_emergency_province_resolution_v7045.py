from __future__ import annotations

from pathlib import Path

from tools.emergencias_guardia.emergencias.engine import event_matches
from tools.emergencias_guardia.emergencias.geo_admin import (
    enrich_event_province,
    resolve_province,
)
from tools.emergencias_guardia.emergencias.models import Event, VALID_CATEGORIES


def _config(*areas: dict) -> dict:
    """Construye la configuración mínima utilizada por event_matches()."""
    return {
        "filters": {
            "minimum_severity": "low",
            "categories": sorted(VALID_CATEGORIES),
        },
        "areas": list(areas),
    }


def _event(**changes) -> Event:
    """Crea un Event neutro para las pruebas geográficas de v7.0.45."""
    values = {
        "event_id": "geo:test",
        "source": "test",
        "source_event_id": "test",
        "category": "earthquake",
        "severity": "medium",
        "title": "Evento geográfico",
    }
    values.update(changes)
    return Event(**values)


def test_local_boundaries_resolve_zaragoza() -> None:
    """Las coordenadas del centro de Zaragoza deben resolver Zaragoza."""
    assert resolve_province(41.6488, -0.8891) == "Zaragoza"


def test_local_boundaries_resolve_huesca() -> None:
    """Las coordenadas de Huesca capital deben resolver Huesca."""
    assert resolve_province(42.1401, -0.4089) == "Huesca"


def test_enrichment_never_overwrites_existing_province() -> None:
    """Una provincia aportada por DGT/u otra fuente conserva autoridad."""
    event = _event(province="Teruel", latitude=41.6488, longitude=-0.8891)
    original_hash = event.raw_hash
    assert enrich_event_province(event) is False
    assert event.province == "Teruel"
    assert event.raw_hash == original_hash
    assert "province_resolved_from_coordinates" not in event.metadata


def test_enrichment_keeps_source_hash_to_avoid_false_update() -> None:
    """La provincia derivada no debe simular un cambio de la fuente original."""
    event = _event(latitude=41.6488, longitude=-0.8891)
    original_hash = event.raw_hash
    assert enrich_event_province(event) is True
    assert event.province == "Zaragoza"
    assert event.raw_hash == original_hash
    assert event.metadata["province_resolved_from_coordinates"] is True


def test_missing_boundary_file_falls_back_without_modifying_event(tmp_path: Path) -> None:
    """Sin cartografía local se conserva exactamente el comportamiento previo."""
    event = _event(latitude=41.6488, longitude=-0.8891)
    missing = tmp_path / "no-existe.geojson"
    assert enrich_event_province(event, missing) is False
    assert event.province == ""


def test_enriched_ign_or_firms_event_matches_selected_province() -> None:
    """IGN/FIRMS pueden pasar el filtro provincial después del enriquecimiento."""
    event = _event(latitude=41.6488, longitude=-0.8891)
    assert enrich_event_province(event) is True
    config = _config({"type": "province", "name": "Zaragoza", "enabled": True})
    assert event_matches(event, config) is True


def test_province_and_radius_keep_existing_or_semantics() -> None:
    """Provincia y radio siguen combinándose mediante OR, sin cambiar _area_matches."""
    event = _event(province="Navarra", latitude=42.8125, longitude=-1.6458)
    config = _config(
        {"type": "province", "name": "Zaragoza", "enabled": True},
        {
            "type": "radius",
            "name": "Pamplona 20 km",
            "latitude": 42.8125,
            "longitude": -1.6458,
            "radius_km": 20,
            "enabled": True,
        },
    )
    assert event_matches(event, config) is True
