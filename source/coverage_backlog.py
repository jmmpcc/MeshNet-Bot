# coverage_backlog.py
# Cobertura desde BacklogServer: Heatmap + Círculos (HTML) y KML con polígonos
# Version v6.1.3

from __future__ import annotations
import os, json, socket, math, time, html
from datetime import datetime, timezone
from typing import List, Tuple, Optional

BACKLOG_HOST = "127.0.0.1"
BACKLOG_PORT = 8766

def _now_utc() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp())

def _send_backlog_cmd(cmd: dict, host: str, port: int, timeout: float = 7.0) -> dict:
    data = (json.dumps(cmd, ensure_ascii=False) + "\n").encode("utf-8")
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.sendall(data)
        s.shutdown(socket.SHUT_WR)
        buf = b""
        s.settimeout(timeout)
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    txt = buf.decode("utf-8", errors="replace").strip()
    if not txt:
        raise RuntimeError("BacklogServer no devolvió datos")
    return json.loads(txt)

def _first(*vals):
    for v in vals:
        if v is None:
            continue
        return v
    return None

def _get(evt: dict, *path):
    cur = evt
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur

def _get_ts(evt: dict) -> Optional[int]:
    v = _first(evt.get("ts"), evt.get("rx_time"))
    if isinstance(v, (int, float)):
        return int(v)
    return None

def _parse_latlon(evt: dict) -> Optional[Tuple[float, float]]:
    lat = _first(_get(evt, "latitude"), _get(evt, "payload", "latitude"), _get(evt, "map", "latitude"))
    lon = _first(_get(evt, "longitude"), _get(evt, "payload", "longitude"), _get(evt, "map", "longitude"))
    if lat is not None and lon is not None:
        try:
            return float(lat), float(lon)
        except Exception:
            pass
    lati = _first(_get(evt, "latitude_i"), _get(evt, "payload", "latitude_i"), _get(evt, "map", "latitude_i"))
    loni = _first(_get(evt, "longitude_i"), _get(evt, "payload", "longitude_i"), _get(evt, "map", "longitude_i"))
    if isinstance(lati, (int, float)) and isinstance(loni, (int, float)):
        return float(lati) / 1e7, float(loni) / 1e7
    return None

def _get_metrics(evt: dict) -> Tuple[Optional[float], Optional[float]]:
    snr = _first(evt.get("rx_snr"), evt.get("snr"), _get(evt, "decoded", "snr"))
    rssi = _first(evt.get("rx_rssi"), evt.get("rssi"))
    try: snr = float(snr) if snr is not None else None
    except: snr = None
    try: rssi = float(rssi) if rssi is not None else None
    except: rssi = None
    return snr, rssi

def _get_hops(evt: dict) -> Tuple[Optional[int], Optional[str]]:
    """Devuelve (hops_real, relay_node_id) desde un evento del backlog.
    hops_real = hop_start - hop_limit (0 = directo, 1+ = repetido).
    relay_node_id es el último nodo que retransmitió el paquete, o None si directo/desconocido.
    """
    hop_start = _first(evt.get("hop_start"), evt.get("hopStart"))
    hop_limit = _first(evt.get("hop_limit"), evt.get("hopLimit"))
    relay = _first(evt.get("relay_node"), evt.get("relayNode"))
    hops: Optional[int] = None
    if isinstance(hop_start, (int, float)) and isinstance(hop_limit, (int, float)):
        hops = max(0, int(hop_start) - int(hop_limit))
    if relay is not None:
        relay = f"!{str(relay).lstrip('!').lower()}"
    return hops, relay

_HOP_COLORS = {0: "#00cc44", 1: "#ff9900", 2: "#ff4400"}

def _hop_color(hops: Optional[int]) -> str:
    """Color hex por número de saltos: verde=directo, naranja=1, rojo-naranja=2, rojo=3+, gris=desconocido."""
    if hops is None:
        return "#888888"
    return _HOP_COLORS.get(hops, "#cc0000")

