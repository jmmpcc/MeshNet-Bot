#!/usr/bin/env python3 	   	 	 	    	   		 
# -*- coding: utf-8 -*- 	  	   	 	 	     	 	
""" 	  		 	 					 	  		 
web_admin.py v6.1.3 — Panel web Meshtastic_Broker	 		  	 	 			  		 		 

Novedades v1.4:	    	   			 		    	 
- Pestaña "🗺️ Mapa Live": mapa Leaflet que coloca/actualiza nodos en tiempo real (stream WebSocket)			 	   		  	 	 			 	
  y seed inicial desde FETCH_BACKLOG (POSITION_APP). Dibuja línea desde HOME_LAT/LON (si están).		 		    	 				  	 	 
- Mantiene las pestañas: Consola, Posiciones, Vecinos, Telemetría, Cobertura.					 			 		   		 		 
- Todas las pestañas de datos (Posiciones, Vecinos, Telemetría) tiran EXCLUSIVAMENTE de FETCH_BACKLOG.
- Sin f-strings dentro del HTML/JS para evitar conflictos con llaves.

Requisitos:
    pip install fastapi uvicorn websockets

Ejecución:
    python web_admin.py  -> http://localhost:8080

ENV interoperabilidad:
  BROKER_HOST, BROKER_PORT, BROKER_CTRL_HOST, BROKER_CTRL_PORT
  WEB_BIND, WEB_PORT
  BRIDGE_ENABLED (0|1), TAG_BRIDGE, TAG_BRIDGE_A2B, TAG_BRIDGE_B2A
  HOME_LAT, HOME_LON            (opcional) -> lat/lon del "home/base" para dibujar líneas en Mapa Live
  MAP_TILE_URL                  (opcional) -> plantilla de tiles Leaflet (OSM por defecto)
  MAP_ATTRIBUTION               (opcional) -> atribución de mapas
"""

import os
import json
import asyncio
import socket
import contextlib
import time
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse

# =================== CONFIG ===================

_TRUTHY = {"1", "true", "t", "yes", "y", "on", "si", "sí"}

BROKER_HOST = os.getenv("BROKER_HOST", "127.0.0.1").strip()
BROKER_PORT = int(os.getenv("BROKER_PORT", "8765"))
BROKER_CTRL_HOST = os.getenv("BROKER_CTRL_HOST", BROKER_HOST).strip()
BROKER_CTRL_PORT = int(os.getenv("BROKER_CTRL_PORT", str(BROKER_PORT + 1)))

WEB_BIND = os.getenv("WEB_BIND", "0.0.0.0").strip()
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))

BRIDGE_ENABLED = os.getenv("BRIDGE_ENABLED", "0").strip().lower() in _TRUTHY
TAG_BRIDGE = os.getenv("TAG_BRIDGE", "[BRIDGE]").strip()
TAG_BRIDGE_A2B = os.getenv("TAG_BRIDGE_A2B", f"{TAG_BRIDGE} A2B").strip()
TAG_BRIDGE_B2A = os.getenv("TAG_BRIDGE_B2A", f"{TAG_BRIDGE} B2A").strip()

# Mapa (HOME para líneas en Live Map)
HOME_LAT = os.getenv("HOME_LAT")
HOME_LON = os.getenv("HOME_LON")
try:
    HOME_LAT = float(HOME_LAT) if HOME_LAT is not None else None
    HOME_LON = float(HOME_LON) if HOME_LON is not None else None
except Exception:
    HOME_LAT, HOME_LON = None, None

MAP_TILE_URL = os.getenv("MAP_TILE_URL", "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png")
MAP_ATTRIBUTION = os.getenv("MAP_ATTRIBUTION", '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a>')

MAPS_DIR = os.path.abspath(os.path.join(os.getcwd(), "bot_data", "maps"))
os.makedirs(MAPS_DIR, exist_ok=True)

# =================== APP ======================

app = FastAPI(title="Meshtastic Broker — Web Admin", version="1.4")

# =================== HELPERS ==================

async def _fetch_backlog_since(since_ts: int) -> List[dict]:
    """
    Llama al BacklogServer con FETCH_BACKLOG y devuelve la lista de filas.
    Espera respuesta tipo {"ok": True, "rows": [...]}
    """
    # web_admin.py -> en _fetch_backlog_since()
    res = await _rpc_ctrl("FETCH_BACKLOG", {
        "since_ts": int(since_ts),
        "portnums": ["TEXT_MESSAGE_APP", "POSITION_APP", "NODEINFO_APP", "NEIGHBORINFO_APP"]
    }, 8.0)

    if not res.get("ok"):
        return []
    rows = res.get("rows") or res.get("data") or []
    # Asegura lista de dicts
    return [r for r in rows if isinstance(r, dict)]

def _pp(pkt: dict) -> dict:
    """Devuelve el payload 'packet' si existe; si no, el propio pkt."""
    return pkt.get("packet") or pkt

def _g(d: dict, *path, default=None):
    """Get anidado seguro: _g(pkt, 'decoded','position','latitude')"""
    cur = d
    for k in path:
        if not isinstance(cur, dict): 
            return default
        cur = cur.get(k)
    return cur if cur is not None else default

def _latlon(pkt: dict):
    p = _pp(pkt)
    # Soporta position.latitude / position.latitudeI (1e7)
    lat = _g(p, 'decoded', 'position', 'latitude')
    lon = _g(p, 'decoded', 'position', 'longitude')
    if lat is None and _g(p, 'decoded', 'position', 'latitudeI') is not None:
        lat = _g(p, 'decoded', 'position', 'latitudeI') / 1e7
        lon = _g(p, 'decoded', 'position', 'longitudeI') / 1e7
    return lat, lon

def _alias(pkt: dict):
    # Preferimos alias ya normalizado si viene a nivel superior
    p = _pp(pkt)
    return pkt.get('from_alias') or _g(p, 'from_alias') or _g(p, 'user', 'longName') \
        or _g(p, 'user', 'shortName') or _g(p, 'fromId') or _g(p, 'from')

def _rssi_snr(pkt: dict):
    p = _pp(pkt)
    rssi = _g(p, 'rxRssi')
    snr = _g(p, 'rxSnr')
    # Algunas canalizaciones lo ponen en summary
    rssi = rssi if rssi is not None else pkt.get('summary', {}).get('rssi')
    snr  = snr  if snr  is not None  else pkt.get('summary', {}).get('snr')
    return rssi, snr

def _ts(pkt: dict):
    p = _pp(pkt)
    # rxTime en segundos epoch si viene del broker; si no, intenta 'ts' ya normalizado
    return p.get('rxTime') or pkt.get('ts') or _now_epoch()


def _now_epoch() -> int:
    """Devuelve epoch (segundos) actual."""
    return int(time.time())

