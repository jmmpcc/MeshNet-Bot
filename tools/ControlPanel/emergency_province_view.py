"""Vista de solo lectura de incidencias actuales con filtros y mapa integrado.

Esta extensión se aplica sobre la app FastAPI ya creada por ``web_admin``. No modifica
la configuración de recogida, la matriz de propagación, el journal ni ningún flujo de
envío. Su única fuente de datos es ``emergencias.storage.load_current()``.

La vista reutiliza la misma instantánea para Lista y Mapa. El mapa se carga únicamente
cuando el usuario selecciona esa vista y usa MapLibre GL JS + OpenFreeMap sin API key.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from fastapi import FastAPI
from fastapi.responses import Response


def build_emergency_snapshot(events: Iterable[Any]) -> dict[str, Any]:
    """Construye la instantánea mínima usada por la vista de Emergencias.

    Args:
        events: iterable de objetos ``Event`` actuales de ``emergencias_guardia``.

    Returns:
        Diccionario JSON-serializable con eventos, provincias, categorías y recuento.

    La función no modifica los eventos recibidos. Sólo expone los campos necesarios
    para Lista y Mapa, manteniendo ``current.json`` como única fuente de verdad.
    ``category`` se conserva con el código original del backend para no duplicar ni
    reinterpretar la taxonomía de Emergencias. ``started_at`` conserva la fecha/hora
    propia del evento; ``updated_at`` sigue disponible como respaldo y para ordenación.
    """
    rows: list[dict[str, Any]] = []
    provinces: set[str] = set()
    categories: set[str] = set()

    for event in events:
        province = str(getattr(event, "province", "") or "").strip()
        category = str(getattr(event, "category", "other") or "other").strip()
        if province:
            provinces.add(province)
        if category:
            categories.add(category)

        rows.append({
            "event_id": str(getattr(event, "event_id", "") or ""),
            "title": str(getattr(event, "title", "") or ""),
            "description": str(getattr(event, "description", "") or ""),
            "source": str(getattr(event, "source", "") or ""),
            "category": category,
            "status": str(getattr(event, "status", "") or ""),
            "severity": str(getattr(event, "severity", "") or ""),
            "municipality": str(getattr(event, "municipality", "") or ""),
            "province": province,
            "road": str(getattr(event, "road", "") or ""),
            "kilometre": getattr(event, "kilometre", None),
            "latitude": getattr(event, "latitude", None),
            "longitude": getattr(event, "longitude", None),
            "started_at": str(getattr(event, "started_at", "") or ""),
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
        "categories": sorted(categories, key=str.casefold),
        "events": rows,
    }


def _extension_script() -> str:
    """Devuelve el JavaScript autocontenido de Lista/Mapa de Emergencias.

    La función sustituye por completo la versión anterior del script porque la nueva
    interfaz comparte estado entre Lista y Mapa, incorpora un tercer filtro por tipo de
    incidencia y gestiona el ciclo de vida de MapLibre. Mantiene los filtros previos,
    el endpoint existente y el enlace externo de Google Maps.

    MapLibre GL JS se carga con versión fijada (5.16.0) sólo cuando se abre la vista Mapa.
    El estilo OpenFreeMap Liberty no requiere API key. Si el recurso cartográfico no
    puede cargarse, Lista continúa funcionando sin degradar el resto del Control Panel.
    """
    return r"""
