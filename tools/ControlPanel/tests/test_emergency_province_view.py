from types import SimpleNamespace

from tools.ControlPanel.emergency_province_view import (
    _extension_script,
    build_emergency_snapshot,
)


def _event(**overrides):
    """Crea un evento mínimo de prueba compatible con la vista Lista/Mapa."""
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
        "updated_at": "2026-08-14T10:00:00+00:00",
        "last_seen": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_snapshot_exposes_view_fields_provinces_and_categories():
    """La instantánea conserva campos del mapa y genera filtros dinámicos."""
    snapshot = build_emergency_snapshot([
        _event(event_id="z", province="Zaragoza", category="road_closed"),
        _event(event_id="h", province="Huesca", category="wildfire"),
        _event(event_id="none", province="", category="wildfire"),
    ])

    assert snapshot["ok"] is True
    assert snapshot["total"] == 3
    assert snapshot["provinces"] == ["Huesca", "Zaragoza"]
    assert snapshot["categories"] == ["road_closed", "wildfire"]
    assert snapshot["events"][0]["title"] == "Incidencia de prueba"
    assert snapshot["events"][0]["latitude"] == 41.65
    assert snapshot["events"][0]["category"] in {"road_closed", "wildfire"}


def test_snapshot_orders_newest_first_and_falls_back_to_last_seen():
    """El orden visual usa updated_at y, si falta, last_seen sin alterar eventos."""
    snapshot = build_emergency_snapshot([
        _event(event_id="old", updated_at="2026-08-14T08:00:00+00:00"),
        _event(
            event_id="new",
            updated_at="",
            last_seen="2026-08-14T11:00:00+00:00",
        ),
    ])

    assert [event["event_id"] for event in snapshot["events"]] == ["new", "old"]


def test_snapshot_defaults_missing_category_without_breaking_old_events():
    """Eventos históricos sin category siguen siendo visibles como ``other``."""
    event = _event()
    delattr(event, "category")

    snapshot = build_emergency_snapshot([event])

    assert snapshot["events"][0]["category"] == "other"
    assert snapshot["categories"] == ["other"]


def test_map_script_reuses_existing_view_and_fixed_maplibre_assets():
    """El mapa se integra en la vista existente con dependencia y estilo fijados."""
    script = _extension_script()

    assert 'id="emergency-view-list"' in script
    assert 'id="emergency-view-map"' in script
    assert 'id="emergency-category-select"' in script
    assert 'id="emergency-map-fit"' in script
    assert "maplibre-gl@5.16.0" in script
    assert "https://tiles.openfreemap.org/styles/liberty" in script
    assert "new maplibregl.NavigationControl" in script
    assert "new maplibregl.Popup" in script
    assert "new maplibregl.Marker" in script
    assert "fitBounds" in script


def test_map_script_preserves_google_maps_link_and_validates_coordinates():
    """La nueva vista mantiene el enlace externo y valida WGS84 antes de mapear."""
    script = _extension_script()

    assert "https://www.google.com/maps/search/?api=1&query=" in script
    assert "rel=\"noopener noreferrer\"" in script
    assert "lat >= -90 && lat <= 90" in script
    assert "lon >= -180 && lon <= 180" in script


def test_map_script_identifies_known_incident_types():
    """Los códigos existentes tienen una presentación humana en popup y lista."""
    script = _extension_script()

    assert "wildfire: ['🔥', 'Incendio forestal']" in script
    assert "road_closed: ['🚧', 'Corte de carretera']" in script
    assert "traffic_collision: ['🚗', 'Colisión de tráfico']" in script
    assert "flood: ['🌊', 'Inundación']" in script
    assert "earthquake: ['🌍', 'Terremoto']" in script