async def _rpc_ctrl(cmd: str, params: Optional[dict] = None, timeout: float = 8.0) -> dict:
    """
    Cliente ligero al BacklogServer del broker (puerto de control).
    Envía una línea JSON: {"cmd":..., "params":{...}} y espera 1 línea JSON de respuesta.
    - cmd: str  -> nombre del comando, p.ej. "FETCH_BACKLOG"
    - params: dict|None -> parámetros del comando
    - timeout: float -> segundos de timeout de socket
    Retorna: dict con al menos {"ok": bool, ...}
    """
    msg = (json.dumps({"cmd": cmd, "params": params or {}}, ensure_ascii=False) + "\n").encode("utf-8")
    loop = asyncio.get_running_loop()

    def _do():
        s = None
        try:
            s = socket.create_connection((BROKER_CTRL_HOST, BROKER_CTRL_PORT), timeout=timeout)
            s.sendall(msg)
            s.settimeout(timeout)
            buf = b""
            while True:
                ch = s.recv(65536)
                if not ch:
                    break
                buf += ch
                if b"\n" in buf:
                    break
            line = buf.decode("utf-8", "ignore").strip()
            return json.loads(line) if line else {"ok": False, "error": "empty_response"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        finally:
            with contextlib.suppress(Exception):
                if s:
                    s.close()
    return await loop.run_in_executor(None, _do)

async def _proxy_broker_stream(ws: WebSocket):
    """
    Reenvía el stream JSONL en tiempo real del broker (puerto principal) al navegador por WebSocket.
    - Este stream alimenta la pestaña “Consola” y el “Mapa Live” (para eventos POSITION_APP).
    """
    await ws.accept()
    while True:
        try:
            reader, _ = await asyncio.open_connection(BROKER_HOST, BROKER_PORT)
            while True:
                line = await reader.readline()
                if not line:
                    break
                await ws.send_text(line.decode("utf-8", "replace").strip())
        except (ConnectionRefusedError, OSError):
            await asyncio.sleep(1)
        except WebSocketDisconnect:
            break
        except Exception:
            await asyncio.sleep(1)
    with contextlib.suppress(Exception):
        await ws.close()

# =================== API CORE =======================

@app.get("/", response_class=HTMLResponse)
async def index():
    """Entrega el HTML único de la interfaz web."""
    return HTMLResponse(_HTML_PAGE)

@app.get("/api/status")
async def api_status():
    """Retorna estado resumido del broker (BROKER_STATUS)."""
    res = await _rpc_ctrl("BROKER_STATUS", None, 4.0)
    return JSONResponse(res, status_code=200 if res.get("ok") else 503)

@app.post("/api/pause")
async def api_pause():
    """Pausa el broker (BROKER_PAUSE)."""
    res = await _rpc_ctrl("BROKER_PAUSE", None, 4.0)
    return JSONResponse(res, status_code=200 if res.get("ok") else 503)

@app.post("/api/resume")
async def api_resume():
    """Reanuda el broker (BROKER_RESUME)."""
    res = await _rpc_ctrl("BROKER_RESUME", None, 4.0)
    return JSONResponse(res, status_code=200 if res.get("ok") else 503)

@app.post("/api/send")
async def api_send(req: Request):
    """
    Envía texto por la malla desde el broker.
    Body JSON: {"text": str, "ch": int, "dest": null|"broadcast"|"!id", "ack": bool, "via": "A"|"B"}
    - via "A": usa SEND_TEXT
    - via "B": usa SEND_TEXT_VIA (si existe el handler en BacklogServer; de lo contrario, dará error).
    """
    body = await req.json()
    text = (body.get("text") or "").strip()
    ch = int(body.get("ch") or 0)
    dest = body.get("dest", None)
    ack = bool(body.get("ack", False))
    via = (body.get("via") or "A").upper()

    if not text:
        return JSONResponse({"ok": False, "error": "empty_text"}, status_code=400)

    if via == "B":
        res = await _rpc_ctrl("SEND_TEXT_VIA", {"side": "B", "text": text, "ch": ch}, 6.0)
        return JSONResponse(res, status_code=200 if res.get("ok") else 400)

    params = {
        "text": text,
        "ch": ch,
        "dest": (None if not dest or str(dest).lower() == "broadcast" else str(dest)),
        "ack": 1 if ack else 0
    }
    res = await _rpc_ctrl("SEND_TEXT", params, 5.0)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)

@app.get("/api/bridge_status")
async def api_bridge_status():
    """
    Devuelve estado del bridge embebido (si BRIDGE_ENABLED).
    - Si no hay handler BRIDGE_STATUS en el broker, devuelve 'patched': False.
    """
    if not BRIDGE_ENABLED:
        return JSONResponse({"ok": True, "enabled": False}, status_code=200)
    res = await _rpc_ctrl("BRIDGE_STATUS", None, 3.0)
    if not res.get("ok"):
        return JSONResponse({"ok": True, "enabled": True, "patched": False}, status_code=200)
    return JSONResponse({"ok": True, "enabled": True, "patched": True, **res}, status_code=200)

# =================== API: POSICIONES =================

@app.get("/api/positions")
async def api_positions(minutes: int = 60, max_nodes: int = 0):
    """
    Devuelve último mensaje con posición por nodo en la ventana 'minutes'.
    Respuesta: {"ok": True, "data": [ {"id","alias","lat","lon","snr","rssi","ts"} ... ]}
    """
    since_ts = _now_epoch() - minutes * 60
    rows = await _fetch_backlog_since(since_ts)

    latest_by_node = {}
    for raw in rows:
        p = _pp(raw)
        # Filtra POSITION_APP o cualquier paquete con lat/lon válidos
        port = _g(p, 'decoded', 'portnum')
        lat, lon = _latlon(raw)
        if port != 'POSITION_APP' and (lat is None or lon is None):
            continue

        node_id = _g(p, 'fromId') or _g(p, 'from') or _g(p, 'user', 'id')
        if not node_id:
            continue

        ts = _ts(raw)
        if ts < since_ts:
            continue

        alias = _alias(raw)
        rssi, snr = _rssi_snr(raw)
        prev = latest_by_node.get(node_id)
        if not prev or ts > prev["ts"]:
            latest_by_node[node_id] = {
                "id": node_id,
                "alias": alias,
                "lat": lat, "lon": lon,
                "rssi": rssi, "snr": snr,
                "ts": ts,
            }

    data = list(latest_by_node.values())
    # Limita opcionalmente
    if max_nodes and len(data) > max_nodes:
        data.sort(key=lambda r: r["ts"], reverse=True)
        data = data[:max_nodes]

    return {"ok": True, "data": data}

# =================== API: VECINOS ====================

