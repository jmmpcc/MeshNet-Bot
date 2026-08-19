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
    """Lee una variable booleana de entorno con valores habituales en MeshNet.

    Uso:
        enabled = _enabled("EMERGENCIAS_PUBLIC_MAP_ENABLED", False)

    Parámetros:
        name: nombre de la variable de entorno.
        default: valor a devolver cuando la variable no existe.

    Funcionalidad:
        Reconoce los valores 1/true/yes/on/si/sí como verdaderos y mantiene el
        publicador completamente desactivado por defecto.
    """
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on", "si", "sí"}


def _safe_float(value: Any, minimum: float, maximum: float) -> float | None:
    """Convierte una coordenada a float y rechaza valores fuera de rango.

    Uso:
        latitude = _safe_float(raw_latitude, -90.0, 90.0)

    Parámetros:
        value: valor recibido desde current.json.
        minimum: límite inferior permitido.
        maximum: límite superior permitido.

    Funcionalidad:
        Evita que una coordenada inválida alcance el mapa público sin modificar
        ni corregir el evento original almacenado por emergencias_guardia.
    """
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
    """Genera la página pública con MapLibre GL JS y OpenFreeMap.

    Uso:
        html = render_public_map_html(refresh_seconds=10)

    Parámetros:
        refresh_seconds: intervalo de comprobación de events.json. Se fuerza un
            mínimo de cinco segundos para evitar sondeos excesivos.

    Funcionalidad:
        - Usa MapLibre GL JS, software libre, como motor cartográfico.
        - Usa OpenFreeMap con datos OpenStreetMap mediante el estilo Liberty.
        - No requiere API key, cuenta ni credenciales en el navegador.
        - Actualiza marcadores sin recargar la página completa.
        - Muestra fecha/hora de la última lectura y estado de conexión.
        - Reutiliza siempre la versión más reciente del evento en cada popup.
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
:root{{--bg:#0b1520;--bar:#122334;--line:#29465f;--text:#eef5fb;--muted:#9bb0c1}}
html,body{{height:100%;margin:0;font-family:system-ui,-apple-system,Segoe UI,Arial,sans-serif;background:var(--bg);color:var(--text)}}
#bar{{min-height:62px;padding:10px 14px;background:var(--bar);display:flex;gap:16px;align-items:center;flex-wrap:wrap;border-bottom:1px solid var(--line)}}
#map{{height:calc(100% - 83px);min-height:420px}}
.muted{{color:var(--muted);font-size:.9rem}}
.badge{{padding:4px 9px;border:1px solid #4b657a;border-radius:999px}}
.status-ok{{color:#79e3bd}}.status-bad{{color:#ffb0aa}}
.meshnet-marker{{width:20px;height:20px;border-radius:50%;border:3px solid #fff;box-shadow:0 2px 8px #0009;cursor:pointer}}
.meshnet-marker.low{{background:#4ba3d8}}.meshnet-marker.medium{{background:#e9ba45}}.meshnet-marker.high{{background:#e77a2d}}.meshnet-marker.critical{{background:#d83d3d}}
.maplibregl-popup-content{{color:#17212b;max-width:340px}}
.maplibregl-popup-content hr{{border:0;border-top:1px solid #d9dfe4}}
#footer{{height:21px;padding:2px 10px;background:#0c1925;color:#8096a8;font-size:.72rem;box-sizing:border-box}}
@media(max-width:640px){{#bar{{gap:8px;font-size:.9rem}}#map{{height:calc(100% - 109px)}}}}
</style>
</head>
<body>
<div id="bar">
  <strong>MeshNet Emergency Map</strong>
  <span class="badge" id="count">0 activas</span>
  <span class="muted">Última actualización: <span id="updated">--</span></span>
  <span class="muted" id="state">Conectando</span>
</div>
<div id="map"></div>
<div id="footer">Cartografía: OpenFreeMap · Datos © OpenStreetMap contributors</div>
<script src="https://unpkg.com/maplibre-gl@5.6.0/dist/maplibre-gl.js"></script>
<script>
const PUBLIC_STYLE = 'https://tiles.openfreemap.org/styles/liberty';
const markers = new Map();
let revision = '';

const esc = (value) => String(value ?? '').replace(/[&<>"']/g, char => ({{
  '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
}}[char]));

function fmt(value) {{
  if (!value) return '--';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? esc(value) : date.toLocaleString('es-ES');
}}

function markerTitle(event) {{
  return `${{event.title}}${{event.municipality ? ' · ' + event.municipality : ''}}`;
}}

function popup(event) {{
  const place = [event.municipality, event.province].filter(Boolean).join(' · ');
  const sourceLink = event.source_url
    ? `<br><a href="${{esc(event.source_url)}}" target="_blank" rel="noopener noreferrer">Fuente</a>`
    : '';
  return `<div><strong>${{esc(event.title)}}</strong><br>${{esc(place)}}` +
    `<br><b>Nivel:</b> ${{esc(event.severity)}}` +
    `<br><b>Estado:</b> ${{esc(event.status)}}` +
    `<br><b>Detectado:</b> ${{fmt(event.started_at || event.first_seen)}}` +
    `<br><b>Actualizado:</b> ${{fmt(event.updated_at || event.last_seen)}}` +
    `${{event.description ? '<hr>' + esc(event.description) : ''}}${{sourceLink}}</div>`;
}}

function markerElement(severity) {{
  const element = document.createElement('div');
  const safeSeverity = ['low','medium','high','critical'].includes(severity) ? severity : 'medium';
  element.className = `meshnet-marker ${{safeSeverity}}`;
  return element;
}}

const map = new maplibregl.Map({{
  container: 'map',
  style: PUBLIC_STYLE,
  center: [-3.7, 40.2],
  zoom: 5.3,
  attributionControl: true
}});
map.addControl(new maplibregl.NavigationControl({{showCompass:true}}), 'top-right');

function sync(data) {{
  if (data.revision === revision) {{
    document.getElementById('updated').textContent = fmt(data.generated_at || data.source_updated_at);
    return;
  }}

  revision = data.revision || '';
  const alive = new Set();

  for (const event of data.events || []) {{
    alive.add(event.event_id);
    const position = [Number(event.longitude), Number(event.latitude)];
    let entry = markers.get(event.event_id);

    if (!entry) {{
      const element = markerElement(event.severity);
      const marker = new maplibregl.Marker({{element}})
        .setLngLat(position)
        .addTo(map);
      element.addEventListener('click', () => {{
        new maplibregl.Popup({{offset:18}})
          .setLngLat(marker.getLngLat())
          .setHTML(popup(entry.event))
          .addTo(map);
      }});
      entry = {{marker, element, event}};
      markers.set(event.event_id, entry);
    }} else {{
      entry.marker.setLngLat(position);
      entry.element.className = markerElement(event.severity).className;
      entry.event = event;
    }}

    entry.event = event;
    entry.element.title = markerTitle(event);
  }}

  for (const [eventId, entry] of markers) {{
    if (!alive.has(eventId)) {{
      entry.marker.remove();
      markers.delete(eventId);
    }}
  }}

  document.getElementById('count').textContent = `${{(data.events || []).length}} activas`;
  document.getElementById('updated').textContent = fmt(data.generated_at || data.source_updated_at);
}}

async function refresh() {{
  try {{
    const response = await fetch('events.json?ts=' + Date.now(), {{cache:'no-store'}});
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

map.on('load', refresh);
setInterval(refresh, {refresh_ms});
</script>
</body>
</html>'''


def render_directory_htaccess(base_url: str) -> str:
    """Protege /emergencias y redirige cualquier recurso no público al dominio raíz.

    Uso:
        content = render_directory_htaccess("https://ciberforense.com.es")

    Parámetros:
        base_url: URL raíz pública a la que redirigir rutas no autorizadas.

    Funcionalidad:
        Deshabilita índices, bloquea dotfiles y cualquier fichero distinto del
        HTML/JSON público, añade cabeceras defensivas y evita exponer recursos
        internos por errores de configuración del hosting.
    """
    root = base_url.rstrip("/") + "/"
    return f'''Options -Indexes\nRewriteEngine On\nRewriteRule ^\\. - [F,L]\nRewriteCond %{{REQUEST_URI}} !^/emergencias/?$ [NC]\nRewriteCond %{{REQUEST_URI}} !^/emergencias/index\\.html$ [NC]\nRewriteCond %{{REQUEST_URI}} !^/emergencias/events\\.json$ [NC]\nRewriteRule ^ {root} [R=302,L]\n<FilesMatch "^(?!index\\.html$|events\\.json$|\\.htaccess$).+">\nRequire all denied\n</FilesMatch>\n<IfModule mod_headers.c>\nHeader always set X-Content-Type-Options "nosniff"\nHeader always set Referrer-Policy "strict-origin-when-cross-origin"\nHeader always set Permissions-Policy "geolocation=(), camera=(), microphone=()"\n</IfModule>\n'''


class ExplicitFTP_TLS(FTP_TLS):
    """FTPS explícito equivalente al ya utilizado por RadioPropagacion.

    La verificación TLS puede activarse mediante
    EMERGENCIAS_PUBLIC_MAP_FTP_SSL_VERIFY=1 cuando el certificado del hosting
    valide correctamente contra el nombre configurado.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        verify = _enabled("EMERGENCIAS_PUBLIC_MAP_FTP_SSL_VERIFY", False)
        context = ssl.create_default_context() if verify else ssl._create_unverified_context()
        super().__init__(*args, context=context, **kwargs)