def _hop_label(hops: Optional[int]) -> str:
    if hops is None:
        return "Saltos: ?"
    if hops == 0:
        return "Directo (0 saltos)"
    return f"{hops} salto{'s' if hops != 1 else ''}"

def _score_from_metrics(snr: Optional[float], rssi: Optional[float]) -> float:
    # 60% SNR (clip [-12,+12]), 40% RSSI (clip [-120,-50]) → [0..1]
    s_snr = None
    if isinstance(snr, (int, float)):
        s_snr = (max(-12.0, min(12.0, snr)) + 12.0) / 24.0
    s_rssi = None
    if isinstance(rssi, (int, float)):
        rr = max(-120.0, min(-50.0, rssi))
        s_rssi = 1.0 - (abs(rr + 50.0) / 70.0)
    if s_snr is not None and s_rssi is not None:
        v = 0.6 * s_snr + 0.4 * s_rssi
    elif s_snr is not None:
        v = s_snr
    elif s_rssi is not None:
        v = s_rssi
    else:
        v = 0.15
    return max(0.0, min(1.0, v))

def _match_node(evt: dict, target: Optional[str]) -> bool:
    if not target:
        return True
    needle = str(target).lstrip("!").lower()
    for k in ("from_id", "from_str", "from", "user", "alias", "from_alias", "sender_alias", "node", "id"):
        v = evt.get(k)
        if v is None: continue
        v = str(v).lstrip("!").lower()
        if v == needle: return True
    return False

# === NUEVO: derivar alias/ID legible del evento ===
# === REEMPLAZO COMPLETO: alias + id legible ===
def _alias_from_evt(evt: dict) -> str:
    """
    Devuelve un nombre legible del emisor:
      - Si hay alias y id:  "Alias (!id)"
      - Si hay solo alias:  "Alias"
      - Si hay solo id:     "!id" (normalizado)
      - Si no hay nada:     "desconocido"
    """
    import re

    # 1) Candidatos de alias (en orden de preferencia)
    alias = None
    for k in ("alias", "from_alias", "sender_alias", "user", "from_str"):
        v = evt.get(k)
        if v:
            v = str(v).strip()
            if v:
                alias = v
                break

    # 2) Candidatos de ID (en orden de preferencia)
    raw_id = None
    for k in ("from_id", "id", "node", "from"):
        v = evt.get(k)
        if v is None:
            continue
        v = str(v).strip()
        if not v:
            continue
        raw_id = v
        break

    # 3) Normalizar ID a formato Meshtastic (!xxxxxxxx) cuando aplique
    def _norm_id(s: str | None) -> str | None:
        if not s:
            return None
        s = s.strip()
        if s.startswith("!"):
            return s  # ya normalizado
        # Heurística: 8 hex o todo dígitos → tratamos como id Meshtastic
        if re.fullmatch(r"[0-9A-Fa-f]{8}", s) or s.isdigit():
            return f"!{s.lower()}"
        return s  # otro tipo de id, déjalo como venga

    nid = _norm_id(raw_id)

    # 4) Composición final
    if alias and nid:
        return f"{alias} ({nid})"
    if alias:
        return alias
    if nid:
        return nid
    return "desconocido"


def _try_import_folium():
    try:
        import folium
        from folium.plugins import HeatMap
        return folium, HeatMap
    except Exception:
        return None, None

def _ensure_dir(d: str) -> None:
    os.makedirs(d, exist_ok=True)

# --------- Modelo simple de radio estimado según entorno ---------
_ENV_PRESETS = {
    "urbano":    (150.0, 1200.0),   # metros: min, max
    "suburbano": (300.0, 2500.0),
    "abierto":   (600.0, 6000.0),
}
def _norm_env(env: str | None) -> str:
    e = (env or "").strip().lower()
    return e if e in _ENV_PRESETS else "urbano"

def _radius_from_score(score: float, env: str) -> float:
    """Heurística de visualización: mejor calidad → mayor área de confianza."""
    min_r, max_r = _ENV_PRESETS[_norm_env(env)]
    return float(min_r + (max_r - min_r) * max(0.0, min(1.0, score)))