@app.get("/api/vecinos")
async def api_vecinos(minutes: int = 60, min_edges: int = 1, max_hops: int = 2):
    since_ts = _now_epoch() - minutes * 60
    rows = await _fetch_backlog_since(since_ts)

    nodes = {}   # node_id -> alias
    edges = {}   # (src, dst) -> count
    relayed_src = set()  # fuentes que claramente pasaron por relay (>=1 salto)
    direct_pairs = set() # (src,dst) directos

    def _bump(a,b):
        if not a or not b or a == b:
            return
        k = (a,b)
        edges[k] = edges.get(k, 0) + 1

    for raw in rows:
        p = _pp(raw)
        if _g(p,'encrypted'):
            continue  # ignoramos frames cifrados

        ts = _ts(raw)
        if ts < since_ts:
            continue

        src = _g(p, 'fromId') or _g(p, 'from')
        dst = _g(p, 'toId') or _g(p, 'to')
        if src: nodes[src] = nodes.get(src) or _alias(raw)

        # directos (no broadcast)
        if dst and not str(dst).startswith('^'):
            nodes[dst] = nodes.get(dst) or dst
            _bump(src, dst)
            direct_pairs.add((src, dst))

        # marca “relayed” si vemos relayNode o hopLimit < hopStart
        relay = _g(p, 'relayNode')
        hop_start = _g(p, 'hopStart'); hop_limit = _g(p, 'hopLimit')
        if relay is not None or (hop_start is not None and hop_limit is not None and hop_limit < hop_start):
            relayed_src.add(src)

    # Filtra aristas por umbral
    edata = [{"src":a,"dst":b,"count":c} for (a,b),c in edges.items() if c >= min_edges]
    ndata = [{"id": nid, "alias": nodes[nid]} for nid in nodes]

    # Estimación simple de hops por nodo emisor:
    #  - 0 si hemos visto un par directo src->dst
    #  - 1 si lo hemos marcado como relayed
    #  - limitado por max_hops
    degree_direct = {a:0 for (a,_) in direct_pairs}
    flat = []
    seen = set()
    now_ts = _now_epoch()
    for nid in nodes:
        if nid in seen:
            continue
        hops = None
        if nid in degree_direct:
            hops = 0
        elif nid in relayed_src:
            hops = 1
        if hops is not None and hops <= max_hops:
            flat.append({
                "id": nid,
                "alias": nodes[nid],
                "hops": hops,
                "snr": None,
                "rssi": None,
                "ts": now_ts
            })
            seen.add(nid)

    # Ordena por aristas salientes
    out_deg = {}
    for e in edata: out_deg[e["src"]] = out_deg.get(e["src"], 0) + e["count"]
    ndata.sort(key=lambda x: out_deg.get(x["id"], 0), reverse=True)

    return {"ok": True, "nodes": ndata, "edges": edata, "data": flat}


# =================== API: TELEMETRÍA =================

@app.get("/api/telemetria/ultimos")
async def api_telemetria_ultimos(minutes: int = 180):
    since_ts = _now_epoch() - minutes * 60
    rows = await _fetch_backlog_since(since_ts)

    def _pick(d: dict, *keys, default=None):
        for k in keys:
            if k in d and d[k] is not None:
                return d[k]
        return default

    def _extract_metrics(row: dict) -> dict:
        """
        Normaliza métricas aunque vengan anidadas o con nombres 'Meshtastic':
        - decoded.telemetry.deviceMetrics: batteryLevel, voltage, channelUtilization, airUtilTx...
        - decoded.telemetry.environmentMetrics: temperature, relativeHumidity, barometricPressure...
        - Alternativas planas: telemetry / telemetry_data / metrics
        """
        def _pick(d: dict, *keys, default=None):
            for k in keys:
                if isinstance(d, dict) and (k in d) and (d[k] is not None):
                    return d[k]
            return default

        out = {"battery": None, "voltage": None, "temperature": None, "humidity": None, "pressure": None}

        # 1) Localiza el contenedor de telemetría principal
        telem = {}
        if isinstance(row, dict):
            telem = (
                (row.get("decoded") or {}).get("telemetry")
                or row.get("telemetry")
                or row.get("telemetry_data")
                or row.get("metrics")
                or {}
            )
        if not isinstance(telem, dict):
            return out

        dev = telem.get("deviceMetrics") or {}
        env = telem.get("environmentMetrics") or {}

        # 2) Normaliza valores
        out["battery"]     = _pick(dev, "batteryLevel", "battery", default=None)
        out["voltage"]     = _pick(dev, "voltage", "batVoltage", default=None)
        out["temperature"] = _pick(env, "temperature", "temp", default=None)
        out["humidity"]    = _pick(env, "relativeHumidity", "humidity", default=None)
        out["pressure"]    = _pick(env, "barometricPressure", "pressure", default=None)

        # 3) Casts suaves
        for k in ("battery", "voltage", "temperature", "humidity", "pressure"):
            v = out[k]
            try:
                if v is None:
                    continue
                # strings tipo "12.3V" -> 12.3
                if isinstance(v, str):
                    import re
                    m = re.search(r"[-+]?\d+(\.\d+)?", v)
                    v = float(m.group(0)) if m else None
                out[k] = float(v)
            except Exception:
                out[k] = None

        # 4) Rangos razonables / limpieza
        if out["battery"] is not None:
            # si viene 0..1, pásalo a %
            if 0.0 <= out["battery"] <= 1.0:
                out["battery"] = round(out["battery"] * 100, 2)
            elif 1.0 < out["battery"] <= 100.0:
                out["battery"] = round(out["battery"], 2)
            else:
                out["battery"] = None
        if out["humidity"] is not None:
            if 0.0 <= out["humidity"] <= 100.0:
                out["humidity"] = round(out["humidity"], 2)
            else:
                out["humidity"] = None
        for k in ("voltage","temperature","pressure"):
            if out[k] is not None:
                out[k] = round(out[k], 2)

        return out

    
    latest = {}  # node_id -> registro con métricas más reciente
    for raw in rows:
        p = _pp(raw)
        if _g(p, 'decoded', 'portnum') not in ('TELEMETRY_APP', 'TELEMETRY'):  # admite alias si tu broker usa otro nombre
            # Aun así, algunos brokers ya “aplanan”; detecta bloque telemetry
            if not _g(p, 'decoded', 'telemetry') and not raw.get('telemetry'):
                continue

        node_id = _g(p, 'fromId') or _g(p, 'from') or _g(p, 'user', 'id')
        if not node_id:
            continue

        ts = _ts(raw)
        if ts < since_ts:
            continue

        alias = _alias(raw)
        metrics = _extract_metrics(p if 'decoded' in p else raw)
        rec = {
            "id": node_id,
            "alias": alias,
            "ts": ts,
            **metrics
        }
        prev = latest.get(node_id)
        if not prev or ts > prev["ts"]:
            latest[node_id] = rec

    return {"ok": True, "data": list(latest.values())}

# =================== API: COBERTURA (HEATMAP) ========