def _mkdirs(ftp: FTP_TLS, remote_dir: str) -> None:
    """Crea de forma tolerante la jerarquía remota antes de cada subida.

    Parámetros:
        ftp: conexión FTP_TLS ya autenticada.
        remote_dir: directorio remoto que debe existir al finalizar.
    """
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
    """Sube bytes en memoria sin crear ficheros temporales.

    Parámetros:
        ftp: conexión FTP_TLS activa.
        remote_dir: directorio remoto de destino.
        name: nombre del fichero remoto.
        content: contenido binario a transferir.
    """
    _mkdirs(ftp, remote_dir)
    with BytesIO(content) as handle:
        ftp.storbinary(f"STOR {name}", handle)


def publish_public_map(current_file: Path) -> dict[str, Any]:
    """Genera y publica /emergencias sin exponer la API interna de MeshNet.

    Uso:
        result = publish_public_map(Path("data/current.json"))

    Parámetros:
        current_file: estado consolidado de emergencias_guardia.

    Funcionalidad:
        Publica index.html, events.json y la protección .htaccess mediante FTPS
        explícito. No necesita ninguna clave cartográfica. Si la publicación está
        desactivada no realiza conexiones de red.
    """
    if not _enabled("EMERGENCIAS_PUBLIC_MAP_ENABLED", False):
        return {"enabled": False, "published": False}

    host = os.getenv("EMERGENCIAS_PUBLIC_MAP_FTP_HOST", "").strip()
    user = os.getenv("EMERGENCIAS_PUBLIC_MAP_FTP_USER", "").strip()
    password = os.getenv("EMERGENCIAS_PUBLIC_MAP_FTP_PASSWORD", "").strip()
    if not host or not user or not password:
        raise RuntimeError("Falta configuración FTPS EMERGENCIAS_PUBLIC_MAP_* obligatoria")

    port = int(os.getenv("EMERGENCIAS_PUBLIC_MAP_FTP_PORT", "21"))
    public_root = os.getenv(
        "EMERGENCIAS_PUBLIC_MAP_FTP_PUBLIC_ROOT", "/public_html"
    ).rstrip("/")
    remote_dir = public_root + "/emergencias"
    base_url = os.getenv(
        "EMERGENCIAS_PUBLIC_MAP_BASE_URL", "https://ciberforense.com.es"
    ).rstrip("/")
    refresh = int(os.getenv("EMERGENCIAS_PUBLIC_MAP_REFRESH_SECONDS", "10"))

    payload = build_public_payload(current_file)
    html = render_public_map_html(refresh)
    htaccess = render_directory_htaccess(base_url)

    ftp = ExplicitFTP_TLS()
    ftp.connect(host, port, timeout=30)
    ftp.login(user, password)
    ftp.prot_p()
    try:
        _upload_bytes(
            ftp,
            remote_dir,
            "events.json",
            json.dumps(
                payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            ).encode("utf-8"),
        )
        _upload_bytes(ftp, remote_dir, "index.html", html.encode("utf-8"))
        _upload_bytes(ftp, remote_dir, ".htaccess", htaccess.encode("utf-8"))
    finally:
        try:
            ftp.quit()
        except Exception:
            ftp.close()

    return {
        "enabled": True,
        "published": True,
        "url": base_url + "/emergencias/",
        "revision": payload["revision"],
        "count": payload["count"],
        "generated_at": payload["generated_at"],
    }


