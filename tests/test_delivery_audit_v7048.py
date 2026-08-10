from __future__ import annotations

import csv
import io
import os
from pathlib import Path

from shared import delivery_audit as audit


def configure(monkeypatch, tmp_path: Path) -> Path:
    db = tmp_path / "delivery.db"
    monkeypatch.setenv("DELIVERY_AUDIT_ENABLED", "1")
    monkeypatch.setenv("DELIVERY_AUDIT_DB", str(db))
    monkeypatch.setenv("DELIVERY_AUDIT_RETENTION_DAYS", "90")
    audit._LAST_CLEANUP_MONOTONIC = 0.0
    return db


def test_groups_same_operation_and_reports_partial(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    op = "op-123"
    assert audit.audit_delivery(app="emergencias", source="DGT", operation_id=op,
                                event_id="E1", transport="meshcore", destination="channel:3",
                                message="Corte CV-128", result="sent")
    assert audit.audit_delivery(app="emergencias", source="DGT", operation_id=op,
                                event_id="E1", transport="aprsis", destination="BLN0",
                                message="Corte CV-128", result="failed", result_detail="socket")
    data = audit.query_operations(hours=0)
    assert data["ok"] is True
    assert len(data["operations"]) == 1
    assert data["operations"][0]["result"] == "partial"
    assert set(data["operations"][0]["transports"]) == {"meshcore", "aprsis"}


def test_filters_and_facets_are_generic(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    audit.audit_delivery(app="farmacias", source="Ayuntamiento de Zaragoza",
                         operation_id="farma-1", transport="meshcore", destination="channel:1",
                         message="FARMACIAS GUARDIA", result="sent")
    audit.audit_delivery(app="emergencias", source="IGN", operation_id="em-1",
                         event_id="IGN-1", transport="meshtastic", destination="channel:0",
                         message="Terremoto Huesca", result="sent")
    data = audit.query_operations(application="farmacias", hours=0)
    assert [x["app"] for x in data["operations"]] == ["farmacias"]
    assert "farmacias" in data["facets"]["applications"]
    assert "emergencias" in data["facets"]["applications"]
    assert "meshcore" in data["facets"]["transports"]


def test_result_normalization_preserves_operational_reasons():
    assert audit.result_from_response({"ok": True}) == "sent"
    assert audit.result_from_response({"ok": False, "error": "boom"}) == "failed"
    assert audit.result_from_response({"ok": True, "sent": False, "reason": "duplicate"}) == "duplicate"
    assert audit.result_from_response({"ok": True, "sent": False, "reason": "rate_limited"}) == "rate_limited"
    assert audit.result_from_response({"ok": True, "skipped": True, "error": "apps_aprs_disabled"}) == "skipped"


def test_sqlite_failure_is_best_effort(monkeypatch, tmp_path):
    monkeypatch.setenv("DELIVERY_AUDIT_ENABLED", "1")
    # Una carpeta usada como fichero SQLite provoca fallo, que nunca debe escapar.
    monkeypatch.setenv("DELIVERY_AUDIT_DB", str(tmp_path))
    assert audit.audit_delivery(app="test", transport="meshcore", result="sent", message="x") is False


def test_csv_export_contains_physical_deliveries(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    audit.audit_delivery(app="emergencias", source="DGT", operation_id="csv-1",
                         transport="aprs_rf", destination="broadcast", message="Prueba", result="sent")
    rows = list(csv.reader(io.StringIO(audit.export_operations_csv(hours=0))))
    assert rows[0][0] == "fecha_utc"
    assert any("aprs_rf" in row for row in rows[1:])


def test_controlpanel_contains_global_delivery_view():
    source = Path("tools/ControlPanel/web_admin.py").read_text(encoding="utf-8")
    assert "Mensajes emitidos" in source
    assert '/api/delivery-audit' in source
    assert "UI 2 · v7.0.48" in source


def test_emergency_dispatcher_keeps_optional_operation_id():
    source = Path("tools/emergencias_guardia/emergencias/emergency_dispatcher.py").read_text(encoding="utf-8")
    assert "operation_id: str | None = None" in source
    assert "send_aprsis_emergency_bulletin" not in source or "_send_aprsis_bulletin" in source