# --------- KML con círculos (polígonos) y pines ---------
def _kml_circle_polygon(lon: float, lat: float, radius_m: float, label: str, num_segs: int = 48) -> str:
   
    # Aproxima círculo geodésico asumiendo Tierra ~esfera y radios pequeños
    # Conversión aproximada de metros a grados (válida para áreas locales)
    # 1° lat ≈ 111,320 m; 1° lon ≈ 111,320 * cos(lat) m
    lat_rad = math.radians(lat)
    d_lat = radius_m / 111320.0
    d_lon = radius_m / (111320.0 * max(0.1, math.cos(lat_rad)))
    coords = []
    for i in range(num_segs + 1):
        ang = 2.0 * math.pi * (i / num_segs)
        lon_i = lon + d_lon * math.cos(ang)
        lat_i = lat + d_lat * math.sin(ang)
        coords.append(f"{lon_i:.7f},{lat_i:.7f},0")
    ring = " ".join(coords)
    return f"""
<Placemark>
    <name>{html.escape(str(label or ""))}</name>
  <Style>
    <PolyStyle><color>40ff0000</color><fill>1</fill><outline>0</outline></PolyStyle>
  </Style>
  <Polygon>
    <outerBoundaryIs><LinearRing><coordinates>{ring}</coordinates></LinearRing></outerBoundaryIs>
  </Polygon>
</Placemark>"""

def _kml_hop_style_id(hops: Optional[int]) -> str:
    if hops is None:
        return "hopX"
    if hops == 0:
        return "hop0"
    if hops == 1:
        return "hop1"
    if hops == 2:
        return "hop2"
    return "hop3"

def _write_kml(points: List[Tuple[float, float, float]], out_path: str, env: str) -> str:
    # KML usa formato de color AABBGGRR (alpha, blue, green, red)
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2">',
        "<Document>",
        "<name>Cobertura (círculos + pines)</name>",
        # Estilos por número de saltos RF (color en AABBGGRR)
        "<Style id='hop0'><IconStyle><color>ff44cc00</color><scale>0.8</scale>"  # verde (directo)
        "<Icon><href>http://maps.google.com/mapfiles/kml/shapes/placemark_circle.png</href></Icon>"
        "</IconStyle></Style>",
        "<Style id='hop1'><IconStyle><color>ff0099ff</color><scale>0.8</scale>"  # naranja (1 salto)
        "<Icon><href>http://maps.google.com/mapfiles/kml/shapes/placemark_circle.png</href></Icon>"
        "</IconStyle></Style>",
        "<Style id='hop2'><IconStyle><color>ff0044ff</color><scale>0.8</scale>"  # rojo-naranja (2 saltos)
        "<Icon><href>http://maps.google.com/mapfiles/kml/shapes/placemark_circle.png</href></Icon>"
        "</IconStyle></Style>",
        "<Style id='hop3'><IconStyle><color>ff0000cc</color><scale>0.8</scale>"  # rojo oscuro (3+)
        "<Icon><href>http://maps.google.com/mapfiles/kml/shapes/placemark_circle.png</href></Icon>"
        "</IconStyle></Style>",
        "<Style id='hopX'><IconStyle><color>ff888888</color><scale>0.7</scale>"  # gris (desconocido)
        "<Icon><href>http://maps.google.com/mapfiles/kml/shapes/placemark_circle.png</href></Icon>"
        "</IconStyle></Style>",
    ]
    # Círculos (polígonos semitransparentes)
    for (lat, lon, w, *rest) in points:
        label = (rest[0] if rest else "Cobertura")
        parts.append(_kml_circle_polygon(lon, lat, _radius_from_score(w, env), label))

    # Pines coloreados por número de saltos
    for (lat, lon, w, *rest) in points:
        label    = rest[0] if rest else "Cobertura"
        hops     = rest[1] if len(rest) > 1 else None
        relay_id = rest[2] if len(rest) > 2 else None
        style_id = _kml_hop_style_id(hops)
        relay_txt = f" • Repetidor: {relay_id}" if relay_id else ""
        desc = f"{_hop_label(hops)}{relay_txt}"
        parts += [
            "<Placemark>",
            f"<name>{html.escape(str(label or ''))}</name>",
            f"<description>{html.escape(desc)}</description>",
            f"<styleUrl>#{style_id}</styleUrl>",
            "<Point>",
            f"<coordinates>{lon:.7f},{lat:.7f},0</coordinates>",
            "</Point>",
            "</Placemark>",
        ]
    parts += ["</Document>", "</kml>"]
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))
    return out_path

