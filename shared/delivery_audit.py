"""Journal común y best-effort de entregas de aplicaciones MeshNet-Bot.

El módulo registra *después* de cada intento de entrega. Nunca participa en la
selección, formateo ni transmisión de mensajes. Cualquier error SQLite se
absorbe y se devuelve como ``False`` para no alterar las aplicaciones.

Uso básico::

    operation_id = new_operation_id("emergencias")
    audit_delivery(
        app="emergencias",
        source="DGT",
        operation_id=operation_id,
        event_id="DGT-123",
        transport="meshcore",
        destination="channel:3",
        message="Corte de tráfico...",
        result="sent",
    )

El ControlPanel utiliza ``query_operations`` para agrupar por ``operation_id``
y mostrar una operación lógica con todas sus entregas físicas.
"""
from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_TRUE = {"1", "true", "yes", "on", "si", "sí", "y"}
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB = _PROJECT_ROOT / "bot_data" / "delivery_audit.db"
_SCHEMA_LOCK = threading.Lock()
_CLEANUP_LOCK = threading.Lock()
_LAST_CLEANUP_MONOTONIC = 0.0


def _enabled() -> bool:
    """Indica si la auditoría está activa. Está habilitada por defecto."""
    return str(os.getenv("DELIVERY_AUDIT_ENABLED", "1") or "1").strip().casefold() in _TRUE


def audit_db_path() -> Path:
    """Devuelve la ruta efectiva de SQLite sin depender del directorio actual."""
    raw = str(os.getenv("DELIVERY_AUDIT_DB", "") or "").strip()
    if not raw:
        return _DEFAULT_DB
    path = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    return path


def new_operation_id(prefix: str = "delivery") -> str:
    """Crea un identificador común para todas las entregas de una operación."""
    safe = "".join(ch for ch in str(prefix or "delivery").casefold() if ch.isalnum() or ch in "_-")
    return f"{safe or 'delivery'}-{uuid.uuid4().hex}"


def result_from_response(response: dict[str, Any] | None) -> str:
    """Normaliza respuestas heterogéneas a estados visuales estables.

    Prioridad: duplicate/rate_limited -> skipped -> failed -> sent. Las
    respuestas del broker que solo contienen ``ok=true`` se consideran
    aceptadas/sent, conservando su semántica actual.
    """
    data = response if isinstance(response, dict) else {}
    reason = str(data.get("reason") or data.get("error") or "").strip().casefold()
    if data.get("duplicate") or reason == "duplicate" or "duplicat" in reason:
        return "duplicate"
    if "rate_limited" in reason or "rate limited" in reason:
        return "rate_limited"
    if data.get("skipped") or reason in {
        "disabled", "automatic_disabled", "service_disabled",
        "severity_below_threshold", "notifications_disabled", "unchanged",
        "no_eligible_events", "source_not_allowed", "apps_aprs_disabled",
        "farmacias_aprs_disabled", "farmacias_aprs_automatic_disabled",
    }:
        return "skipped"
    if data.get("ok") is False:
        return "failed"
    if data.get("sent") is False and reason and reason not in {""}:
        return "skipped" if data.get("ok", True) else "failed"
    if data.get("sent") is True:
        return "sent"
    return "sent" if data.get("ok") else "failed"


def _connect(path: Path | None = None) -> sqlite3.Connection:
    db_path = path or audit_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path), timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=5000")
    try:
        connection.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        pass
    connection.execute("PRAGMA synchronous=NORMAL")
    return connection


