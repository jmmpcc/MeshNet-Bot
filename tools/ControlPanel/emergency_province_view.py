"""Vista adicional, de solo lectura, de incidencias actuales por provincia.

Esta extensión se aplica sobre la app FastAPI ya creada por ``web_admin``. No modifica
la configuración de recogida, la matriz de propagación, el journal ni ningún flujo de
envío. Su única fuente de datos es ``emergencias.storage.load_current()``.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from fastapi import FastAPI
from fastapi.responses import Response


def build_emergency_snapshot(events: Iterable[Any]) -> dict[str, Any]:
    """Construye la instantánea mínima usada por el filtro visual del Control Panel.

    Args:
        events: iterable de objetos ``Event`` actuales de ``emergencias_guardia``.

    Returns:
        Diccionario JSON-serializable con eventos, provincias disponibles y recuento.

    La función no modifica los eventos recibidos. Sólo expone los campos necesarios
    para la visualización, manteniendo ``current.json`` como única fuente de verdad.
    """
    rows: list[dict[str, Any]] = []
    provinces: set[str] = set()

    for event in events:
        province = str(getattr(event, "province", "") or "").strip()
        if province:
            provinces.add(province)

        rows.append({
            "event_id": str(getattr(event, "event_id", "") or ""),
            "title": str(getattr(event, "title", "") or ""),
            "description": str(getattr(event, "description", "") or ""),
            "source": str(getattr(event, "source", "") or ""),
            "status": str(getattr(event, "status", "") or ""),
            "severity": str(getattr(event, "severity", "") or ""),
            "municipality": str(getattr(event, "municipality", "") or ""),
            "province": province,
            "road": str(getattr(event, "road", "") or ""),
            "kilometre": getattr(event, "kilometre", None),
            "latitude": getattr(event, "latitude", None),
            "longitude": getattr(event, "longitude", None),
            "updated_at": str(
                getattr(event, "updated_at", "")
                or getattr(event, "last_seen", "")
                or ""
            ),
        })

    rows.sort(key=lambda item: item["updated_at"], reverse=True)
    return {
        "ok": True,
        "total": len(rows),
        "provinces": sorted(provinces, key=str.casefold),
        "events": rows,
    }


def _extension_script() -> str:
    """Devuelve el JavaScript autocontenido que añade el filtro al Resumen.

    Se inyecta como script independiente para no reescribir ``web_admin.py``. El script
    espera a que el panel dinámico de Emergencias exista y, si el DOM se reconstruye,
    vuelve a insertar únicamente su bloque sin duplicarlo.
    """
    return r"""