@app.post("/api/cobertura")
async def api_cobertura_post(req: Request):
    body = await req.json()
    hours = int(body.get("hours") or 24)
    target = body.get("target_node")
    env = (body.get("env") or "auto")

    since_ts = _now_epoch() - hours * 3600
    rows = await _fetch_backlog_since(since_ts)

    samples = []
    for raw in rows:
        p = _pp(raw)
        if _g(p, 'decoded', 'portnum') != 'POSITION_APP':
            continue
        lat, lon = _latlon(raw)
        if lat is None or lon is None:
            continue
        if target and ( (_g(p,'toId') or _g(p,'to')) != target and (_g(p,'fromId') or _g(p,'from')) != target ):
            continue
        rssi, snr = _rssi_snr(raw)
        canal = _g(p, 'meta', 'channelIndex') or _g(p, 'channel')
        alias = _alias(raw)
        ts = _ts(raw)
        samples.append({"alias": alias, "lat": lat, "lon": lon, "rssi": rssi, "snr": snr, "canal": canal, "ts": ts})

    # Genera HTML Leaflet simple en bot_data/maps
    fname = f"coverage_{int(time.time())}.html"
    full = os.path.join(MAPS_DIR, fname)
    points_js = json.dumps(samples, ensure_ascii=False)

    with open(full, "w", encoding="utf-8") as f:
        f.write(f"""
    <!DOCTYPE html><html><head><meta charset="utf-8"/>
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <style>html,body,#map{{height:100%;margin:0}}</style></head>
    <body><div id="map"></div>
    <script>
      var map = L.map('map').setView([41.65,-0.88], 8);
      L.tileLayer('{MAP_TILE_URL}', {{ attribution: `{MAP_ATTRIBUTION}` }}).addTo(map);

      var pts = {points_js};
      var group = [];
      pts.forEach(function(s){{ 
        var m = L.circleMarker([s.lat, s.lon], {{ radius:5, opacity:0.7, fillOpacity:0.5 }}).addTo(map)
          .bindPopup((s.alias||'-') + '<br/>RSSI: ' + (s.rssi ?? '-') + '<br/>SNR: ' + (s.snr ?? '-'));
        group.push(m); 
      }});
      if (group.length){{ 
        var fg = L.featureGroup(group); 
        map.fitBounds(fg.getBounds().pad(0.2)); 
      }}
    </script></body></html>
    """)




    return {"ok": True, "html": _to_maps_url(full), "kml": None}


def _to_maps_url(path: Optional[str]) -> Optional[str]:
    """Convierte una ruta absoluta en URL servida por /maps/.."""
    if not path:
        return None
    fname = os.path.basename(path)
    return f"/maps/{fname}"

@app.get("/maps/{fname}")
async def serve_map(fname: str):
    """
    Sirve archivos de mapas (HTML/KML/KMZ) desde bot_data/maps.
    """
    safe = fname.replace("/", "").replace("\\", "")
    full = os.path.join(MAPS_DIR, safe)
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="map_not_found")
    if full.lower().endswith(".html"):
        return FileResponse(full, media_type="text/html")
    if full.lower().endswith(".kml"):
        return FileResponse(full, media_type="application/vnd.google-earth.kml+xml")
    if full.lower().endswith(".kmz"):
        return FileResponse(full, media_type="application/vnd.google-earth.kmz")
    return FileResponse(full)

# =================== API: HOME INFO (mapa) ===========

@app.get("/api/home")
async def api_home():
    """
    Devuelve configuración para el Mapa Live:
    - Coordenadas HOME (si están definidas)
    - Plantilla de tiles y atribución
    """
    return JSONResponse({
        "ok": True,
        "home": {"lat": HOME_LAT, "lon": HOME_LON},
        "tiles": {"url": MAP_TILE_URL, "attribution": MAP_ATTRIBUTION}
    }, status_code=200)

# =================== WS STREAM =======================

@app.websocket("/ws/events")
async def ws_events(ws: WebSocket):
    """WebSocket para el stream JSONL del broker (consola & mapa live)."""
    try:
        await _proxy_broker_stream(ws)
    except WebSocketDisconnect:
        return

# =================== UI (HTML/CSS/JS) ================