def _ensure_schema(connection: sqlite3.Connection) -> None:
    with _SCHEMA_LOCK:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS delivery_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp_utc TEXT NOT NULL,
            app TEXT NOT NULL,
            source TEXT,
            event_id TEXT,
            operation_id TEXT NOT NULL,
            category TEXT,
            severity TEXT,
            status TEXT,
            transport TEXT NOT NULL,
            destination TEXT,
            channel TEXT,
            message TEXT,
            result TEXT NOT NULL,
            result_detail TEXT,
            parts INTEGER,
            bytes INTEGER,
            metadata_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_delivery_timestamp
            ON delivery_audit(timestamp_utc DESC);
        CREATE INDEX IF NOT EXISTS idx_delivery_operation
            ON delivery_audit(operation_id, timestamp_utc DESC);
        CREATE INDEX IF NOT EXISTS idx_delivery_app
            ON delivery_audit(app, timestamp_utc DESC);
        CREATE INDEX IF NOT EXISTS idx_delivery_transport
            ON delivery_audit(transport, timestamp_utc DESC);
        CREATE INDEX IF NOT EXISTS idx_delivery_result
            ON delivery_audit(result, timestamp_utc DESC);
        """)
        connection.commit()


def _cleanup_if_due(connection: sqlite3.Connection) -> None:
    """Aplica retención como máximo una vez por hora por proceso."""
    global _LAST_CLEANUP_MONOTONIC
    now_mono = time.monotonic()
    if now_mono - _LAST_CLEANUP_MONOTONIC < 3600:
        return
    with _CLEANUP_LOCK:
        if now_mono - _LAST_CLEANUP_MONOTONIC < 3600:
            return
        try:
            days = max(1, int(os.getenv("DELIVERY_AUDIT_RETENTION_DAYS", "90") or "90"))
        except (TypeError, ValueError):
            days = 90
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        connection.execute("DELETE FROM delivery_audit WHERE timestamp_utc < ?", (cutoff,))
        connection.commit()
        _LAST_CLEANUP_MONOTONIC = now_mono


def audit_delivery(
    *,
    app: str,
    transport: str,
    result: str,
    operation_id: str | None = None,
    source: str = "",
    event_id: str = "",
    category: str = "",
    severity: str = "",
    status: str = "",
    destination: str = "",
    channel: str | int | None = None,
    message: str = "",
    result_detail: str = "",
    parts: int | None = None,
    metadata: dict[str, Any] | None = None,
    timestamp_utc: str | None = None,
) -> bool:
    """Registra una entrega sin permitir que SQLite afecte al flujo llamante.

    Todos los parámetros son datos de observabilidad. La función nunca relanza
    errores de disco, permisos, bloqueo, serialización o esquema.
    """
    if not _enabled():
        return False
    app_value = str(app or "").strip().casefold()
    transport_value = str(transport or "").strip().casefold()
    result_value = str(result or "").strip().casefold()
    if not app_value or not transport_value or not result_value:
        return False
    op_value = str(operation_id or new_operation_id(app_value)).strip()
    timestamp = timestamp_utc or datetime.now(timezone.utc).isoformat()
    encoded_message = str(message or "")
    try:
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False, separators=(",", ":"), default=str)
        with _connect() as connection:
            _ensure_schema(connection)
            _cleanup_if_due(connection)
            connection.execute(
                """INSERT INTO delivery_audit (
                    timestamp_utc, app, source, event_id, operation_id, category,
                    severity, status, transport, destination, channel, message,
                    result, result_detail, parts, bytes, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    timestamp, app_value, str(source or ""), str(event_id or ""), op_value,
                    str(category or ""), str(severity or ""), str(status or ""),
                    transport_value, str(destination or ""), "" if channel is None else str(channel),
                    encoded_message, result_value, str(result_detail or ""),
                    None if parts is None else int(parts), len(encoded_message.encode("utf-8")),
                    metadata_json,
                ),
            )
            connection.commit()
        return True
    except Exception:
        return False


def _where_clause(
    *, application: str = "", source: str = "", transport: str = "",
    result: str = "", query: str = "", hours: int = 24,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    values: list[Any] = []
    if hours > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=min(hours, 24 * 365))).isoformat()
        clauses.append("timestamp_utc >= ?")
        values.append(cutoff)
    for column, value in (
        ("app", application), ("source", source), ("transport", transport), ("result", result),
    ):
        value = str(value or "").strip()
        if value:
            clauses.append(f"{column} = ?")
            values.append(value)
    q = str(query or "").strip()
    if q:
        like = f"%{q}%"
        clauses.append("(message LIKE ? OR source LIKE ? OR event_id LIKE ? OR destination LIKE ?)")
        values.extend([like, like, like, like])
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", values


