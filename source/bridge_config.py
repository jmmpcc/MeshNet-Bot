#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bridge_config.py v7.0.13
=========================

Gestor seguro de configuración de Bridge para MeshNet Bot.

Objetivo
--------
Este módulo introduce una capa de configuración externa para decidir cómo debe
funcionar la pasarela/bridge entre nodos, sin tocar inicialmente el WebPanel ni
modificar la lógica estable de bridge_in_broker.py.

Uso principal
-------------
1) Validar configuración:

    python bridge_config.py validate
    python bridge_config.py validate --config bot_data/bridge_config.json

2) Ver estado legible:

    python bridge_config.py status

3) Exportar variables equivalentes para .env/systemd/docker:

    python bridge_config.py export-env

4) Crear una configuración inicial si no existe:

    python bridge_config.py init

Diseño 24/7
-----------
- No abre conexiones TCP.
- No modifica el broker.
- No transmite por RF.
- No escribe salvo con el comando init o save explícito.
- Valida configuraciones peligrosas antes de que el broker/bridge las usen.
- Mantiene compatibilidad: si no existe bridge_config.json, se puede seguir con
  las variables .env actuales.

Perfiles soportados
-------------------
- off
- embedded_b
- meshcore_embedded
- external_ab
- external_ac
- external_abc

Autoridad de configuración
--------------------------
Archivo recomendado:

    bot_data/bridge_config.json

La función resolve_bridge_runtime() convierte el JSON validado en variables
runtime equivalentes para el broker o el triple bridge.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


# =============================================================================
# Constantes
# =============================================================================

VERSION = "v7.0.13"
DEFAULT_CONFIG_PATH = os.path.join("bot_data", "bridge_config.json")

VALID_PROFILES = {
    "off",
    "embedded_b",
    "meshcore_embedded",
    "external_ab",
    "external_ac",
    "external_abc",
}

VALID_HUB_MODES = {"broker", "tcp"}
VALID_PEERS = {"B", "C"}
_VALID_NODE_KEYS = {"A", "B", "C"}

_PROFILE_ALLOWED_PEERS = {
    "off": set(),
    "embedded_b": {"B"},
    "meshcore_embedded": {"B"},
    "external_ab": {"B"},
    "external_ac": {"C"},
    "external_abc": {"B", "C"},
}

_PROFILE_REQUIRED_MAPS = {
    "off": set(),
    "embedded_b": {"A2B", "B2A"},
    "meshcore_embedded": set(),
    "external_ab": {"A2B", "B2A"},
    "external_ac": {"A2C", "C2A"},
    "external_abc": {"A2B", "B2A", "A2C", "C2A"},
}

_MAP_RE = re.compile(r"^\s*\d+\s*:\s*\d+\s*(?:,\s*\d+\s*:\s*\d+\s*)*$")


# =============================================================================
# Dataclasses de salida
# =============================================================================

