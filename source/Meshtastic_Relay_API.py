#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Version v7.0.12
"""
Meshtastic Relay Checker – Version v7.0.12 versión portable y robusta.

Funciones principales (resumen):
    sincronizar_nodos_y_guardar(path=FICHERO_SALIDA)
        → Llama a `meshtastic --host HOST --nodes` y guarda la tabla textual.
    get_visible_nodes_from_file_ordenados(path=FICHERO_SALIDA)
        → Lista [(id, alias, minutos)] ordenada por “último visto”.
    get_visible_nodes_with_hops(path=FICHERO_SALIDA)
        → Igual que la anterior, pero añade hops de la tabla: [(id, alias, minutos, hops|None)].
    cargar_aliases_desde_nodes(path=FICHERO_SALIDA)
        → Devuelve {id: alias} con heurística flexible (User/Aka/Alias/Name).
    check_route_con_timeout(node_id)
        → Traceroute “resumen”, devuelve (estado, hops).
    check_route_detallado(node_id)
        → Traceroute “detallado”, devuelve (estado, hops, [ruta_ids], salida_bruta).
    get_vecinos_directos_desde_tabla()
        → Vecinos directos según tabla (--nodes): hops_tabla == 0.
    formatear_ruta_con_alias(path_ids, aliases)
        → Texto “!ID (Alias) --> !ID (Alias) ...”.
    send_test_message(node_id|None, text, canal=0)
        → Envía mensaje por CLI (broadcast si node_id=None).
    export_csv(rows)
        → Guarda verificación en CSV (NodeID, Alias, Traceroute, Hops, Resultado, Canal).

Novedades clave:
- Parser de `--nodes` mejorado: reconoce cabeceras con columnas “User/AKA/ID/Hops/Since”.
- Cálculo de hops fiable (usa índice de columna “Hops”; regex tolerante a valores no puros).
- Ordenación por “Since” robusta (soporta “now”, “X secs/mins/hours ago”).
- Portable Windows/Linux: descubrimiento automático del ejecutable `meshtastic`.
"""

from __future__ import annotations

import csv
import logging
import os
import re
import shutil
import subprocess
from subprocess import PIPE, STDOUT
from typing import Dict, List, Tuple, Optional

from meshtastic_api_adapter import api_list_nodes, api_traceroute, api_send_text

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

DEFAULT_HOST = "192.168.1.201"
HOST = os.getenv("MESHTASTIC_HOST", DEFAULT_HOST)

MENSAJE_PRUEBA: str   = "Mensaje de prueba desde el nodo base"
CSV_FILENAME: str     = "relay_nodes.csv"
LOG_FILENAME: str     = "relay_debug.log"
FICHERO_SALIDA: str   = "salida_nodos.txt"
TIMEOUT_SEGUNDOS: int = 15

# Definición clara de “directo”:
DIRECT_HOPS_TABLA = 0            # En TABLA (--nodes): hops 0 = enlace directo
DIRECT_HOPS_TRACEROUTE = 1       # En TRACEROUTE: 1 salto = origen --> destino

# --- Meshtastic_Relay.py ---

import re, time
from datetime import datetime

# … (imports existentes) …

# Normaliza nombres de columnas a claves canónicas
def _norm_header(h: str) -> str:
    t = (h or "").strip().lower()
    t = re.sub(r"[\s_]+", "", t)  # quita espacios y _
    # alias frecuentes
    if t in ("user","alias","aka","name"): return "alias"
    if t in ("id","nodeid","node"):       return "id"
    if t in ("hops","hopscount","numhops","Hops"):return "hops"
    # claves de “última vez visto”
    if t in ("lastheard","lastheardtime","heard","lastseen","since","sincelastheard","lasthour"):
        return "last_heard"
    # por si tu tabla trae “since/last heard” en texto descriptivo
    if t in ("sinceago","last","seen"):    return "last_heard"
    return t


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    filename=LOG_FILENAME,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
def log(msg: str) -> None:
    """Añade una línea al log del sistema."""
    logging.info(msg)