def publish_if_changed(current_file: Path, state_file: Path) -> dict[str, Any]:
    """Publica sólo cuando cambia la revisión pública.

    Uso:
        result = publish_if_changed(current_file, state_file)

    Parámetros:
        current_file: current.json de emergencias_guardia.
        state_file: fichero local que recuerda la última revisión publicada.

    Funcionalidad:
        Evita transferencias FTPS cuando el contenido público no ha cambiado. El
        estado sólo se actualiza después de una publicación completada con éxito.
    """
    payload = build_public_payload(current_file)
    previous: dict[str, Any] = {}
    if state_file.exists():
        try:
            previous = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = {}

    if previous.get("revision") == payload["revision"]:
        return {
            "enabled": _enabled("EMERGENCIAS_PUBLIC_MAP_ENABLED", False),
            "published": False,
            "unchanged": True,
            "revision": payload["revision"],
        }

    result = publish_public_map(current_file)
    if result.get("published"):
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(
            json.dumps(
                {
                    "revision": result["revision"],
                    "published_at": result["generated_at"],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    return result


def main() -> int:
    """Entrada CLI invocada por systemd.path cuando cambia current.json.

    No modifica current.json. Lee el estado consolidado, comprueba la revisión y
    ejecuta la publicación independiente únicamente cuando corresponde.
    """
    base = Path(__file__).resolve().parents[1]
    current_file = Path(
        os.getenv(
            "EMERGENCIAS_PUBLIC_MAP_CURRENT_FILE", str(base / "data" / "current.json")
        )
    )
    state_file = Path(
        os.getenv(
            "EMERGENCIAS_PUBLIC_MAP_STATE_FILE",
            str(base / "data" / "public_map_state.json"),
        )
    )
    result = publish_if_changed(current_file, state_file)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