@dataclass
class BridgeValidationResult:
    """
    Resultado de validar una configuración de bridge.

    Campos:
        ok:
            True si la configuración no tiene errores bloqueantes.
        errors:
            Lista de errores que impiden aplicar la configuración.
        warnings:
            Lista de avisos no bloqueantes.
        config:
            Configuración normalizada con valores por defecto aplicados.
        runtime:
            Diccionario runtime equivalente a variables de entorno.

    Uso:
        result = validate_bridge_config(config)
        if result.ok:
            env = result.runtime["env"]
    """
    ok: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    config: Dict[str, Any] = field(default_factory=dict)
    runtime: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Devuelve el resultado como dict serializable."""
        return asdict(self)


# =============================================================================
# Configuración por defecto
# =============================================================================

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "profile": "embedded_b",
    "hub": {
        "mode": "broker",
    },
    "nodes": {
        "A": {
            "name": "Nodo A Meshtastic principal",
            "host": "192.168.1.22",
            "port": 4403,
            "role": "hub",
        },
        "B": {
            "name": "Nodo B secundario",
            "host": "192.168.1.23",
            "port": 4403,
            "role": "peer",
        },
        "C": {
            "name": "Nodo C externo",
            "host": "",
            "port": 4403,
            "role": "peer",
        },
    },
    "peers": ["B"],
    "maps": {
        "A2B": "0:0",
        "B2A": "0:0",
        "A2C": "",
        "C2A": "",
    },
    "traffic": {
        "forward_text": True,
        "forward_position": False,
        "forward_telemetry": False,
        "require_ack": False,
        "block_bbs": True,
        "block_bbs_force": True,
    },
    "limits": {
        "rate_limit_per_side": 8,
        "dedup_ttl_sec": 45,
        "peer_suppress_sec": 75,
        "tx_queue_max": 300,
        "retry_schedule_sec": [2, 6, 15],
        "a2b_retries": 3,
        "b2a_retries": 2,
    },
    "tags": {
        "default": "[BRIDGE]",
        "A2B": "[BRIDGE A>B]",
        "B2A": "[BRIDGE B>A]",
        "A2C": "[BRIDGE A>C]",
        "C2A": "[BRIDGE C>A]",
    },
}


# =============================================================================
# Helpers genéricos
# =============================================================================

def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """
    Mezcla dos diccionarios de forma recursiva.

    Uso:
        cfg = _deep_merge(DEFAULT_CONFIG, user_config)

    Parámetros:
        base:
            Diccionario base.
        override:
            Diccionario con valores de usuario.

    Funcionalidad:
        - Conserva claves por defecto no presentes en override.
        - Si ambos valores son dict, mezcla recursivamente.
        - En cualquier otro caso, override reemplaza al valor base.
    """
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _truthy(v: Any, default: bool = False) -> bool:
    """
    Convierte valores frecuentes a booleano.

    Acepta True/False, 1/0, sí/no, yes/no, on/off.
    """
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if not s:
        return default
    return s in {"1", "true", "t", "yes", "y", "on", "si", "sí"}


def _safe_int(v: Any, default: int, min_value: Optional[int] = None, max_value: Optional[int] = None) -> int:
    """
    Convierte a entero con límites opcionales.

    Devuelve default si no puede convertir.
    """
    try:
        n = int(v)
    except Exception:
        n = int(default)
    if min_value is not None:
        n = max(int(min_value), n)
    if max_value is not None:
        n = min(int(max_value), n)
    return n


def _norm_profile(v: Any) -> str:
    """Normaliza el nombre del perfil."""
    return str(v or "embedded_b").strip().lower()


def _norm_peer(v: Any) -> str:
    """Normaliza un peer: B/C."""
    return str(v or "").strip().upper()


def _norm_host(v: Any) -> str:
    """Normaliza host/IP sin validar DNS ni abrir red."""
    return str(v or "").strip()


def _norm_map(v: Any) -> str:
    """Normaliza un mapa de canales eliminando espacios innecesarios."""
    s = str(v or "").strip()
    if not s:
        return ""
    parts: List[str] = []
    for item in s.split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        a, b = item.split(":", 1)
        try:
            parts.append(f"{int(a.strip())}:{int(b.strip())}")
        except Exception:
            # Si no es convertible, devolvemos el original para que validate lo marque.
            return s
    return ",".join(parts)


def _map_to_env(value: str) -> str:
    """Devuelve un mapa listo para variables de entorno."""
    return _norm_map(value)


def _retry_schedule_to_env(values: Iterable[Any]) -> str:
    """
    Convierte una lista de segundos de reintento a CSV.

    Ejemplo:
        [2, 6, 15] -> "2,6,15"
    """
    out: List[str] = []
    for item in values or []:
        try:
            n = int(item)
            if n > 0:
                out.append(str(n))
        except Exception:
            continue
    return ",".join(out) if out else "2,6,15"


def _env_bool(v: bool) -> str:
    """Convierte bool a 1/0 para .env."""
    return "1" if bool(v) else "0"


def _atomic_write_text(path: str | Path, text: str, encoding: str = "utf-8") -> None:
    """
    Escribe texto de forma atómica.

    Uso:
        _atomic_write_text("bot_data/bridge_config.json", json_text)

    Funcionalidad:
        - Escribe primero en un fichero temporal en el mismo directorio.
        - Reemplaza el destino con os.replace().
        - Reduce riesgo de fichero corrupto si se interrumpe el proceso.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        os.replace(tmp_name, p)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except Exception:
            pass


# =============================================================================
# Carga / guardado
# =============================================================================

def default_bridge_config() -> Dict[str, Any]:
    """
    Devuelve una copia profunda de la configuración por defecto.

    Uso:
        cfg = default_bridge_config()
    """
    return copy.deepcopy(DEFAULT_CONFIG)