<script id="meshnet-emergency-province-view">
(() => {
  let snapshot = {events: [], provinces: []};
  const escLocal = value => String(value ?? '').replace(/[&<>\"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]));
  const severityLabel = value => ({critical:'Crítica',high:'Alta',medium:'Media',low:'Baja'}[String(value || '').toLowerCase()] || value || 'Sin nivel');

  function renderEmergencyProvinceEvents() {
    const body = document.querySelector('#emergency-province-events');
    const count = document.querySelector('#emergency-province-count');
    if (!body || !count) return;
    const province = document.querySelector('#emergency-province-select')?.value || '';
    const severity = document.querySelector('#emergency-severity-select')?.value || '';
    const rows = (snapshot.events || []).filter(event =>
      (!province || event.province === province) &&
      (!severity || String(event.severity || '').toLowerCase() === severity)
    );
    count.textContent = `${rows.length} de ${snapshot.events.length} incidencias visibles`;
    if (!rows.length) {
      body.innerHTML = '<p class="hint">No hay incidencias que coincidan con los filtros seleccionados.</p>';
      return;
    }
    body.innerHTML = rows.map(event => {
      const place = [event.municipality, event.province].filter(Boolean).filter((v,i,a) => a.indexOf(v) === i).join(' · ');
      const road = event.road ? `${event.road}${event.kilometre != null ? ` · km ${event.kilometre}` : ''}` : '';
      const coords = event.latitude != null && event.longitude != null
        ? `<a href="https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(event.latitude + ',' + event.longitude)}" target="_blank" rel="noopener">📍 ${escLocal(Number(event.latitude).toFixed(5))}, ${escLocal(Number(event.longitude).toFixed(5))}</a>`
        : '';
      return `<div class="status-card" style="margin-top:8px;align-items:flex-start">
        <span class="dot ${String(event.severity).toLowerCase()==='critical'?'warn':'ok'}"></span>
        <div style="min-width:0;flex:1">
          <strong>${escLocal(event.title || 'Incidencia')}</strong>
          <div class="hint">${escLocal(place || 'Provincia no indicada')} · ${escLocal(severityLabel(event.severity))} · ${escLocal(event.status || 'sin estado')}</div>
          ${road ? `<div class="hint">${escLocal(road)}</div>` : ''}
          ${event.description ? `<div style="margin-top:4px">${escLocal(event.description)}</div>` : ''}
          ${coords ? `<div style="margin-top:5px">${coords}</div>` : ''}
        </div>
      </div>`;
    }).join('');
  }

  async function loadEmergencyProvinceEvents() {
    const body = document.querySelector('#emergency-province-events');
    if (body) body.innerHTML = '<p class="hint">Cargando incidencias actuales…</p>';
    try {
      const response = await fetch('/api/emergencias/current-view', {headers: {'Accept':'application/json'}});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'No se pudieron leer las incidencias actuales');
      snapshot = data;
      const select = document.querySelector('#emergency-province-select');
      if (select) {
        const previous = select.value;
        select.innerHTML = '<option value="">Todas las provincias</option>' +
          (data.provinces || []).map(p => `<option value="${escLocal(p)}">${escLocal(p)}</option>`).join('');
        if ([...select.options].some(option => option.value === previous)) select.value = previous;
      }
      renderEmergencyProvinceEvents();
    } catch (error) {
      if (body) body.innerHTML = `<p class="hint">${escLocal(error.message)}</p>`;
    }
  }

  function ensureEmergencyProvincePanel() {
    const overview = document.querySelector('#overview-emergencias_guardia');
    if (!overview || document.querySelector('#emergency-province-view')) return;
    const section = document.createElement('div');
    section.id = 'emergency-province-view';
    section.className = 'config-section';
    section.style.marginTop = '14px';
    section.innerHTML = `<div class="row"><div><strong>Incidencias actuales</strong><div id="emergency-province-count" class="hint">Cargando…</div></div><button class="secondary" id="emergency-province-refresh">Actualizar</button></div>
      <div class="channel-grid" style="margin-top:8px">
        <label>Provincia<select id="emergency-province-select"><option value="">Todas las provincias</option></select></label>
        <label>Severidad<select id="emergency-severity-select"><option value="">Todas las severidades</option><option value="critical">Crítica</option><option value="high">Alta</option><option value="medium">Media</option><option value="low">Baja</option></select></label>
      </div>
      <div id="emergency-province-events"><p class="hint">Cargando incidencias actuales…</p></div>`;
    overview.appendChild(section);
    section.querySelector('#emergency-province-select').addEventListener('change', renderEmergencyProvinceEvents);
    section.querySelector('#emergency-severity-select').addEventListener('change', renderEmergencyProvinceEvents);
    section.querySelector('#emergency-province-refresh').addEventListener('click', loadEmergencyProvinceEvents);
    loadEmergencyProvinceEvents();
  }

  ensureEmergencyProvincePanel();
  setInterval(ensureEmergencyProvincePanel, 1000);
})();
</script>
"""


def apply_emergency_province_view(app: FastAPI) -> FastAPI:
    """Añade la vista provincial a una app ControlPanel existente.

    Args:
        app: aplicación FastAPI creada por ``web_admin`` y ya ampliada, si procede,
            por otras extensiones del Control Panel.

    Returns:
        La misma instancia de FastAPI. La función es idempotente.

    Se registra un endpoint GET de solo lectura y un middleware limitado a la raíz HTML.
    El middleware no altera APIs, respuestas JSON ni acciones mutantes.
    """
    if getattr(app.state, "emergency_province_view_installed", False):
        return app
    app.state.emergency_province_view_installed = True

    @app.get("/api/emergencias/current-view")
    def emergency_current_view() -> dict[str, Any]:
        from tools.emergencias_guardia.emergencias.storage import load_current

        return build_emergency_snapshot(load_current().values())

    script = _extension_script()

    @app.middleware("http")
    async def inject_emergency_province_view(request, call_next):
        response = await call_next(request)
        if request.url.path != "/":
            return response
        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type.lower():
            return response

        body = b"".join([chunk async for chunk in response.body_iterator])
        html = body.decode("utf-8", errors="replace")
        if "meshnet-emergency-province-view" not in html:
            html = html.replace("</body>", script + "</body>")

        headers = {key: value for key, value in response.headers.items() if key.lower() != "content-length"}
        return Response(
            content=html,
            status_code=response.status_code,
            headers=headers,
            media_type="text/html",
        )

    return app
