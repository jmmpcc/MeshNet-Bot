"""Visor de solo lectura para trazas históricas RX de MeshCore.

La fuente de verdad es el backlog JSONL que el broker ya mantiene. Esta extensión no
crea una segunda base de datos: localiza una recepción por su identificador estable y
representa únicamente los puntos cuya posición fue conocida al recibir el mensaje.
"""
from __future__ import annotations

import html
import json
import os
import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

_TRACE_ID_RE = re.compile(r"^[0-9a-f]{20}$")


def _offline_log_path() -> Path:
    """Devuelve el backlog compartido sin modificarlo.

    Puede sobreescribirse con CONTROLPANEL_BROKER_OFFLINE_LOG. El valor por defecto
    apunta al bot_data del repositorio, que es el mismo directorio persistente que
    utiliza el despliegue estándar.
    """
    configured = (os.getenv("CONTROLPANEL_BROKER_OFFLINE_LOG") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parents[2] / "bot_data" / "broker_offline_log.jsonl"


def _find_trace(trace_id: str, path: Path | None = None) -> dict[str, Any] | None:
    """Busca una recepción MeshCore concreta en el backlog existente.

    Lee en streaming para no cargar el histórico completo en memoria. Conserva la
    última coincidencia por robustez ante replays del backlog.
    """
    if not _TRACE_ID_RE.fullmatch(str(trace_id or "")):
        return None
    target = path or _offline_log_path()
    try:
        handle = target.open("r", encoding="utf-8")
    except OSError:
        return None

    found = None
    with handle:
        for line in handle:
            try:
                item = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if item.get("meshcore_trace_id") == trace_id:
                found = item
    return found


def _valid_point(lat: Any, lon: Any) -> tuple[float, float] | None:
    """Normaliza una coordenada y rechaza valores ausentes o fuera de rango."""
    try:
        latitude, longitude = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    if latitude == 0.0 and longitude == 0.0:
        return None
    return latitude, longitude


def _trace_points(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Construye la secuencia geográfica emisor -> repetidores -> receptor."""
    points: list[dict[str, Any]] = []

    sender = _valid_point(item.get("meshcore_from_lat"), item.get("meshcore_from_lon"))
    if sender:
        points.append({
            "role": "Emisor",
            "name": item.get("meshcore_from_name") or item.get("from_alias") or "Emisor",
            "lat": sender[0],
            "lon": sender[1],
        })

    for index, repeater in enumerate(item.get("meshcore_repeaters") or [], 1):
        if not isinstance(repeater, dict):
            continue
        point = _valid_point(repeater.get("lat"), repeater.get("lon"))
        if not point:
            continue
        points.append({
            "role": f"Repetidor {index}",
            "name": repeater.get("name") or repeater.get("hash") or f"Repetidor {index}",
            "lat": point[0],
            "lon": point[1],
        })

    receiver = _valid_point(
        item.get("meshcore_receiver_lat"),
        item.get("meshcore_receiver_lon"),
    )
    if receiver:
        points.append({
            "role": "Receptor",
            "name": item.get("meshcore_receiver_name") or "MeshNet",
            "lat": receiver[0],
            "lon": receiver[1],
        })
    return points


def _render_trace_html(item: dict[str, Any]) -> str:
    """Genera el mapa HTML de una recepción sin ejecutar datos del mensaje como HTML."""
    points = _trace_points(item)
    repeaters = item.get("meshcore_repeaters") or []
    located_repeaters = sum(
        1 for repeater in repeaters
        if isinstance(repeater, dict) and _valid_point(repeater.get("lat"), repeater.get("lon"))
    )
    total_repeaters = item.get("meshcore_path_len")
    try:
        total_repeaters = int(total_repeaters)
    except (TypeError, ValueError):
        total_repeaters = len(repeaters)

    path_text = html.escape(str(item.get("meshcore_path_text") or "desconocida"))
    message = html.escape(str(item.get("text") or ""))
    rssi = item.get("rx_rssi")
    snr = item.get("rx_snr")
    signal = []
    if rssi is not None:
        signal.append(f"RSSI {html.escape(str(rssi))} dBm")
    if snr is not None:
        signal.append(f"SNR {html.escape(str(snr))} dB")
    signal_text = " · ".join(signal) if signal else "sin métricas RF"

    data_json = json.dumps(points, ensure_ascii=False).replace("</", "<\\/")
    route_complete = bool(total_repeaters == located_repeaters)
    location_note = (
        f"{located_repeaters}/{total_repeaters} repetidores localizados"
        if total_repeaters
        else "ruta directa o sin repetidores"
    )

    return f"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Traza MeshCore</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
body{{margin:0;font-family:system-ui,-apple-system,sans-serif;background:#101418;color:#eef2f5}}
header{{padding:14px 18px;background:#182027;border-bottom:1px solid #34414b}}
h1{{font-size:18px;margin:0 0 8px}} .meta{{font-size:14px;line-height:1.5;color:#cbd5dc}}
#map{{height:calc(100vh - 150px);min-height:360px}} .empty{{padding:30px;text-align:center}}
</style>
</head>
<body>
<header>
<h1>Traza MeshCore RX</h1>
<div class="meta">Ruta: {path_text}<br>{html.escape(signal_text)} · {html.escape(location_note)}<br>Mensaje: {message}</div>
</header>
<div id="map"></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const points = {data_json};
const routeComplete = {str(route_complete).lower()};
const mapNode = document.getElementById('map');
if (!points.length) {{
  mapNode.className = 'empty';
  mapNode.textContent = 'La ruta fue recibida, pero ninguno de sus nodos tenía coordenadas conocidas.';
}} else {{
  const map = L.map('map');
  L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
    maxZoom: 19, attribution: '&copy; OpenStreetMap contributors'
  }}).addTo(map);
  const latlngs = [];
  points.forEach((point) => {{
    const ll = [point.lat, point.lon];
    latlngs.push(ll);
    const popup = document.createElement('div');
    const role = document.createElement('strong');
    role.textContent = point.role;
    popup.appendChild(role);
    popup.appendChild(document.createElement('br'));
    popup.appendChild(document.createTextNode(point.name));
    L.marker(ll).addTo(map).bindPopup(popup);
  }});
  // Una línea continua solo es fiel si todos los repetidores de la ruta
  // tienen posición. Con saltos sin localizar mostramos los marcadores
  // conocidos, pero no inventamos un tramo geográfico entre ellos.
  if (latlngs.length > 1 && routeComplete) L.polyline(latlngs).addTo(map);
  if (latlngs.length === 1) map.setView(latlngs[0], 13);
  else map.fitBounds(latlngs, {{padding:[30,30]}});
}}
</script>
</body>
</html>"""


def apply_meshcore_rx_trace_map(app: FastAPI) -> FastAPI:
    """Registra el visor RX MeshCore sobre la app existente del Control Panel.

    La ruta es exclusivamente GET y de solo lectura. No cambia la página principal,
    endpoints existentes, configuración, TX ni persistencia.
    """
    if getattr(app.state, "meshcore_rx_trace_map_installed", False):
        return app
    app.state.meshcore_rx_trace_map_installed = True

    @app.get("/meshcore/trace/{trace_id}", response_class=HTMLResponse)
    def meshcore_rx_trace(trace_id: str) -> HTMLResponse:
        if not _TRACE_ID_RE.fullmatch(str(trace_id or "")):
            raise HTTPException(status_code=404, detail="Traza no encontrada")
        item = _find_trace(trace_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Traza no encontrada")
        return HTMLResponse(_render_trace_html(item))

    return app