_HTML_PAGE = r"""
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Meshtastic Broker — Web Admin</title>
<link rel="preconnect" href="https://unpkg.com"/>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" integrity="" crossorigin=""/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" integrity="" crossorigin=""></script>
<style>
:root {
  --bg:#0b1320; --card:#121a2b; --ink:#e6eef7; --muted:#9fb0c7;
  --ok:#2dd4bf; --warn:#f59e0b; --bad:#ef4444; --btn:#1f2a44; --accent:#7dd3fc;
  --chip:#203153; --border:#1b2741; --ink2:#cfe3ff;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:system-ui,Segoe UI,Roboto}
header{padding:1rem;border-bottom:1px solid var(--border);background:var(--card);display:flex;gap:.75rem;align-items:center}
.tag{background:var(--chip);color:#a7c7ff;padding:.3rem .6rem;border-radius:.6rem}
.wrap{max-width:1300px;margin:0 auto;padding:1rem;display:grid;grid-template-columns:380px 1fr;gap:1rem}
.card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:1rem;box-shadow:0 10px 30px rgba(0,0,0,.25)}
h2{margin:.2rem 0 1rem 0;font-size:1rem;color:var(--ink2);letter-spacing:.02em}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:.5rem}
button,input,select{background:var(--btn);color:var(--ink);border:1px solid #2a3a60;border-radius:10px;padding:.55rem .75rem}
button{cursor:pointer} button:hover{filter:brightness(1.08)}
.log{height:400px;overflow:auto;background:#0a101c;border:1px solid var(--border);border-radius:10px;padding:.75rem;font-family:ui-monospace,Consolas,monospace}
.row{padding:.15rem .25rem;border-bottom:1px dashed #1c2640}
.muted{color:var(--muted)}
.ok{color:var(--ok)} .bad{color:var(--bad)} .warn{color:var(--warn)}
.tabs{display:flex;gap:.5rem;margin-bottom:.5rem;flex-wrap:wrap}
.tab{padding:.35rem .6rem;border-radius:.6rem;border:1px solid #2a3a60;background:#17223b;cursor:pointer}
.tab.active{background:#1e2c4b;}
.kv{display:grid;grid-template-columns:110px 1fr;gap:.25rem .75rem;align-items:center}
.badge{display:inline-block;padding:.1rem .45rem;border-radius:.35rem;font-size:.85rem;border:1px solid #28406b;background:#17223b}
.badge.snr-good{background:rgba(34,197,94,.18);border-color:#22c55e;color:#bbf7d0}
.badge.snr-mid{background:rgba(245,158,11,.15);border-color:#f59e0b;color:#fde68a}
.badge.snr-bad{background:rgba(239,68,68,.15);border-color:#ef4444;color:#fecaca}
.small{font-size:.85rem}
.list{display:grid;gap:.35rem}
.item{padding:.55rem;border:1px solid var(--border);border-radius:10px;background:#0f1728;display:flex;justify-content:space-between;align-items:center}
.left{display:flex;gap:.6rem;align-items:center}
.icon{font-size:1.1rem}
.right{text-align:right}
a.link{color:var(--accent);text-decoration:none}
a.link:hover{text-decoration:underline}
iframe.map{width:100%;height:520px;border:1px solid var(--border);border-radius:12px;background:#0a101c}
.hr{height:1px;background:#1b2741;margin:.75rem 0}
#liveMap{width:100%;height:520px;border:1px solid var(--border);border-radius:12px}
</style>
</head>
<body>
<header>
  <div class="tag">🛰️ Meshtastic Broker — Web Admin</div>
  <div id="statusLine" class="muted">Estado: …</div>
</header>

<div class="wrap">
  <!-- Columna izquierda: control -->
  <div class="card">
    <h2>⚙️ Estado & Control</h2>
    <div class="grid2" style="margin-bottom:.5rem">
      <button id="btnRefresh">🔄 Estado</button>
      <button id="btnPause">⏸️ Pausar</button>
      <button id="btnResume">▶️ Reanudar</button>
      <button id="btnBacklog">🗂️ Status JSON</button>
    </div>
    <div class="kv small" style="margin-bottom:.75rem">
      <div>Conectado</div><div id="stConnected" class="muted">-</div>
      <div>Estado</div><div id="stStatus" class="muted">-</div>
      <div>Cooldown</div><div id="stCooldown" class="muted">-</div>
      <div>Nodo</div><div id="stNode" class="muted">-</div>
      <div>Versión</div><div id="stVer" class="muted">-</div>
      <div>Desde</div><div id="stSince" class="muted">-</div>
    </div>

    <div class="hr"></div>
    <h2>📤 Enviar mensaje</h2>
    <div style="margin-bottom:.5rem">
      <input id="txText" placeholder="Mensaje…" style="width:100%"/>
    </div>
    <div class="grid2" style="margin-bottom:.5rem">
      <div><input id="txCh" type="number" value="0" style="width:80px"/></div>
      <div><input id="txDest" placeholder="broadcast o !id"/></div>
    </div>
    <div class="grid2">
      <label class="small"><input type="checkbox" id="txAck"/> ACK</label>
      <div>
        <select id="txVia">
          <option value="A">Vía Nodo A</option>
          <option value="B">Vía Nodo B</option>
        </select>
      </div>
      <button id="btnSend">📨 Enviar</button>
    </div>

    <div class="hr"></div>
    <h2>🔁 Bridge</h2>
    <div id="bridgeBox" class="muted small">Comprobando…</div>
  </div>

  <!-- Columna derecha: tabs y vistas -->
  <div class="card">
    <div class="tabs">
      <div class="tab active" id="tabLog">📟 Consola</div>
      <div class="tab" id="tabPos">📡 Posiciones</div>
      <div class="tab" id="tabVec">🧭 Vecinos</div>
      <div class="tab" id="tabTel">📈 Telemetría</div>
      <div class="tab" id="tabCov">🗺️ Cobertura</div>
      <div class="tab" id="tabMap">🗺️ Mapa Live</div>
    </div>

    <!-- Consola -->
    <div id="viewLog">
      <div id="log" class="log"></div>
    </div>

    <!-- Posiciones -->
    <div id="viewPos" style="display:none">
      <div style="display:flex;gap:.5rem;align-items:center;margin-bottom:.5rem">
        <span class="small muted">Ventana</span>
        <select id="posMinutes">
          <option value="30">30 min</option>
          <option value="60" selected>60 min</option>
          <option value="180">3 h</option>
          <option value="1440">24 h</option>
        </select>
        <button id="btnLoadPos">📡 Cargar</button>
      </div>
      <div id="posList" class="list"></div>
    </div>

    <!-- Vecinos -->
    <div id="viewVec" style="display:none">
      <div style="display:flex;gap:.5rem;align-items:center;margin-bottom:.5rem;flex-wrap:wrap">
        <span class="small muted">Ventana</span>
        <select id="vecMinutes">
          <option value="30">30 min</option>
          <option value="60" selected>60 min</option>
          <option value="180">3 h</option>
          <option value="1440">24 h</option>
        </select>
        <span class="small muted">Máx. hops</span>
        <select id="vecHops">
          <option value="0">0 (Directo)</option>
          <option value="1" selected>1</option>
          <option value="2">2</option>
        </select>
        <button id="btnLoadVec">🧭 Cargar</button>
      </div>
      <div id="vecList" class="list"></div>
    </div>

    <!-- Telemetría -->
    <div id="viewTel" style="display:none">
      <div style="display:flex;gap:.5rem;align-items:center;margin-bottom:.5rem;flex-wrap:wrap">
        <span class="small muted">Ventana</span>
        <select id="telMinutes">
          <option value="60">60 min</option>
          <option value="180" selected>3 h</option>
          <option value="720">12 h</option>
          <option value="1440">24 h</option>
        </select>
        <button id="btnLoadTel">📈 Cargar</button>
      </div>
      <div id="telList" class="list"></div>
    </div>

    <!-- Cobertura -->
    <div id="viewCov" style="display:none">
      <div style="display:flex;gap:.5rem;align-items:center;margin-bottom:.75rem;flex-wrap:wrap">
        <span class="small muted">Horas</span>
        <select id="covHours">
          <option value="6">6 h</option>
          <option value="12">12 h</option>
          <option value="24" selected>24 h</option>
          <option value="48">48 h</option>
          <option value="72">72 h</option>
        </select>
        <input id="covTarget" placeholder="!id objetivo (opcional)"/>
        <select id="covEnv">
          <option value="auto" selected>Auto</option>
          <option value="urbano">Urbano</option>
          <option value="rural">Rural</option>
          <option value="montaña">Montaña</option>
        </select>
        <button id="btnCov">🔥 Generar mapa</button>
        <a id="btnKml" class="link small" href="#" style="display:none">Descargar KML</a>
      </div>
      <iframe id="covFrame" class="map"></iframe>
    </div>

    <!-- Mapa Live -->
    <div id="viewMap" style="display:none">
      <div style="display:flex;gap:.5rem;align-items:center;margin-bottom:.5rem;flex-wrap:wrap">
        <span class="small muted">Semilla</span>
        <select id="mapSeedMinutes">
          <option value="60" selected>60 min</option>
          <option value="180">3 h</option>
          <option value="720">12 h</option>
          <option value="1440">24 h</option>
        </select>
        <button id="btnMapSeed">🌱 Cargar seed</button>
        <span class="small muted">Home:</span><span id="homeInfo" class="small muted">–</span>
      </div>
      <div id="liveMap"></div>
    </div>

  </div>
</div>

<script>
const $ = (s)=>document.querySelector(s);
const log = $("#log");
const statusLine = $("#statusLine");

// Tabs
const tabLog=$("#tabLog"), tabPos=$("#tabPos"), tabVec=$("#tabVec"), tabTel=$("#tabTel"), tabCov=$("#tabCov"), tabMap=$("#tabMap");
const viewLog=$("#viewLog"), viewPos=$("#viewPos"), viewVec=$("#viewVec"), viewTel=$("#viewTel"), viewCov=$("#viewCov"), viewMap=$("#viewMap");
function setTab(t){
  [tabLog,tabPos,tabVec,tabTel,tabCov,tabMap].forEach(x=>x.classList.remove("active"));
  [viewLog,viewPos,viewVec,viewTel,viewCov,viewMap].forEach(x=>x.style.display="none");
  if(t==="log"){ tabLog.classList.add("active"); viewLog.style.display="block"; }
  if(t==="pos"){ tabPos.classList.add("active"); viewPos.style.display="block"; }
  if(t==="vec"){ tabVec.classList.add("active"); viewVec.style.display="block"; }
  if(t==="tel"){ tabTel.classList.add("active"); viewTel.style.display="block"; }
  if(t==="cov"){ tabCov.classList.add("active"); viewCov.style.display="block"; }
  if(t==="map"){ tabMap.classList.add("active"); viewMap.style.display="block"; setTimeout(initLiveMapOnce, 50); }
}
tabLog.onclick=()=>setTab("log");
tabPos.onclick=()=>setTab("pos");
tabVec.onclick=()=>setTab("vec");
tabTel.onclick=()=>setTab("tel");
tabCov.onclick=()=>setTab("cov");
tabMap.onclick=()=>setTab("map");

function row(html){ const d=document.createElement("div"); d.className="row"; d.innerHTML=html; return d; }
function addLog(s){ log.appendChild(row(s)); log.scrollTop=log.scrollHeight; }

async function call(method, url, body){
  const opt = { method, headers: {"Content-Type":"application/json"} };
  if(body) opt.body = JSON.stringify(body);
  const r = await fetch(url, opt);
  let j = null;
  try{ j = await r.json(); }catch{ j = {ok:false,error:"bad_json"}; }
  return j;
}

// Estado & control
async function refresh(){
  const r = await call("GET","/api/status");
  if(!r.ok){ statusLine.textContent = "Estado: KO ("+(r.error||"")+")"; return; }
  statusLine.textContent = "Estado: OK";
  $("#stConnected").textContent = String(!!r.connected);
  $("#stStatus").textContent    = r.status || "-";
  $("#stCooldown").textContent  = (r.cooldown_remaining!=null? r.cooldown_remaining+"s":"-");
  $("#stNode").textContent      = (r.node_host? r.node_host+" : "+(r.node_port||""): "-");
  $("#stVer").textContent       = r.version || "-";
  $("#stSince").textContent     = r.since || "-";

  const b = await call("GET","/api/bridge_status");
  const el=document.getElementById("bridgeBox");
  if(el && b && b.ok){
    if(b.enabled===false){ el.textContent = "Bridge: deshabilitado"; }
    else{
      const patched=(b.patched===true);
      let t='Bridge: habilitado';
      if(patched && b.running!==undefined){
        t+= b.running?' (RUNNING)':' (STOPPED)';
        if(b.local_id_a) t+=' • A='+b.local_id_a;
        if(b.local_id_b) t+=' • B='+b.local_id_b;
      } else { t+=' (parche BacklogServer no detectado)'; }
      el.textContent=t;
    }
  }
}
$("#btnRefresh").onclick = refresh;
$("#btnPause").onclick   = async()=>{ const r=await call("POST","/api/pause"); addLog(`<span class="warn">[pause]</span> ${JSON.stringify(r)}`); refresh(); };
$("#btnResume").onclick  = async()=>{ const r=await call("POST","/api/resume"); addLog(`<span class="ok">[resume]</span> ${JSON.stringify(r)}`); refresh(); };

// Enviar
$("#btnSend").onclick    = async()=>{
  const text=($("#txText").value||'').trim();
  const ch=parseInt($("#txCh").value||"0");
  const dest=($("#txDest").value||'').trim()||null;
  const ack=$("#txAck").checked;
  const via=$("#txVia").value||"A";
  if(!text){ alert("Escribe un texto"); return; }
  const r = await call("POST","/api/send",{text,ch,dest,ack,via});
  addLog(`<span class="ok">[send via ${via}]</span> ${JSON.stringify(r)}`);
};

// Status JSON (atajo)
$("#btnBacklog").onclick = async()=>{
  const r = await call("GET", `/api/status`);
  addLog(`<span class="tag">[status]</span> ${JSON.stringify(r)}`);
};

// ======= POSICIONES =======
function badgeSnr(v){
  if(v==null || isNaN(v)) return `<span class="badge">SNR: –</span>`;
  const n = Number(v);
  if(n >= 10) return `<span class="badge snr-good">SNR: ${n.toFixed(1)}</span>`;
  if(n >= 0)  return `<span class="badge snr-mid">SNR: ${n.toFixed(1)}</span>`;
  return `<span class="badge snr-bad">SNR: ${n.toFixed(1)}</span>`;
}
function ago(ts){
  const now = Math.floor(Date.now()/1000);
  const diff = Math.max(0, now - (parseInt(ts)||now));
  if(diff<60) return `${diff}s`;
  const m=Math.floor(diff/60); if(m<60) return `${m}m`;
  const h=Math.floor(m/60); if(h<48) return `${h}h`;
  const d=Math.floor(h/24); return `${d}d`;
}
function renderPosItem(it){
  const alias = (it.alias||"").trim();
  const name = alias? `${alias} <span class="muted">(${it.id||"-"})</span>` : (it.id||"-");
  const snr = badgeSnr(it.snr);
  const rssi = (it.rssi!=null? `<span class="badge">RSSI: ${it.rssi}</span>`:"");
  const when = `<span class="badge">${ago(it.ts)}</span>`;
  const ll = (it.lat!=null && it.lon!=null)? `<span class="badge">📍 ${Number(it.lat).toFixed(5)}, ${Number(it.lon).toFixed(5)}</span>` : `<span class="badge">📍 –</span>`;
  return `
    <div class="item">
      <div class="left">
        <div class="icon">📡</div>
        <div>
          <div>${name}</div>
          <div class="small muted">${ll} ${snr} ${rssi}</div>
        </div>
      </div>
      <div class="right small">${when}</div>
    </div>`;
}
async function loadPositions(){
  const mins = parseInt($("#posMinutes").value||"60");
  const r = await call("GET", `/api/positions?minutes=${mins}&max_nodes=0`);
  const box = $("#posList");
  box.innerHTML = "";
  if(!r.ok){ box.innerHTML = `<div class="muted">No disponible: ${r.error||"error"}</div>`; return; }
  const data = r.data||[];
  if(!data.length){ box.innerHTML = `<div class="muted">Sin datos en la ventana seleccionada.</div>`; return; }
  const fr = document.createDocumentFragment();
  for(const it of data){ const d=document.createElement("div"); d.innerHTML=renderPosItem(it); fr.appendChild(d.firstElementChild); }
  box.appendChild(fr);
}
$("#btnLoadPos").onclick = loadPositions;

// ======= VECINOS =======
function renderVecItem(it){
  const alias = (it.alias||"").trim();
  const name = alias? `${alias} <span class="muted">(${it.id||"-"})</span>` : (it.id||"-");
  const hops = (it.hops==null? "–" : it.hops);
  const cls = (it.hops===0? "snr-good" : (it.hops===1? "snr-mid" : ""));
  const bhops = `<span class="badge ${cls}">HOPS: ${hops}</span>`;
  const snr = (it.snr!=null? badgeSnr(it.snr): "");
  const rssi = (it.rssi!=null? `<span class="badge">RSSI: ${it.rssi}</span>`:"");
  const when = `<span class="badge">${ago(it.ts)}</span>`;
  return `
    <div class="item">
      <div class="left">
        <div class="icon">🧭</div>
        <div>
          <div>${name}</div>
          <div class="small muted">${bhops} ${snr} ${rssi}</div>
        </div>
      </div>
      <div class="right small">${when}</div>
    </div>`;
}
async function loadVecinos(){
  const mins = parseInt($("#vecMinutes").value||"60");
  const hops = parseInt($("#vecHops").value||"1");
  const r = await call("GET", `/api/vecinos?minutes=${mins}&max_hops=${hops}`);
  const box = $("#vecList");
  box.innerHTML="";
  if(!r.ok){ box.innerHTML = `<div class="muted">No disponible: ${r.error||"error"}</div>`; return; }
  const data = r.data||[];
  if(!data.length){ box.innerHTML = `<div class="muted">Sin datos en la ventana seleccionada.</div>`; return; }
  const fr = document.createDocumentFragment();
  for(const it of data){ const d=document.createElement("div"); d.innerHTML=renderVecItem(it); fr.appendChild(d.firstElementChild); }
  box.appendChild(fr);
}
$("#btnLoadVec").onclick = loadVecinos;

// ======= TELEMETRÍA =======
function renderTelItem(it){
  const alias = (it.alias||"").trim();
  const name = alias? `${alias} <span class="muted">(${it.id||"-"})</span>` : (it.id||"-");
  const batt = (it.battery!=null? `<span class="badge">🔋 ${it.battery}%</span>` : "");
  const volt = (it.voltage!=null? `<span class="badge">⚡ ${it.voltage}V</span>` : "");
  const temp = (it.temperature!=null? `<span class="badge">🌡️ ${it.temperature}°C</span>` : "");
  const hum  = (it.humidity!=null? `<span class="badge">💧 ${it.humidity}%</span>` : "");
  const pres = (it.pressure!=null? `<span class="badge">🧭 ${it.pressure}hPa</span>` : "");
  const when = `<span class="badge">${ago(it.ts)}</span>`;
  return `
    <div class="item">
      <div class="left">
        <div class="icon">📈</div>
        <div>
          <div>${name}</div>
          <div class="small muted">${batt} ${volt} ${temp} ${hum} ${pres}</div>
        </div>
      </div>
      <div class="right small">${when}</div>
    </div>`;
}
async function loadTelemetria(){
  const mins = parseInt($("#telMinutes").value||"180");
  const r = await call("GET", `/api/telemetria/ultimos?minutes=${mins}`);
  const box = $("#telList");
  box.innerHTML="";
  if(!r.ok){ box.innerHTML = `<div class="muted">No disponible: ${r.error||"error"}</div>`; return; }
  const data = r.data||[];
  if(!data.length){ box.innerHTML = `<div class="muted">Sin datos en la ventana seleccionada.</div>`; return; }
  const fr = document.createDocumentFragment();
  for(const it of data){ const d=document.createElement("div"); d.innerHTML=renderTelItem(it); fr.appendChild(d.firstElementChild); }
  box.appendChild(fr);
}
$("#btnLoadTel").onclick = loadTelemetria;

// ======= COBERTURA =======
$("#btnCov").onclick = async()=>{
  const hours = parseInt($("#covHours").value||"24");
  const target = ($("#covTarget").value||"").trim() || null;
  const env = ($("#covEnv").value||"auto");
  const r = await call("POST","/api/cobertura",{hours, target_node: target, env});
  const f = $("#covFrame");
  const a = $("#btnKml");
  if(!r.ok){ f.srcdoc = `<div style='color:#fca5a5;padding:1rem'>⚠️ No se pudo generar: ${r.error||"error"}</div>`; a.style.display="none"; return; }
  if(r.html){ f.src = r.html; }
  if(r.kml){ a.href = r.kml; a.style.display="inline-block"; } else { a.style.display="none"; }
};

// ======= MAPA LIVE =======
let _map=null, _tiles=null, _homeMarker=null, _homeLat=null, _homeLon=null;
const _markers = new Map();    // id -> Leaflet marker
const _polylines = new Map();  // id -> Leaflet polyline

async function initLiveMapOnce(){
  if(_map) return;
  // Cargar config del mapa (tiles + HOME)
  const cfg = await call("GET","/api/home");
  const tiles = (cfg.ok && cfg.tiles)? cfg.tiles : {url:"https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", attribution:"OSM"};
  _homeLat = (cfg.ok && cfg.home && typeof cfg.home.lat==="number")? cfg.home.lat : null;
  _homeLon = (cfg.ok && cfg.home && typeof cfg.home.lon==="number")? cfg.home.lon : null;

  if(_homeLat!=null && _homeLon!=null){
    $("#homeInfo").textContent = `${_homeLat.toFixed(5)}, ${_homeLon.toFixed(5)}`;
  }else{
    $("#homeInfo").textContent = "–";
  }

  _map = L.map('liveMap', {zoomControl:true});
  _tiles = L.tileLayer(tiles.url, {attribution: tiles.attribution||""});
  _tiles.addTo(_map);

  // Foco inicial: HOME o Zaragoza como fallback
  const start = (_homeLat!=null && _homeLon!=null)? [_homeLat,_homeLon] : [41.65, -0.88];
  _map.setView(start, 9);

  if(_homeLat!=null && _homeLon!=null){
    _homeMarker = L.marker([_homeLat,_homeLon], {title:"HOME"}).addTo(_map).bindPopup("HOME");
  }
  // Semilla inicial desde backlog (últimas posiciones)
  await seedMapFromBacklog();
}

async function seedMapFromBacklog(){
  const mins = parseInt($("#mapSeedMinutes").value||"60");
  const r = await call("GET", `/api/positions?minutes=${mins}&max_nodes=0`);
  if(!r.ok || !Array.isArray(r.data)) return;
  let any=false;
  for(const it of r.data){
    if(it.lat==null || it.lon==null) continue;
    upsertMarker(it.id||"unknown", it.alias||"", it.lat, it.lon, it.ts||0);
    any=true;
  }
  if(any){
    const group = L.featureGroup([..._markers.values()].map(m=>m));
    if(_homeMarker) group.addLayer(_homeMarker);
    _map.fitBounds(group.getBounds().pad(0.2));
  }
}
$("#btnMapSeed").onclick = seedMapFromBacklog;

function upsertMarker(id, alias, lat, lon, ts){
  const name = alias? `${alias} (${id})` : id;
  const pos = [Number(lat), Number(lon)];
  let mk = _markers.get(id);
  if(!mk){
    mk = L.marker(pos, {title:name});
    mk.addTo(_map).bindPopup(`${name}<br/>${pos[0].toFixed(5)}, ${pos[1].toFixed(5)}<br/>${new Date(ts*1000).toLocaleString()}`);
    _markers.set(id, mk);
  }else{
    mk.setLatLng(pos);
    mk.setPopupContent(`${name}<br/>${pos[0].toFixed(5)}, ${pos[1].toFixed(5)}<br/>${new Date(ts*1000).toLocaleString()}`);
  }
  // Polyline desde HOME si hay HOME
  if(_homeLat!=null && _homeLon!=null){
    let pl = _polylines.get(id);
    const line = [[_homeLat,_homeLon], pos];
    if(!pl){
      pl = L.polyline(line, {weight:2, opacity:0.7});
      pl.addTo(_map);
      _polylines.set(id, pl);
    }else{
      pl.setLatLngs(line);
    }
  }
}

// WebSocket stream -> detectar POSITION_APP en vivo
(function connectWS(){
  const ws = new WebSocket((location.protocol==="https:"?"wss":"ws")+"://"+location.host+"/ws/events");
  ws.onopen = ()=> addLog(`<span class="ok">[ws]</span> conectado`);
  ws.onclose= ()=> { addLog(`<span class="bad">[ws]</span> desconectado; reintento en 2s`); setTimeout(connectWS,2000); };
  // 🔁 SUSTITUIR TODO el onmessage por este bloque:
  ws.onmessage = (evt) => {
     // 1) Siempre, escribe la línea en la consola (para ver vida)
    addLog(evt.data.replace(/[<>&]/g, s=>({"<":"&lt;",">":"&gt;","&":"&amp;"}[s])));

    // 2) Y además, intenta parsear para actualizar mapa/telemetría si cuadra
    let m;
    try { m = JSON.parse(evt.data); } catch(e) { return; }

    const p = m.packet ?? m;
    if (p.encrypted) return;

    const port = p?.decoded?.portnum || m?.summary?.portnum || p?.portnum || "UNKNOWN";
    const ts = m?.ts ?? p?.rxTime ?? p?.decoded?.time ?? Date.now()/1000;
    const rssi = m?.summary?.rssi ?? p?.rxRssi ?? null;
    const snr  = m?.summary?.snr  ?? p?.rxSnr  ?? null;
    const fromId    = m?.fromId ?? p?.fromId ?? null;
    const fromAlias = m?.from_alias ?? null;
    const canal = m?.summary?.canal ?? p?.meta?.channelIndex ?? null;

    // POSICIÓN -> live map
    if (port === "POSITION_APP") {
      const latI = p?.decoded?.position?.latitudeI;
      const lonI = p?.decoded?.position?.longitudeI;
      const lat  = p?.decoded?.position?.latitude  ?? (typeof latI === "number" ? latI/1e7 : null);
      const lon  = p?.decoded?.position?.longitude ?? (typeof lonI === "number" ? lonI/1e7 : null);
      if (typeof lat === "number" && typeof lon === "number") {
        window.initLiveMapOnce && initLiveMapOnce();
        updateLiveMapMarker({ id: fromId, alias: fromAlias, pos: [lat, lon], rssi, snr });
      }
    }

    // TEXTO (si aplica)
    const text = m?.summary?.text ?? p?.decoded?.text ?? null;

    // Ahora actualiza cada pestaña según el portnum
    try {
      if (port === "POSITION_APP" && pos) {
        // Mapa live + tabla de posiciones
        addPositionRow({ ts, fromId, fromAlias, pos, rssi, snr, canal });
        updateLiveMapMarker({ id: fromId, alias: fromAlias, pos, rssi, snr });
      } else if (port === "TELEMETRY_APP") {
        addTelemetryRow({ ts, fromId, fromAlias, rssi, snr, obj: p?.decoded?.telemetry ?? {} });
      } else if (port === "NEIGHBORINFO_APP") {
        addNeighborRows({ ts, fromId, fromAlias, neighbors: p?.decoded?.neighborInfo ?? [] });
      } else if (port === "TEXT_MESSAGE_APP" && text) {
        addMessagesRow({ ts, fromId, fromAlias, toId, toAlias, text, rssi, snr, canal });
      } else {
        // Otros puertos -> opcional
        addMiscRow({ ts, port, fromId, fromAlias, toId, toAlias, rssi, snr });
      }
    } catch(e) {
      console.error(e);
    }
  };

  function fmtTime(ts){ const d=new Date(ts*1000); return d.toLocaleString(); }

  function addRow(tbodySel, html){
    const tb = document.querySelector(tbodySel);
    if(!tb) return;
    const tr = document.createElement("tr");
    tr.innerHTML = html;
    tb.prepend(tr);
    // Limita a 500 filas
    while (tb.rows.length > 500) tb.deleteRow(tb.rows.length-1);
  }

  function addPositionRow(o){
    const [lat,lon] = o.pos;
    addRow("#posTable tbody",
      `<td>${fmtTime(o.ts)}</td>
      <td>${o.fromAlias||o.fromId||"-"}</td>
      <td>${lat.toFixed(5)}, ${lon.toFixed(5)}</td>
      <td>${o.rssi ?? "-"}</td>
      <td>${o.snr ?? "-"}</td>
      <td>${o.canal ?? "-"}</td>`);
  }

  function updateLiveMapMarker(o){
    window.initLiveMapOnce && initLiveMapOnce();
    if(!_map) return;
    const key = o.id || o.alias || Math.random().toString(36).slice(2);
    let m = _markers.get(key);
    if(!m){
      m = L.circleMarker(o.pos, { radius:6, opacity:0.8, fillOpacity:0.6 }).addTo(_map);
      _markers.set(key, m);
    }else{
      m.setLatLng(o.pos);
    }
    m.bindPopup(`${o.alias || o.id || "-"}<br/>${o.pos[0].toFixed(5)}, ${o.pos[1].toFixed(5)}<br/>RSSI: ${o.rssi ?? "-"} • SNR: ${o.snr ?? "-"}`);
  }

  function addTelemetryRow(o){
    addRow("#telTable tbody",
      `<td>${fmtTime(o.ts)}</td>
      <td>${o.fromAlias||o.fromId||"-"}</td>
      <td><code>${JSON.stringify(o.obj)}</code></td>
      <td>${o.rssi ?? "-"}</td><td>${o.snr ?? "-"}</td>`);
  }

  function addNeighborRows(o){
    const list = Array.isArray(o.neighbors) ? o.neighbors : [];
    for(const n of list){
      addRow("#vecinosTable tbody",
        `<td>${fmtTime(o.ts)}</td>
        <td>${o.fromAlias||o.fromId||"-"}</td>
        <td>${n.nodeId || "-"}</td>
        <td>${n.rssi ?? "-"}</td>
        <td>${n.snr ?? "-"}</td>`);
    }
  }

  function addMessagesRow(o){
    addRow("#msgsTable tbody",
      `<td>${fmtTime(o.ts)}</td>
      <td>${o.fromAlias||o.fromId||"-"}</td>
      <td>${o.toAlias||o.toId||"-"}</td>
      <td>${(o.text||"").replace(/[<>&]/g, s=>({"<":"&lt;",">":"&gt;","&":"&amp;"}[s]))}</td>
      <td>${o.canal ?? "-"}</td>
      <td>${o.rssi ?? "-"}</td><td>${o.snr ?? "-"}</td>`);
  }

  function addMiscRow(o){
    addRow("#otrosTable tbody",
      `<td>${fmtTime(o.ts)}</td>
      <td>${o.port}</td>
      <td>${o.fromAlias||o.fromId||"-"}</td>
      <td>${o.toAlias||o.toId||"-"}</td>
      <td>${o.rssi ?? "-"}</td><td>${o.snr ?? "-"}</td>`);
  }


})();

refresh();
</script>
</body>
</html>
"""

# =================== MAIN ============================

if __name__ == "__main__":
    import uvicorn
    # Ejecuta el servidor ASGI con el objeto 'app' directamente (evita problemas por nombre de módulo)
    uvicorn.run(app, host=WEB_BIND, port=WEB_PORT, reload=False)