# --------- Construcción principal (HTML+KML) ---------
def build_coverage_from_backlog(
    *,
    hours: int = 24,
    target_node: Optional[str] = None,
    output_dir: str = "bot_data/maps",
    backlog_host: str = BACKLOG_HOST,
    backlog_port: int = BACKLOG_PORT,
    env: str = "urbano",
    make_kml: bool = True,
) -> dict:
    """
    Devuelve dict con rutas generadas:
      {"html": "/.../coverage_backlog_<node>_<h>h.html", "kml": "/.../...kml" (si make_kml)}
    El HTML contiene Heatmap + Círculos (Folium). El KML contiene polígonos circulares + pines.
    """
    _ensure_dir(output_dir)
    since_ts = _now_utc() - int(hours) * 3600

    req = {
        "cmd": "FETCH_BACKLOG",
        "params": {
            "since_ts": int(since_ts),
            "portnums": ["POSITION_APP"],
            # "limit": 25000,  # opcional si quieres acotar
        },
    }
    res = _send_backlog_cmd(req, host=backlog_host, port=backlog_port)
    if not res.get("ok"):
        raise RuntimeError(f"BacklogServer devolvió error: {res}")

    rows = res.get("data") or []

    # Índice de posiciones por nodo: node_id → (lat, lon)
    # Se usa para localizar repetidores en el mapa cuando tenemos su relay_node ID.
    relay_positions: dict = {}
    for evt in rows:
        nid = _first(evt.get("from"), evt.get("from_id"), evt.get("from_str"))
        if nid:
            ll = _parse_latlon(evt)
            if ll:
                nkey = f"!{str(nid).lstrip('!').lower()}"
                relay_positions.setdefault(nkey, ll)  # guardamos solo la primera posición conocida

    points: List[Tuple] = []  # (lat, lon, score, label, hops, relay_nid)
    for evt in rows:
        if not _match_node(evt, target_node):
            continue
        ts = _get_ts(evt)
        if ts is None or ts < since_ts:
            continue
        ll = _parse_latlon(evt)
        if not ll:
            continue
        lat, lon = ll
        snr, rssi = _get_metrics(evt)
        w = _score_from_metrics(snr, rssi)
        label = _alias_from_evt(evt)
        hops, relay_nid = _get_hops(evt)
        points.append((lat, lon, w, label, hops, relay_nid))

    if not points:
        raise RuntimeError("Sin puntos válidos en la ventana solicitada (comprueba horas/nodo).")

    node_slug = (str(target_node).lstrip("!").lower() if target_node else "all")
    base = f"coverage_backlog_{node_slug}_{hours}h"
    out = {"html": None, "kml": None}

    # --- HTML: Heatmap + Círculos + capas de saltos RF (si Folium disponible) ---
    out_html = os.path.join(output_dir, base + ".html")
    title = (
        f"Cobertura (últimas {hours} h) — "
        + (f"Nodo: {target_node}" if target_node else "Todos los nodos")
    )
    html_path = _build_html_heatmap_and_circles(
        points, out_html, title, env,
        relay_positions=relay_positions,
    )
    out["html"] = html_path

    # --- KML: polígonos circulares + pines coloreados por saltos ---
    if make_kml:
        out_kml = os.path.join(output_dir, base + ".kml")
        _write_kml(points, out_kml, env=_norm_env(env))
        out["kml"] = out_kml

    return out

