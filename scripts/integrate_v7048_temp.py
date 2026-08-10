from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: str, old: str, new: str) -> None:
    p = ROOT / path
    text = p.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"anchor not found in {path}: {old[:100]!r}")
    if text.count(old) != 1:
        raise RuntimeError(f"anchor not unique in {path}: {old[:100]!r}")
    p.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------
# 1. Journal común: nuevo módulo completamente desacoplado.
# ---------------------------------------------------------------------------
shared = ROOT / "shared" / "delivery_audit.py"
shared.write_text(r'''"""Journal común y best-effort de entregas de aplicaciones MeshNet-Bot.

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
''', encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------
# 2. Emergencias: registrar resultados sin modificar decisiones/transporte.
# ---------------------------------------------------------------------------
replace_once(
    "tools/emergencias_guardia/emergencias/emergency_dispatcher.py",
    "from .models import Event, SEVERITY_RANK\n",
    "from .models import Event, SEVERITY_RANK\nfrom shared.delivery_audit import audit_delivery, new_operation_id, result_from_response\n",
)

p = ROOT / "tools/emergencias_guardia/emergencias/emergency_dispatcher.py"
t = p.read_text(encoding="utf-8")
start = t.index("def dispatch_secondary_outputs(")
replacement = r'''def dispatch_secondary_outputs(
    event: Event,
    message: str,
    operation_id: str | None = None,
) -> dict[str, Any]:
    """Distribuye una emergencia por las salidas secundarias configuradas.

    Debe llamarse únicamente después de una entrega Mesh correcta. Cada salida
    se evalúa de forma independiente. Una excepción APRS-IS se encapsula en el
    resultado y nunca se propaga hacia el flujo principal.

    ``operation_id`` es opcional y solo enlaza observabilidad. Si se omite se
    crea uno nuevo; no interviene en deduplicación, MIN_INTERVAL ni transporte.
    """
    op_id = operation_id or new_operation_id("emergencias")
    try:
        aprs_rf = _send_aprs_rf(event, message)
    except Exception as exc:  # noqa: BLE001 - aislamiento deliberado de salida secundaria
        aprs_rf = {
            "ok": False,
            "sent": False,
            "reason": "request_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
    try:
        aprsis = _send_aprsis_bulletin(event, message)
    except Exception as exc:  # noqa: BLE001 - aislamiento deliberado de salida secundaria
        aprsis = {
            "ok": False,
            "sent": False,
            "reason": "request_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }

    # Se conserva la llamada histórica fuera de un try propio: la auditoría no
    # cambia el comportamiento previo de Voice RF ante una excepción inesperada.
    voice_rf = _voice_result(event, message)
    result = DispatchResult(
        aprs_rf=aprs_rf,
        aprsis_bulletin=aprsis,
        voice_rf=voice_rf,
    ).to_dict()

    audit_items = (
        ("aprs_rf", aprs_rf, str(aprs_rf.get("dest") or os.getenv("APRS_EMERG_DEST", "broadcast"))),
        ("aprsis", aprsis, str(aprsis.get("bulletin") or "boletin")),
        ("voice_rf", voice_rf, "servicio-voz"),
    )
    for transport, response, destination in audit_items:
        detail = str(response.get("reason") or response.get("error") or "")
        parts = response.get("rf_parts", response.get("parts", response.get("chunks")))
        audit_delivery(
            app="emergencias",
            source=event.source,
            event_id=event.event_id,
            operation_id=op_id,
            category=event.category,
            severity=event.severity,
            status=event.status,
            transport=transport,
            destination=destination,
            message=str(response.get("aprs_text") or response.get("text") or message),
            result=result_from_response(response),
            result_detail=detail,
            parts=parts if isinstance(parts, int) else None,
            metadata={"response": response, "secondary": True},
        )
    return result
'''
p.write_text(t[:start] + replacement, encoding="utf-8", newline="\n")


# notifier imports + helper
replace_once(
    "tools/emergencias_guardia/emergencias/notifier.py",
    "from .storage import load_state, save_state\n",
    "from .storage import load_state, save_state\nfrom shared.delivery_audit import audit_delivery, new_operation_id, result_from_response\n",
)

p = ROOT / "tools/emergencias_guardia/emergencias/notifier.py"
t = p.read_text(encoding="utf-8")
helper_anchor = "\ndef _send_message(\n"
helper = r'''

def _audit_mesh_delivery(
    *,
    operation_id: str,
    event: Event | None,
    target: dict[str, Any],
    message: str,
    response: dict[str, Any],
    route: str,
    change: str = "",
) -> None:
    """Registra una entrega Mesh ya resuelta sin intervenir en el envío.

    ``event`` puede ser ``None`` en una difusión agrupada con varios eventos.
    El helper común absorbe cualquier error SQLite, por lo que esta llamada es
    siempre best-effort.
    """
    audit_delivery(
        app="emergencias",
        source=event.source if event else "multiple",
        event_id=event.event_id if event else "",
        operation_id=operation_id,
        category=event.category if event else route,
        severity=event.severity if event else "",
        status=event.status if event else "",
        transport=str(target.get("network") or "mesh"),
        destination=f"channel:{target.get('channel')}",
        channel=target.get("channel"),
        message=message,
        result=result_from_response(response),
        result_detail=str(response.get("reason") or response.get("error") or ""),
        metadata={"response": response, "route": route, "change": change},
    )
'''
if helper_anchor not in t:
    raise RuntimeError("notifier helper anchor not found")
t = t.replace(helper_anchor, helper + helper_anchor, 1)

# send_route: operation + auditable individual broker result
old = '''    results = []\n    delay = max(0.0, float(notifications.get("inter_message_delay_seconds", 8)))\n    for destination in targets:\n        for index, message in enumerate(messages):\n            response = _send_message(config, destination, message)\n            if not response.get("ok"):\n                raise RuntimeError(\n                    f"broker rechazó mensaje {index + 1} ({destination['network']}): {response}"\n                )\n            results.append({"target": destination, "response": response})\n'''
new = '''    results = []\n    operation_id = new_operation_id("emergencias")\n    delay = max(0.0, float(notifications.get("inter_message_delay_seconds", 8)))\n    audit_event = selected[0] if len(selected) == 1 else None\n    for destination in targets:\n        for index, message in enumerate(messages):\n            try:\n                response = _send_message(config, destination, message)\n            except Exception as exc:\n                response = {"ok": False, "sent": False, "reason": "request_failed", "error": f"{type(exc).__name__}: {exc}"}\n                _audit_mesh_delivery(operation_id=operation_id, event=audit_event, target=destination, message=message, response=response, route=route)\n                raise\n            _audit_mesh_delivery(operation_id=operation_id, event=audit_event, target=destination, message=message, response=response, route=route)\n            if not response.get("ok"):\n                raise RuntimeError(\n                    f"broker rechazó mensaje {index + 1} ({destination['network']}): {response}"\n                )\n            results.append({"target": destination, "response": response})\n'''
if old not in t:
    raise RuntimeError("send_route delivery block not found")
t = t.replace(old, new, 1)
t = t.replace(
    "secondary_outputs.append(dispatch_secondary_outputs(event, event_message))",
    "secondary_outputs.append(dispatch_secondary_outputs(event, event_message, operation_id=operation_id))",
    1,
)

# Incremental: un operation_id por evento y transporte, manteniendo reintentos.
old = '''        for index, (item, event, message) in enumerate(zip(batch, events, messages)):\n            try:\n                for destination in targets:\n                    response = _send_message(config, destination, message)\n                    if not response.get("ok"):\n                        raise RuntimeError(\n                            f"broker rechazó el mensaje ({destination['network']}): {response}"\n                        )\n            except Exception as exc:\n'''
new = '''        for index, (item, event, message) in enumerate(zip(batch, events, messages)):\n            operation_id = new_operation_id("emergencias")\n            try:\n                for destination in targets:\n                    try:\n                        response = _send_message(config, destination, message)\n                    except Exception as exc:\n                        response = {"ok": False, "sent": False, "reason": "request_failed", "error": f"{type(exc).__name__}: {exc}"}\n                        _audit_mesh_delivery(operation_id=operation_id, event=event, target=destination, message=message, response=response, route=route, change=change)\n                        raise\n                    _audit_mesh_delivery(operation_id=operation_id, event=event, target=destination, message=message, response=response, route=route, change=change)\n                    if not response.get("ok"):\n                        raise RuntimeError(\n                            f"broker rechazó el mensaje ({destination['network']}): {response}"\n                        )\n            except Exception as exc:\n'''
if old not in t:
    raise RuntimeError("incremental delivery block not found")
t = t.replace(old, new, 1)
t = t.replace(
    "secondary = dispatch_secondary_outputs(event, message)",
    "secondary = dispatch_secondary_outputs(event, message, operation_id=operation_id)",
    1,
)
p.write_text(t, encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------
# 3. Farmacias: broadcast, DM y APRS auditados tras la respuesta existente.
# ---------------------------------------------------------------------------
replace_once(
    "tools/farmacias_guardia/farmacias_guardia.py",
    "from shared.app_aprs_dispatcher import send_application_aprs\n",
    "from shared.app_aprs_dispatcher import send_application_aprs\nfrom shared.delivery_audit import audit_delivery, new_operation_id, result_from_response\n",
)

p = ROOT / "tools/farmacias_guardia/farmacias_guardia.py"
t = p.read_text(encoding="utf-8")
anchor = "\ndef send_broadcast_message(network: str, channel: int, message: str) -> tuple[dict[str, Any], str, int]:\n"
helper = r'''

def _farmacias_source_label() -> str:
    """Etiqueta legible de la fuente para el journal de entregas."""
    return str(os.getenv("FARMACIAS_SOURCE_LABEL", "Ayuntamiento de Zaragoza") or "Ayuntamiento de Zaragoza").strip()


def _audit_farmacias_delivery(
    *, operation_id: str, transport: str, destination: str, message: str,
    response: dict[str, Any], channel: int | str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Registra el resultado sin permitir que la auditoría altere Farmacias."""
    audit_delivery(
        app="farmacias",
        source=_farmacias_source_label(),
        operation_id=operation_id,
        transport=transport,
        destination=destination,
        channel=channel,
        message=message,
        result=result_from_response(response),
        result_detail=str(response.get("reason") or response.get("error") or ""),
        parts=response.get("chunks") if isinstance(response.get("chunks"), int) else None,
        metadata={"response": response, **(metadata or {})},
    )
'''
if anchor not in t:
    raise RuntimeError("farmacias audit helper anchor missing")
t = t.replace(anchor, helper + anchor, 1)

# Replace _send_to_targets completely up to farmacias_aprs_summary.
start = t.index("def _send_to_targets(")
end = t.index("\ndef farmacias_aprs_summary(", start)
new_send_targets = r'''def _send_to_targets(pharmacies: list[Pharmacy] | None = None, header: str | None = None) -> dict[str, Any]:
    """Difunde a los destinos existentes y audita cada fragmento aceptado/rechazado."""
    delay = max(0, int(os.getenv("FARMACIAS_INTER_MESSAGE_DELAY_SECONDS", "8")))
    deliveries = []
    operation_id = new_operation_id("farmacias")
    for network, channel in broadcast_targets():
        if channel < 0:
            raise RuntimeError(f"canal FARMACIAS no configurado para {network}")
        messages, results = broadcast_messages(network, pharmacies, header), []
        actual_network, actual_channel = network, channel
        for index, message in enumerate(messages):
            try:
                response, actual_network, actual_channel = send_broadcast_message(network, channel, message)
            except Exception as exc:
                response = {"ok": False, "sent": False, "reason": "request_failed", "error": f"{type(exc).__name__}: {exc}"}
                _audit_farmacias_delivery(
                    operation_id=operation_id, transport=network,
                    destination=f"channel:{channel}", channel=channel,
                    message=message, response=response,
                    metadata={"fragment": index + 1, "broadcast": True},
                )
                raise
            _audit_farmacias_delivery(
                operation_id=operation_id, transport=actual_network,
                destination=f"channel:{actual_channel}", channel=actual_channel,
                message=message, response=response,
                metadata={"fragment": index + 1, "broadcast": True, "requested_network": network},
            )
            if not response.get("ok"):
                raise RuntimeError(f"broker rechazó fragmento {index + 1} ({network}): {response}")
            results.append(response)
            if index + 1 < len(messages) and delay:
                time.sleep(delay)
        deliveries.append({"network": actual_network, "channel": actual_channel, "messages": len(messages), "results": results})
    return {"sent": True, **deliveries[0], "deliveries": deliveries, "operation_id": operation_id}
'''
t = t[:start] + new_send_targets + t[end:]

# Replace send_farmacias_aprs completely.
start = t.index("def send_farmacias_aprs(")
end = t.index("\ndef send_current(", start)
new_aprs = r'''def send_farmacias_aprs(
    *,
    pharmacies: list[Pharmacy] | None = None,
    requested: bool = False,
    operation_id: str | None = None,
) -> dict[str, Any]:
    """Solicita APRS y registra su resultado sin alterar sus autorizaciones."""
    op_id = operation_id or new_operation_id("farmacias")
    text = farmacias_aprs_summary(pharmacies)
    if not env_bool("FARMACIAS_APRS_ENABLED", "0"):
        result = {"ok": True, "skipped": True, "error": "farmacias_aprs_disabled"}
    elif not requested and not env_bool("FARMACIAS_APRS_AUTOMATIC", "0"):
        result = {
            "ok": True,
            "skipped": True,
            "error": "farmacias_aprs_automatic_disabled",
        }
    else:
        result = send_application_aprs(
            source="farmacias",
            text=text,
            dest=os.getenv(
                "FARMACIAS_APRS_DESTINATION",
                os.getenv("APPS_APRS_DESTINATION", "broadcast"),
            ),
            origin="app_farmacias",
        )
    _audit_farmacias_delivery(
        operation_id=op_id,
        transport="aprs",
        destination=str(result.get("dest") or os.getenv("FARMACIAS_APRS_DESTINATION", "broadcast")),
        message=text,
        response=result,
        metadata={"requested": requested},
    )
    return result
'''
t = t[:start] + new_aprs + t[end:]
t = t.replace(
    'delivery["aprs"] = send_farmacias_aprs(requested=aprs_requested)',
    'delivery["aprs"] = send_farmacias_aprs(requested=aprs_requested, operation_id=delivery.get("operation_id"))',
    1,
)

# Replace DM helper: misma semántica, solo journal posterior.
start = t.index("def _send_meshcore_dm(")
end = t.index("\ndef _handle_broker_event(", start)
new_dm = r'''def _send_meshcore_dm(contact_prefix: str, messages: list[str], *, send_all: bool = False) -> int:
    """Envía respuesta DM MeshCore y registra cada fragmento best-effort."""
    prefix = norm(contact_prefix)
    if not prefix:
        raise RuntimeError("evento MeshCore sin pubkey_prefix; no se puede responder por DM")

    delay = max(0.0, float(os.getenv("FARMACIAS_DM_INTER_MESSAGE_DELAY_SECONDS", "1.0")))
    max_messages = max(1, int(os.getenv("FARMACIAS_DM_MAX_MESSAGES_PER_RESPONSE", "6")))
    selected_messages = messages if send_all else messages[:max_messages]
    operation_id = new_operation_id("farmacias-dm")
    for index, message in enumerate(selected_messages):
        try:
            response = broker_request(
                "MESHCORE_SEND",
                {"kind": "contact", "contact_prefix": prefix, "text": str(message)},
            )
        except Exception as exc:
            response = {"ok": False, "sent": False, "reason": "request_failed", "error": f"{type(exc).__name__}: {exc}"}
            _audit_farmacias_delivery(
                operation_id=operation_id, transport="meshcore",
                destination=f"contact:{prefix}", message=str(message), response=response,
                metadata={"direct_reply": True, "fragment": index + 1},
            )
            raise
        _audit_farmacias_delivery(
            operation_id=operation_id, transport="meshcore",
            destination=f"contact:{prefix}", message=str(message), response=response,
            metadata={"direct_reply": True, "fragment": index + 1},
        )
        if not response.get("ok"):
            raise RuntimeError(f"broker rechazó respuesta DM {index + 1}: {response}")
        if index + 1 < len(selected_messages) and delay:
            time.sleep(delay)
    return len(selected_messages)
'''
t = t[:start] + new_dm + t[end:]
p.write_text(t, encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------
# 4. ControlPanel: API global + vista visual agradable y desacoplada.
# ---------------------------------------------------------------------------
replace_once(
    "tools/ControlPanel/web_admin.py",
    "from fastapi.responses import HTMLResponse, JSONResponse\n",
    "from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse\n",
)
replace_once(
    "tools/ControlPanel/web_admin.py",
    "from pydantic import BaseModel, Field\n",
    "from pydantic import BaseModel, Field\nfrom shared.delivery_audit import export_operations_csv, query_operations\n",
)

p = ROOT / "tools/ControlPanel/web_admin.py"
t = p.read_text(encoding="utf-8")
api_anchor = "\n    return app\n\n\nDASHBOARD = "
api = r'''

    @app.get("/api/delivery-audit")
    def get_delivery_audit(
        application: str = "",
        source: str = "",
        transport: str = "",
        result: str = "",
        q: str = "",
        hours: int = 24,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Devuelve el journal común sin depender del estado de una app concreta."""
        return query_operations(
            application=application.strip().casefold(),
            source=source.strip(),
            transport=transport.strip().casefold(),
            result=result.strip().casefold(),
            query=q.strip(),
            hours=max(0, min(hours, 24 * 365)),
            limit=max(1, min(limit, 500)),
            offset=max(0, offset),
        )

    @app.get("/api/delivery-audit/export.csv")
    def export_delivery_audit(
        application: str = "",
        source: str = "",
        transport: str = "",
        result: str = "",
        q: str = "",
        hours: int = 24,
    ) -> PlainTextResponse:
        """Exporta las entregas filtradas como CSV sin modificar el journal."""
        csv_text = export_operations_csv(
            application=application.strip().casefold(),
            source=source.strip(),
            transport=transport.strip().casefold(),
            result=result.strip().casefold(),
            query=q.strip(),
            hours=max(0, min(hours, 24 * 365)),
        )
        return PlainTextResponse(
            "\ufeff" + csv_text,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=meshnet-delivery-audit.csv"},
        )
'''
if api_anchor not in t:
    raise RuntimeError("ControlPanel API anchor missing")
t = t.replace(api_anchor, api + api_anchor, 1)

# CSS adicional antes del media query.
css_anchor = "@media(max-width:520px){.grid{grid-template-columns:1fr}header,main{padding:16px}.card{padding:17px}}"
css = r'''.audit-shell{margin-bottom:20px}.audit-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(125px,1fr));gap:10px;margin:14px 0}.audit-stat{background:#091a2a;border:1px solid var(--line);border-radius:14px;padding:13px}.audit-stat span{display:block;color:var(--muted);font-size:.76rem;text-transform:uppercase;letter-spacing:.04em}.audit-stat strong{display:block;font-size:1.45rem;margin-top:4px}.audit-filters{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:9px;margin:12px 0}.audit-filters select,.audit-filters input{width:100%;background:#173149;color:white;border:1px solid #365773;border-radius:9px;padding:9px}.audit-table-wrap{overflow:auto;border:1px solid var(--line);border-radius:14px}.audit-table{width:100%;border-collapse:collapse;min-width:900px}.audit-table th{position:sticky;top:0;background:#132d45;color:#a9bed2;text-align:left;font-size:.75rem;text-transform:uppercase;letter-spacing:.04em;padding:10px;border-bottom:1px solid var(--line)}.audit-table td{padding:11px 10px;border-bottom:1px solid #213e57;vertical-align:top}.audit-row{cursor:pointer}.audit-row:hover{background:#17314988}.audit-message{max-width:520px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.audit-detail{display:none;background:#071726}.audit-detail.open{display:table-row}.audit-detail>td{padding:14px}.delivery-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}.delivery-card{background:#10283e;border:1px solid #294a65;border-radius:12px;padding:12px}.delivery-card strong{display:block;margin-bottom:6px}.transport-chip{display:inline-block;border-radius:20px;padding:4px 8px;margin:2px;background:#203f58;color:#dceafa;font-size:.78rem}.transport-chip.meshcore{background:#154c3d;color:#8ef2ce}.transport-chip.meshtastic{background:#173f69;color:#9fd0ff}.transport-chip.aprs,.transport-chip.aprs_rf,.transport-chip.aprsis{background:#6a4218;color:#ffd39a}.result-pill{display:inline-block;border-radius:20px;padding:4px 9px;font-size:.78rem;font-weight:800}.result-pill.ok{background:#124d3d;color:#7bf0c7}.result-pill.partial{background:#66511d;color:#ffe08b}.result-pill.error{background:#5b2d32;color:#ffd4d1}.result-pill.skipped,.result-pill.duplicate,.result-pill.rate_limited{background:#33455a;color:#c7d7e5}.audit-meta{font-size:.8rem;color:var(--muted);overflow-wrap:anywhere}.audit-empty{text-align:center;padding:26px;color:var(--muted)}
'''
if css_anchor not in t:
    raise RuntimeError("ControlPanel CSS anchor missing")
t = t.replace(css_anchor, css + css_anchor, 1)

# Versión + card global antes del grid de aplicaciones.
t = t.replace("UI 2 · v7.0.47", "UI 2 · v7.0.48", 1)
html_anchor = '</section><div id="tools" class="grid">'
audit_html = r'''</section><section class="card audit-shell"><div class="row"><div><h2>Mensajes emitidos</h2><p class="sub">Histórico común de entregas de Emergencias, Farmacias y futuras aplicaciones.</p></div><button class="secondary" onclick="loadDeliveryAudit()">Actualizar</button></div><div id="audit-stats" class="audit-stats"><div class="audit-stat"><span>Estado</span><strong>…</strong></div></div><div class="audit-filters"><select id="audit-hours" onchange="loadDeliveryAudit()"><option value="24">Últimas 24 h</option><option value="168">7 días</option><option value="720">30 días</option><option value="2160">90 días</option><option value="0">Todo</option></select><select id="audit-app" onchange="loadDeliveryAudit()"><option value="">Todas las aplicaciones</option></select><select id="audit-source" onchange="loadDeliveryAudit()"><option value="">Todas las fuentes</option></select><select id="audit-transport" onchange="loadDeliveryAudit()"><option value="">Todos los medios</option></select><select id="audit-result" onchange="loadDeliveryAudit()"><option value="">Todos los resultados</option></select><input id="audit-query" placeholder="Buscar mensaje, evento o destino…" onkeydown="if(event.key==='Enter')loadDeliveryAudit()"></div><div class="toolbar"><button onclick="loadDeliveryAudit()">Aplicar filtros</button><button class="secondary" onclick="exportDeliveryAudit()">Exportar CSV</button></div><div class="audit-table-wrap"><table class="audit-table"><thead><tr><th>Fecha / hora</th><th>Aplicación</th><th>Fuente</th><th>Mensaje</th><th>Medios</th><th>Resultado</th></tr></thead><tbody id="audit-body"><tr><td colspan="6" class="audit-empty">Cargando actividad…</td></tr></tbody></table></div></section><div id="tools" class="grid">'''
if html_anchor not in t:
    raise RuntimeError("ControlPanel HTML anchor missing")
t = t.replace(html_anchor, audit_html, 1)

# JS antes de load().
js_anchor = "async function load(){loadAutoReply();"
js = r'''const auditTransportLabels={meshcore:'MeshCore',meshtastic:'Meshtastic',aprs:'APRS',aprs_rf:'APRS RF',aprsis:'APRS-IS',voice_rf:'Voz RF'};
const auditAppLabels={emergencias:'Emergencias',farmacias:'Farmacias'};
function auditFilters(){return {hours:document.querySelector('#audit-hours')?.value||'24',application:document.querySelector('#audit-app')?.value||'',source:document.querySelector('#audit-source')?.value||'',transport:document.querySelector('#audit-transport')?.value||'',result:document.querySelector('#audit-result')?.value||'',q:document.querySelector('#audit-query')?.value||''}}
function auditQueryString(){const p=new URLSearchParams(auditFilters());[...p.keys()].forEach(k=>{if(!p.get(k))p.delete(k)});return p.toString()}
function auditResultLabel(v){return ({ok:'OK',partial:'PARCIAL',error:'ERROR',skipped:'OMITIDO',duplicate:'DUPLICADO',rate_limited:'LIMITADO'}[v]||String(v||'').toUpperCase())}
function auditResultPill(v){return `<span class="result-pill ${esc(v)}">${esc(auditResultLabel(v))}</span>`}
function auditChips(items){return (items||[]).map(x=>`<span class="transport-chip ${esc(x)}">${esc(auditTransportLabels[x]||x)}</span>`).join('')}
function setAuditSelect(id,values,labelText){const n=document.querySelector(id);if(!n)return;const previous=n.value;n.innerHTML=`<option value="">${esc(labelText)}</option>`+(values||[]).map(v=>`<option value="${esc(v)}">${esc(id==='#audit-app'?(auditAppLabels[v]||v):(id==='#audit-transport'?(auditTransportLabels[v]||v):v))}</option>`).join('');if([...n.options].some(o=>o.value===previous))n.value=previous}
function toggleAuditDetail(id){const n=document.getElementById('audit-detail-'+id);if(n)n.classList.toggle('open')}
function auditDetailHtml(operation,index){const deliveries=(operation.deliveries||[]).map(d=>`<div class="delivery-card"><strong>${esc(auditTransportLabels[d.transport]||d.transport)} · ${auditResultPill(d.result)}</strong><div class="audit-meta">Destino: ${esc(d.destination||'—')}${d.channel?` · Canal: ${esc(d.channel)}`:''}</div><div class="audit-meta">${esc(new Date(d.timestamp_utc).toLocaleString('es-ES'))}${d.parts!=null?` · Partes: ${esc(d.parts)}`:''}${d.bytes!=null?` · ${esc(d.bytes)} bytes`:''}</div>${d.result_detail?`<div class="audit-meta">Detalle: ${esc(d.result_detail)}</div>`:''}<div style="margin-top:7px;overflow-wrap:anywhere">${esc(d.message||'')}</div></div>`).join('');return `<tr id="audit-detail-${index}" class="audit-detail"><td colspan="6"><div class="audit-meta">Operación: ${esc(operation.operation_id)}${operation.event_id?` · Evento: ${esc(operation.event_id)}`:''}${operation.category?` · Categoría: ${esc(catLabels[operation.category]||operation.category)}`:''}${operation.severity?` · Severidad: ${esc(operation.severity)}`:''}</div><div class="delivery-grid" style="margin-top:10px">${deliveries}</div></td></tr>`}
async function loadDeliveryAudit(){const body=document.querySelector('#audit-body'),stats=document.querySelector('#audit-stats');if(!body||!stats)return;body.innerHTML='<tr><td colspan="6" class="audit-empty">Cargando actividad…</td></tr>';try{const d=await request('/api/delivery-audit?'+auditQueryString());if(!d.ok)throw Error(d.error||'No se pudo leer el journal');setAuditSelect('#audit-app',d.facets.applications,'Todas las aplicaciones');setAuditSelect('#audit-source',d.facets.sources,'Todas las fuentes');setAuditSelect('#audit-transport',d.facets.transports,'Todos los medios');setAuditSelect('#audit-result',d.facets.results,'Todos los resultados');stats.innerHTML=[['Operaciones',d.summary.total],['Correctas',d.summary.ok],['Parciales',d.summary.partial],['Errores',d.summary.error]].map(x=>`<div class="audit-stat"><span>${esc(x[0])}</span><strong>${esc(x[1])}</strong></div>`).join('');if(!d.operations.length){body.innerHTML='<tr><td colspan="6" class="audit-empty">No hay entregas para estos filtros.</td></tr>';return}body.innerHTML=d.operations.map((o,i)=>{const when=new Date(o.timestamp_utc).toLocaleString('es-ES');return `<tr class="audit-row" onclick="toggleAuditDetail(${i})"><td>${esc(when)}</td><td><strong>${esc(auditAppLabels[o.app]||o.app)}</strong></td><td>${esc(o.source||'—')}</td><td class="audit-message" title="${esc(o.message||'')}">${esc(o.message||'—')}</td><td>${auditChips(o.transports)}</td><td>${auditResultPill(o.result)}</td></tr>${auditDetailHtml(o,i)}`}).join('')}catch(e){body.innerHTML=`<tr><td colspan="6" class="audit-empty">${esc(e.message)}</td></tr>`;stats.innerHTML='<div class="audit-stat"><span>Journal</span><strong>Sin datos</strong></div>'}}
function exportDeliveryAudit(){window.location='/api/delivery-audit/export.csv?'+auditQueryString()}
'''
if js_anchor not in t:
    raise RuntimeError("ControlPanel JS anchor missing")
t = t.replace(js_anchor, js + js_anchor.replace("loadAutoReply();", "loadAutoReply();loadDeliveryAudit();"), 1)
p.write_text(t, encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------
# 5. Configuración, versión y documentación.
# ---------------------------------------------------------------------------
p = ROOT / ".env_example"
t = p.read_text(encoding="utf-8")
if "DELIVERY_AUDIT_ENABLED=" not in t:
    t += "\n# Journal común de entregas para ControlPanel (v7.0.48).\nDELIVERY_AUDIT_ENABLED=1\nDELIVERY_AUDIT_DB=bot_data/delivery_audit.db\nDELIVERY_AUDIT_RETENTION_DAYS=90\n"
p.write_text(t, encoding="utf-8", newline="\n")

p = ROOT / "README.md"
t = p.read_text(encoding="utf-8")
t = t.replace("v7.0.47", "v7.0.48", 1)
p.write_text(t, encoding="utf-8", newline="\n")

p = ROOT / "tools/ControlPanel/README.md"
t = p.read_text(encoding="utf-8")
if "Mensajes emitidos" not in t:
    t += r'''

## Mensajes emitidos — v7.0.48

El ControlPanel incorpora un journal común de entregas en `bot_data/delivery_audit.db`.
La pantalla **Mensajes emitidos** agrupa por operación lógica y muestra fecha/hora,
aplicación, fuente, mensaje, transportes y resultado. Al desplegar una fila se ve
el detalle de cada entrega física. Los filtros permiten seleccionar aplicación,
fuente, medio, resultado y periodo, con exportación CSV.

La auditoría es estrictamente best-effort: una avería, bloqueo o falta de permisos
en SQLite nunca invalida ni retrasa el envío original de la aplicación.
'''
p.write_text(t, encoding="utf-8", newline="\n")

changelog = ROOT / "docs" / "CHANGELOG_v7.0.48.md"
changelog.write_text(r'''# v7.0.48 — Journal común de mensajes emitidos

## Objetivo

Añadir observabilidad transversal para aplicaciones independientes sin introducir
ninguna dependencia en el camino crítico de transmisión.

## Cambios

- Nuevo `shared/delivery_audit.py` con SQLite WAL, `busy_timeout` y retención configurable.
- Cada fila representa un intento de entrega por un transporte concreto.
- `operation_id` agrupa MeshCore, Meshtastic, APRS/APRS-IS y otras salidas de una misma operación.
- Estados normalizados: `sent`, `failed`, `skipped`, `duplicate`, `rate_limited`.
- Integración best-effort en Emergencias para Mesh, APRS RF, APRS-IS y Voice RF.
- Integración best-effort en Farmacias para broadcast, respuestas DM y APRS.
- Nueva sección global **Mensajes emitidos** en ControlPanel.
- Filtros por periodo, aplicación, fuente, transporte, resultado y búsqueda libre.
- Detalle desplegable por transporte y exportación CSV.
- Retención predeterminada de 90 días.

## Seguridad operativa

El journal se escribe exclusivamente después de obtener el resultado de la salida
existente. `audit_delivery()` absorbe cualquier excepción de SQLite. No modifica
broker, gateway APRS, deduplicación, intervalos, reintentos ni decisiones de ruta.
''', encoding="utf-8", newline="\n")

p = ROOT / "docs" / "Historial_Versiones.md"
t = p.read_text(encoding="utf-8")
if "## v7.0.48" not in t:
    t = "## v7.0.48 — Journal común de mensajes emitidos\n\n- SQLite común best-effort para auditoría de entregas de aplicaciones independientes.\n- ControlPanel añade vista visual de operaciones, transportes, fuentes y resultados.\n- Emergencias y Farmacias registran resultados sin alterar sus rutas ni mecanismos de envío.\n- Filtros, detalle por transporte, retención y exportación CSV.\n\n" + t
p.write_text(t, encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------
# 6. Pruebas de regresión específicas.
# ---------------------------------------------------------------------------
test = ROOT / "tests" / "test_delivery_audit_v7048.py"
test.write_text(r'''from __future__ import annotations

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
''', encoding="utf-8", newline="\n")

print("v7.0.48 integration prepared")
