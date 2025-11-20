# positions_store.py 	   	 	 	    	   		 
# Version v6.1.3 	  	   	 	 	     	 	

import os, json, time 	  		 	 					 	  		 
from datetime import datetime, timezone, timedelta	 		  	 	 			  		 		 
from threading import RLock	    	   			 		    	 

_POS_LOG_LOCK = RLock()			 	   		  	 	 			 	
POSITIONS_LOG_PATH = os.path.join("bot_data", "positions.jsonl")		 		    	 				  	 	 
POSITIONS_KEEP_DAYS = 7					 			 		   		 		 

def _ts_now_utc() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp())

def _compact_positions_log_if_needed(path: str, keep_days: int) -> None:
    if keep_days <= 0:
        return
    try:
        if not os.path.exists(path) or os.path.getsize(path) < 5 * 1024 * 1024:
            return
        cutoff = _ts_now_utc() - keep_days * 86400
        tmp = path + ".tmp"
        with open(path, "r", encoding="utf-8") as fin, open(tmp, "w", encoding="utf-8") as fout:
            for line in fin:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if int(rec.get("ts", 0)) >= cutoff:
                    fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    except Exception:
        pass

def append_position_record(rec: dict) -> None:
    os.makedirs(os.path.dirname(POSITIONS_LOG_PATH), exist_ok=True)
    with _POS_LOG_LOCK:
        with open(POSITIONS_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        _compact_positions_log_if_needed(POSITIONS_LOG_PATH, POSITIONS_KEEP_DAYS)

def read_positions_recent(last_minutes: int, max_nodes: int) -> list[dict]:
    cutoff = _ts_now_utc() - last_minutes * 60
    best: dict[str, dict] = {}
    if not os.path.exists(POSITIONS_LOG_PATH):
        return []
    with open(POSITIONS_LOG_PATH, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            ts = int(rec.get("ts", 0))
            if ts < cutoff:
                continue
            nid = rec.get("id") or ""
            if not nid:
                continue
            prev = best.get(nid)
            if prev is None or ts > int(prev.get("ts", 0)):
                best[nid] = rec
    rows = list(best.values())
    rows.sort(key=lambda r: int(r.get("ts", 0)), reverse=True)
    return rows[:max_nodes] if max_nodes > 0 else rows

def build_kml(rows: list[dict], name="Meshtastic Positions") -> bytes:
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<kml xmlns="http://www.opengis.net/kml/2.2">',
           f'  <Document><name>{name}</name>']
    for r in rows:
        lat, lon = r.get("lat"), r.get("lon")
        if lat is None or lon is None: continue
        alias = r.get("alias") or r.get("id")
        coords = f"{lon},{lat},{r.get('alt') or 0}"
        desc = f"!{r.get('id')} • SNR {r.get('rx_snr')} • RSSI {r.get('rx_rssi')}"
        out.append(f"    <Placemark><name>{alias}</name><description>{desc}</description>"
                   f"<Point><coordinates>{coords}</coordinates></Point></Placemark>")
    out.append("  </Document></kml>")
    return "\n".join(out).encode("utf-8")

def build_gpx(rows: list[dict], name="Meshtastic Positions") -> bytes:
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<gpx version="1.1" creator="BotMesh" xmlns="http://www.topografix.com/GPX/1/1">',
           f'  <metadata><name>{name}</name></metadata>']
    for r in rows:
        lat, lon = r.get("lat"), r.get("lon")
        if lat is None or lon is None: continue
        alias = r.get("alias") or r.get("id")
        out.append(f'  <wpt lat="{lat}" lon="{lon}"><name>{alias}</name>'
                   f'<desc>!{r.get("id")}</desc></wpt>')
    out.append("</gpx>")
    return "\n".join(out).encode("utf-8")

# === [NUEVO] TelemetryStore ===================================================
import os, json, time, threading
from typing import Dict, Any, Optional, List

class TelemetryStore:
    """
    Guarda telemetría recibida (TELEMETRY_APP) en:
      - JSONL de histórico (append-only)
      - 'último por nodo' en memoria (consulta rápida)
    No interfiere con PositionStore.
    """
    def __init__(self, jsonl_path: str = os.path.join("bot_data", "telemetry_log.jsonl")):
        self.jsonl_path = jsonl_path
        self._last_by_node: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(jsonl_path) or ".", exist_ok=True)

    def _append_jsonl(self, row: Dict[str, Any]) -> None:
        try:
            with open(self.jsonl_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def ingest_packet(self, pkt: Dict[str, Any]) -> None:
        """
        pkt: dict ya decodificado del broker (incluye decoded.portnum == 'TELEMETRY_APP')
        Extrae métricas 'device' y/o 'environment' si están presentes.
        """
        ts = pkt.get("rxTime") or pkt.get("timestamp") or time.time()
        from_id = None
        try:
            from_id = pkt.get("fromId") or pkt.get("from") or pkt.get("from_id")
        except Exception:
            pass

        decoded = (pkt.get("decoded") or {})
        payload = decoded.get("telemetry", {}) or decoded.get("payload", {}) or {}

        # Normalizamos posibles campos
        device = payload.get("deviceMetrics") or payload.get("device") or {}
        env    = payload.get("environmentMetrics") or payload.get("environment") or {}

        if not (device or env):
            return

        row = {
            "ts": ts,
            "from": from_id,
            "device": device or None,
            "environment": env or None,
        }

        with self._lock:
            # Actualizamos 'último por nodo'
            cur = self._last_by_node.get(from_id) or {}
            cur.update({
                "ts": ts,
                "device": {**(cur.get("device") or {}), **device} if device else (cur.get("device") or None),
                "environment": {**(cur.get("environment") or {}), **env} if env else (cur.get("environment") or None),
            })
            self._last_by_node[from_id] = cur

        self._append_jsonl(row)

    def get_last(self, node_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._last_by_node.get(node_id)

    def query_since(self, since_ts: float, node_id: Optional[str] = None, limit: int = 500) -> List[Dict[str, Any]]:
        """
        Lee el JSONL y devuelve eventos desde since_ts (filtrando opcionalmente por node_id).
        """
        out = []
        try:
            with open(self.jsonl_path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        row = json.loads(line)
                        if row.get("ts", 0) >= since_ts:
                            if node_id and row.get("from") != node_id:
                                continue
                            out.append(row)
                            if len(out) >= limit:
                                break
                    except Exception:
                        continue
        except FileNotFoundError:
            pass
        return out
# === [FIN TelemetryStore] =====================================================
