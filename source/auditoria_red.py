# auditoria_red.py
import os, json, csv, math, time, hashlib, logging
from datetime import datetime, timezone
from statistics import fmean
from typing import Any, Dict, List, Optional, Set


from telegram import Update, InputFile
from telegram.ext import ContextTypes


# ──────────────────────────────────────────────────────────────────────────────
# Reutilización opcional de utilidades del proyecto; si no existen, fallback.
# ──────────────────────────────────────────────────────────────────────────────
try:
    from utils_time import humanize_ago as _ago
except Exception:
    def _ago(seconds: float) -> str:
        m = int(seconds // 60); h = m // 60; d = h // 24
        return f"{d}d {h%24}h {m%60}m" if d else f"{h}h {m%60}m" if h else f"{m}m"

try:
    from utils_io import read_jsonl as _read_jsonl
except Exception:
    def _read_jsonl(path: str) -> List[Dict[str, Any]]:
        out = []
        if not os.path.exists(path): return out
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if line:
                    try: out.append(json.loads(line))
                    except: pass
        return out

try:
    from utils_stats import pctl as _pctl
except Exception:
    def _pctl(p: float, arr: List[float]) -> Optional[float]:
        if not arr: return None
        arr = sorted(arr); k = (len(arr)-1) * (p/100.0)
        i = math.floor(k); j = math.ceil(k)
        if i == j: return float(arr[i])
        return float(arr[i]*(j-k) + arr[j]*(k-i))

# ──────────────────────────────────────────────────────────────────────────────
# Rutas y parámetros (.env)
# ──────────────────────────────────────────────────────────────────────────────
PATH_POS   = os.getenv("BOT_POSITIONS_PATH",   "bot_data/positions.jsonl")
PATH_COV   = os.getenv("BOT_COVERAGE_PATH",    "bot_data/coverage.jsonl")
PATH_TEL   = os.getenv("BOT_TELEMETRY_PATH",   "bot_data/telemetry.jsonl")  # opcional
PATH_OFF   = os.getenv("BOT_OFFLINE_LOG_PATH", "bot_data/broker_offline_log.jsonl")

OUT_DIR         = os.getenv("BOT_REPORTS_DIR", "bot_data/reportes")
HEATMAP_OUTDIR  = os.getenv("BOT_MAPS_DIR", OUT_DIR)
MAPS_DIR = os.getenv("BOT_MAPS_DIR", "bot_data/maps")

# Ventanas y filtros
WINDOW_H              = int(os.getenv("AUD_WINDOW_H", "72"))
MIN_SAMPLES_NEIGHBOR  = int(os.getenv("AUD_MIN_SAMPLES", "3"))
STALE_SEC             = int(os.getenv("AUD_STALE_SEC", str(72*3600)))
HOPS_TARGET_MAX       = int(os.getenv("AUD_HOPS_MAX", "3"))
SNR_OK                = float(os.getenv("AUD_SNR_OK", "0.0"))
P90_SNR_STRONG        = float(os.getenv("AUD_P90_STRONG", "8.0"))
ROUTER_MIN_NEI        = int(os.getenv("AUD_ROUTER_NEI", "6"))
CITY_BACKBONE_MAX     = int(os.getenv("AUD_BACKBONE_MAX", "5"))
DEFAULT_HOP_LIMIT     = int(os.getenv("AUD_HOP_DEFAULT", "4"))
BEACON_ROUTER_S       = int(os.getenv("AUD_BEACON_R", "600"))
BEACON_CLIENT_S       = int(os.getenv("AUD_BEACON_C", "1200"))
TEL_ROUTER_S          = int(os.getenv("AUD_TEL_R", "1200"))
TEL_CLIENT_S          = int(os.getenv("AUD_TEL_C", "1800"))

# Preset LoRa estimado (para cálculo de carga si faltan longitudes reales)
LORA_SF = int(os.getenv("AUD_LORA_SF", "12"))
LORA_BW = int(os.getenv("AUD_LORA_BW", "125"))   # kHz
LORA_CR = int(os.getenv("AUD_LORA_CR", "5"))     # 5→4/5 … 8→4/8
LORA_PL = int(os.getenv("AUD_LORA_PAYLOAD", "20"))

# Heatmap
HEATMAP_ENABLE = os.getenv("AUD_HEATMAP_ENABLE", "1") == "1"
HEATMAP_BOUNDS = os.getenv("AUD_HEATMAP_BOUNDS", "")  # "lat1,lon1,lat2,lon2"

# Persistencia (auditoría integral)
PERSIST_RATIO_MIN = float(os.getenv("AUD_PERSIST_RATIO_MIN", "0.25"))
PERSIST_DAYS_MIN  = int(os.getenv("AUD_PERSIST_DAYS_MIN", "3"))

# Zona horaria para histogramas/humanización
try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo(os.getenv("AUD_TZ", "Europe/Madrid"))
except Exception:
    _TZ = timezone.utc  # fallback


# --- En auditoria_red.py ---

import re

from typing import Dict, Any, List, Optional

NODOS_TXT_PATH = os.getenv("NODOS_TXT_PATH", "bot_data/nodos.txt")

_COL_MAP = {
    "N": 0,
    "User": 1,
    "ID": 2,
    "AKA": 3,
    "Hardware": 4,
    "Pubkey": 5,
    "Role": 6,
    "Latitude": 7,
    "Longitude": 8,
    "Altitude": 9,
    "Battery": 10,
    "ChannelUtil": 11,   # "Channel util."
    "TxAirUtil": 12,     # "Tx air util."
    "SNR": 13,
    "Hops": 14,
    "Channel": 15,
    "LastHeard": 16,
    "Since": 17,
}

def _to_float(x: str) -> Optional[float]:
    if not x or x.strip().upper() in ("N/A", "-"):
        return None
    # Quita sufijos tipo "m", "%", "°", "dB"
    x = x.replace("°", "").replace("m", "").replace("%", "").replace("dB", "").strip()
    try:
        return float(x)
    except Exception:
        return None

def _to_int(x: str) -> Optional[int]:
    if not x or x.strip().upper() in ("N/A", "-"):
        return None
    try:
        return int(x)
    except Exception:
        # puede venir como "7 mins ago", "1 hour ago" en otra columna; ignora
        return None

def _to_dt(x: str) -> Optional[datetime]:
    # Formato visto en nodos.txt: "2025-11-11 23:38:44"
    x = (x or "").strip()
    if not x or x.upper() == "N/A":
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S",):
        try:
            return datetime.strptime(x, fmt)
        except Exception:
            pass
    return None

def _clean_battery(x: str) -> Optional[str]:
    # En el volcado aparece "Powered", "90%", etc.
    x = (x or "").strip()
    if not x or x.upper() == "N/A":
        return None
    return x

def _parse_nodos_txt(path: str = NODOS_TXT_PATH) -> Dict[str, Dict[str, Any]]:
    """
    Lee bot_data/nodos.txt y devuelve { nid: {...campos normalizados...} }.
    Campos principales: alias, nid, aka, role, lat, lon, alt_m, battery, snr_db, hops, channel, last_heard_dt.
    """
    if not os.path.exists(path):
        return {}

    out: Dict[str, Dict[str, Any]] = {}
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for raw in f:
            s = raw.rstrip("\n")
            if not s:
                continue
            # descarta marcos y cabeceras
            if s.startswith(("╒", "╞", "╘", "╧", "╤", "══", "──", "Connected to radio", "N ", "│   N ")):
                continue
            if "│" not in s:
                continue

            # corta por separador vertical, quita extremos (primer/último son bordes)
            parts = [p.strip() for p in s.split("│")]
            if len(parts) < 19:
                continue
            # partes útiles están entre el primer y último borde
            cells = parts[1:-1]

            try:
                user  = cells[_COL_MAP["User"]]
                nid   = cells[_COL_MAP["ID"]]
                aka   = cells[_COL_MAP["AKA"]]
                role  = cells[_COL_MAP["Role"]]
                lat   = _to_float(cells[_COL_MAP["Latitude"]])
                lon   = _to_float(cells[_COL_MAP["Longitude"]])
                alt_m = _to_float(cells[_COL_MAP["Altitude"]])
                batt  = _clean_battery(cells[_COL_MAP["Battery"]])
                ch_u  = _to_float(cells[_COL_MAP["ChannelUtil"]])
                tx_u  = _to_float(cells[_COL_MAP["TxAirUtil"]])
                snr   = _to_float(cells[_COL_MAP["SNR"]])
                hops  = _to_int(cells[_COL_MAP["Hops"]])
                chan  = _to_int(cells[_COL_MAP["Channel"]])
                lhd   = _to_dt(cells[_COL_MAP["LastHeard"]])
            except Exception:
                continue

            if not nid:
                continue

            out[nid] = {
                "alias": (aka or user or None),
                "user": user or None,
                "nid": nid,
                "aka": aka or None,
                "role": (role if role and role.upper() != "N/A" else None),
                "lat": lat, "lon": lon, "alt_m": alt_m,
                "battery": batt,
                "channel_util_pct": ch_u,       # %
                "tx_air_util_pct": tx_u,        # %
                "snr_db": snr,
                "hops": hops,
                "channel": chan,
                "last_heard": lhd,
            }
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Extractores
# ──────────────────────────────────────────────────────────────────────────────
def _safe_iso_to_ts(x):
    try:
        if x is None:
            return None
        # numérico ya en float/int o string numérica
        if isinstance(x, (int, float)) or (isinstance(x, str) and x.strip().replace('.','',1).isdigit()):
            v = float(x)
            if v > 10_000_000_000:  # heurística: epoch en ms
                v /= 1000.0
            return v
        # ISO 8601
        return datetime.fromisoformat(str(x).replace("Z","+00:00")).timestamp()
    except:
        return None


def _extract_metrics(row: Dict[str, Any]) -> Dict[str, Any]:
    ts = row.get("ts") or row.get("timestamp") or row.get("time")
    if isinstance(ts, str): ts = _safe_iso_to_ts(ts)
    if ts is None: ts = time.time()

    snr = row.get("snr") or (row.get("rx") or {}).get("snr") or (row.get("radio") or {}).get("snr")
    try: snr = float(snr) if snr is not None else None
    except: snr = None

    rssi = row.get("rssi") or (row.get("rx") or {}).get("rssi")
    try: rssi = float(rssi) if rssi is not None else None
    except: rssi = None

    hops = row.get("hops") or (row.get("route") or {}).get("hops") or row.get("hop_count")
    try: hops = int(hops) if hops is not None else None
    except: hops = None

    nid = row.get("from_id") or row.get("node_id") or row.get("nid") or row.get("from") or "?"
    alias = row.get("alias") or row.get("from_alias") or row.get("name")
    app = (row.get("decoded") or {}).get("portnum") or row.get("portnum") or row.get("type") or "?"

    pl  = row.get("payload_len") or row.get("len") or None
    try: pl = int(pl) if pl is not None else None
    except: pl = None

    pos = (row.get("position") or (row.get("decoded") or {}).get("position") or row.get("pos") or {})
    lat = pos.get("lat") or pos.get("latitude") or pos.get("latitudeI")
    lon = pos.get("lon") or pos.get("lng") or pos.get("longitude") or pos.get("longitudeI")
    try:
        if isinstance(lat, int) and abs(lat) > 18000000: lat = lat/1e7
        if isinstance(lon, int) and abs(lon) > 18000000: lon = lon/1e7
        lat = float(lat) if lat is not None else None
        lon = float(lon) if lon is not None else None
    except:
        lat, lon = None, None

    mid = row.get("id") or row.get("msg_id")
    if mid is None:
        raw = f"{nid}|{int(ts)}|{app}|{pl}".encode("utf-8", "ignore")
        mid = hashlib.blake2s(raw, digest_size=8).hexdigest()

    return {"ts": float(ts), "nid": str(nid), "alias": alias, "snr": snr, "rssi": rssi,
            "hops": hops, "app": app, "pl": pl, "lat": lat, "lon": lon, "mid": mid}

# ──────────────────────────────────────────────────────────────────────────────
# Utilidades internas
# ──────────────────────────────────────────────────────────────────────────────
def _ensure_outdir():
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(HEATMAP_OUTDIR, exist_ok=True)

def _collect_rows(hours: int) -> List[Dict[str, Any]]:
    cutoff = time.time() - hours*3600
    rows: List[Dict[str, Any]] = []
    rows += _read_jsonl(PATH_COV)
    rows += _read_jsonl(PATH_POS)
    if os.path.exists(PATH_OFF): rows += _read_jsonl(PATH_OFF)
    if os.path.exists(PATH_TEL): rows += _read_jsonl(PATH_TEL)  # solo si existe
    out = []
    for r in rows:
        try:
            ts = r.get("ts") or r.get("timestamp") or r.get("time")
            if isinstance(ts, str): ts = _safe_iso_to_ts(ts)
            if ts is None: continue
            if float(ts) >= cutoff: out.append(r)
        except:
            pass
    return out

# ──────────────────────────────────────────────────────────────────────────────
# Vecinos (versión extendida)
# ──────────────────────────────────────────────────────────────────────────────

def _build_neighbors(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    Construye el diccionario de vecinos a partir de los eventos recientes.
    Incluye arrays de SNR, RSSI, HOPS, payloads; contador por APP; set de mensajes (para dup_ratio).
    Solo se consideran nodos con hops ≤ HOPS_NEIGHBOR_MAX.
    """
    T: Dict[str, Dict[str, Any]] = {}
    now = time.time()
    HOPS_NEIGHBOR_MAX = int(os.getenv("AUD_HOPS_NEIGHBOR_MAX", "1"))

    for r in rows:
        m = _extract_metrics(r)
        nid = m.get("nid")
        if not nid:
            continue

        # Filtro: solo vecinos (hops 0–1 por defecto)
        if m.get("hops") is not None and m["hops"] > HOPS_NEIGHBOR_MAX:
            continue

        ent = T.setdefault(nid, {
            "alias": m.get("alias"),
            "snr": [],          # floats
            "rssi": [],         # floats
            "hops": [],         # ints
            "payloads": [],     # ints (len)
            "apps": {},         # dict app->count
            "msgs": set(),      # ids únicos para dup_ratio
            "last_ts": 0.0
        })

        if m.get("snr") is not None:
            ent["snr"].append(m["snr"])
        if m.get("rssi") is not None:
            ent["rssi"].append(m["rssi"])
        if m.get("hops") is not None:
            ent["hops"].append(m["hops"])
        if m.get("pl") is not None:
            ent["payloads"].append(m["pl"])

        app = m.get("app") or "?"
        ent["apps"][app] = ent["apps"].get(app, 0) + 1

        mid = m.get("mid")
        if mid is not None:
            ent["msgs"].add(mid)

        ts = m.get("ts") or 0.0
        if ts:
            ent["last_ts"] = max(ent["last_ts"], ts)

        if not ent["alias"] and m.get("alias"):
            ent["alias"] = m["alias"]

    # Limpieza: caducidad y mínimo de muestras
    MIN_SAMPLES_NEIGHBOR = int(os.getenv("AUD_MIN_SAMPLES", "3"))
    STALE_SEC = int(os.getenv("AUD_STALE_SEC", str(72 * 3600)))

    pruned = {}
    for nid, ent in T.items():
        if (now - ent["last_ts"]) > STALE_SEC:
            continue
        # cuenta de señales útiles (snr+rssi+hops)
        n_samples = len(ent["snr"]) + len(ent["rssi"]) + len(ent["hops"])
        if n_samples < MIN_SAMPLES_NEIGHBOR:
            continue
        pruned[nid] = ent
    return pruned

def _summarize_neighbors(T: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    now = time.time()
    for nid, e in T.items():
        snr_list   = e.get("snr", [])
        rssi_list  = e.get("rssi", [])
        hops_list  = e.get("hops", [])
        payloads   = e.get("payloads", [])
        apps       = e.get("apps", {})
        msgs       = e.get("msgs", set())
        last_ts    = e.get("last_ts", 0.0)

        snr_p50 = _pctl(50, snr_list) if snr_list else None
        snr_p90 = _pctl(90, snr_list) if snr_list else None
        snr_avg = fmean(snr_list) if snr_list else None
        rssi_avg = fmean(rssi_list) if rssi_list else None
        hops_avg = fmean(hops_list) if hops_list else None
        hops_max = int(max(hops_list)) if hops_list else None
        payload_avg = fmean(payloads) if payloads else None

        msgs_total = sum(int(v) for v in apps.values()) if apps else 0
        uniq = len(msgs) if isinstance(msgs, (set, list, tuple)) else 0
        dup_ratio = max(0.0, 1.0 - min(1.0, (uniq / msgs_total))) if msgs_total else 0.0

        samples = len(snr_list) + len(rssi_list) + len(hops_list)
        out.append({
            "nid": nid,
            "alias": e.get("alias"),
            "samples": samples,
            "snr_avg": snr_avg,
            "snr_p50": snr_p50,
            "snr_p90": snr_p90,
            "rssi_avg": rssi_avg,
            "hops_avg": hops_avg,
            "hops_max": hops_max,
            "apps": apps,
            "payload_avg": payload_avg,
            "dup_ratio": dup_ratio,
            "last_seen_sec": int(now - last_ts) if last_ts else None,
        })

    out.sort(
        key=lambda r: (-(r["snr_p90"] if r["snr_p90"] is not None else -999),
                       (r["hops_avg"] if r["hops_avg"] is not None else 999))
    )
    return out

# ──────────────────────────────────────────────────────────────────────────────
# Heurísticas de recomendación
# ──────────────────────────────────────────────────────────────────────────────
def _recommend_local(nei: List[Dict[str, Any]]) -> Dict[str, Any]:
    n_total = len(nei)
    n_good  = sum(1 for n in nei if (n["snr_p50"] is not None and n["snr_p50"] >= SNR_OK))
    hops_ok = sum(1 for n in nei if (n["hops_avg"] is not None and n["hops_avg"] <= HOPS_TARGET_MAX))

    if n_total == 0:
        role, why = "CLIENT_MUTE", ["Sin vecinos válidos recientes."]
    elif n_good >= ROUTER_MIN_NEI and hops_ok >= ROUTER_MIN_NEI:
        role, why = "REPEATER/ROUTER", [f"{n_good} vecinos con SNR≥{SNR_OK} y hops medios ≤{HOPS_TARGET_MAX}."]
    elif n_good >= 3 and hops_ok >= 3:
        role, why = "CLIENT", ["Vecindario moderado y rutas aceptables."]
    else:
        role, why = "CLIENT_MUTE", ["Pocos vecinos o enlaces pobres."]

    p90s = [n["snr_p90"] for n in nei if n["snr_p90"] is not None]
    strong = (sum(1 for v in p90s if v >= P90_SNR_STRONG)/len(p90s)) if p90s else 0.0
    if strong >= 0.6: txp, why2 = "BAJA-MEDIA", "Enlaces mayoritariamente fuertes; reducir potencia."
    elif strong >= 0.3: txp, why2 = "MEDIA", "Enlaces mixtos; potencia media."
    else: txp, why2 = "MEDIA-ALTA", "Enlaces débiles o escasos."

    hop_limit = DEFAULT_HOP_LIMIT if role != "REPEATER/ROUTER" else max(3, HOPS_TARGET_MAX)
    if role == "REPEATER/ROUTER": beacon, telem = BEACON_ROUTER_S, TEL_ROUTER_S
    else:                          beacon, telem = BEACON_CLIENT_S, TEL_CLIENT_S

    return {"role": role, "tx_power": txp, "hop_limit": hop_limit,
            "beacon_s": beacon, "telemetry_s": telem, "rationale": why+[why2]}

def _recommend_per_neighbor(n: Dict[str, Any]) -> Dict[str, Any]:
    action = []
    snr_p50   = n.get("snr_p50")
    snr_p90   = n.get("snr_p90")
    hops_avg  = n.get("hops_avg")
    dup_ratio = float(n.get("dup_ratio", 0.0))

    if snr_p50 is not None and snr_p50 >= SNR_OK and (hops_avg if hops_avg is not None else 99) <= HOPS_TARGET_MAX:
        action.append("Candidato a ROUTER si ubicación elevada y 24/7")
    if snr_p90 is not None and snr_p90 >= P90_SNR_STRONG:
        action.append("Bajar potencia TX para reducir interferencia local")
    if dup_ratio >= 0.3:
        action.append("Revisar cadencia de balizas/telemetría; probable redundancia")
    if (hops_avg if hops_avg is not None else 99) > HOPS_TARGET_MAX:
        action.append("No usar como repetidor; priorizar enlace hacia backbone o reubicar")
    if not action:
        action.append("Mantener como CLIENT_MUTE")

    return {
        "nid": n.get("nid"),
        "alias": n.get("alias"),
        "apps": n.get("apps", {}),
        "suggestions": action
    }

# ──────────────────────────────────────────────────────────────────────────────
# Carga de canal (estimación)
# ──────────────────────────────────────────────────────────────────────────────
def _airtime_ms(sf, bw_khz, cr_num, pl_bytes):
    bw = bw_khz*1000.0
    tsym = (2.0**sf)/bw
    preamble = 8.0
    H = 0
    DE = 1 if sf >= 11 else 0
    CR = cr_num-4
    payload_sym = 8 + max(math.ceil((8*pl_bytes - 4*sf + 28 + 16 - 20*H)/(4*(sf-2*DE)))*(CR+4), 0)
    n_sym = preamble + 4.25 + payload_sym
    return n_sym * tsym * 1000.0

def _channel_load_estimate(rows: List[Dict[str, Any]], hours: int) -> Dict[str, Any]:
    """
    Estima la carga de canal en la ventana:
      - duty_window: fracción 0..1 del tiempo de aire utilizado en la ventana
      - airtime_ms:  tiempo de aire total en ms
      - airtime_by_app: ms por APP
      - airtime_by_app_pct: % por APP (0..100)
      - dup_ratio:  proporción de duplicados en la ventana (0..1)
      - dups_count: nº de mensajes duplicados estimados
      - uniq_count: nº de mensajes únicos
    Siempre devuelve todas las claves aunque no haya datos.
    """
    total_ms = 0.0
    by_app: Dict[str, float] = {}
    # Para duplicados: contamos ids únicos y totales por APP.
    per_app_msgs_total: Dict[str, int] = {}
    uniq_ids: Set[Any] = set()

    # Parametrización LoRa segura
    sf = int(os.getenv("LORA_SF", str(LORA_SF)))
    bw = int(os.getenv("LORA_BW", str(LORA_BW)))
    cr = int(os.getenv("LORA_CR", str(LORA_CR)))
    cr = min(max(cr, 5), 8)  # clamp 5..8

    for r in rows:
        m = _extract_metrics(r)
        # Longitud payload fallback
        pl = m.get("pl")
        if pl is None:
            pl = LORA_PL
        try:
            pl = int(pl)
        except Exception:
            pl = LORA_PL
        pl = max(1, pl)

        # Tiempo de aire y agregados
        tms = _airtime_ms(sf, bw, cr, pl)
        total_ms += tms

        app = m.get("app") or "?"
        by_app[app] = by_app.get(app, 0.0) + tms

        # Duplicados
        mid = m.get("mid")  # tu extractor ya intenta poblarlo si existe
        per_app_msgs_total[app] = per_app_msgs_total.get(app, 0) + 1
        if mid is not None:
            uniq_ids.add(("__global__", mid))
        else:
            # Si no hay id de mensaje, no contribuimos a 'uniq', solo al total.
            pass

    # Duty
    window_ms = max(1.0, float(hours)) * 3_600_000.0
    duty = total_ms / window_ms

    # % por app en 0..100
    by_app_pct = {k: ((v / total_ms) * 100.0 if total_ms else 0.0) for k, v in by_app.items()}

    # Duplicados globales (aprox.): totales vs únicos
    total_pkts = sum(per_app_msgs_total.values())
    uniq_count = len(uniq_ids)
    if total_pkts > 0:
        dup_ratio = max(0.0, 1.0 - min(1.0, uniq_count / float(total_pkts)))
        dups_count = max(0, total_pkts - uniq_count)
    else:
        dup_ratio = 0.0
        dups_count = 0
        uniq_count = 0

    # Retorno con claves garantizadas
    return {
        "duty_window": float(duty),
        "airtime_ms": float(total_ms),
        "airtime_by_app": by_app,
        "airtime_by_app_pct": by_app_pct,
        "dup_ratio": float(dup_ratio),
        "dups_count": int(dups_count),
        "uniq_count": int(uniq_count),
    }

# ──────────────────────────────────────────────────────────────────────────────
# Persistencia (auditoría integral)
# ──────────────────────────────────────────────────────────────────────────────

def _parse_window_to_hours(token, default_hours):
    if not token: 
        return default_hours
    s = str(token).strip().lower()
    try:
        if s.endswith("h"):
            return int(float(s[:-1]))
        if s.endswith("m"):
            # minutos → horas
            return max(1, int(math.ceil(float(s[:-1]) / 60.0)))
        if s.endswith("d"):
            return int(float(s[:-1]) * 24)
        # número pelado → horas
        return int(float(s))
    except Exception:
        return default_hours


def _presence_profile(rows: List[Dict[str, Any]], hours: int) -> Dict[str, Dict[str, Any]]:
    """
    Calcula la presencia horaria y persistencia de cada nodo (solo hops ≤ HOPS_NEIGHBOR_MAX).
    Devuelve para cada nid:
      - presence_ratio: fracción de horas activas
      - days_seen: nº de días con actividad
      - hours_heard / hours_total
      - hour_hist: histograma 24h
    """
    HOPS_NEIGHBOR_MAX = int(os.getenv("AUD_HOPS_NEIGHBOR_MAX", "1"))
    TZNAME = os.getenv("AUD_TZ", "Europe/Madrid")
    try:
        import pytz
        _TZ = pytz.timezone(TZNAME)
    except Exception:
        _TZ = timezone.utc

    out: Dict[str, Dict[str, Any]] = {}
    now = time.time()
    cutoff = now - (hours * 3600)
    total_hours = max(1, hours)
    HOUR_BINS = range(24)

    for r in rows:
        m = _extract_metrics(r)
        if not m["nid"]:
            continue
        if m["ts"] < cutoff:
            continue
        if m["hops"] is not None and m["hops"] > HOPS_NEIGHBOR_MAX:
            continue  # ignora no vecinos

        nid = m["nid"]
        ent = out.setdefault(nid, {"hours": set(), "days": set(), "hour_hist": [0]*24})

        dt = datetime.fromtimestamp(m["ts"], tz=_TZ)
        # dentro del bucle principal de _presence_profile, después de calcular dt:
        ent.setdefault("first_ts", m["ts"])
        ent["first_ts"] = min(ent["first_ts"], m["ts"])
        ent["last_ts"]  = max(ent.get("last_ts", m["ts"]), m["ts"])

        ent["hours"].add((dt.year, dt.month, dt.day, dt.hour))
        ent["days"].add((dt.year, dt.month, dt.day))
        ent["hour_hist"][dt.hour] += 1

    for nid, ent in out.items():
        hours_heard = len(ent["hours"])
        days_seen = len(ent["days"])
        presence_ratio = hours_heard / total_hours
        ent.update({
            "hours_heard": hours_heard,
            "hours_total": total_hours,
            "days_seen": days_seen,
            "presence_ratio": presence_ratio
        })
    return out

def _is_persistent(p: Dict[str, Any]) -> bool:
    return (p.get("presence_ratio", 0.0) >= PERSIST_RATIO_MIN) or (p.get("days_seen", 0) >= PERSIST_DAYS_MIN)

# ──────────────────────────────────────────────────────────────────────────────
# Heatmap con capas (Persistentes VS Volátiles)
# ──────────────────────────────────────────────────────────────────────────────
def _generate_heatmap(base_name: str, hours: int) -> Optional[str]:
    if not HEATMAP_ENABLE: 
        return None

    out_html = os.path.join(HEATMAP_OUTDIR, f"{base_name}_heatmap.html")
    os.makedirs(os.path.dirname(out_html), exist_ok=True)

    try:
        import folium
        from folium.plugins import HeatMap
    except Exception:
        return None

    cutoff = time.time() - hours*3600

    # Cargamos NIDs persistentes del CSV si existe (generado en auditoría integral)
    persist_csv = os.path.join(OUT_DIR, f"{base_name}_persistentes.csv")
    persist_ids = set()
    if os.path.exists(persist_csv):
        try:
            with open(persist_csv, newline='', encoding='utf-8') as f:
                reader = csv.DictReader(f, delimiter=';')
                for row in reader:
                    if row.get("PERSIST") == "1":
                        persist_ids.add(row["NID"])
        except Exception as e:
            logging.debug(f"[heatmap] persistentes CSV no disponible: {e}")

    pts_persist, pts_volatile = [], []

    for r in _read_jsonl(PATH_COV) + _read_jsonl(PATH_POS):
        m = _extract_metrics(r)
        if m["ts"] < cutoff: 
            continue
        if m["lat"] is None or m["lon"] is None: 
            continue
        w = 0.5
        if m["snr"] is not None:
            w = (max(-12.0, min(12.0, m["snr"])) + 12.0)/24.0
        elif m["rssi"] is not None:
            w = (max(-120.0, min(-40.0, m["rssi"])) + 120.0)/80.0
        w = max(0.05, min(1.0, w))
        (pts_persist if m.get("nid") in persist_ids else pts_volatile).append([m["lat"], m["lon"], w])

    if not pts_persist and not pts_volatile:
        return None

    all_pts = pts_persist + pts_volatile
    clat = sum(p[0] for p in all_pts)/len(all_pts)
    clon = sum(p[1] for p in all_pts)/len(all_pts)

    m = folium.Map(location=[clat, clon], zoom_start=12, control_scale=True)

    # Capa persistentes → verde
    if pts_persist:
        HeatMap(
            pts_persist,
            name="Persistentes",
            radius=18, blur=16,
            gradient={0.2: "limegreen", 0.5: "green", 0.8: "darkgreen"},
            max_zoom=13
        ).add_to(m)

    # Capa no persistentes → azul
    if pts_volatile:
        HeatMap(
            pts_volatile,
            name="Volátiles",
            radius=18, blur=18,
            gradient={0.2: "lightblue", 0.5: "blue", 0.8: "darkblue"},
            max_zoom=13
        ).add_to(m)

    folium.LayerControl(collapsed=False).add_to(m)

    if HEATMAP_BOUNDS:
        try:
            lat1, lon1, lat2, lon2 = [float(x.strip()) for x in HEATMAP_BOUNDS.split(",")]
            m.fit_bounds([[lat1, lon1], [lat2, lon2]])
        except:
            pass

    m.save(out_html)
    return out_html

# ──────────────────────────────────────────────────────────────────────────────
# Exportadores CSV
# ──────────────────────────────────────────────────────────────────────────────
def _save_csv_neighbors(nei: List[Dict[str, Any]], out_csv: str) -> None:
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=';')
        w.writerow(["NID","ALIAS","SAMPLES","SNR_AVG","SNR_P50","SNR_P90",
                    "RSSI_AVG","HOPS_AVG","HOPS_MAX","DUP_RATIO","PAYLOAD_AVG","APPS","ULTIMO_VISTO"])
        for n in nei:
            snr_avg   = n.get("snr_avg")
            snr_p50   = n.get("snr_p50")
            snr_p90   = n.get("snr_p90")
            rssi_avg  = n.get("rssi_avg")
            hops_avg  = n.get("hops_avg")
            hops_max  = n.get("hops_max")
            dup_ratio = n.get("dup_ratio", 0.0)
            payload_avg = n.get("payload_avg")
            apps_dict = n.get("apps", {})

            w.writerow([
                n.get("nid",""), n.get("alias",""), n.get("samples",0),
                f"{snr_avg:.2f}"   if snr_avg   is not None else "",
                f"{snr_p50:.2f}"   if snr_p50   is not None else "",
                f"{snr_p90:.2f}"   if snr_p90   is not None else "",
                f"{rssi_avg:.1f}"  if rssi_avg  is not None else "",
                f"{hops_avg:.2f}"  if hops_avg  is not None else "",
                hops_max if hops_max is not None else "",
                f"{float(dup_ratio):.2f}",
                f"{payload_avg:.1f}" if payload_avg is not None else "",
                json.dumps(apps_dict, ensure_ascii=False),
                _ago(n.get("last_seen_sec") or 0)
            ])


def _save_csv_recommendations(local_rec: Dict[str, Any], per_neighbor: List[Dict[str, Any]], out_csv: str) -> None:
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=';')
        w.writerow(["SCOPE","NID","ALIAS","RECOMMENDATION"])
        w.writerow(["LOCAL","","", json.dumps(local_rec, ensure_ascii=False)])
        for r in per_neighbor:
            w.writerow(["NEIGHBOR", r.get("nid",""), r.get("alias",""), json.dumps(r.get("suggestions",[]), ensure_ascii=False)])

def _save_csv_persistentes(nei: List[Dict[str, Any]],
                           presence: Dict[str, Any],
                           out_csv: str) -> None:
    """
    CSV con persistencia y perfil horario.
    Columnas:
      NID;ALIAS;PERSIST;PRES_RATIO;HOURS_HEARD;HOURS_TOTAL;DAYS_SEEN;FIRST;LAST;H00..H23
    """
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=';')
        header = ["NID","ALIAS","PERSIST","PRES_RATIO","HOURS_HEARD","HOURS_TOTAL","DAYS_SEEN","FIRST","LAST"]
        header += [f"H{h:02d}" for h in range(24)]
        w.writerow(header)
        _nei_by_id = {n["nid"]: n for n in nei}
        for nid, pr in presence.items():
            n = _nei_by_id.get(nid, {"alias": ""})
            hist = pr.get("hour_hist", [0]*24)
            first = (datetime.fromtimestamp(pr["first_ts"], tz=timezone.utc).astimezone(_TZ).isoformat(timespec="seconds")
                     if pr.get("first_ts") else "")
            last  = (datetime.fromtimestamp(pr["last_ts"], tz=timezone.utc).astimezone(_TZ).isoformat(timespec="seconds")
                     if pr.get("last_ts") else "")
            row = [
                nid,
                n.get("alias",""),
                "1" if _is_persistent(pr) else "0",
                f"{pr.get('presence_ratio',0.0):.3f}",
                pr.get("hours_heard",0),
                pr.get("hours_total",0),
                pr.get("days_seen",0),
                first,
                last,
            ] + [int(x) for x in hist]
            w.writerow(row)

def _enriquecer_con_nodos_txt(nei: List[Dict[str, Any]], nodos_by_id: Dict[str, Dict[str, Any]]) -> None:
    for n in nei:
        nid = n.get("nid")
        if not nid:
            continue
        extra = nodos_by_id.get(nid)
        if not extra:
            continue
        # alias preferente si está vacío
        if not n.get("alias") and extra.get("alias"):
            n["alias"] = extra["alias"]

        # role y batería
        if extra.get("role"):
            n["role"] = extra["role"]
        if extra.get("battery") is not None:
            n["battery"] = extra["battery"]

        # Utilización de canal (porcentaje en texto → num)
        if extra.get("channel_util_pct") is not None:
            n["channel_util_pct"] = extra["channel_util_pct"]
        if extra.get("tx_air_util_pct") is not None:
            n["tx_air_util_pct"] = extra["tx_air_util_pct"]

        # SNR/Hops puntuales de tabla si faltan agregados
        if n.get("snr_p50") is None and extra.get("snr_db") is not None:
            n["snr_p50"] = extra["snr_db"]
        if n.get("hops_avg") is None and extra.get("hops") is not None:
            n["hops_avg"] = float(extra["hops"])
        if n.get("hops_max") is None and extra.get("hops") is not None:
            n["hops_max"] = int(extra["hops"])

        # Última vez visto
        if extra.get("last_heard"):
            n["last_heard_dt"] = extra["last_heard"]


def _find_latest_coverage_html() -> Optional[str]:
    """Devuelve la ruta del último coverage_*.html en MAPS_DIR o None."""
    try:
        if not os.path.exists(MAPS_DIR):
            return None
        htmls = [
            os.path.join(MAPS_DIR, f)
            for f in os.listdir(MAPS_DIR)
            if f.lower().endswith(".html") and f.startswith("coverage")
        ]
        if not htmls:
            return None
        return max(htmls, key=os.path.getmtime)
    except Exception:
        return None

# Opcional pero muy útil para diagnosticar “sin datos”
def _file_diag(hours: int) -> str:
    def _stat(path):
        if not os.path.exists(path):
            return (path, "NO_EXISTE", 0, "-")
        cnt, last_ts = 0, None
        for r in _read_jsonl(path):
            t = _safe_iso_to_ts(r.get("ts") or r.get("timestamp") or r.get("time"))
            if t is not None:
                cnt += 1
                last_ts = t if (last_ts is None or t > last_ts) else last_ts
        last_s = "-"
        if last_ts is not None:
            last_s = datetime.fromtimestamp(last_ts, tz=timezone.utc).astimezone(_TZ).isoformat(timespec="seconds")
        return (path, "OK", cnt, last_s)
    parts = []
    for p in (PATH_COV, PATH_POS, PATH_OFF, PATH_TEL):
        parts.append(" → ".join(map(str, _stat(p))))
    return "Diagnóstico de fuentes:\n" + "\n".join(parts) + f"\nVentana solicitada: {hours} h"

# ──────────────────────────────────────────────────────────────────────────────
# Comandos Telegram
# ──────────────────────────────────────────────────────────────────────────────

async def auditoria_red_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /auditoria_red [horas=72]
    Diagnóstico rápido: SNR/HOPS por vecino, recomendación local y CSV/JSON.
    """
   # en auditoria_red_cmd:
    args = context.args or []
    try:
        tok = args[0] if args else None
        hours = _parse_window_to_hours(tok, WINDOW_H)
    except Exception:
        hours = WINDOW_H


    _ensure_outdir()
    rows = _collect_rows(hours)
    if not rows:
            await update.effective_message.reply_text(
                "Sin datos en la ventana solicitada.\n" + _file_diag(hours),
                disable_web_page_preview=True
            )
            return


    T   = _build_neighbors(rows)
    nei = _summarize_neighbors(T)
    # Carga y enriquece con nodos.txt si existe (inmediatamente tras construir `nei`)
    _nodos_by_id = _parse_nodos_txt(NODOS_TXT_PATH)
    if _nodos_by_id:
        _enriquecer_con_nodos_txt(nei, _nodos_by_id)

    # Si no hay vecinos válidos por JSONL, sintetiza entradas desde nodos.txt
    if not nei and _nodos_by_id:
        def _f(x):
            try:
                return None if x is None else float(x)
            except Exception:
                return None
        def _i(x):
            try:
                return None if x is None else int(x)
            except Exception:
                return None

        synthetic = []
        for nid, extra in _nodos_by_id.items():
            snr = _f(extra.get("snr_db"))
            hops = _i(extra.get("hops"))
            # tras construir `synthetic.append({...})`, usa este cuerpo completo:
            synthetic.append({
                "nid": nid,
                "alias": (extra.get("alias") or extra.get("aka") or nid),
                "samples": 1,
                "snr_avg": snr,
                "snr_p50": snr,
                "snr_p90": snr,
                "rssi_avg": None,          # <- añade
                "hops_avg": _f(hops),
                "hops_max": hops,
                "apps": {},                # <- añade
                "payload_avg": None,       # <- añade
                "dup_ratio": 0.0,          # <- añade
                "last_seen_sec": 0,
                "_synthetic": True
            })

        nei.extend(synthetic)

    # Reordenar como hace _summarize_neighbors (fuertes primero, rutas cortas)
    nei.sort(key=lambda r: (-(r["snr_p90"] if r.get("snr_p90") is not None else -999),
                            (r["hops_avg"] if r.get("hops_avg") is not None else 999)))

    local_rec = _recommend_local(nei)
    routers_obj = min(CITY_BACKBONE_MAX, max(2, math.ceil(len(nei)/15)))

   


    ts  = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    base = f"auditoria_red_{ts}"
    out_csv  = os.path.join(OUT_DIR, f"{base}.csv")
    out_json = os.path.join(OUT_DIR, f"{base}.json")

    _save_csv_neighbors(nei, out_csv)
    payload = {
        "generated_at": ts, "window_hours": hours,
        "neighbors_count": len(nei),
        "routers_objetivo_ciudad": routers_obj,
        "recommendation": local_rec, "neighbors": nei[:150]
    }
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    top = nei[:8]
    lines = []
    for i, n in enumerate(top, 1):
        s50 = f"{n['snr_p50']:.1f}" if n['snr_p50'] is not None else "?"
        s90 = f"{n['snr_p90']:.1f}" if n['snr_p90'] is not None else "?"
        ha  = f"{n['hops_avg']:.1f}" if n['hops_avg'] is not None else "?"
        hm  = f"{n['hops_max']}" if n['hops_max'] is not None else "?"
        lines.append(f"{i}. {n.get('alias') or '?'} ({n['nid']}) — SNR p50 {s50}, p90 {s90} — hops avg {ha} máx {hm}")

    msg = (
        f"Auditoría de red ({hours} h)\n"
        f"Vecinos válidos: {len(nei)} — Objetivo routers ciudad: {routers_obj}\n\n"
        f"Rol sugerido: {local_rec['role']}\n"
        f"TX: {local_rec['tx_power']} | hop_limit: {local_rec['hop_limit']}\n"
        f"Baliza: {local_rec['beacon_s']} s | Telemetría: {local_rec['telemetry_s']} s\n"
        f"Motivos: {'; '.join(local_rec['rationale'])}\n\n"
        f"Vecinos principales:\n" + "\n".join(lines)
    )
    await update.effective_message.reply_text(msg, disable_web_page_preview=True)

    for path in (out_csv, out_json):
        try:
            with open(path, "rb") as f:
                await update.effective_message.reply_document(InputFile(f, filename=os.path.basename(path)))
        except Exception:
            pass


async def auditoria_integral_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /auditoria_integral [horas=72]
    Informe integral con métricas por vecino, recomendaciones locales y por vecino,
    estimación de carga de canal, persistentes y heatmap por capas.
    """
    args = context.args or []
    try:
        tok = args[0] if args else None
        hours = _parse_window_to_hours(tok, WINDOW_H)

    except Exception:
        hours = WINDOW_H

    _ensure_outdir()
    rows = _collect_rows(hours)
    if not rows:
        await update.effective_message.reply_text(
            "Sin datos en la ventana solicitada.\n" + _file_diag(hours),
            disable_web_page_preview=True
        )
        return

    T   = _build_neighbors(rows)
    nei = _summarize_neighbors(T)

    # Carga y enriquece con nodos.txt si existe (inmediatamente tras construir `nei`)
    _nodos_by_id = _parse_nodos_txt(NODOS_TXT_PATH)
    if _nodos_by_id:
        _enriquecer_con_nodos_txt(nei, _nodos_by_id)

    # Si no hay vecinos válidos por JSONL, sintetiza entradas desde nodos.txt
        # Si no hay vecinos válidos por JSONL, sintetiza entradas desde nodos.txt
    if not nei and _nodos_by_id:
        def _f(x):
            try:
                return None if x is None else float(x)
            except Exception:
                return None
        def _i(x):
            try:
                return None if x is None else int(x)
            except Exception:
                return None

        synthetic = []
        for nid, extra in _nodos_by_id.items():
            snr  = _f(extra.get("snr_db"))
            hops = _i(extra.get("hops"))
            synthetic.append({
                "nid": nid,
                "alias": (extra.get("alias") or extra.get("aka") or nid),
                "samples": 1,
                "snr_avg": snr,
                "snr_p50": snr,
                "snr_p90": snr,
                "rssi_avg": None,        # clave garantizada
                "hops_avg": _f(hops),    # promedio = valor puntual
                "hops_max": hops,
                "apps": {},              # clave garantizada
                "payload_avg": None,     # clave garantizada
                "dup_ratio": 0.0,        # clave garantizada
                "last_seen_sec": 0,
                "_synthetic": True
            })
        nei.extend(synthetic)

    # Reordenar como hace _summarize_neighbors (fuertes primero, rutas cortas)
    nei.sort(key=lambda r: (-(r["snr_p90"] if r.get("snr_p90") is not None else -999),
                            (r["hops_avg"] if r.get("hops_avg") is not None else 999)))

    local_rec    = _recommend_local(nei)
    per_neighbor = [_recommend_per_neighbor(n) for n in nei]
    load         = _channel_load_estimate(rows, hours)
    routers_obj  = min(CITY_BACKBONE_MAX, max(2, math.ceil(len(nei)/15)))

   # tras: load = _channel_load_estimate(rows, hours)
    duty_pct  = f"{float(load.get('duty_window', 0.0))*100:.2f}%"
    dup_pct   = f"{float(load.get('dup_ratio', 0.0))*100:.2f}%"

    # Persistencia
    presence = _presence_profile(rows, hours)
    _nei_by_id = {n["nid"]: n for n in nei}
    for nid, pr in presence.items():
        if nid in _nei_by_id:
            _nei_by_id[nid]["presence_ratio"] = pr.get("presence_ratio", 0.0)
            _nei_by_id[nid]["days_seen"]      = pr.get("days_seen", 0)
            _nei_by_id[nid]["hours_heard"]    = pr.get("hours_heard", 0)
            _nei_by_id[nid]["hours_total"]    = pr.get("hours_total", 0)
            _nei_by_id[nid]["persistent"]     = _is_persistent(pr)
            _nei_by_id[nid]["hour_hist"]      = pr.get("hour_hist", [0]*24)

    persistentes = [n for n in nei if n.get("persistent")]
    persistentes.sort(key=lambda x: (-x.get("presence_ratio",0.0), -(x.get("snr_p50") if x.get("snr_p50") is not None else -999)))

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    base = f"auditoria_integral_{ts}"
    out_csv_nei   = os.path.join(OUT_DIR, f"{base}_neighbors.csv")
    out_csv_reco  = os.path.join(OUT_DIR, f"{base}_recomendaciones.csv")
    out_csv_pers  = os.path.join(OUT_DIR, f"{base}_persistentes.csv")
    out_json      = os.path.join(OUT_DIR, f"{base}.json")

    _save_csv_neighbors(nei, out_csv_nei)
    _save_csv_recommendations(local_rec, per_neighbor, out_csv_reco)
    _save_csv_persistentes(nei, presence, out_csv_pers)

    payload = {
        "generated_at": ts,
        "window_hours": hours,
        "neighbors_count": len(nei),
        "routers_objetivo_ciudad": routers_obj,
        "local_recommendation": local_rec,
        "per_neighbor_recommendations": per_neighbor[:400],
        "channel_load": {
        "duty_window_ratio": load.get("duty_window", 0.0),
        "airtime_ms_total":  load.get("airtime_ms", 0.0),
        "airtime_by_app_ms": load.get("airtime_by_app", {}),
        "airtime_by_app_pct": load.get("airtime_by_app_pct", {}),
        "dup_ratio": load.get("dup_ratio", 0.0),
        "dups_count": load.get("dups_count", 0),
        "uniq_count": load.get("uniq_count", 0),
        },

        "persistent_neighbors": [
            {
                "nid": n["nid"],
                "alias": n.get("alias"),
                "presence_ratio": n.get("presence_ratio", 0.0),
                "days_seen": n.get("days_seen", 0),
                "hours_heard": n.get("hours_heard", 0),
                "hours_total": n.get("hours_total", 0),
                "hour_hist": n.get("hour_hist", []),
                "snr_p50": n.get("snr_p50"),
                "hops_avg": n.get("hops_avg"),
            }
            for n in persistentes[:300]
        ],
        "neighbors": nei[:600]
    }
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    # --- Usar último mapa de cobertura si existe; si no, generar uno nuevo ---
    out_html = _find_latest_coverage_html()
    if out_html:
        logging.info(f"[auditoria_integral] usando mapa existente: {out_html}")
    else:
        try:
            out_html = _generate_heatmap(base, hours)  # ← firma correcta: (base_name: str, hours: int)
            logging.info(f"[auditoria_integral] nuevo mapa generado: {out_html}")
        except Exception as e:
            logging.warning(f"No se pudo generar el mapa: {e}")
            out_html = None

    if out_html and os.path.exists(out_html):
        try:
            with open(out_html, "rb") as f:
                await update.effective_message.reply_document(InputFile(f, filename=os.path.basename(out_html)))
        except Exception as e:
            logging.warning(f"No se pudo enviar el mapa de cobertura: {e}")

    
    top = nei[:8]
    lines = []
    for i, n in enumerate(top, 1):
        s50 = f"{n['snr_p50']:.1f}" if n['snr_p50'] is not None else "?"
        s90 = f"{n['snr_p90']:.1f}" if n['snr_p90'] is not None else "?"
        ha  = f"{n['hops_avg']:.1f}" if n['hops_avg'] is not None else "?"
        hm  = f"{n['hops_max']}" if n['hops_max'] is not None else "?"
        lines.append(f"{i}. {n.get('alias') or '?'} ({n['nid']}) — SNR p50 {s50}, p90 {s90} — hops avg {ha} máx {hm}")

    msg = (
        f"Auditoría integral ({hours} h)\n"
        f"Vecinos válidos: {len(nei)} — Objetivo routers ciudad: {routers_obj}\n"
        f"Carga estimada de canal: {duty_pct}  •  Duplicados: {dup_pct}\n"
        f"Configuración sugerida para este nodo:\n"
        f"Rol: {local_rec['role']} | TX: {local_rec['tx_power']} | hop_limit: {local_rec['hop_limit']}\n"
        f"Baliza: {local_rec['beacon_s']} s | Telemetría: {local_rec['telemetry_s']} s\n"
        f"Motivos: {'; '.join(local_rec['rationale'])}\n"
    )

    top_p = persistentes[:5]
    if top_p:
        lines_p = []
        for i, n in enumerate(top_p, 1):
            pr = f"{n.get('presence_ratio',0.0)*100:.1f}%"
            d  = n.get("days_seen", 0)
            s50 = f"{n['snr_p50']:.1f}" if n.get('snr_p50') is not None else "?"
            ha  = f"{n['hops_avg']:.1f}" if n.get('hops_avg') is not None else "?"
            lines_p.append(f"{i}. {n.get('alias') or '?'} ({n['nid']}) — presencia {pr}, días {d}, SNR p50 {s50}, hops avg {ha}")
        msg += "\nNodos persistentes:\n" + "\n".join(lines_p)

    msg += "\n\nVecinos principales:\n" + "\n".join(lines)

    await update.effective_message.reply_text(msg, disable_web_page_preview=True)

    for path in (out_csv_nei, out_csv_reco, out_csv_pers, out_json):
        try:
            with open(path, "rb") as f:
                await update.effective_message.reply_document(InputFile(f, filename=os.path.basename(path)))
        except Exception:
            pass