def normalize_bridge_config(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Aplica defaults y normaliza tipos básicos.

    Uso:
        cfg = normalize_bridge_config(json.load(f))

    Parámetros:
        raw:
            Diccionario leído desde bridge_config.json. Si es None o inválido,
            se parte del DEFAULT_CONFIG.

    Funcionalidad:
        - Rellena claves ausentes.
        - Normaliza profile, peers, mapas, puertos, booleanos y límites.
        - No valida semántica avanzada. Eso lo hace validate_bridge_config().
    """
    if not isinstance(raw, dict):
        raw = {}

    cfg = _deep_merge(DEFAULT_CONFIG, raw)

    cfg["enabled"] = _truthy(cfg.get("enabled"), True)
    cfg["profile"] = _norm_profile(cfg.get("profile"))

    hub = cfg.setdefault("hub", {})
    hub["mode"] = str(hub.get("mode") or "broker").strip().lower()

    nodes = cfg.setdefault("nodes", {})
    for key in _VALID_NODE_KEYS:
        node = nodes.setdefault(key, {})
        node["name"] = str(node.get("name") or f"Nodo {key}").strip()
        node["host"] = _norm_host(node.get("host"))
        node["port"] = _safe_int(node.get("port"), 4403, min_value=1, max_value=65535)
        node["role"] = str(node.get("role") or ("hub" if key == "A" else "peer")).strip().lower()

    peers_raw = cfg.get("peers")
    if not isinstance(peers_raw, list):
        peers_raw = []
    peers: List[str] = []
    for p in peers_raw:
        np = _norm_peer(p)
        if np and np not in peers:
            peers.append(np)
    cfg["peers"] = peers

    maps = cfg.setdefault("maps", {})
    for key in ("A2B", "B2A", "A2C", "C2A"):
        maps[key] = _norm_map(maps.get(key, ""))

    traffic = cfg.setdefault("traffic", {})
    traffic["forward_text"] = _truthy(traffic.get("forward_text"), True)
    traffic["forward_position"] = _truthy(traffic.get("forward_position"), False)
    traffic["forward_telemetry"] = _truthy(traffic.get("forward_telemetry"), False)
    traffic["require_ack"] = _truthy(traffic.get("require_ack"), False)
    traffic["block_bbs"] = _truthy(traffic.get("block_bbs"), True)
    traffic["block_bbs_force"] = _truthy(traffic.get("block_bbs_force"), True)

    limits = cfg.setdefault("limits", {})
    limits["rate_limit_per_side"] = _safe_int(limits.get("rate_limit_per_side"), 8, min_value=1, max_value=120)
    limits["dedup_ttl_sec"] = _safe_int(limits.get("dedup_ttl_sec"), 45, min_value=5, max_value=3600)
    limits["peer_suppress_sec"] = _safe_int(limits.get("peer_suppress_sec"), 75, min_value=5, max_value=3600)
    limits["tx_queue_max"] = _safe_int(limits.get("tx_queue_max"), 300, min_value=10, max_value=10000)
    limits["a2b_retries"] = _safe_int(limits.get("a2b_retries"), 3, min_value=0, max_value=20)
    limits["b2a_retries"] = _safe_int(limits.get("b2a_retries"), 2, min_value=0, max_value=20)

    rs = limits.get("retry_schedule_sec")
    if not isinstance(rs, list):
        rs = [2, 6, 15]
    fixed_rs: List[int] = []
    for item in rs:
        try:
            n = int(item)
            if n > 0:
                fixed_rs.append(n)
        except Exception:
            continue
    limits["retry_schedule_sec"] = fixed_rs or [2, 6, 15]

    tags = cfg.setdefault("tags", {})
    tags["default"] = str(tags.get("default") or "[BRIDGE]").strip()
    for key in ("A2B", "B2A", "A2C", "C2A"):
        if tags.get(key) is None:
            tags[key] = ""
        tags[key] = str(tags.get(key) or "").strip()

    return cfg


def load_bridge_config(path: str | Path = DEFAULT_CONFIG_PATH, *, allow_missing: bool = True) -> Dict[str, Any]:
    """
    Carga y normaliza bridge_config.json.

    Uso:
        cfg = load_bridge_config()
        cfg = load_bridge_config("/ruta/bridge_config.json")

    Parámetros:
        path:
            Ruta del JSON de configuración.
        allow_missing:
            Si True y el fichero no existe, devuelve configuración por defecto.
            Si False y no existe, lanza FileNotFoundError.

    Funcionalidad:
        - Lee JSON UTF-8.
        - Aplica defaults.
        - No abre sockets ni modifica el sistema.
    """
    p = Path(path)
    if not p.exists():
        if allow_missing:
            return normalize_bridge_config({})
        raise FileNotFoundError(str(p))

    with p.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    return normalize_bridge_config(raw)


def save_bridge_config(config: Dict[str, Any], path: str | Path = DEFAULT_CONFIG_PATH) -> None:
    """
    Guarda bridge_config.json de forma atómica.

    Uso:
        save_bridge_config(cfg)

    Parámetros:
        config:
            Configuración a guardar. Se normaliza antes de escribir.
        path:
            Ruta destino.
    """
    cfg = normalize_bridge_config(config)
    text = json.dumps(cfg, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    _atomic_write_text(path, text)


# =============================================================================
# Validación
# =============================================================================

def _validate_map(name: str, value: str, errors: List[str]) -> None:
    """Valida un mapa de canales tipo '0:0,2:1'."""
    if not value:
        return
    if not _MAP_RE.match(value):
        errors.append(f"maps.{name} tiene formato inválido: {value!r}. Usa formato '0:0,2:1'.")
        return

    seen_src: set[int] = set()
    for item in value.split(","):
        a, b = item.split(":", 1)
        src = int(a.strip())
        dst = int(b.strip())
        if src in seen_src:
            errors.append(f"maps.{name} repite el canal origen {src}.")
        seen_src.add(src)
        if src < 0 or dst < 0:
            errors.append(f"maps.{name} contiene canales negativos: {item!r}.")
        if src > 15 or dst > 15:
            errors.append(f"maps.{name} contiene canales > 15: {item!r}.")


def validate_bridge_config(config: Optional[Dict[str, Any]] = None, *, env: Optional[Dict[str, str]] = None) -> BridgeValidationResult:
    """
    Valida una configuración completa de bridge.

    Uso:
        result = validate_bridge_config(cfg)
        if not result.ok:
            print(result.errors)

    Parámetros:
        config:
            Configuración ya cargada o diccionario bruto.
        env:
            Entorno opcional para validar incoherencias con .env actual.
            Si no se pasa, usa os.environ.

    Funcionalidad:
        - Comprueba perfil, modo hub, peers, mapas y nodos.
        - Detecta combinaciones peligrosas como BRIDGE_ENABLED=1 y MESHCORE_ENABLE=1.
        - Genera runtime env si la configuración es coherente.
    """
    cfg = normalize_bridge_config(config or {})
    errors: List[str] = []
    warnings: List[str] = []
    env_src = env if env is not None else os.environ

    enabled = bool(cfg.get("enabled"))
    profile = _norm_profile(cfg.get("profile"))
    hub_mode = str(cfg.get("hub", {}).get("mode") or "broker").strip().lower()
    peers = [_norm_peer(p) for p in cfg.get("peers", [])]
    peer_set = set(peers)

    if profile not in VALID_PROFILES:
        errors.append(f"profile inválido: {profile!r}. Valores válidos: {', '.join(sorted(VALID_PROFILES))}.")

    if hub_mode not in VALID_HUB_MODES:
        errors.append(f"hub.mode inválido: {hub_mode!r}. Valores válidos: broker, tcp.")

    for p in peers:
        if p not in VALID_PEERS:
            errors.append(f"peer inválido en peers: {p!r}. Solo se admite B o C.")

    if not enabled and profile != "off":
        warnings.append("enabled=false pero profile no es 'off'. Se tratará como bridge apagado.")

    if profile == "off" and enabled:
        warnings.append("profile='off' con enabled=true. Se tratará como apagado por prioridad del perfil.")

    allowed_peers = _PROFILE_ALLOWED_PEERS.get(profile, set())
    if enabled and profile != "off" and profile in VALID_PROFILES and peer_set - allowed_peers:
        errors.append(
            f"profile={profile!r} no permite peers {sorted(peer_set - allowed_peers)}. "
            f"Permitidos: {sorted(allowed_peers)}."
        )

    if (not enabled or profile == "off") and peer_set:
        warnings.append("El bridge está apagado; se ignorará la lista peers.")

    if profile != "off" and enabled:
        required_peers = set(allowed_peers)
        # external_ac exige C, external_ab exige B, external_abc exige B y C.
        if profile in {"embedded_b", "meshcore_embedded", "external_ab"} and "B" not in peer_set:
            errors.append(f"profile={profile!r} requiere peers=['B'].")
        if profile == "external_ac" and "C" not in peer_set:
            errors.append("profile='external_ac' requiere peers=['C'].")
        if profile == "external_abc" and not {"B", "C"}.issubset(peer_set):
            errors.append("profile='external_abc' requiere peers=['B','C'].")

    # Regla operativa 24/7: si el broker ya gestiona A, mejor HUB_MODE=broker.
    if profile.startswith("external_") and hub_mode != "broker":
        warnings.append(
            "En producción 24/7 se recomienda hub.mode='broker' para no abrir otro TCP contra el nodo A."
        )

    maps = cfg.get("maps", {})
    for key in ("A2B", "B2A", "A2C", "C2A"):
        _validate_map(key, str(maps.get(key) or ""), errors)

    required_maps = _PROFILE_REQUIRED_MAPS.get(profile, set()) if enabled else set()
    for key in sorted(required_maps):
        if not str(maps.get(key) or "").strip():
            errors.append(f"profile={profile!r} requiere maps.{key} con formato 'origen:destino'.")

    # Hosts requeridos.
    nodes = cfg.get("nodes", {})
    node_a = nodes.get("A", {})
    node_b = nodes.get("B", {})
    node_c = nodes.get("C", {})

    if enabled and profile != "off":
        if profile.startswith("external_") and hub_mode == "tcp" and not _norm_host(node_a.get("host")):
            errors.append("hub.mode='tcp' requiere nodes.A.host.")
        if "B" in peer_set and profile in {"embedded_b", "external_ab", "external_abc"}:
            if not _norm_host(node_b.get("host")):
                errors.append(f"profile={profile!r} requiere nodes.B.host.")
        if "C" in peer_set and profile in {"external_ac", "external_abc"}:
            if not _norm_host(node_c.get("host")):
                errors.append(f"profile={profile!r} requiere nodes.C.host.")

    # Avisos de coherencia con entorno real si se proporciona.
    env_bridge_enabled = _truthy(env_src.get("BRIDGE_ENABLED"), False)
    env_meshcore_enabled = _truthy(env_src.get("MESHCORE_ENABLE"), False)
    if env_bridge_enabled and env_meshcore_enabled:
        warnings.append(
            "El entorno actual tiene BRIDGE_ENABLED=1 y MESHCORE_ENABLE=1. "
            "El broker prioriza BRIDGE_ENABLED, pero es una configuración ambigua."
        )

    traffic = cfg.get("traffic", {})
    if profile.startswith("external_") and not _truthy(traffic.get("block_bbs"), True):
        warnings.append("En bridges externos se recomienda traffic.block_bbs=true para evitar cruzar comandos #BBS.")

    runtime = resolve_bridge_runtime(cfg, assume_valid=True)
    return BridgeValidationResult(
        ok=(len(errors) == 0),
        errors=errors,
        warnings=warnings,
        config=cfg,
        runtime=runtime,
    )


# =============================================================================
# Resolución runtime / exportación
# =============================================================================

def resolve_bridge_runtime(config: Optional[Dict[str, Any]] = None, *, assume_valid: bool = False) -> Dict[str, Any]:
    """
    Convierte bridge_config.json en configuración runtime equivalente.

    Uso:
        runtime = resolve_bridge_runtime(cfg)
        env = runtime["env"]

    Parámetros:
        config:
            Configuración normalizada o bruta.
        assume_valid:
            Si False, valida antes y lanza ValueError si hay errores.
            Si True, solo normaliza y resuelve.

    Funcionalidad:
        Genera variables compatibles con:
        - broker embebido A↔B.
        - MeshCore embebido.
        - triple bridge externo con HUB_MODE=broker/tcp.
    """
    cfg = normalize_bridge_config(config or {})

    if not assume_valid:
        result = validate_bridge_config(cfg)
        if not result.ok:
            raise ValueError("Configuración bridge inválida: " + "; ".join(result.errors))

    enabled = bool(cfg.get("enabled"))
    profile = _norm_profile(cfg.get("profile"))
    if profile == "off":
        enabled = False

    hub_mode = str(cfg.get("hub", {}).get("mode") or "broker").strip().lower()
    nodes = cfg.get("nodes", {})
    maps = cfg.get("maps", {})
    traffic = cfg.get("traffic", {})
    limits = cfg.get("limits", {})
    tags = cfg.get("tags", {})
    peers = [_norm_peer(p) for p in cfg.get("peers", []) if _norm_peer(p) in VALID_PEERS]

    env: Dict[str, str] = {}

    # Variables comunes.
    env["BRIDGE_PROFILE"] = profile
    env["BRIDGE_CONFIG_ENABLED"] = _env_bool(enabled)
    env["HUB_MODE"] = hub_mode
    env["FORWARD_TEXT"] = _env_bool(traffic.get("forward_text", True))
    env["FORWARD_POSITION"] = _env_bool(traffic.get("forward_position", False))
    env["FORWARD_TELEMETRY"] = _env_bool(traffic.get("forward_telemetry", False))
    env["REQUIRE_ACK"] = _env_bool(traffic.get("require_ack", False))
    env["RATE_LIMIT_PER_SIDE"] = str(limits.get("rate_limit_per_side", 8))
    env["DEDUP_TTL"] = str(limits.get("dedup_ttl_sec", 45))
    env["TRIPLE_BLOCK_BBS"] = _env_bool(traffic.get("block_bbs", True))
    env["TRIPLE_BLOCK_BBS_FORCE"] = _env_bool(traffic.get("block_bbs_force", True))

    # Variables compatibles con bridge embebido.
    env["BRIDGE_FORWARD_TEXT"] = env["FORWARD_TEXT"]
    env["BRIDGE_FORWARD_POSITION"] = env["FORWARD_POSITION"]
    env["BRIDGE_REQUIRE_ACK"] = env["REQUIRE_ACK"]
    env["BRIDGE_RATE_LIMIT_PER_SIDE"] = env["RATE_LIMIT_PER_SIDE"]
    env["BRIDGE_DEDUP_TTL"] = env["DEDUP_TTL"]
    env["BRIDGE_PEER_SUPPRESS_SECS"] = str(limits.get("peer_suppress_sec", 75))
    env["BRIDGE_TX_QUEUE_MAX"] = str(limits.get("tx_queue_max", 300))
    env["BRIDGE_RETRY_SCHEDULE"] = _retry_schedule_to_env(limits.get("retry_schedule_sec", [2, 6, 15]))
    env["BRIDGE_RETRIES_A2B"] = str(limits.get("a2b_retries", 3))
    env["BRIDGE_RETRIES_B2A"] = str(limits.get("b2a_retries", 2))

    # Tags.
    env["TAG_BRIDGE"] = str(tags.get("default") or "[BRIDGE]")
    env["TAG_BRIDGE_A2B"] = str(tags.get("A2B") or "")
    env["TAG_BRIDGE_B2A"] = str(tags.get("B2A") or "")
    env["TAG_BRIDGE_A2C"] = str(tags.get("A2C") or "")
    env["TAG_BRIDGE_C2A"] = str(tags.get("C2A") or "")

    # Nodos.
    node_a = nodes.get("A", {})
    node_b = nodes.get("B", {})
    node_c = nodes.get("C", {})

    env["A_HOST"] = _norm_host(node_a.get("host"))
    env["A_PORT"] = str(_safe_int(node_a.get("port"), 4403, 1, 65535))
    env["B_HOST"] = _norm_host(node_b.get("host"))
    env["B_PORT"] = str(_safe_int(node_b.get("port"), 4403, 1, 65535))
    env["C_HOST"] = _norm_host(node_c.get("host"))
    env["C_PORT"] = str(_safe_int(node_c.get("port"), 4403, 1, 65535))

    # Alias compatibles con bridge_in_broker.py, que históricamente usa B_HOST/B_PORT
    # y mapas A2B/B2A.
    env["BRIDGE_B_HOST"] = env["B_HOST"]
    env["BRIDGE_B_PORT"] = env["B_PORT"]

    env["A2B_CH_MAP"] = _map_to_env(str(maps.get("A2B") or ""))
    env["B2A_CH_MAP"] = _map_to_env(str(maps.get("B2A") or ""))
    env["A2C_CH_MAP"] = _map_to_env(str(maps.get("A2C") or ""))
    env["C2A_CH_MAP"] = _map_to_env(str(maps.get("C2A") or ""))

    env["BRIDGE_A2B_CH_MAP"] = env["A2B_CH_MAP"]
    env["BRIDGE_B2A_CH_MAP"] = env["B2A_CH_MAP"]

    # Selección de peers para triple bridge.
    env["BRIDGE_PEERS"] = ",".join(peers)

    # Activación real según perfil.
    if not enabled:
        env["BRIDGE_ENABLED"] = "0"
        env["MESHCORE_ENABLE"] = "0"
        env["BRIDGE_PEERS"] = ""
        bridge_kind = "off"
    elif profile == "embedded_b":
        env["BRIDGE_ENABLED"] = "1"
        env["MESHCORE_ENABLE"] = "0"
        bridge_kind = "broker_embedded_meshtastic_b"
    elif profile == "meshcore_embedded":
        env["BRIDGE_ENABLED"] = "0"
        env["MESHCORE_ENABLE"] = "1"
        bridge_kind = "broker_embedded_meshcore_b"
    elif profile in {"external_ab", "external_ac", "external_abc"}:
        env["BRIDGE_ENABLED"] = "0"
        env["MESHCORE_ENABLE"] = "0"
        bridge_kind = "external_triple_bridge"
    else:
        env["BRIDGE_ENABLED"] = "0"
        env["MESHCORE_ENABLE"] = "0"
        bridge_kind = "unknown"

    return {
        "version": VERSION,
        "enabled": enabled,
        "profile": profile,
        "bridge_kind": bridge_kind,
        "hub_mode": hub_mode,
        "peers": peers,
        "env": env,
    }


def export_runtime_env(config: Optional[Dict[str, Any]] = None, *, shell: bool = False) -> str:
    """
    Exporta la configuración runtime como texto .env o shell.

    Uso:
        text = export_runtime_env(cfg)
        text = export_runtime_env(cfg, shell=True)

    Parámetros:
        shell:
            False -> formato KEY=VALUE.
            True  -> formato export KEY='VALUE'.
    """
    runtime = resolve_bridge_runtime(config or {}, assume_valid=False)
    env = runtime.get("env", {})
    lines: List[str] = [
        f"# bridge_config.py {VERSION}",
        f"# profile={runtime.get('profile')} bridge_kind={runtime.get('bridge_kind')}",
    ]

    for key in sorted(env.keys()):
        value = str(env[key])
        if shell:
            safe = value.replace("'", "'\\''")
            lines.append(f"export {key}='{safe}'")
        else:
            lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


# =============================================================================
# Estado legible
# =============================================================================

def bridge_config_status(config: Optional[Dict[str, Any]] = None, *, env: Optional[Dict[str, str]] = None) -> str:
    """
    Devuelve un resumen legible de la configuración.

    Uso:
        print(bridge_config_status(cfg))
    """
    result = validate_bridge_config(config or {}, env=env)
    cfg = result.config
    runtime = result.runtime
    env_out = runtime.get("env", {})

    lines: List[str] = []
    lines.append(f"Bridge Config {VERSION}")
    lines.append("=" * 72)
    lines.append(f"Estado válido     : {'SI' if result.ok else 'NO'}")
    lines.append(f"Activado          : {'SI' if runtime.get('enabled') else 'NO'}")
    lines.append(f"Perfil            : {runtime.get('profile')}")
    lines.append(f"Tipo runtime      : {runtime.get('bridge_kind')}")
    lines.append(f"Modo hub          : {runtime.get('hub_mode')}")
    lines.append(f"Peers             : {', '.join(runtime.get('peers') or []) or '-'}")
    lines.append("")

    nodes = cfg.get("nodes", {})
    lines.append("Nodos")
    lines.append("-----")
    for key in ("A", "B", "C"):
        node = nodes.get(key, {})
        lines.append(
            f"{key}: host={node.get('host') or '-'} port={node.get('port')} "
            f"role={node.get('role')} name={node.get('name') or '-'}"
        )
    lines.append("")

    maps = cfg.get("maps", {})
    lines.append("Mapas")
    lines.append("-----")
    for key in ("A2B", "B2A", "A2C", "C2A"):
        lines.append(f"{key}: {maps.get(key) or '-'}")
    lines.append("")

    traffic = cfg.get("traffic", {})
    lines.append("Tráfico")
    lines.append("-------")
    lines.append(f"Texto              : {'SI' if traffic.get('forward_text') else 'NO'}")
    lines.append(f"Posición           : {'SI' if traffic.get('forward_position') else 'NO'}")
    lines.append(f"Telemetría         : {'SI' if traffic.get('forward_telemetry') else 'NO'}")
    lines.append(f"ACK requerido      : {'SI' if traffic.get('require_ack') else 'NO'}")
    lines.append(f"Bloquear BBS       : {'SI' if traffic.get('block_bbs') else 'NO'}")
    lines.append(f"Bloqueo BBS force  : {'SI' if traffic.get('block_bbs_force') else 'NO'}")
    lines.append("")

    limits = cfg.get("limits", {})
    lines.append("Límites 24/7")
    lines.append("------------")
    lines.append(f"Rate-limit/sentido : {limits.get('rate_limit_per_side')} msg/min")
    lines.append(f"Dedup TTL          : {limits.get('dedup_ttl_sec')} s")
    lines.append(f"Peer suppress      : {limits.get('peer_suppress_sec')} s")
    lines.append(f"TX queue max       : {limits.get('tx_queue_max')}")
    lines.append(f"Retry schedule     : {limits.get('retry_schedule_sec')}")
    lines.append(f"Retries A2B        : {limits.get('a2b_retries')}")
    lines.append(f"Retries B2A        : {limits.get('b2a_retries')}")
    lines.append("")

    if result.errors:
        lines.append("ERRORES")
        lines.append("-------")
        for err in result.errors:
            lines.append(f"- {err}")
        lines.append("")

    if result.warnings:
        lines.append("AVISOS")
        lines.append("------")
        for warn in result.warnings:
            lines.append(f"- {warn}")
        lines.append("")

    lines.append("Variables clave resueltas")
    lines.append("-------------------------")
    for key in (
        "BRIDGE_ENABLED",
        "MESHCORE_ENABLE",
        "HUB_MODE",
        "BRIDGE_PEERS",
        "A2B_CH_MAP",
        "B2A_CH_MAP",
        "A2C_CH_MAP",
        "C2A_CH_MAP",
        "TRIPLE_BLOCK_BBS",
        "TRIPLE_BLOCK_BBS_FORCE",
    ):
        lines.append(f"{key}={env_out.get(key, '')}")

    return "\n".join(lines)


# =============================================================================
# CLI
# =============================================================================

def _cmd_init(args: argparse.Namespace) -> int:
    """Crea un bridge_config.json inicial."""
    path = Path(args.config)
    if path.exists() and not args.force:
        print(f"ERROR: ya existe {path}. Usa --force para sobrescribir.", file=sys.stderr)
        return 2

    cfg = default_bridge_config()
    save_bridge_config(cfg, path)
    print(f"OK creado {path}")
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    """Valida bridge_config.json."""
    try:
        cfg = load_bridge_config(args.config, allow_missing=not args.strict_missing)
    except Exception as e:
        print(f"ERROR cargando configuración: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    result = validate_bridge_config(cfg)
    if result.ok:
        print("OK bridge_config.json válido")
        if result.warnings:
            print("AVISOS:")
            for w in result.warnings:
                print(f"- {w}")
        return 0

    print("ERROR bridge_config.json inválido")
    for err in result.errors:
        print(f"- {err}")
    if result.warnings:
        print("AVISOS:")
        for w in result.warnings:
            print(f"- {w}")
    return 1


def _cmd_status(args: argparse.Namespace) -> int:
    """Muestra estado legible."""
    try:
        cfg = load_bridge_config(args.config, allow_missing=not args.strict_missing)
    except Exception as e:
        print(f"ERROR cargando configuración: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    print(bridge_config_status(cfg))
    return 0


def _cmd_export_env(args: argparse.Namespace) -> int:
    """Exporta variables runtime."""
    try:
        cfg = load_bridge_config(args.config, allow_missing=not args.strict_missing)
        print(export_runtime_env(cfg, shell=args.shell), end="")
        return 0
    except Exception as e:
        print(f"ERROR exportando variables: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


def _cmd_print_json(args: argparse.Namespace) -> int:
    """Imprime configuración normalizada o runtime en JSON."""
    try:
        cfg = load_bridge_config(args.config, allow_missing=not args.strict_missing)
        if args.runtime:
            obj = validate_bridge_config(cfg).to_dict()
        else:
            obj = cfg
        print(json.dumps(obj, ensure_ascii=False, indent=2))
        return 0
    except Exception as e:
        print(f"ERROR generando JSON: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


def build_arg_parser() -> argparse.ArgumentParser:
    """Construye el parser CLI."""
    parser = argparse.ArgumentParser(
        prog="bridge_config.py",
        description="Validador y exportador de configuración Bridge para MeshNet Bot.",
    )
    parser.add_argument("--version", action="version", version=f"bridge_config.py {VERSION}")

    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--config",
            default=DEFAULT_CONFIG_PATH,
            help=f"Ruta de bridge_config.json. Por defecto: {DEFAULT_CONFIG_PATH}",
        )
        p.add_argument(
            "--strict-missing",
            action="store_true",
            help="Si el fichero no existe, tratarlo como error en vez de usar defaults.",
        )

    p_init = sub.add_parser("init", help="Crear bridge_config.json inicial.")
    add_common(p_init)
    p_init.add_argument("--force", action="store_true", help="Sobrescribir si ya existe.")
    p_init.set_defaults(func=_cmd_init)

    p_val = sub.add_parser("validate", help="Validar configuración.")
    add_common(p_val)
    p_val.set_defaults(func=_cmd_validate)

    p_status = sub.add_parser("status", help="Mostrar resumen legible.")
    add_common(p_status)
    p_status.set_defaults(func=_cmd_status)

    p_exp = sub.add_parser("export-env", help="Exportar variables equivalentes.")
    add_common(p_exp)
    p_exp.add_argument("--shell", action="store_true", help="Usar formato export KEY='VALUE'.")
    p_exp.set_defaults(func=_cmd_export_env)

    p_json = sub.add_parser("print-json", help="Imprimir configuración normalizada o resultado runtime.")
    add_common(p_json)
    p_json.add_argument("--runtime", action="store_true", help="Imprimir validación + runtime.")
    p_json.set_defaults(func=_cmd_print_json)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """
    Entrada CLI.

    Uso:
        raise SystemExit(main())
    """
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
