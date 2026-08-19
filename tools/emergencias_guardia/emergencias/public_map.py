from __future__ import annotations

import hashlib
import json
import os
import ssl
from datetime import datetime, timezone
from ftplib import FTP_TLS
from io import BytesIO
from pathlib import Path
from typing import Any

TERMINAL_STATUSES = {"resolved", "cancelled", "expired", "closed"}


def _enabled(name: str, default: bool = False) -> bool:
    """Lee una variable booleana de entorno con valores habituales en MeshNet."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on", "si", "sí"}


def _safe_float(value: Any, minimum: float, maximum: float) -> float | None:
    """Convierte una coordenada a float y rechaza valores fuera de rango."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if minimum <= number <= maximum else None


def build_public_payload(current_file: Path) -> dict[str, Any]:
    """Construye el JSON público a partir de current.json sin modificarlo.

    Uso:
        payload = build_public_payload(DATA_DIR / "current.json")

    Parámetros:
        current_file: fichero current.json consolidado por emergencias_guardia.

    Funcionalidad:
        - Publica únicamente incidencias no terminales.
        - Exige coordenadas geográficas válidas.
        - No vuelve a filtrar, deduplicar ni agrupar incidencias.
        - No publica metadata interna ni configuración del sistema.
        - Incluye carretera y punto kilométrico cuando existen para que el visor
          pueda mostrar el detalle operativo completo del evento.
        - Calcula una revisión estable para evitar subidas FTPS innecesarias.
    """
    if current_file.exists():
        source = json.loads(current_file.read_text(encoding="utf-8"))
    else:
        source = {"updated_at": "", "events": []}

    events: list[dict[str, Any]] = []
    for item in source.get("events", []):
        if str(item.get("status", "active")).casefold() in TERMINAL_STATUSES:
            continue

        latitude = _safe_float(item.get("latitude"), -90.0, 90.0)
        longitude = _safe_float(item.get("longitude"), -180.0, 180.0)
        if latitude is None or longitude is None:
            continue

        events.append({
            "event_id": str(item.get("event_id", "")),
            "source": str(item.get("source", "")),
            "category": str(item.get("category", "other")),
            "status": str(item.get("status", "active")),
            "severity": str(item.get("severity", "medium")),
            "verification": str(item.get("verification", "unverified")),
            "title": str(item.get("title", "Incidencia")),
            "description": str(item.get("description", "")),
            "municipality": str(item.get("municipality", "")),
            "province": str(item.get("province", "")),
            "road": str(item.get("road", "")),
            "kilometre": item.get("kilometre"),
            "latitude": latitude,
            "longitude": longitude,
            "started_at": str(item.get("started_at", "")),
            "updated_at": str(item.get("updated_at", "") or item.get("last_seen", "")),
            "first_seen": str(item.get("first_seen", "")),
            "last_seen": str(item.get("last_seen", "")),
            "source_url": str(item.get("source_url", "")),
        })

    events.sort(
        key=lambda event: (
            event["severity"], event["province"], event["municipality"], event["event_id"]
        )
    )
    generated_at = datetime.now(timezone.utc).isoformat()
    canonical = json.dumps(events, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    revision = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    return {
        "schema_version": 1,
        "generated_at": generated_at,
        "source_updated_at": str(source.get("updated_at", "")),
        "revision": revision,
        "count": len(events),
        "events": events,
    }


def render_public_map_html(refresh_seconds: int = 10) -> str:
    """Genera el mapa público interactivo con filtros y detalle de incidencias.

    Uso:
        html = render_public_map_html(refresh_seconds=10)

    Parámetros:
        refresh_seconds: intervalo de comprobación de events.json. Se fuerza un
            mínimo de cinco segundos para evitar sondeos excesivos.

    Funcionalidad:
        - Mantiene MapLibre GL JS + OpenFreeMap, sin API key.
        - Filtra localmente por periodo, provincia, severidad y categoría usando
          únicamente events.json; no añade endpoints ni carga al backend.
        - Muestra contadores totales y por categoría sobre el conjunto filtrado.
        - Usa el color para identificar el tipo de incidencia y borde/tamaño para
          expresar la severidad sin perder ninguna de las dos dimensiones.
        - En escritorio muestra un resumen al pasar el puntero por un marcador.
        - Al pulsar/tocar fija una ficha completa hasta cerrarla o seleccionar otra.
        - Reencuadra sólo cuando el usuario lo solicita o cambia un filtro.
    """
    refresh_ms = max(5, int(refresh_seconds)) * 1000
    return f'''<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MeshNet Emergency Map</title>
<meta name="robots" content="index,follow">
<meta name="referrer" content="strict-origin-when-cross-origin">
<link rel="stylesheet" href="https://unpkg.com/maplibre-gl@5.6.0/dist/maplibre-gl.css">
<style>
:root{{--bg:#0b1520;--bar:#122334;--line:#29465f;--text:#eef5fb;--muted:#9bb0c1;--accent:#66b3ff}}
*{{box-sizing:border-box}}
html,body{{height:100%;margin:0;font-family:system-ui,-apple-system,Segoe UI,Arial,sans-serif;background:var(--bg);color:var(--text)}}
#bar{{padding:10px 14px;background:var(--bar);border-bottom:1px solid var(--line)}}
#headline{{display:flex;gap:16px;align-items:center;flex-wrap:wrap}}
#filters{{display:grid;grid-template-columns:repeat(5,minmax(135px,1fr));gap:8px;margin-top:10px}}
#filters label{{font-size:.76rem;color:var(--muted);display:flex;flex-direction:column;gap:3px}}
#filters select,#fit{{background:#0b1722;color:var(--text);border:1px solid #39576e;border-radius:7px;padding:7px 8px}}
#fit{{cursor:pointer;align-self:end}}
#summary{{display:flex;gap:7px;flex-wrap:wrap;margin-top:9px}}
.category-chip{{display:inline-flex;align-items:center;gap:5px;padding:5px 8px;border:1px solid #39576e;border-radius:999px;background:#0c1a26;color:var(--text);cursor:pointer;font-size:.8rem}}
.category-chip.active{{outline:2px solid var(--accent);border-color:var(--accent)}}
.category-dot{{width:10px;height:10px;border-radius:50%;display:inline-block}}
.muted{{color:var(--muted);font-size:.9rem}}
.badge{{padding:4px 9px;border:1px solid #4b657a;border-radius:999px}}
.status-ok{{color:#79e3bd}}.status-bad{{color:#ffb0aa}}
#map{{height:calc(100% - 190px);min-height:430px}}
.meshnet-marker{{border-radius:50%;box-shadow:0 2px 8px #0009;cursor:pointer;transition:transform .12s ease}}
.meshnet-marker:hover{{transform:scale(1.18)}}
.maplibregl-popup-content{{color:#17212b;max-width:390px}}
.maplibregl-popup-content hr{{border:0;border-top:1px solid #d9dfe4}}
.quick-popup .maplibregl-popup-content{{max-width:300px;padding:8px 10px}}
#footer{{height:21px;padding:2px 10px;background:#0c1925;color:#8096a8;font-size:.72rem}}
@media(max-width:900px){{#filters{{grid-template-columns:repeat(2,minmax(130px,1fr))}}#map{{height:calc(100% - 275px)}}}}
@media(max-width:560px){{#filters{{grid-template-columns:1fr}}#map{{height:calc(100% - 410px);min-height:420px}}}}
</style>
</head>
<body>
<div id="bar">
  <div id="headline">
    <strong>MeshNet Emergency Map</strong>
    <span class="badge" id="count">0 visibles</span>
    <span class="muted">Total publicado: <span id="published-count">0</span></span>
    <span class="muted">Última actualización: <span id="updated">--</span></span>
    <span class="muted" id="state">Conectando</span>
  </div>
  <div id="filters">
    <label>Periodo
      <select id="filter-period">
        <option value="24" selected>Últimas 24 horas</option>
        <option value="48">Últimas 48 horas</option>
        <option value="72">Últimas 72 horas</option>
        <option value="168">Últimos 7 días</option>
        <option value="0">Todas</option>
      </select>
    </label>
    <label>Provincia
      <select id="filter-province"><option value="">Todas las provincias</option></select>
    </label>
    <label>Severidad
      <select id="filter-severity">
        <option value="">Todas las severidades</option>
        <option value="critical">Crítica</option>
        <option value="high">Alta</option>
        <option value="medium">Media</option>
        <option value="low">Baja</option>
      </select>
    </label>
    <label>Tipo
      <select id="filter-category"><option value="">Todos los tipos</option></select>
    </label>
    <button id="fit" type="button">Reencuadrar</button>
  </div>
  <div id="summary" aria-label="Recuento por categoría"></div>
</div>
<div id="map"></div>
<div id="footer">Cartografía: OpenFreeMap · Datos © OpenStreetMap contributors</div>
<script src="https://unpkg.com/maplibre-gl@5.6.0/dist/maplibre-gl.js"></script>
<script>
const PUBLIC_STYLE = 'https://tiles.openfreemap.org/styles/liberty';
const markers = new Map();
let revision = '';
let publishedEvents = [];
let quickPopup = null;
let pinnedPopup = null;

const CATEGORY_PRESENTATION = {{
  wildfire: {{icon:'🔥', label:'Incendio forestal', color:'#d32f2f'}},
  fire: {{icon:'🔥', label:'Incendio', color:'#e53935'}},
  traffic_collision: {{icon:'🚗', label:'Colisión de tráfico', color:'#f9a825'}},
  traffic_accident: {{icon:'🚗', label:'Accidente de tráfico', color:'#fbc02d'}},
  road_closed: {{icon:'🚧', label:'Corte de carretera', color:'#ef6c00'}},
  road_closure: {{icon:'🚧', label:'Corte de carretera', color:'#ef6c00'}},
  road_incident: {{icon:'🚧', label:'Incidencia vial', color:'#fb8c00'}},
  flood: {{icon:'🌊', label:'Inundación', color:'#1976d2'}},
  earthquake: {{icon:'🌍', label:'Terremoto', color:'#795548'}},
  storm: {{icon:'⛈️', label:'Tormenta', color:'#7b1fa2'}},
  weather: {{icon:'🌦️', label:'Fenómeno meteorológico', color:'#5e35b1'}},
  civil_protection: {{icon:'⚠️', label:'Protección Civil', color:'#c2185b'}},
  emergency: {{icon:'⚠️', label:'Emergencia', color:'#ad1457'}},
  other: {{icon:'📍', label:'Otra incidencia', color:'#607d8b'}}
}};

const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({{
  '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
}}[char]));

function categoryPresentation(value) {{
  const key = String(value || 'other').trim().toLowerCase();
  if (CATEGORY_PRESENTATION[key]) return CATEGORY_PRESENTATION[key];
  return {{
    icon:'📍',
    label:key.replace(/[_-]+/g,' ').replace(/\\b\\w/g, char => char.toUpperCase()) || 'Otra incidencia',
    color:'#607d8b'
  }};
}}

function severityLabel(value) {{
  return ({{critical:'Crítica',high:'Alta',medium:'Media',low:'Baja'}}[String(value || '').toLowerCase()] || value || 'Sin nivel');
}}

function verificationLabel(value) {{
  return ({{official:'Oficial',verified:'Verificada',unverified:'Sin verificar'}}[String(value || '').toLowerCase()] || value || 'Sin dato');
}}

function fmt(value) {{
  if (!value) return '--';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? esc(value) : date.toLocaleString('es-ES');
}}

function eventReferenceDate(event) {{
  for (const value of [event.updated_at, event.last_seen, event.started_at, event.first_seen]) {{
    if (!value) continue;
    const date = new Date(value);
    if (!Number.isNaN(date.getTime())) return date;
  }}
  return null;
}}

function eventPlace(event) {{
  return [event.municipality, event.province].filter(Boolean).filter((value,index,array) => array.indexOf(value) === index).join(' · ');
}}

function quickPopupHtml(event) {{
  const category = categoryPresentation(event.category);
  return `<div><strong>${{esc(category.icon)}} ${{esc(category.label)}}</strong>` +
    `<br><b>${{esc(event.title || 'Incidencia')}}</b>` +
    `<br>${{esc(eventPlace(event) || 'Ubicación no indicada')}}` +
    `<br><b>Nivel:</b> ${{esc(severityLabel(event.severity))}}` +
    `<br><b>Estado:</b> ${{esc(event.status || 'sin estado')}}` +
    `<br><b>Fecha / hora:</b> ${{fmt(event.started_at || event.first_seen || event.updated_at)}}</div>`;
}}

function fullPopupHtml(event) {{
  const category = categoryPresentation(event.category);
  const road = event.road
    ? `<br><b>Carretera:</b> ${{esc(event.road)}}${{event.kilometre != null && event.kilometre !== '' ? ` · km ${{esc(event.kilometre)}}` : ''}}`
    : '';
  const sourceLink = event.source_url
    ? `<br><b>Fuente oficial:</b> <a href="${{esc(event.source_url)}}" target="_blank" rel="noopener noreferrer">Abrir fuente</a>`
    : '';
  return `<div><strong>${{esc(category.icon)}} ${{esc(category.label)}}</strong>` +
    `<br><b>Título:</b> ${{esc(event.title || 'Incidencia')}}` +
    `${{event.description ? `<hr>${{esc(event.description)}}` : ''}}` +
    `<br><b>Estado:</b> ${{esc(event.status || 'sin estado')}}` +
    `<br><b>Severidad:</b> ${{esc(severityLabel(event.severity))}}` +
    `<br><b>Verificación:</b> ${{esc(verificationLabel(event.verification))}}` +
    `<br><b>Fuente:</b> ${{esc(event.source || 'sin dato')}}` +
    `<br><b>Municipio:</b> ${{esc(event.municipality || '--')}}` +
    `<br><b>Provincia:</b> ${{esc(event.province || '--')}}` + road +
    `<br><b>Coordenadas:</b> ${{esc(Number(event.latitude).toFixed(5))}}, ${{esc(Number(event.longitude).toFixed(5))}}` +
    `<br><b>Inicio:</b> ${{fmt(event.started_at)}}` +
    `<br><b>Actualización:</b> ${{fmt(event.updated_at)}}` +
    `<br><b>Primera detección:</b> ${{fmt(event.first_seen)}}` +
    `<br><b>Última detección:</b> ${{fmt(event.last_seen)}}` +
    `<br><b>ID:</b> ${{esc(event.event_id || '--')}}` + sourceLink + `</div>`;
}}

function markerAppearance(event) {{
  const category = categoryPresentation(event.category);
  const severity = String(event.severity || '').toLowerCase();
  const config = {{critical:{{size:26,border:4}},high:{{size:23,border:4}},medium:{{size:20,border:3}},low:{{size:18,border:2}}}}[severity] || {{size:20,border:3}};
  return {{...config,color:category.color}};
}}

function applyMarkerAppearance(element,event) {{
  const appearance = markerAppearance(event);
  element.className = 'meshnet-marker';
  element.style.width = `${{appearance.size}}px`;
  element.style.height = `${{appearance.size}}px`;
  element.style.border = `${{appearance.border}}px solid white`;
  element.style.background = appearance.color;
}}

function markerElement(event) {{
  const element = document.createElement('div');
  applyMarkerAppearance(element,event);
  return element;
}}

const map = new maplibregl.Map({{container:'map',style:PUBLIC_STYLE,center:[-3.7,40.2],zoom:5.3,attributionControl:true}});
map.addControl(new maplibregl.NavigationControl({{showCompass:true}}),'top-right');

function filteredEvents() {{
  const hours = Number(document.getElementById('filter-period').value || 24);
  const province = document.getElementById('filter-province').value;
  const severity = document.getElementById('filter-severity').value;
  const category = document.getElementById('filter-category').value;
  const cutoff = hours > 0 ? Date.now() - hours * 3600000 : null;
  return publishedEvents.filter(event => {{
    if (province && event.province !== province) return false;
    if (severity && String(event.severity || '').toLowerCase() !== severity) return false;
    if (category && String(event.category || '') !== category) return false;
    if (cutoff != null) {{
      const date = eventReferenceDate(event);
      if (date && date.getTime() < cutoff) return false;
    }}
    return true;
  }});
}}

function populateFilters() {{
  const province = document.getElementById('filter-province');
  const category = document.getElementById('filter-category');
  const previousProvince = province.value;
  const previousCategory = category.value;
  const provinces = [...new Set(publishedEvents.map(event => event.province).filter(Boolean))].sort((a,b) => a.localeCompare(b,'es',{{sensitivity:'base'}}));
  const categories = [...new Set(publishedEvents.map(event => String(event.category || 'other')))].sort((a,b) => categoryPresentation(a).label.localeCompare(categoryPresentation(b).label,'es',{{sensitivity:'base'}}));
  province.innerHTML = '<option value="">Todas las provincias</option>' + provinces.map(value => `<option value="${{esc(value)}}">${{esc(value)}}</option>`).join('');
  category.innerHTML = '<option value="">Todos los tipos</option>' + categories.map(value => {{
    const item = categoryPresentation(value);
    return `<option value="${{esc(value)}}">${{esc(item.icon)}} ${{esc(item.label)}}</option>`;
  }}).join('');
  if ([...province.options].some(option => option.value === previousProvince)) province.value = previousProvince;
  if ([...category.options].some(option => option.value === previousCategory)) category.value = previousCategory;
}}

function renderCategorySummary(rows) {{
  const summary = document.getElementById('summary');
  const selectedCategory = document.getElementById('filter-category').value;
  const counts = new Map();
  for (const event of rows) {{
    const key = String(event.category || 'other');
    counts.set(key,(counts.get(key) || 0) + 1);
  }}
  const entries = [...counts.entries()].sort((a,b) => categoryPresentation(a[0]).label.localeCompare(categoryPresentation(b[0]).label,'es',{{sensitivity:'base'}}));
  summary.innerHTML = entries.length ? entries.map(([key,count]) => {{
    const item = categoryPresentation(key);
    const active = selectedCategory === key ? ' active' : '';
    return `<button type="button" class="category-chip${{active}}" data-category="${{esc(key)}}"><span class="category-dot" style="background:${{item.color}}"></span>${{esc(item.icon)}} ${{esc(item.label)}}: <b>${{count}}</b></button>`;
  }}).join('') : '<span class="muted">Sin incidencias para los filtros seleccionados</span>';
  summary.querySelectorAll('[data-category]').forEach(button => {{
    button.addEventListener('click',() => {{
      const select = document.getElementById('filter-category');
      const value = button.dataset.category || '';
      select.value = select.value === value ? '' : value;
      renderFiltered(true);
    }});
  }});
}}

function attachMarkerInteractions(entry) {{
  entry.element.addEventListener('mouseenter',() => {{
    if (pinnedPopup) return;
    if (quickPopup) quickPopup.remove();
    quickPopup = new maplibregl.Popup({{offset:16,closeButton:false,closeOnClick:false,className:'quick-popup'}}).setLngLat(entry.marker.getLngLat()).setHTML(quickPopupHtml(entry.event)).addTo(map);
  }});
  entry.element.addEventListener('mouseleave',() => {{
    if (quickPopup) {{quickPopup.remove();quickPopup=null;}}
  }});
  entry.element.addEventListener('click',event => {{
    event.stopPropagation();
    if (quickPopup) {{quickPopup.remove();quickPopup=null;}}
    if (pinnedPopup) pinnedPopup.remove();
    pinnedPopup = new maplibregl.Popup({{offset:20,closeButton:true,closeOnClick:false,maxWidth:'390px'}}).setLngLat(entry.marker.getLngLat()).setHTML(fullPopupHtml(entry.event)).addTo(map);
    pinnedPopup.on('close',() => {{pinnedPopup=null;}});
  }});
}}

function syncMarkers(rows) {{
  const alive = new Set();
  for (const event of rows) {{
    alive.add(event.event_id);
    const position = [Number(event.longitude),Number(event.latitude)];
    let entry = markers.get(event.event_id);
    if (!entry) {{
      const element = markerElement(event);
      const marker = new maplibregl.Marker({{element}}).setLngLat(position).addTo(map);
      entry = {{marker,element,event}};
      markers.set(event.event_id,entry);
      attachMarkerInteractions(entry);
    }} else {{
      entry.marker.setLngLat(position);
      entry.event = event;
      applyMarkerAppearance(entry.element,event);
    }}
    entry.event = event;
    entry.element.title = `${{categoryPresentation(event.category).label}} · ${{event.title || 'Incidencia'}}`;
  }}
  for (const [eventId,entry] of markers) {{
    if (!alive.has(eventId)) {{entry.marker.remove();markers.delete(eventId);}}
  }}
}}

function fitVisible(rows = filteredEvents()) {{
  if (!rows.length) return;
  if (rows.length === 1) {{map.easeTo({{center:[Number(rows[0].longitude),Number(rows[0].latitude)],zoom:12}});return;}}
  const bounds = new maplibregl.LngLatBounds();
  rows.forEach(event => bounds.extend([Number(event.longitude),Number(event.latitude)]));
  map.fitBounds(bounds,{{padding:50,maxZoom:12}});
}}

function renderFiltered(reframe = false) {{
  const rows = filteredEvents();
  syncMarkers(rows);
  renderCategorySummary(rows);
  document.getElementById('count').textContent = `${{rows.length}} visibles`;
  document.getElementById('published-count').textContent = String(publishedEvents.length);
  if (reframe && rows.length) fitVisible(rows);
}}

function sync(data) {{
  document.getElementById('updated').textContent = fmt(data.generated_at || data.source_updated_at);
  document.getElementById('published-count').textContent = String((data.events || []).length);
  if (data.revision === revision) return;
  revision = data.revision || '';
  publishedEvents = data.events || [];
  populateFilters();
  renderFiltered(false);
}}

async function refresh() {{
  try {{
    const response = await fetch('events.json?ts=' + Date.now(),{{cache:'no-store'}});
    if (!response.ok) throw new Error('HTTP ' + response.status);
    sync(await response.json());
    const state = document.getElementById('state');
    state.textContent = 'Datos en directo';
    state.className = 'muted status-ok';
  }} catch (error) {{
    const state = document.getElementById('state');
    state.textContent = 'Sin actualización';
    state.className = 'muted status-bad';
  }}
}}

['filter-period','filter-province','filter-severity','filter-category'].forEach(id => {{document.getElementById(id).addEventListener('change',() => renderFiltered(true));}});
document.getElementById('fit').addEventListener('click',() => fitVisible());
map.on('load',refresh);
setInterval(refresh,{refresh_ms});
</script>
</body>
</html>'''


def render_directory_htaccess(base_url: str) -> str:
    """Protege /emergencias y redirige cualquier recurso no público al dominio raíz."""
    root = base_url.rstrip("/") + "/"
    return f'''Options -Indexes\nRewriteEngine On\nRewriteRule ^\\. - [F,L]\nRewriteCond %{{REQUEST_URI}} !^/emergencias/?$ [NC]\nRewriteCond %{{REQUEST_URI}} !^/emergencias/index\\.html$ [NC]\nRewriteCond %{{REQUEST_URI}} !^/emergencias/events\\.json$ [NC]\nRewriteRule ^ {root} [R=302,L]\n<FilesMatch "^(?!index\\.html$|events\\.json$|\\.htaccess$).+">\nRequire all denied\n</FilesMatch>\n<IfModule mod_headers.c>\nHeader always set X-Content-Type-Options "nosniff"\nHeader always set Referrer-Policy "strict-origin-when-cross-origin"\nHeader always set Permissions-Policy "geolocation=(), camera=(), microphone=()"\n</IfModule>\n'''


class ExplicitFTP_TLS(FTP_TLS):
    """FTPS explícito equivalente al ya utilizado por RadioPropagacion."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        verify = _enabled("EMERGENCIAS_PUBLIC_MAP_FTP_SSL_VERIFY", False)
        context = ssl.create_default_context() if verify else ssl._create_unverified_context()
        super().__init__(*args, context=context, **kwargs)


def _mkdirs(ftp: FTP_TLS, remote_dir: str) -> None:
    """Crea de forma tolerante la jerarquía remota antes de cada subida."""
    parts = [part for part in remote_dir.replace("\\", "/").strip("/").split("/") if part]
    try:
        ftp.cwd("/")
    except Exception:
        pass

    for part in parts:
        try:
            ftp.cwd(part)
            continue
        except Exception:
            pass
        try:
            ftp.mkd(part)
        except Exception:
            pass
        ftp.cwd(part)


def _upload_bytes(ftp: FTP_TLS, remote_dir: str, name: str, content: bytes) -> None:
    """Sube bytes en memoria sin crear ficheros temporales."""
    _mkdirs(ftp, remote_dir)
    with BytesIO(content) as handle:
        ftp.storbinary(f"STOR {name}", handle)


def publish_public_map(current_file: Path) -> dict[str, Any]:
    """Genera y publica /emergencias sin exponer la API interna de MeshNet."""
    if not _enabled("EMERGENCIAS_PUBLIC_MAP_ENABLED", False):
        return {"enabled": False, "published": False}

    host = os.getenv("EMERGENCIAS_PUBLIC_MAP_FTP_HOST", "").strip()
    user = os.getenv("EMERGENCIAS_PUBLIC_MAP_FTP_USER", "").strip()
    password = os.getenv("EMERGENCIAS_PUBLIC_MAP_FTP_PASSWORD", "").strip()
    if not host or not user or not password:
        raise RuntimeError("Falta configuración FTPS EMERGENCIAS_PUBLIC_MAP_* obligatoria")

    port = int(os.getenv("EMERGENCIAS_PUBLIC_MAP_FTP_PORT", "21"))
    public_root = os.getenv("EMERGENCIAS_PUBLIC_MAP_FTP_PUBLIC_ROOT", "/public_html").rstrip("/")
    remote_dir = public_root + "/emergencias"
    base_url = os.getenv("EMERGENCIAS_PUBLIC_MAP_BASE_URL", "https://ciberforense.com.es").rstrip("/")
    refresh = int(os.getenv("EMERGENCIAS_PUBLIC_MAP_REFRESH_SECONDS", "10"))

    payload = build_public_payload(current_file)
    html = render_public_map_html(refresh)
    htaccess = render_directory_htaccess(base_url)

    ftp = ExplicitFTP_TLS()
    ftp.connect(host, port, timeout=30)
    ftp.login(user, password)
    ftp.prot_p()
    try:
        _upload_bytes(ftp, remote_dir, "events.json", json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        _upload_bytes(ftp, remote_dir, "index.html", html.encode("utf-8"))
        _upload_bytes(ftp, remote_dir, ".htaccess", htaccess.encode("utf-8"))
    finally:
        try:
            ftp.quit()
        except Exception:
            ftp.close()

    return {"enabled": True, "published": True, "url": base_url + "/emergencias/", "revision": payload["revision"], "count": payload["count"], "generated_at": payload["generated_at"]}


def publish_if_changed(current_file: Path, state_file: Path) -> dict[str, Any]:
    """Publica sólo cuando cambia la revisión pública."""
    payload = build_public_payload(current_file)
    previous: dict[str, Any] = {}
    if state_file.exists():
        try:
            previous = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = {}

    if previous.get("revision") == payload["revision"]:
        return {"enabled": _enabled("EMERGENCIAS_PUBLIC_MAP_ENABLED", False), "published": False, "unchanged": True, "revision": payload["revision"]}

    result = publish_public_map(current_file)
    if result.get("published"):
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps({"revision": result["revision"], "published_at": result["generated_at"]}, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> int:
    """Entrada CLI invocada por systemd.path cuando cambia current.json."""
    base = Path(__file__).resolve().parents[1]
    current_file = Path(os.getenv("EMERGENCIAS_PUBLIC_MAP_CURRENT_FILE", str(base / "data" / "current.json")))
    state_file = Path(os.getenv("EMERGENCIAS_PUBLIC_MAP_STATE_FILE", str(base / "data" / "public_map_state.json")))
    result = publish_if_changed(current_file, state_file)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
