"""Pruebas de regresión para fecha/hora de incidencias y auditoría desplegable."""
from __future__ import annotations

from types import SimpleNamespace

from tools.ControlPanel.delivery_audit_collapsible import (
    _delivery_audit_collapsible_script,
)
from tools.ControlPanel.emergency_province_view import (
    _extension_script,
    build_emergency_snapshot,
)


def test_emergency_snapshot_exposes_started_at_without_removing_updated_at() -> None:
    """La vista debe conservar inicio y actualización para cada incidencia."""
    event = SimpleNamespace(
        event_id="event-1",
        title="Incidencia de prueba",
        description="",
        source="test",
        category="road_closed",
        status="active",
        severity="high",
        municipality="Zaragoza",
        province="Zaragoza",
        road="A-2",
        kilometre=315.0,
        latitude=41.65,
        longitude=-0.88,
        started_at="2026-08-16T14:10:00+00:00",
        updated_at="2026-08-16T14:15:00+00:00",
        last_seen="2026-08-16T14:20:00+00:00",
    )

    snapshot = build_emergency_snapshot([event])

    assert snapshot["events"][0]["started_at"] == "2026-08-16T14:10:00+00:00"
    assert snapshot["events"][0]["updated_at"] == "2026-08-16T14:15:00+00:00"


def test_emergency_script_renders_datetime_in_shared_list_and_map_summary() -> None:
    """Lista y popup deben compartir el mismo bloque con fecha/hora visible."""
    script = _extension_script()

    assert "function emergencyDateTimePresentation(event)" in script
    assert "event?.started_at || event?.updated_at" in script
    assert "<strong>Fecha / hora:</strong>" in script
    assert "emergencyEventSummaryHtml(event, false)" in script
    assert "emergencyEventSummaryHtml(event, true)" in script


def test_delivery_audit_extension_moves_existing_dom_into_closed_details() -> None:
    """La auditoría se pliega reutilizando sus nodos, sin recrear filtros ni tabla."""
    script = _delivery_audit_collapsible_script()

    assert "document.querySelector('section.audit-shell')" in script
    assert "document.createElement('details')" in script
    assert "while (section.firstChild) content.appendChild(section.firstChild);" in script
    assert "details.open ? 'OCULTAR' : 'DESPLEGAR'" in script
    assert "details.open = true" not in script