<script id="meshnet-emergency-province-view">
(() => {
  const MAPLIBRE_JS = 'https://unpkg.com/maplibre-gl@5.16.0/dist/maplibre-gl.js';
  const MAPLIBRE_CSS = 'https://unpkg.com/maplibre-gl@5.16.0/dist/maplibre-gl.css';
  const MAP_STYLE = 'https://tiles.openfreemap.org/styles/liberty';

  let snapshot = {events: [], provinces: [], categories: []};
  let currentView = 'list';
  let emergencyMap = null;
  let emergencyMarkers = [];
  let mapAssetsPromise = null;

  const escLocal = value => String(value ?? '').replace(/[&<>\"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]));
  const severityLabel = value => ({critical:'Crítica',high:'Alta',medium:'Media',low:'Baja'}[String(value || '').toLowerCase()] || value || 'Sin nivel');
  const severityColor = value => ({critical:'#d32f2f',high:'#f57c00',medium:'#1976d2',low:'#388e3c'}[String(value || '').toLowerCase()] || '#607d8b');

  /** Presentación humana de los códigos de categoría publicados por Emergencias. */
  function categoryPresentation(value) {
    const key = String(value || 'other').trim().toLowerCase();
    const known = {
      wildfire: ['🔥', 'Incendio forestal'],
      fire: ['🔥', 'Incendio'],
      traffic_collision: ['🚗', 'Colisión de tráfico'],
      traffic_accident: ['🚗', 'Accidente de tráfico'],
      road_closed: ['🚧', 'Corte de carretera'],
      road_closure: ['🚧', 'Corte de carretera'],
      road_incident: ['🚧', 'Incidencia vial'],
      flood: ['🌊', 'Inundación'],
      earthquake: ['🌍', 'Terremoto'],
      storm: ['⛈️', 'Tormenta'],
      weather: ['🌦️', 'Fenómeno meteorológico'],
      civil_protection: ['⚠️', 'Protección Civil'],
      emergency: ['⚠️', 'Emergencia'],
      other: ['📍', 'Otra incidencia'],
    };
    if (known[key]) return {icon: known[key][0], label: known[key][1]};
    const label = key.replace(/[_-]+/g, ' ').replace(/\b\w/g, ch => ch.toUpperCase());
    return {icon: '📍', label: label || 'Otra incidencia'};
  }

  /** Devuelve los eventos que cumplen exactamente los filtros visibles. */
  function filteredEmergencyEvents() {
    const province = document.querySelector('#emergency-province-select')?.value || '';
    const severity = document.querySelector('#emergency-severity-select')?.value || '';
    const category = document.querySelector('#emergency-category-select')?.value || '';
    return (snapshot.events || []).filter(event =>
      (!province || event.province === province) &&
      (!severity || String(event.severity || '').toLowerCase() === severity) &&
      (!category || String(event.category || '') === category)
    );
  }

  /** Valida las coordenadas antes de entregarlas al motor cartográfico. */
  function validCoordinates(event) {
    const lat = Number(event?.latitude);
    const lon = Number(event?.longitude);
    return Number.isFinite(lat) && Number.isFinite(lon) &&
      lat >= -90 && lat <= 90 && lon >= -180 && lon <= 180;
  }

  /**
   * Formatea la fecha/hora de la incidencia para Lista y Mapa.
   *
   * Se prioriza ``started_at`` porque representa el comienzo publicado por la fuente.
   * Si no existe, se utiliza ``updated_at`` para no perder información en fuentes que
   * sólo facilitan una marca temporal de actualización. Los valores no parseables se
   * muestran literalmente en vez de ocultarlos.
   */
  function emergencyDateTimePresentation(event) {
    const raw = String(event?.started_at || event?.updated_at || '').trim();
    if (!raw) return '';
    const parsed = new Date(raw);
    if (Number.isNaN(parsed.getTime())) return raw;
    return new Intl.DateTimeFormat('es-ES', {
      day: '2-digit', month: '2-digit', year: 'numeric',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    }).format(parsed);
  }

  /** HTML compartido por la tarjeta de Lista y el popup del Mapa. */
  function emergencyEventSummaryHtml(event, compact = false) {
    const place = [event.municipality, event.province].filter(Boolean).filter((v,i,a) => a.indexOf(v) === i).join(' · ');
    const road = event.road ? `${event.road}${event.kilometre != null ? ` · km ${event.kilometre}` : ''}` : '';
    const category = categoryPresentation(event.category);
    const dateTime = emergencyDateTimePresentation(event);
    const coords = validCoordinates(event)
      ? `<a href="https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(event.latitude + ',' + event.longitude)}" target="_blank" rel="noopener noreferrer">📍 ${escLocal(Number(event.latitude).toFixed(5))}, ${escLocal(Number(event.longitude).toFixed(5))} · Google Maps</a>`
      : '';
    const description = !compact && event.description ? `<div style="margin-top:4px">${escLocal(event.description)}</div>` : '';
    return `<div style="min-width:${compact ? '210px' : '0'};max-width:${compact ? '300px' : 'none'}">
      <strong>${escLocal(category.icon)} ${escLocal(category.label)}</strong>
      <div style="margin-top:3px"><strong>${escLocal(event.title || 'Incidencia')}</strong></div>
      <div class="hint">${escLocal(place || 'Provincia no indicada')} · ${escLocal(severityLabel(event.severity))} · ${escLocal(event.status || 'sin estado')}</div>
      ${dateTime ? `<div class="hint"><strong>Fecha / hora:</strong> ${escLocal(dateTime)}</div>` : ''}
      ${road ? `<div class="hint">${escLocal(road)}</div>` : ''}
      ${description}
      ${coords ? `<div style="margin-top:5px">${coords}</div>` : ''}
    </div>`;
  }

  /** Renderiza la lista conservando el comportamiento histórico de la vista. */
  function renderEmergencyList(rows) {
    const body = document.querySelector('#emergency-province-events');
    if (!body) return;
    if (!rows.length) {
      body.innerHTML = '<p class="hint">No hay incidencias que coincidan con los filtros seleccionados.</p>';
      return;
    }
    body.innerHTML = rows.map(event => `<div class="status-card" style="margin-top:8px;align-items:flex-start">
      <span class="dot ${String(event.severity).toLowerCase()==='critical'?'warn':'ok'}"></span>
      <div style="min-width:0;flex:1">${emergencyEventSummaryHtml(event, false)}</div>
    </div>`).join('');
  }

  /** Carga MapLibre de forma diferida y una sola vez. */
  function ensureMapLibreAssets() {
    if (window.maplibregl) return Promise.resolve();
    if (mapAssetsPromise) return mapAssetsPromise;
    mapAssetsPromise = new Promise((resolve, reject) => {
      if (!document.querySelector('link[data-meshnet-maplibre]')) {
        const link = document.createElement('link');
        link.rel = 'stylesheet';
        link.href = MAPLIBRE_CSS;
        link.dataset.meshnetMaplibre = '1';
        document.head.appendChild(link);
      }
      const existing = document.querySelector('script[data-meshnet-maplibre]');
      if (existing) {
        existing.addEventListener('load', resolve, {once:true});
        existing.addEventListener('error', () => reject(new Error('No se pudo cargar MapLibre.')), {once:true});
        return;
      }
      const script = document.createElement('script');
      script.src = MAPLIBRE_JS;
      script.async = true;
      script.dataset.meshnetMaplibre = '1';
      script.onload = resolve;
      script.onerror = () => reject(new Error('No se pudo cargar MapLibre.'));
      document.head.appendChild(script);
    });
    return mapAssetsPromise;
  }

  /** Elimina únicamente los marcadores de incidencias; el mapa base se reutiliza. */
  function clearEmergencyMarkers() {
    emergencyMarkers.forEach(marker => marker.remove());
    emergencyMarkers = [];
  }

  /** Ajusta la cámara a todos los marcadores actualmente visibles. */
  function fitEmergencyMap(rows) {
    if (!emergencyMap || !window.maplibregl) return;
    const mappable = rows.filter(validCoordinates);
    if (!mappable.length) return;
    if (mappable.length === 1) {
      emergencyMap.easeTo({center:[Number(mappable[0].longitude), Number(mappable[0].latitude)], zoom:12});
      return;
    }
    const bounds = new maplibregl.LngLatBounds();
    mappable.forEach(event => bounds.extend([Number(event.longitude), Number(event.latitude)]));
    emergencyMap.fitBounds(bounds, {padding:48, maxZoom:12});
  }

  /** Crea o actualiza el mapa con exactamente los mismos eventos filtrados que Lista. */
  async function renderEmergencyMap(rows, reframe = false) {
    const mapBody = document.querySelector('#emergency-map-container');
    const mapHint = document.querySelector('#emergency-map-hint');
    if (!mapBody || currentView !== 'map') return;
    const mappable = rows.filter(validCoordinates);
    mapHint.textContent = `${mappable.length} de ${rows.length} incidencias visibles tienen coordenadas`;
    if (!mappable.length) {
      clearEmergencyMarkers();
      mapBody.innerHTML = '<p class="hint" style="padding:16px">Las incidencias visibles no disponen de coordenadas válidas.</p>';
      return;
    }

    try {
      await ensureMapLibreAssets();
      if (!document.querySelector('#emergency-map-canvas')) {
        mapBody.innerHTML = '<div id="emergency-map-canvas" style="width:100%;height:430px;border-radius:10px;overflow:hidden"></div>';
      }
      if (!emergencyMap) {
        emergencyMap = new maplibregl.Map({
          container: 'emergency-map-canvas',
          style: MAP_STYLE,
          center: [Number(mappable[0].longitude), Number(mappable[0].latitude)],
          zoom: 7,
        });
        emergencyMap.addControl(new maplibregl.NavigationControl({showCompass:false}), 'top-right');
        emergencyMap.on('load', () => {
          renderEmergencyMap(filteredEmergencyEvents(), true);
        });
        return;
      }

      emergencyMap.resize();
      clearEmergencyMarkers();
      mappable.forEach(event => {
        const element = document.createElement('button');
        element.type = 'button';
        element.title = `${categoryPresentation(event.category).label} · ${severityLabel(event.severity)}`;
        element.style.cssText = `width:18px;height:18px;border-radius:50%;border:2px solid white;background:${severityColor(event.severity)};box-shadow:0 1px 5px rgba(0,0,0,.55);cursor:pointer;padding:0`;
        const popup = new maplibregl.Popup({offset:14, maxWidth:'320px'}).setHTML(emergencyEventSummaryHtml(event, true));
        const marker = new maplibregl.Marker({element})
          .setLngLat([Number(event.longitude), Number(event.latitude)])
          .setPopup(popup)
          .addTo(emergencyMap);
        emergencyMarkers.push(marker);
      });
      if (reframe) fitEmergencyMap(rows);
    } catch (error) {
      mapBody.innerHTML = `<p class="hint" style="padding:16px">${escLocal(error.message || 'No se pudo cargar el mapa.')}</p>`;
    }
  }

  /** Render central: ambos modos consumen el mismo resultado filtrado. */
  function renderEmergencyProvinceEvents(reframeMap = true) {
    const count = document.querySelector('#emergency-province-count');
    if (!count) return;
    const rows = filteredEmergencyEvents();
    count.textContent = `${rows.length} de ${snapshot.events.length} incidencias visibles`;
    if (currentView === 'list') renderEmergencyList(rows);
    else renderEmergencyMap(rows, reframeMap);
  }

  /** Cambia entre Lista y Mapa sin realizar una nueva llamada al backend. */
  function setEmergencyView(view) {
    currentView = view === 'map' ? 'map' : 'list';
    const listBody = document.querySelector('#emergency-province-events');
    const mapPanel = document.querySelector('#emergency-map-panel');
    const listButton = document.querySelector('#emergency-view-list');
    const mapButton = document.querySelector('#emergency-view-map');
    if (listBody) listBody.style.display = currentView === 'list' ? '' : 'none';
    if (mapPanel) mapPanel.style.display = currentView === 'map' ? '' : 'none';
    if (listButton) listButton.disabled = currentView === 'list';
    if (mapButton) mapButton.disabled = currentView === 'map';
    renderEmergencyProvinceEvents(true);
  }

  /** Actualiza los selectores dinámicos conservando la selección cuando siga disponible. */
  function populateDynamicSelect(selector, values, emptyLabel, labelFormatter = value => value) {
    if (!selector) return;
    const previous = selector.value;
    selector.innerHTML = `<option value="">${escLocal(emptyLabel)}</option>` +
      values.map(value => `<option value="${escLocal(value)}">${escLocal(labelFormatter(value))}</option>`).join('');
    if ([...selector.options].some(option => option.value === previous)) selector.value = previous;
  }

  async function loadEmergencyProvinceEvents() {
    const body = document.querySelector('#emergency-province-events');
    if (body && currentView === 'list') body.innerHTML = '<p class="hint">Cargando incidencias actuales…</p>';
    try {
      const response = await fetch('/api/emergencias/current-view', {headers: {'Accept':'application/json'}});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'No se pudieron leer las incidencias actuales');
      snapshot = data;
      populateDynamicSelect(document.querySelector('#emergency-province-select'), data.provinces || [], 'Todas las provincias');
      populateDynamicSelect(
        document.querySelector('#emergency-category-select'),
        data.categories || [],
        'Todos los tipos',
        value => `${categoryPresentation(value).icon} ${categoryPresentation(value).label}`
      );
      renderEmergencyProvinceEvents(true);
    } catch (error) {
      if (body) body.innerHTML = `<p class="hint">${escLocal(error.message)}</p>`;
      const mapBody = document.querySelector('#emergency-map-container');
      if (mapBody) mapBody.innerHTML = `<p class="hint" style="padding:16px">${escLocal(error.message)}</p>`;
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
        <label>Tipo<select id="emergency-category-select"><option value="">Todos los tipos</option></select></label>
      </div>
      <div class="row" style="margin-top:10px;justify-content:flex-start;gap:8px">
        <button class="secondary" id="emergency-view-list" disabled>Lista</button>
        <button class="secondary" id="emergency-view-map">Mapa</button>
      </div>
      <div id="emergency-province-events"><p class="hint">Cargando incidencias actuales…</p></div>
      <div id="emergency-map-panel" style="display:none;margin-top:8px">
        <div class="row" style="margin-bottom:6px"><div id="emergency-map-hint" class="hint">Mapa de incidencias visibles</div><button class="secondary" id="emergency-map-fit">Reencuadrar</button></div>
        <div id="emergency-map-container"><p class="hint">El mapa se cargará al abrir esta vista.</p></div>
      </div>`;
    overview.appendChild(section);
    section.querySelector('#emergency-province-select').addEventListener('change', () => renderEmergencyProvinceEvents(true));
    section.querySelector('#emergency-severity-select').addEventListener('change', () => renderEmergencyProvinceEvents(true));
    section.querySelector('#emergency-category-select').addEventListener('change', () => renderEmergencyProvinceEvents(true));
    section.querySelector('#emergency-province-refresh').addEventListener('click', loadEmergencyProvinceEvents);
    section.querySelector('#emergency-view-list').addEventListener('click', () => setEmergencyView('list'));
    section.querySelector('#emergency-view-map').addEventListener('click', () => setEmergencyView('map'));
    section.querySelector('#emergency-map-fit').addEventListener('click', () => fitEmergencyMap(filteredEmergencyEvents()));
    loadEmergencyProvinceEvents();
  }

  ensureEmergencyProvincePanel();
  setInterval(ensureEmergencyProvincePanel, 1000);
})();
</script>
"""


def apply_emergency_province_view(app: FastAPI) -> FastAPI:
    """Añade la vista Lista/Mapa de Emergencias a un Control Panel existente.

    Args:
        app: aplicación FastAPI creada por ``web_admin`` y ya ampliada, si procede,
            por otras extensiones del Control Panel.

    Returns:
        La misma instancia de FastAPI. La función es idempotente.

    Se registra el endpoint GET histórico de solo lectura y un middleware limitado a
    la raíz HTML. El middleware no altera APIs, respuestas JSON ni acciones mutantes.
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