def _aggregate_status(deliveries: list[dict[str, Any]]) -> str:
    statuses = {str(item.get("result") or "") for item in deliveries}
    if "failed" in statuses:
        return "partial" if "sent" in statuses else "error"
    if "sent" in statuses:
        return "ok"
    if "rate_limited" in statuses:
        return "rate_limited"
    if "duplicate" in statuses:
        return "duplicate"
    return "skipped"


def query_operations(
    *,
    application: str = "",
    source: str = "",
    transport: str = "",
    result: str = "",
    query: str = "",
    hours: int = 24,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """Consulta y agrupa el journal para consumo directo del ControlPanel."""
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    try:
        with _connect() as connection:
            _ensure_schema(connection)
            where, values = _where_clause(
                application=application, source=source, transport=transport,
                result=result, query=query, hours=hours,
            )
            rows = connection.execute(
                "SELECT * FROM delivery_audit" + where + " ORDER BY timestamp_utc DESC, id DESC LIMIT 10000",
                values,
            ).fetchall()
            facet_rows = connection.execute(
                "SELECT app, source, transport, result FROM delivery_audit ORDER BY timestamp_utc DESC LIMIT 10000"
            ).fetchall()
    except Exception as exc:
        return {
            "ok": False, "error": f"{type(exc).__name__}: {exc}", "operations": [],
            "summary": {"total": 0, "ok": 0, "partial": 0, "error": 0},
            "facets": {"applications": [], "sources": [], "transports": [], "results": []},
        }

    grouped: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in rows:
        item = dict(row)
        try:
            item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            item["metadata"] = {}
        op_id = item["operation_id"]
        if op_id not in grouped:
            grouped[op_id] = {
                "operation_id": op_id,
                "timestamp_utc": item["timestamp_utc"],
                "app": item["app"],
                "source": item["source"],
                "event_id": item["event_id"],
                "category": item["category"],
                "severity": item["severity"],
                "status": item["status"],
                "message": item["message"],
                "deliveries": [],
            }
            order.append(op_id)
        group = grouped[op_id]
        group["deliveries"].append(item)
        if not group.get("message") and item.get("message"):
            group["message"] = item["message"]
        if not group.get("source") and item.get("source"):
            group["source"] = item["source"]

    all_operations = [grouped[key] for key in order]
    for operation in all_operations:
        operation["result"] = _aggregate_status(operation["deliveries"])
        operation["transports"] = list(dict.fromkeys(
            item["transport"] for item in operation["deliveries"] if item.get("transport")
        ))
    visible = all_operations[offset:offset + limit]
    summary = {"total": len(all_operations), "ok": 0, "partial": 0, "error": 0, "other": 0}
    for operation in all_operations:
        key = operation["result"]
        if key in summary:
            summary[key] += 1
        else:
            summary["other"] += 1

    facets = {
        "applications": sorted({str(row["app"]) for row in facet_rows if row["app"]}),
        "sources": sorted({str(row["source"]) for row in facet_rows if row["source"]}),
        "transports": sorted({str(row["transport"]) for row in facet_rows if row["transport"]}),
        "results": sorted({str(row["result"]) for row in facet_rows if row["result"]}),
    }
    return {
        "ok": True,
        "operations": visible,
        "summary": summary,
        "facets": facets,
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < len(all_operations),
    }


def export_operations_csv(**filters: Any) -> str:
    """Exporta operaciones/entregas visibles a CSV UTF-8 con BOM opcional externo."""
    data = query_operations(limit=500, offset=0, **filters)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "fecha_utc", "operacion", "aplicacion", "fuente", "evento", "categoria",
        "severidad", "estado_evento", "transporte", "destino", "canal", "resultado",
        "detalle", "partes", "bytes", "mensaje",
    ])
    for operation in data.get("operations", []):
        for item in operation.get("deliveries", []):
            writer.writerow([
                item.get("timestamp_utc", ""), operation.get("operation_id", ""),
                item.get("app", ""), item.get("source", ""), item.get("event_id", ""),
                item.get("category", ""), item.get("severity", ""), item.get("status", ""),
                item.get("transport", ""), item.get("destination", ""), item.get("channel", ""),
                item.get("result", ""), item.get("result_detail", ""), item.get("parts", ""),
                item.get("bytes", ""), item.get("message", ""),
            ])
    return output.getvalue()