# ---------------------------------------------------------------------------
# Localización del ejecutable de meshtastic (portable)
# ---------------------------------------------------------------------------

def _resolve_cli_path() -> str:
    """
    Determina la ruta del ejecutable 'meshtastic' de forma portable.
    Prioridad:
      1) Variable de entorno MESHTASTIC_CLI_PATH
      2) En Windows: buscar 'meshtastic.exe' (también en PATH)
      3) En cualquier SO: 'meshtastic' en PATH
      4) Ruta típica de pip en Windows (ajústala si hace falta)
    """
    env_path = os.getenv("MESHTASTIC_CLI_PATH")
    if env_path and os.path.exists(env_path):
        return env_path

    cand = shutil.which("meshtastic.exe")
    if cand:
        return cand

    cand = shutil.which("meshtastic")
    if cand:
        return cand

    posible = os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python313\Scripts\meshtastic.exe")
    if os.path.exists(posible):
        return posible

    raise RuntimeError(
        "No se encontró el ejecutable 'meshtastic'. "
        "Añádelo al PATH o define MESHTASTIC_CLI_PATH con la ruta completa."
    )

# ---------------------------------------------------------------------------
# Ejecutor de comandos
# ---------------------------------------------------------------------------

def run_command(cmd_args: List[str], timeout: int | None = None) -> str:
    """
    Ejecuta meshtastic (o meshtastic.exe) con los argumentos dados y devuelve stdout+stderr como texto UTF-8.
    *cmd_args* debe empezar con 'meshtastic'.

    Parámetros:
        cmd_args: p.ej. ["meshtastic", "--host", HOST, "--nodes"]
        timeout:  segundos de espera

    Retorna: str con la salida combinada.
    """
    if not cmd_args or cmd_args[0] != "meshtastic":
        raise ValueError("cmd_args debe empezar por 'meshtastic'")

    cli = _resolve_cli_path()
    full_cmd = [cli] + cmd_args[1:]
    log(f"⏳ Ejecutando: {' '.join(full_cmd)}")
    try:
        completed = subprocess.run(
            full_cmd,
            stdout=PIPE,
            stderr=STDOUT,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=timeout,
        )
        return completed.stdout or ""
    except FileNotFoundError as exc:
        raise RuntimeError(
            "❌ No se pudo ejecutar 'meshtastic'. Revisa MESHTASTIC_CLI_PATH o el PATH del sistema."
        ) from exc

# ---------------------------------------------------------------------------
# Sincronización de nodos
# ---------------------------------------------------------------------------

def sincronizar_nodos_y_guardar(path: str = FICHERO_SALIDA) -> str:
    """
    API-first: consulta los nodos por API y vuelca una lista legible a path,
    manteniendo compatibilidad con el resto del código que lee el fichero.
    """
    rows = api_list_nodes(HOST, max_n=200)
    # Formato legible (similar a tu salida previa):
    lines = ["N  │ User/Alias │ ID │ Hops │ Visto (min)"]
    for i, r in enumerate(rows, start=1):
        lines.append(f"{i:>3}│ {r['alias'] or '-':<12} │ {r['id']:<10} │ {r.get('hops') if r.get('hops') is not None else '-':>4} │ {r.get('last_heard_min') if r.get('last_heard_min') is not None else '-':>4}")
    text = "\n".join(lines)
    with open(path, "w", encoding="utf-8") as fp:
        fp.write(text + "\n")
    log(f"💾 Nodos (API-first) guardados en {path}")
    return text

# ---------------------------------------------------------------------------
# Utilidades de parsing
# ---------------------------------------------------------------------------

