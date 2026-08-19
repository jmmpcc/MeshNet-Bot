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

    Funcionalidad:
        Publica únicamente incidencias no terminales que tengan coordenadas válidas.
        No ejecuta filtros, deduplicación ni agrupación: reutiliza exactamente el
        resultado ya consolidado por emergencias_guardia. La revisión se calcula sobre
        el contenido visible y permite evitar subidas cuando no existe ningún cambio.
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

    events.sort(key=lambda e: (e["severity"], e["province"], e["municipality"], e["event_id"]))
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


def render_public_map_html(api_key: str, refresh_seconds: int = 10) -> str:
    """Genera la página pública Google Maps que refresca events.json sin recargar."""
    key = api_key.replace("&", "&amp;").replace('"', "&quot;")
    refresh_ms = max(5, int(refresh_seconds)) * 1000
    return f'''<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MeshNet Emergency Map</title>
<meta name="robots" content="index,follow"><meta name="referrer" content="strict-origin-when-cross-origin">
<style>html,body{{height:100%;margin:0;font-family:system-ui,Arial,sans-serif;background:#111;color:#eee}}#bar{{padding:10px 14px;background:#191919;display:flex;gap:18px;align-items:center;flex-wrap:wrap}}#map{{height:calc(100% - 68px)}}.muted{{opacity:.72;font-size:.9rem}}.badge{{padding:3px 8px;border:1px solid #555;border-radius:999px}}</style>
</head><body><div id="bar"><strong>MeshNet Emergency Map</strong><span class="badge" id="count">0 activas</span><span class="muted">Última actualización: <span id="updated">--</span></span><span class="muted" id="state">Conectando</span></div><div id="map"></div>
<script>
let map; const markers = new Map(); let revision = '';
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
function fmt(v){{ if(!v) return '--'; const d=new Date(v); return Number.isNaN(d.getTime())?esc(v):d.toLocaleString('es-ES'); }}
function markerTitle(e){{return `${{e.title}}${{e.municipality ? ' · '+e.municipality : ''}}`;}}
function popup(e){{const place=[e.municipality,e.province].filter(Boolean).join(' · '); return `<div style="max-width:320px"><strong>${{esc(e.title)}}</strong><br>${{esc(place)}}<br><b>Nivel:</b> ${{esc(e.severity)}}<br><b>Estado:</b> ${{esc(e.status)}}<br><b>Detectado:</b> ${{fmt(e.started_at || e.first_seen)}}<br><b>Actualizado:</b> ${{fmt(e.updated_at || e.last_seen)}}${{e.description?'<hr>'+esc(e.description):''}}</div>`;}}
function sync(data){{ if(data.revision===revision){{document.getElementById('updated').textContent=fmt(data.generated_at);return;}} revision=data.revision||''; const alive=new Set(); for(const e of data.events||[]){{alive.add(e.event_id); const pos={{lat:Number(e.latitude),lng:Number(e.longitude)}}; let m=markers.get(e.event_id); if(!m){{m=new google.maps.Marker({{position:pos,map,title:markerTitle(e)}}); const info=new google.maps.InfoWindow(); m.addListener('click',()=>{{info.setContent(popup(m.__meshnetEvent));info.open({{map,anchor:m}})}}); markers.set(e.event_id,m);}} else {{m.setPosition(pos);m.setTitle(markerTitle(e));}} m.__meshnetEvent=e;}} for(const [id,m] of markers){{if(!alive.has(id)){{m.setMap(null);markers.delete(id);}}}} document.getElementById('count').textContent=`${{(data.events||[]).length}} activas`; document.getElementById('updated').textContent=fmt(data.generated_at||data.source_updated_at);}}
async function refresh(){{try{{const r=await fetch('events.json?ts='+Date.now(),{{cache:'no-store'}});if(!r.ok)throw new Error('HTTP '+r.status);sync(await r.json());document.getElementById('state').textContent='Datos en directo';}}catch(e){{document.getElementById('state').textContent='Sin actualización';}}}}
function initMap(){{map=new google.maps.Map(document.getElementById('map'),{{center:{{lat:40.2,lng:-3.7}},zoom:6,mapTypeControl:false,streetViewControl:false}});refresh();setInterval(refresh,{refresh_ms});}}
window.initMap=initMap;
</script><script async src="https://maps.googleapis.com/maps/api/js?key={key}&callback=initMap"></script></body></html>'''