# === NUEVO: función combinada backlog + positions.jsonl ===
def build_coverage_combined(
    *,
    hours: int = 24,
    target_node: Optional[str] = None,
    output_dir: str = "bot_data/maps",
    backlog_host: str = BACKLOG_HOST,
    backlog_port: int = BACKLOG_PORT,
    env: str = "urbano",
    make_kml: bool = True,
) -> dict:
    """
    Intenta primero generar cobertura desde BacklogServer.
    Si no hay puntos válidos, cae a positions_store.POSITIONS_LOG_PATH.
    Devuelve dict con {"html": ruta|None, "kml": ruta|None}.
    """
    try:
        # Intentar primero backlog
        return build_coverage_from_backlog(
            hours=hours,
            target_node=target_node,
            output_dir=output_dir,
            backlog_host=backlog_host,
            backlog_port=backlog_port,
            env=env,
            make_kml=make_kml,
        )
    except Exception as e:
        # Fallback a positions.jsonl
        try:
            import positions_store as ps
        except ImportError:
            raise RuntimeError(f"Sin datos de cobertura: {e}")

        minutes = hours * 60
        rows = ps.read_positions_recent(last_minutes=minutes, max_nodes=0)
        if not rows:
            raise RuntimeError("Sin puntos válidos ni en backlog ni en positions.jsonl")

        # Reusar builders de positions_store (KML/GPX)
        kml_bytes = ps.build_kml(rows, name=f"Cobertura {hours}h")
        out_kml = os.path.join(output_dir, f"coverage_positions_{hours}h.kml")
        os.makedirs(output_dir, exist_ok=True)
        with open(out_kml, "wb") as f:
            f.write(kml_bytes)

        # No hay HTML heatmap en fallback, solo KML
        return {"html": None, "kml": out_kml}