def minutes_from_lastheard(lastheard: str) -> int:
    """
    Convierte la columna `last_heard` a minutos:
    - numérico (epoch seconds) -> ahora - ts
    - ISO “YYYY-MM-DD HH:MM[:SS]”
    - “now/just now/an hour ago/…”
    Si no reconoce, devuelve 9999.
    """
    if not lastheard:
        return 9_999
    t = lastheard.strip().lower()
    if t in {"now","just now"}:
        return 0
    if "a minute" in t:
        return 1
    if "an hour" in t:
        return 60
    # quitar “ago” si viene en inglés
    t = t.replace("ago", "").strip()

    # epoch seconds
    if re.fullmatch(r"\d{9,12}", t):
        try:
            ts = int(t)
            return max(0, int((time.time() - ts) // 60))
        except Exception:
            pass

    # ISO “YYYY-MM-DD HH:MM[:SS]”
    m = re.search(r"\b(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}(?::\d{2})?)", lastheard)
    if m:
        try:
            fmt = "%Y-%m-%d %H:%M:%S" if len(m.group(2)) == 8 else "%Y-%m-%d %H:%M"
            dt = datetime.strptime(f"{m.group(1)} {m.group(2)}", fmt)
            return max(0, int((datetime.now() - dt).total_seconds() // 60))
        except Exception:
            pass

    # abreviados “3h 10m”, “45m”, “90s”
    # (por si alguna build imprime relativo en texto)
    m2 = re.fullmatch(r"(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?", t)
    if m2 and any(m2.groups()):
        h = int(m2.group(1) or 0)
        mm = int(m2.group(2) or 0)
        ss = int(m2.group(3) or 0)
        return h*60 + mm + (ss // 60)

    return 9_999



_minutes_pattern = re.compile(r"(\d+)\s*(hours?|hrs?|minutes?|mins?|seconds?|secs?)", re.I)

def parse_minutes(texto: str) -> int:
    """
    Convierte expresiones tipo '5 mins', '2 hours', '10 seconds' a minutos (int).
    'now' o 'just now' → 0. Si no reconoce nada → 9999 (para ordenar al final).
    """
    if not texto:
        return 9_999
    m = _minutes_pattern.search(texto)
    if not m:
        if texto.strip().lower() in {"now", "just now"}:
            return 0
        return 9_999
    value, unit = int(m.group(1)), m.group(2).lower()
    if unit.startswith(("hour", "hr")):
        return value * 60
    if unit.startswith("min"):
        return value
    return 0  # segundos → 0 min

def _normalize_col(col: str) -> str:
    return col.strip().lower().replace(" ", "").replace("-", "")

def _smart_split(line: str) -> List[str]:
    """
    Divide una línea de tabla de manera flexible:
    - Si hay '│' Unicode → split por '│'
    - Si hay '|' ASCII → split por '|'
    - Si no, divide por grupos de 2+ espacios
    """
    if "│" in line:
        parts = [p.strip() for p in line.split("│")]
    elif "|" in line:
        parts = [p.strip() for p in line.split("|")]
    else:
        parts = [p.strip() for p in re.split(r"\s{2,}", line)]
    return [p for p in parts if p]

def _extract_id_alias_guess(parts: List[str]) -> Tuple[Optional[str], Optional[str]]:
    """
    Fallback cuando no hay cabeceras claras:
    - ID = primer campo que empiece por '!'
    - Alias = celda anterior o posterior (si no es otro ID)
    """
    nid = None
    alias = None
    for i, p in enumerate(parts):
        if p.startswith("!"):
            nid = p
            if i > 0 and not parts[i-1].startswith("!"):
                alias = parts[i-1]
            elif i + 1 < len(parts) and not parts[i+1].startswith("!"):
                alias = parts[i+1]
            break
    return nid, alias

def _parse_nodes_table(path_or_text) -> list[dict]:
    """
    Parsea la tabla de 'meshtastic --nodes'.
    Devuelve lista de dicts con al menos: id, alias, hops_text, last_heard (si existe).
    Acepta ruta de fichero o el texto completo.
    """
    if "\n" in str(path_or_text) or "│" in str(path_or_text):
        content = str(path_or_text)
    else:
        with open(path_or_text, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()

    lines = [ln.rstrip("\r") for ln in content.splitlines() if ln.strip()]
    # localiza cabecera (línea con separadores “│”)
    header_line = None
    for ln in lines:
        if "│" in ln and not ln.strip().startswith("N"):
            header_line = ln
            break
    if not header_line:
        return []

    headers = [h.strip() for h in header_line.split("│")]
    H = [_norm_header(h) for h in headers]

    rows: list[dict] = []
    for ln in lines:
        if "│" not in ln or ln == header_line or ln.strip().startswith("N"):
            continue
        parts = [p.strip() for p in ln.split("│")]
        if len(parts) != len(H):
            # tolera anchos distintos
            parts = (parts + [""] * len(H))[:len(H)]
        rec = {}
        for key, val in zip(H, parts):
            if key:
                rec[key] = val
        # estandariza algunas claves útiles
        if "hops" in rec and rec.get("hops"):
            rec["hops_text"] = rec.get("hops")
        rows.append(rec)
    return rows

# ---------------------------------------------------------------------------
# Lectura de nodos visibles (versión clásica y nueva con hops)
# ---------------------------------------------------------------------------

def get_visible_nodes_from_file_ordenados(
    path: str = FICHERO_SALIDA,
) -> List[Tuple[str, str, int]]:
    """
    Lee 'salida_nodos.txt' y devuelve [(node_id, alias, minutos)] ordenado asc.
    Compatibilidad: no incluye hops para no romper llamadas existentes.
    Si el archivo no existe intenta sincronizar; si falla devuelve lista vacía.
    """
    if not os.path.exists(path):
        log(f"📂 {path} no existe. Sincronizando para generarlo…")
        try:
            sincronizar_nodos_y_guardar(path)
        except Exception as exc:
            log(f"❌ Sincronización fallida: {exc}")
            return []

    rows = _parse_nodes_table(path)
    out: List[Tuple[str, str, int]] = []
    for r in rows:
        mins = parse_minutes(r.get("last_seen_text", "") or "")
        out.append((r.get("id", ""), r.get("alias", "") or "", mins))
    out.sort(key=lambda x: x[2])
    return out

def _to_int_safe(s: Optional[str]) -> Optional[int]:
    """Convierte string a int usando la primera coincidencia numérica (acepta '0', '0 hops', etc.)."""
    if not s:
        return None
    m = re.search(r"-?\d+", s)
    if not m:
        return None
    try:
        return int(m.group(0))
    except Exception:
        return None

def get_visible_nodes_with_hops(
    path: str = FICHERO_SALIDA,
) -> List[Tuple[str, str, int, Optional[int]]]:
    """
    Versión extendida que añade hops de la tabla si existe.
    Devuelve [(node_id, alias, minutos, hops_tabla|None)] ordenado ascendente por minutos.
    """
    if not os.path.exists(path):
        log(f"📂 {path} no existe. Sincronizando para generarlo…")
        try:
            sincronizar_nodos_y_guardar(path)
        except Exception as exc:
            log(f"❌ Sincronización fallida: {exc}")
            return []

    rows = _parse_nodes_table(path)
    out: List[Tuple[str, str, int, Optional[int]]] = []
    for r in rows:
        mins = parse_minutes(r.get("last_seen_text", "") or "")
        hops_val = _to_int_safe(r.get("hops_text"))
        out.append((r.get("id", ""), r.get("alias", "") or "", mins, hops_val))
    out.sort(key=lambda x: x[2])
    return out

def cargar_aliases_desde_nodes(path: str = FICHERO_SALIDA) -> Dict[str, str]:
    """
    Devuelve un diccionario { node_id: alias } a partir del fichero --nodes.
    Si no existe, intenta generarlo llamando a sincronizar_nodos_y_guardar().
    (Parser rápido: usa heurística flexible para no depender de cabeceras.)
    """
    if not os.path.exists(path):
        try:
            sincronizar_nodos_y_guardar(path)
        except Exception:
            return {}
    aliases: Dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip("\n")
                if not line.strip():
                    continue
                if ("!" not in line) and ("│" not in line) and ("|" not in line):
                    continue
                parts = _smart_split(line)
                nid, alias = _extract_id_alias_guess(parts)
                if nid and nid.startswith("!"):
                    aliases[nid] = (alias or "")
    except FileNotFoundError:
        return {}
    except Exception as e:
        log(f"❌ Error cargando aliases: {e}")
    return aliases

# ---------------------------------------------------------------------------
# Traceroute
# ---------------------------------------------------------------------------

def _parse_traceroute_path(output: str) -> List[str]:
    """
    Extrae la ruta de la salida de 'meshtastic --traceroute', devolviendo
    una lista de IDs en orden. Si no encuentra flechas, devuelve [].
    """
    line = None
    for ln in output.splitlines():
        if "Route traced" in ln and "-->" in ln:
            line = ln.strip()
            break
    if not line:
        return []
    try:
        ruta_txt = line.split(":", 1)[1]
    except Exception:
        return []
    nodos = [p.strip() for p in ruta_txt.split("-->") if p.strip()]
    return [n for n in nodos if n.startswith("!")]

def check_route_detallado(node_id: str) -> Tuple[str, int, List[str], str]:
    res = api_traceroute(HOST, node_id, timeout=TIMEOUT_SEGUNDOS)
    if res["ok"]:
        return "✔ Ruta encontrada", int(res["hops"]), list(res["route"]), str(res["raw"])
    return "Sin ruta", 0, [], str(res["raw"])

def check_route_con_timeout(node_id: str) -> Tuple[str, int]:
    res = api_traceroute(HOST, node_id, timeout=TIMEOUT_SEGUNDOS)
    return ("✔ Ruta encontrada", int(res["hops"])) if res["ok"] else ("Sin ruta", 0)

# ---------------------------------------------------------------------------
# Vecinos directos (tabla)
# ---------------------------------------------------------------------------

def get_vecinos_directos_desde_tabla() -> List[Tuple[str, str]]:
    """
    Devuelve vecinos directos según la TABLA (--nodes), es decir, hops_tabla == 0.
    Formato: [(node_id, alias), ...]
    """
    vecinos: List[Tuple[str, str]] = []
    for node_id, alias, _mins, hops_tabla in get_visible_nodes_with_hops():
        if hops_tabla is not None and hops_tabla == DIRECT_HOPS_TABLA:
            vecinos.append((node_id, alias))
    return vecinos

def formatear_ruta_con_alias(path_ids: List[str], aliases: Dict[str, str]) -> str:
    """
    Devuelve la ruta formateada como: !ID (Alias) --> !ID (Alias) ...
    Si no hay alias, deja solo el ID.
    """
    partes = []
    for nid in path_ids:
        ali = aliases.get(nid, "")
        partes.append(f"{nid} ({ali})" if ali else nid)
    return " --> ".join(partes)

# ---------------------------------------------------------------------------
# Mensajería
# ---------------------------------------------------------------------------

def send_test_message(node_id: str | None, text: str, canal: int = 0) -> str:
    r = api_send_text(HOST, node_id, text, channel=int(canal), want_ack=False, timeout=TIMEOUT_SEGUNDOS)
    if r["ok"]:
        pid = r.get("packet_id")
        return f"Enviado (API-first){f' • packet_id={pid}' if pid else ''}"
    return f"Error en envío: {r.get('raw','')}"

# ---------------------------------------------------------------------------
# Exportar CSV
# ---------------------------------------------------------------------------

def export_csv(rows) -> None:
    """
    Guarda los resultados de verificación en relay_nodes.csv.
    Espera filas con: [NodeID, Alias, Traceroute, Hops, Resultado Envío, Canal]
    """
    log(f"💾 Exportando {len(rows)} filas a {CSV_FILENAME}")
    with open(CSV_FILENAME, "w", newline="", encoding="utf-8") as fp:
        writer = csv.writer(fp)
        writer.writerow(
            ["NodeID", "Alias", "Traceroute", "Hops", "Resultado Envío", "Canal"]
        )
        writer.writerows(rows)


if __name__ == "__main__":
    # Uso rápido sin Telegram: sincroniza y deja salida en disco
    print("Sincronizando nodos...")
    try:
        sincronizar_nodos_y_guardar()
        print(f"OK. Tabla guardada en {FICHERO_SALIDA}")
    except Exception as e:
        print(f"Error: {e}")