def render_directory_htaccess(base_url: str) -> str:
    """Protege /emergencias y redirige cualquier recurso no público al dominio raíz."""
    root = base_url.rstrip("/") + "/"
    return f'''Options -Indexes\nRewriteEngine On\nRewriteRule ^\\. - [F,L]\nRewriteCond %{{REQUEST_URI}} !^/emergencias/?$ [NC]\nRewriteCond %{{REQUEST_URI}} !^/emergencias/index\\.html$ [NC]\nRewriteCond %{{REQUEST_URI}} !^/emergencias/events\\.json$ [NC]\nRewriteRule ^ {root} [R=302,L]\n<FilesMatch "^(?!index\\.html$|events\\.json$|\\.htaccess$).+">\nRequire all denied\n</FilesMatch>\n<IfModule mod_headers.c>\nHeader always set X-Content-Type-Options "nosniff"\nHeader always set X-Frame-Options "SAMEORIGIN"\nHeader always set Referrer-Policy "strict-origin-when-cross-origin"\nHeader always set Permissions-Policy "geolocation=(), camera=(), microphone=()"\n</IfModule>\n'''


class ExplicitFTP_TLS(FTP_TLS):
    """FTPS explícito equivalente al ya utilizado por RadioPropagacion."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        verify = _enabled("EMERGENCIAS_PUBLIC_MAP_FTP_SSL_VERIFY", False)
        context = ssl.create_default_context() if verify else ssl._create_unverified_context()
        super().__init__(*args, context=context, **kwargs)


def _mkdirs(ftp: FTP_TLS, remote_dir: str) -> None:
    """Crea de forma tolerante la jerarquía remota antes de cada subida."""
    parts = [p for p in remote_dir.replace("\\", "/").strip("/").split("/") if p]
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
    """Sube bytes en memoria sin crear ficheros temporales con credenciales."""
    _mkdirs(ftp, remote_dir)
    with BytesIO(content) as handle:
        ftp.storbinary(f"STOR {name}", handle)


def publish_public_map(current_file: Path) -> dict[str, Any]:
    """Genera y publica /emergencias sin exponer la API interna de MeshNet.

    Requiere EMERGENCIAS_PUBLIC_MAP_ENABLED=1, una API key de Google Maps
    restringida al dominio y las credenciales FTPS específicas de publicación.
    Si está desactivado no realiza conexión alguna.
    """
    if not _enabled("EMERGENCIAS_PUBLIC_MAP_ENABLED", False):
        return {"enabled": False, "published": False}
    api_key = os.getenv("EMERGENCIAS_PUBLIC_MAP_GOOGLE_MAPS_API_KEY", "").strip()
    host = os.getenv("EMERGENCIAS_PUBLIC_MAP_FTP_HOST", "").strip()
    user = os.getenv("EMERGENCIAS_PUBLIC_MAP_FTP_USER", "").strip()
    password = os.getenv("EMERGENCIAS_PUBLIC_MAP_FTP_PASSWORD", "").strip()
    if not api_key or not host or not user or not password:
        raise RuntimeError("Falta configuración EMERGENCIAS_PUBLIC_MAP_* obligatoria")

    port = int(os.getenv("EMERGENCIAS_PUBLIC_MAP_FTP_PORT", "21"))
    public_root = os.getenv("EMERGENCIAS_PUBLIC_MAP_FTP_PUBLIC_ROOT", "/public_html").rstrip("/")
    remote_dir = public_root + "/emergencias"
    base_url = os.getenv("EMERGENCIAS_PUBLIC_MAP_BASE_URL", "https://ciberforense.com.es").rstrip("/")
    refresh = int(os.getenv("EMERGENCIAS_PUBLIC_MAP_REFRESH_SECONDS", "10"))
    payload = build_public_payload(current_file)
    html = render_public_map_html(api_key, refresh)
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

    return {
        "enabled": True,
        "published": True,
        "url": base_url + "/emergencias/",
        "revision": payload["revision"],
        "count": payload["count"],
        "generated_at": payload["generated_at"],
    }


def publish_if_changed(current_file: Path, state_file: Path) -> dict[str, Any]:
    """Publica sólo cuando cambia la revisión pública y registra la última subida."""
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
            json.dumps({"revision": result["revision"], "published_at": result["generated_at"]}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
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