# === NUEVO: helpers para construir HTML/KML desde una lista de (lat, lon, score) ===
def _build_html_heatmap_and_circles(
    points: List[Tuple],
    out_html: str,
    title: str,
    env: str,
    relay_positions: Optional[dict] = None,
) -> Optional[str]:
    folium, HeatMap = _try_import_folium()
    if not (folium and HeatMap):
        return None
    lat0, lon0 = points[0][0], points[0][1]
    m = folium.Map(location=[lat0, lon0], zoom_start=12, control_scale=True, tiles="OpenStreetMap")

    # Heatmap (peso = score) — no necesita label
    HeatMap([(lat, lon, w) for (lat, lon, w, *rest) in points],
            name="Cobertura (Heatmap)", radius=18, blur=20, max_zoom=16,
            min_opacity=0.2, max_val=1.0).add_to(m)

    # Círculos con popup "alias • score • r≈..."
    circles = folium.FeatureGroup(name=f"Cobertura (Círculos • {env})", show=True)
    for (lat, lon, w, *rest) in points:
        label = (rest[0] if rest else "—")
        r_m = _radius_from_score(w, env)
        folium.Circle(
            location=(lat, lon),
            radius=r_m,
            weight=0,
            fill=True,
            fill_opacity=0.18 + 0.32 * w,
            popup=f"{label} • score={w:.2f} • r≈{int(r_m)} m",
        ).add_to(circles)
    circles.add_to(m)

    # Puntos con popup "alias • score"
    pts_fg = folium.FeatureGroup(name="Sondas (puntos)", show=False)
    for (lat, lon, w, *rest) in points:
        label = (rest[0] if rest else "—")
        folium.CircleMarker(
            location=(lat, lon),
            radius=3,
            opacity=0.6,
            fill=True,
            fill_opacity=0.7,
            popup=f"{label} • score={w:.2f}",
        ).add_to(pts_fg)
    pts_fg.add_to(m)

    # ── Capa: Saltos RF (marcadores coloreados por número de hops) ─────────
    has_hop_data = any(len(p) > 4 and p[4] is not None for p in points)
    if has_hop_data:
        hops_fg = folium.FeatureGroup(name="Saltos RF (hops)", show=True)
        for (lat, lon, w, *rest) in points:
            label    = rest[0] if rest else "—"
            hops     = rest[1] if len(rest) > 1 else None
            relay_id = rest[2] if len(rest) > 2 else None
            color    = _hop_color(hops)
            relay_txt = f"<br>Repetidor: <code>{relay_id}</code>" if relay_id else ""
            popup_html = (
                f"<b>{html.escape(str(label))}</b><br>"
                f"{_hop_label(hops)}{relay_txt}<br>"
                f"score RF={w:.2f}"
            )
            folium.CircleMarker(
                location=(lat, lon),
                radius=7,
                color=color,
                fill=True,
                fill_color=color,
                fill_opacity=0.8,
                popup=folium.Popup(popup_html, max_width=250),
                tooltip=_hop_label(hops),
            ).add_to(hops_fg)
        hops_fg.add_to(m)

        # ── Capa: Nodos repetidores con posición conocida ──────────────────
        relay_fg = folium.FeatureGroup(name="Nodos repetidores", show=True)
        shown_relays: set = set()
        for (lat, lon, w, *rest) in points:
            relay_id = rest[2] if len(rest) > 2 else None
            if (relay_id and relay_positions and relay_id in relay_positions
                    and relay_id not in shown_relays):
                rlat, rlon = relay_positions[relay_id]
                folium.Marker(
                    location=(rlat, rlon),
                    popup=f"Repetidor: {relay_id}",
                    tooltip=f"↗ {relay_id}",
                    icon=folium.Icon(color="orange", icon="signal", prefix="fa"),
                ).add_to(relay_fg)
                shown_relays.add(relay_id)
        relay_fg.add_to(m)

        # ── Capa: Rutas de salto origen→repetidor (desactivada por defecto) ─
        lines_fg = folium.FeatureGroup(name="Rutas de salto", show=False)
        for (lat, lon, w, *rest) in points:
            hops     = rest[1] if len(rest) > 1 else None
            relay_id = rest[2] if len(rest) > 2 else None
            if (hops and hops > 0 and relay_id
                    and relay_positions and relay_id in relay_positions):
                rlat, rlon = relay_positions[relay_id]
                folium.PolyLine(
                    locations=[(lat, lon), (rlat, rlon)],
                    color=_hop_color(hops),
                    weight=2,
                    opacity=0.5,
                    tooltip=f"→ {relay_id}",
                ).add_to(lines_fg)
        lines_fg.add_to(m)

        # ── Leyenda de saltos ──────────────────────────────────────────────
        legend_html = """
        <div style="position:fixed;bottom:30px;right:10px;background:rgba(255,255,255,0.92);
             padding:8px 12px;border:1px solid #ccc;border-radius:8px;z-index:9999;font-size:12px;">
            <b>Saltos RF</b><br>
            <span style="color:#00cc44">&#9679;</span> Directo (0)<br>
            <span style="color:#ff9900">&#9679;</span> 1 salto<br>
            <span style="color:#ff4400">&#9679;</span> 2 saltos<br>
            <span style="color:#cc0000">&#9679;</span> 3+ saltos<br>
            <span style="color:#888888">&#9679;</span> Desconocido
        </div>"""
        m.get_root().html.add_child(folium.Element(legend_html))

    folium.LayerControl().add_to(m)
    title_html = f"""
    <div style="position: fixed; top: 10px; left: 50%; transform: translateX(-50%);
         background: rgba(255,255,255,0.9); padding: 6px 12px; border: 1px solid #ccc; border-radius: 8px; z-index: 9999;">
        <b>{title}</b> — Entorno: <i>{_norm_env(env)}</i>
    </div>"""
    m.get_root().html.add_child(folium.Element(title_html))
    os.makedirs(os.path.dirname(out_html), exist_ok=True)
    m.save(out_html)
    return out_html


