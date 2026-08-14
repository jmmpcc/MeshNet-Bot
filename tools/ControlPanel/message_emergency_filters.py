"""Filtros adicionales de Emergencias para la vista existente de Mensajes enviados.

Esta extensión amplía el Control Panel sin modificar ``web_admin.py``. Reutiliza el
journal común mediante ``query_operations`` y la fuente estructurada de incidencias
actuales mediante ``load_current``. Nunca deduce provincias leyendo el texto del
mensaje y no participa en propagación, deduplicación ni transmisión.
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.responses import Response

from shared.delivery_audit import query_operations


def enrich_and_filter_operations(
    operations: list[dict[str, Any]],
    event_provinces: dict[str, str],
    *,
    province: str = "",
    severity: str = "",
    category: str = "",
) -> list[dict[str, Any]]:
    """Enriquece operaciones con provincia y aplica filtros estructurados.

    Args:
        operations: operaciones lógicas devueltas por ``query_operations``.
        event_provinces: mapa ``event_id -> province`` construido desde ``current.json``.
        province: provincia exacta solicitada; vacío significa todas.
        severity: severidad normalizada; vacío significa todas.
        category: categoría técnica exacta; vacío significa todas.

    Returns:
        Nueva lista de operaciones. Los objetos originales no se modifican.

    La provincia se obtiene primero de metadatos estructurados de cualquier entrega
    (compatibilidad futura) y después por ``event_id`` desde la instantánea actual.
    Nunca se analiza el contenido textual del mensaje.
    """
    expected_province = str(province or "").strip()
    expected_severity = str(severity or "").strip().casefold()
    expected_category = str(category or "").strip().casefold()
    result: list[dict[str, Any]] = []

    for operation in operations:
        item = dict(operation)
        resolved_province = ""
        for delivery in item.get("deliveries", []) or []:
            metadata = delivery.get("metadata") if isinstance(delivery, dict) else {}
            if isinstance(metadata, dict):
                resolved_province = str(metadata.get("province") or "").strip()
                if resolved_province:
                    break
        if not resolved_province:
            resolved_province = str(event_provinces.get(str(item.get("event_id") or ""), "") or "").strip()
        item["province"] = resolved_province

        if expected_province and resolved_province != expected_province:
            continue
        if expected_severity and str(item.get("severity") or "").casefold() != expected_severity:
            continue
        if expected_category and str(item.get("category") or "").casefold() != expected_category:
            continue
        result.append(item)

    return result


def build_extended_audit(
    *,
    application: str = "",
    source: str = "",
    transport: str = "",
    result: str = "",
    query: str = "",
    hours: int = 24,
    province: str = "",
    severity: str = "",
    category: str = "",
    limit: int = 100,
) -> dict[str, Any]:
    """Construye la respuesta ampliada usada por los filtros de Mensajes.

    Mantiene los filtros históricos en ``query_operations`` y aplica Provincia,
    Severidad y Tipo de incidencia después de agrupar operaciones. Esto evita
    modificar el esquema SQLite y conserva compatibilidad con el journal existente.
    """
    from tools.emergencias_guardia.emergencias.storage import load_current

    base = query_operations(
        application=application,
        source=source,
        transport=transport,
        result=result,
        query=query,
        hours=hours,
        limit=500,
        offset=0,
    )
    if not base.get("ok"):
        return base

    current = load_current()
    event_provinces = {
        str(event.event_id): str(event.province or "").strip()
        for event in current.values()
        if getattr(event, "event_id", "")
    }
    enriched = enrich_and_filter_operations(
        list(base.get("operations", [])),
        event_provinces,
        province=province,
        severity=severity,
        category=category,
    )

    summary = {"total": len(enriched), "ok": 0, "partial": 0, "error": 0, "other": 0}
    for operation in enriched:
        key = str(operation.get("result") or "")
        summary[key if key in summary else "other"] += 1

    all_for_facets = enrich_and_filter_operations(list(base.get("operations", [])), event_provinces)
    facets = dict(base.get("facets", {}))
    facets.update({
        "provinces": sorted({str(op.get("province") or "") for op in all_for_facets if op.get("province")}, key=str.casefold),
        "severities": sorted({str(op.get("severity") or "") for op in all_for_facets if op.get("severity")}),
        "categories": sorted({str(op.get("category") or "") for op in all_for_facets if op.get("category")}),
    })

    safe_limit = max(1, min(int(limit), 500))
    return {
        **base,
        "operations": enriched[:safe_limit],
        "summary": summary,
        "facets": facets,
        "limit": safe_limit,
        "offset": 0,
        "has_more": len(enriched) > safe_limit,
    }


def _extension_script() -> str:
    """JavaScript que inserta los nuevos selectores dentro de los filtros existentes."""
    return r'''
<script id="meshnet-message-emergency-filters">
(() => {
  const categoryLabels = typeof catLabels !== 'undefined' ? catLabels : {};
  const severityLabelsLocal = {critical:'Crítica', high:'Alta', medium:'Media', low:'Baja'};

  function addOptionSelect(id, anchor, label) {
    if (document.querySelector(id) || !anchor) return document.querySelector(id);
    const select = document.createElement('select');
    select.id = id.slice(1);
    select.innerHTML = `<option value="">${label}</option>`;
    select.addEventListener('change', () => loadDeliveryAudit());
    anchor.insertAdjacentElement('afterend', select);
    return select;
  }

  function setExtendedSelect(id, values, label, labels={}) {
    const node = document.querySelector(id);
    if (!node) return;
    const previous = node.value;
    node.innerHTML = `<option value="">${label}</option>` + (values || []).map(value =>
      `<option value="${esc(value)}">${esc(labels[value] || value)}</option>`
    ).join('');
    if ([...node.options].some(option => option.value === previous)) node.value = previous;
  }

  function ensureAuditEmergencyFilters() {
    const source = document.querySelector('#audit-source');
    if (!source) return false;
    let anchor = source;
    const province = addOptionSelect('#audit-province', anchor, 'Todas las provincias');
    if (province) anchor = province;
    const severity = addOptionSelect('#audit-severity', anchor, 'Todas las severidades');
    if (severity) anchor = severity;
    addOptionSelect('#audit-category', anchor, 'Todos los tipos');
    return true;
  }

  function extendedAuditQueryString() {
    const base = auditFilters();
    base.province = document.querySelector('#audit-province')?.value || '';
    base.severity = document.querySelector('#audit-severity')?.value || '';
    base.category = document.querySelector('#audit-category')?.value || '';
    const params = new URLSearchParams(base);
    [...params.keys()].forEach(key => { if (!params.get(key)) params.delete(key); });
    return params.toString();
  }

  loadDeliveryAudit = async function() {
    const body = document.querySelector('#audit-body');
    const stats = document.querySelector('#audit-stats');
    if (!body || !stats) return;
    ensureAuditEmergencyFilters();
    body.innerHTML = '<tr><td colspan="6" class="audit-empty">Cargando actividad…</td></tr>';
    try {
      const d = await request('/api/delivery-audit-extended?' + extendedAuditQueryString());
      if (!d.ok) throw Error(d.error || 'No se pudo leer el journal');
      setAuditSelect('#audit-app', d.facets.applications, 'Todas las aplicaciones');
      setAuditSelect('#audit-source', d.facets.sources, 'Todas las fuentes');
      setAuditSelect('#audit-transport', d.facets.transports, 'Todos los medios');
      setAuditSelect('#audit-result', d.facets.results, 'Todos los resultados');
      setExtendedSelect('#audit-province', d.facets.provinces, 'Todas las provincias');
      setExtendedSelect('#audit-severity', d.facets.severities, 'Todas las severidades', severityLabelsLocal);
      setExtendedSelect('#audit-category', d.facets.categories, 'Todos los tipos', categoryLabels);
      stats.innerHTML = [['Operaciones',d.summary.total],['Correctas',d.summary.ok],['Parciales',d.summary.partial],['Errores',d.summary.error]].map(x => `<div class="audit-stat"><span>${esc(x[0])}</span><strong>${esc(x[1])}</strong></div>`).join('');
      if (!d.operations.length) {
        body.innerHTML = '<tr><td colspan="6" class="audit-empty">No hay entregas para estos filtros.</td></tr>';
        return;
      }
      body.innerHTML = d.operations.map((o,i) => {
        const when = new Date(o.timestamp_utc).toLocaleString('es-ES');
        const province = o.province ? ` · ${esc(o.province)}` : '';
        return `<tr class="audit-row" onclick="toggleAuditDetail(${i})"><td>${esc(when)}</td><td><strong>${esc(auditAppLabels[o.app]||o.app)}</strong></td><td>${esc(o.source||'—')}${province}</td><td class="audit-message" title="${esc(o.message||'')}">${esc(o.message||'—')}</td><td>${auditChips(o.transports)}</td><td>${auditResultPill(o.result)}</td></tr>${auditDetailHtml(o,i)}`;
      }).join('');
    } catch(e) {
      body.innerHTML = `<tr><td colspan="6" class="audit-empty">${esc(e.message)}</td></tr>`;
      stats.innerHTML = '<div class="audit-stat"><span>Journal</span><strong>Sin datos</strong></div>';
    }
  };

  if (ensureAuditEmergencyFilters()) loadDeliveryAudit();
  setInterval(ensureAuditEmergencyFilters, 1200);
})();
</script>
'''


def apply_message_emergency_filters(app: FastAPI) -> FastAPI:
    """Aplica los filtros de Emergencias sobre la vista de Mensajes ya existente.

    Args:
        app: instancia FastAPI del Control Panel.

    Returns:
        La misma instancia, ampliada de forma idempotente.

    Sólo registra un GET de lectura e inyecta JavaScript en la página raíz. Todas las
    acciones mutantes y rutas históricas permanecen intactas.
    """
    if getattr(app.state, "message_emergency_filters_installed", False):
        return app
    app.state.message_emergency_filters_installed = True

    @app.get('/api/delivery-audit-extended')
    def delivery_audit_extended(
        hours: int = 24,
        application: str = '',
        source: str = '',
        transport: str = '',
        result: str = '',
        q: str = '',
        province: str = '',
        severity: str = '',
        category: str = '',
        limit: int = 100,
    ) -> dict[str, Any]:
        return build_extended_audit(
            hours=hours,
            application=application,
            source=source,
            transport=transport,
            result=result,
            query=q,
            province=province,
            severity=severity,
            category=category,
            limit=limit,
        )

    script = _extension_script()

    @app.middleware('http')
    async def inject_message_emergency_filters(request, call_next):
        response = await call_next(request)
        if request.url.path != '/':
            return response
        content_type = response.headers.get('content-type', '')
        if 'text/html' not in content_type.lower():
            return response
        body = b''.join([chunk async for chunk in response.body_iterator])
        html = body.decode('utf-8', errors='replace')
        if 'meshnet-message-emergency-filters' not in html:
            html = html.replace('</body>', script + '</body>')
        headers = {key: value for key, value in response.headers.items() if key.lower() != 'content-length'}
        return Response(content=html, status_code=response.status_code, headers=headers, media_type='text/html')

    return app