def _rows_to_points(rows: List[dict], *, since_ts: int, target_node: Optional[str]) -> List[Tuple[float, float, float]]:
    """Convierte filas (events/positions) a [(lat, lon, score)] usando _parse_latlon + _get_metrics."""
    pts: List[Tuple[float, float, float]] = []
    for evt in rows:
        ts = _get_ts(evt)
        if ts is None or ts < since_ts:
            continue
        if not _match_node(evt, target_node):
            continue
        ll = _parse_latlon(evt)
        if not ll:
            # compat extra: claves 'lat'/'lng'/'lon'
            lat = evt.get("lat")
            lon = evt.get("lon", evt.get("lng"))
            try:
                if lat is not None and lon is not None:
                    ll = (float(lat), float(lon))
            except Exception:
                ll = None
        if not ll:
            continue
        lat, lon = ll
        snr, rssi = _get_metrics(evt)
        w = _score_from_metrics(snr, rssi)
        # ANTES:
        # pts.append((lat, lon, w))

        # AHORA:
        label = _alias_from_evt(evt)
        hops, relay_nid = _get_hops(evt)  # será (None, None) desde positions.jsonl
        pts.append((lat, lon, w, label, hops, relay_nid))

    return pts


# === NUEVO: cobertura desde positions_store (positions.jsonl) con HTML+KML ===
def build_coverage_from_positions_jsonl(
    *,
    hours: int = 24,
    target_node: Optional[str] = None,
    output_dir: str = "bot_data/maps",
    env: str = "urbano",
    make_kml: bool = True,
) -> dict:
    """
    Usa positions_store.POSITIONS_LOG_PATH (positions.jsonl) para generar:
    - HTML (heatmap + círculos) si Folium está instalado.
    - KML (polígonos circulares + pines).
    Devuelve {"html": path|None, "kml": path|None}.
    """
    try:
        import positions_store as ps
    except ImportError as e:
        raise RuntimeError("positions_store no disponible para fallback") from e

    minutes = hours * 60
    rows = ps.read_positions_recent(last_minutes=minutes, max_nodes=0)  # reutiliza tu lógica existente
    if not rows:
        raise RuntimeError("positions.jsonl no tiene posiciones en la ventana solicitada")

    since_ts = _now_utc() - int(hours) * 3600
    points = _rows_to_points(rows, since_ts=since_ts, target_node=target_node)
    if not points:
        raise RuntimeError("positions.jsonl no produjo puntos válidos (filtros/estructura)")

    node_slug = (str(target_node).lstrip('!').lower() if target_node else "all")
    base = f"coverage_positions_{node_slug}_{hours}h"
    os.makedirs(output_dir, exist_ok=True)

    # HTML
    out_html = os.path.join(output_dir, base + ".html")
    title = f"Cobertura (últimas {hours} h) — " + (f"Nodo: {target_node}" if target_node else "Todos los nodos")
    html_path = _build_html_heatmap_and_circles(points, out_html, title, env)

    # KML
    kml_path = None
    if make_kml:
        out_kml = os.path.join(output_dir, base + ".kml")
        _write_kml(points, out_kml, env=_norm_env(env))
        kml_path = out_kml

    return {"html": html_path, "kml": kml_path}


# === NUEVO: función combinada backlog → fallback positions.jsonl (ambas con HTML+KML) ===
def build_coverage_combined(
    *,
    hours: int = 24,
    target_node: Optional[str] = None,
    output_dir: str = "bot_data/maps",
    backlog_host: str = BACKLOG_HOST,
    backlog_port: int = BACKLOG_PORT,
    env: str = "urbano",
    make_kml: bool = True,
) -> dict:
    """
    1) Intenta BacklogServer (HTML+KML).
    2) Si no hay puntos válidos, cae a positions.jsonl (HTML+KML).
    Devuelve {"html": path|None, "kml": path|None}.
    """
    try:
        return build_coverage_from_backlog(
            hours=hours,
            target_node=target_node,
            output_dir=output_dir,
            backlog_host=backlog_host,
            backlog_port=backlog_port,
            env=env,
            make_kml=make_kml,
        )
    except Exception:
        # Fallback automático a positions.jsonl
        return build_coverage_from_positions_jsonl(
            hours=hours,
            target_node=target_node,
            output_dir=output_dir,
            env=env,
            make_kml=make_kml,
        )
