"""Panel operativo seguro para aplicaciones independientes de MeshNet."""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
import secrets
import unicodedata
import subprocess
import sys
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastapi import FastAPI, HTTPException, Request as FastAPIRequest
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
REPO_DIR = BASE_DIR.parent.parent
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
UNIT_RE = re.compile(r"^[A-Za-z0-9_.@:-]+\.(service|timer)$")
SYSTEMD_OPS = {"status", "start", "stop", "restart", "enable", "disable"}
MAX_OUTPUT = 256 * 1024


@dataclass(frozen=True)
class ActionDefinition:
    id: str
    name: str
    kind: str
    argv: tuple[str, ...] = ()
    unit: str = ""
    operation: str = ""
    timeout: float = 30
    mutating: bool = False
    confirm: bool = False


@dataclass(frozen=True)
class ToolDefinition:
    id: str
    name: str
    description: str
    url: str
    health_path: str = "/health"
    actions: tuple[ActionDefinition, ...] = field(default_factory=tuple)


def _expand(value: str) -> str:
    return value.replace("${PYTHON}", sys.executable).replace("${REPO}", str(REPO_DIR))


def load_tools(root: Path | None = None) -> tuple[ToolDefinition, ...]:
    root = root or Path(os.getenv("CONTROLPANEL_MANIFESTS", str(BASE_DIR / "manifests")))
    tools: list[ToolDefinition] = []
    for path in sorted(root.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        tool_id = str(raw.get("id", ""))
        if not ID_RE.fullmatch(tool_id):
            raise ValueError(f"id de aplicación no válido en {path}")
        actions = []
        for item in raw.get("actions", []):
            action_id, kind = str(item.get("id", "")), str(item.get("kind", "command"))
            if not ID_RE.fullmatch(action_id) or kind not in {"command", "systemd"}:
                raise ValueError(f"acción no válida en {path}: {action_id}")
            argv = tuple(_expand(str(value)) for value in item.get("argv", []))
            unit, operation = str(item.get("unit", "")), str(item.get("operation", ""))
            if kind == "command" and not argv:
                raise ValueError(f"argv vacío en {path}: {action_id}")
            if kind == "systemd" and (not UNIT_RE.fullmatch(unit) or operation not in SYSTEMD_OPS):
                raise ValueError(f"acción systemd no válida en {path}: {action_id}")
            actions.append(ActionDefinition(
                action_id, str(item.get("name", action_id)), kind, argv, unit, operation,
                min(max(float(item.get("timeout", 30)), 1), 300),
                bool(item.get("mutating", operation not in {"", "status"})),
                bool(item.get("confirm", False)),
            ))
        if len({action.id for action in actions}) != len(actions):
            raise ValueError(f"acciones duplicadas en {path}")
        url = str(raw["url"])
        if raw.get("url_env"):
            url = os.getenv(str(raw["url_env"]), url)
        tools.append(ToolDefinition(
            tool_id, str(raw["name"]), str(raw.get("description", "")),
            os.path.expandvars(url), str(raw.get("health_path", "/health")),
            tuple(actions),
        ))
    if len({tool.id for tool in tools}) != len(tools):
        raise ValueError("identificadores de aplicación duplicados")
    return tuple(tools)


DEFAULT_TOOLS = load_tools()


class ToolRegistry:
    def __init__(self, path: Path, tools: tuple[ToolDefinition, ...] = DEFAULT_TOOLS):
        self.path, self.tools, self._lock = path, {t.id: t for t in tools}, threading.Lock()
        self._enabled = self._load()

    def _load(self) -> dict[str, bool]:
        try:
            values = json.loads(self.path.read_text(encoding="utf-8")).get("enabled", {})
            if not isinstance(values, dict):
                raise ValueError
            return {key: bool(values.get(key, False)) for key in self.tools}
        except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
            return {key: False for key in self.tools}

    def set_enabled(self, tool_id: str, enabled: bool) -> None:
        self.get(tool_id)
        with self._lock:
            self._enabled[tool_id] = enabled
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"enabled": self._enabled}, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self.path)

    def items(self) -> list[dict[str, Any]]:
        result = []
        for tool in self.tools.values():
            item = asdict(tool)
            item["enabled"] = self._enabled[tool.id]
            for action in item["actions"]:
                for secret in ("argv", "unit", "operation", "timeout"):
                    action.pop(secret, None)
            result.append(item)
        return result

    def get(self, tool_id: str) -> ToolDefinition:
        if tool_id not in self.tools:
            raise KeyError(tool_id)
        return self.tools[tool_id]

    def action(self, tool_id: str, action_id: str) -> ActionDefinition:
        for action in self.get(tool_id).actions:
            if action.id == action_id:
                return action
        raise KeyError(action_id)

    def enabled(self, tool_id: str) -> bool:
        return self._enabled.get(tool_id, False)


class EnabledPayload(BaseModel):
    enabled: bool


class ActionPayload(BaseModel):
    confirmed: bool = False


class EmergencyFiltersPayload(BaseModel):
    severities: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    rules: dict[str, list[str]] | None = None


class EmergencyRadiusPayload(BaseModel):
    enabled: bool = False
    latitude: float = 41.6488
    longitude: float = -0.8891
    radius_km: float = 150


class EmergencyCollectionPayload(BaseModel):
    sources: list[str]
    provinces: list[str]
    categories: list[str]
    firms_map_key: str = ""
    radius: EmergencyRadiusPayload = Field(default_factory=EmergencyRadiusPayload)


class CommunicationChannelsPayload(BaseModel):
    transport: str
    meshcore_channel: int
    meshtastic_channel: int

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump() if hasattr(self, "model_dump") else self.dict()


class RouteChannelsPayload(BaseModel):
    meshcore_channel: int
    meshtastic_channel: int

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump() if hasattr(self, "model_dump") else self.dict()


class TransportPayload(BaseModel):
    transport: str


class AutoReplyPayload(BaseModel):
    enabled: bool = False
    template: str = "Recibido, {message}"
    meshcore_channels: list[int] = Field(default_factory=list)
    meshtastic_channels: list[int] = Field(default_factory=list)


FARMACIAS_ENV_FILE = Path(
    os.getenv("CONTROLPANEL_FARMACIAS_ENV", str(REPO_DIR / "tools/farmacias_guardia/.env"))
)
AUTO_REPLY_CONFIG_FILE = Path(os.getenv(
    "CONTROLPANEL_AUTO_REPLY_CONFIG", str(REPO_DIR / "bot_data/auto_reply.json")
))
CHANNEL_KEYS = {
    "RADIO_PROFILE",
    "FARMACIAS_BROADCAST_TRANSPORT",
    "FARMACIAS_MIXED_PROFILE_BROADCAST",
    "FARMACIAS_MESHCORE_CHANNEL",
    "FARMACIAS_MESHTASTIC_CHANNEL",
}
EMERGENCIAS_CONFIG_FILE = Path(os.getenv(
    "CONTROLPANEL_EMERGENCIAS_CONFIG",
    str(REPO_DIR / "tools/emergencias_guardia/data/config.json"),
))
EMERGENCIAS_ENV_FILE = Path(os.getenv(
    "CONTROLPANEL_EMERGENCIAS_ENV",
    str(REPO_DIR / "tools/emergencias_guardia/.env"),
))
EMERGENCY_SOURCE_IDS = {
    "municipal_json", "dgt_datex", "ign_earthquakes", "nasa_firms",
}
EMERGENCY_CATEGORIES = {
    "wildfire", "urban_fire", "industrial_fire", "traffic_collision",
    "road_closed", "lane_closed", "traffic_obstruction", "flood", "storm",
    "snow", "strong_wind", "extreme_temperature", "chemical", "power_outage",
    "water_outage", "gas_outage", "public_safety", "civil_protection",
    "earthquake", "tsunami", "volcanic", "landslide", "other",
}
SPANISH_PROVINCES = (
    "A Coruña", "Álava", "Albacete", "Alicante", "Almería", "Asturias", "Ávila",
    "Badajoz", "Barcelona", "Bizkaia", "Burgos", "Cáceres", "Cádiz", "Cantabria",
    "Castellón", "Ceuta", "Ciudad Real", "Córdoba", "Cuenca", "Gipuzkoa", "Girona",
    "Granada", "Guadalajara", "Huelva", "Huesca", "Illes Balears", "Jaén", "La Rioja",
    "Las Palmas", "León", "Lleida", "Lugo", "Madrid", "Málaga", "Melilla", "Murcia",
    "Navarra", "Ourense", "Palencia", "Pontevedra", "Salamanca", "Santa Cruz de Tenerife",
    "Segovia", "Sevilla", "Soria", "Tarragona", "Teruel", "Toledo", "Valencia",
    "Valladolid", "Zamora", "Zaragoza",
)


def read_env_values(path: Path, keys: set[str]) -> dict[str, str]:
    """Lee únicamente claves públicas permitidas sin exponer el resto del .env."""
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in keys:
            values[key] = value.strip().strip('"').strip("'")
    return values


def update_env_values(path: Path, updates: dict[str, str]) -> None:
    """Actualiza atómicamente claves permitidas conservando comentarios y secretos."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    output, replaced = [], set()
    for line in lines:
        match = re.match(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*)=", line)
        if match and match.group(2) in updates:
            key = match.group(2)
            output.append(f"{key}={updates[key]}")
            replaced.add(key)
        else:
            output.append(line)
    if output and output[-1]:
        output.append("")
    output.extend(f"{key}={updates[key]}" for key in updates if key not in replaced)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing_mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        existing_mode = 0o600
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            if hasattr(os, "fchmod"):
                os.fchmod(handle.fileno(), existing_mode)
            else:
                os.chmod(temporary, existing_mode)
            handle.write("\n".join(output) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _emergency_config() -> dict[str, Any]:
    from tools.emergencias_guardia.emergencias.config import DEFAULT_CONFIG, _merge
    try:
        supplied = json.loads(EMERGENCIAS_CONFIG_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        supplied = {}
    except (json.JSONDecodeError, OSError) as exc:
        raise HTTPException(status_code=500, detail="Configuración de emergencias no válida") from exc
    return _merge(DEFAULT_CONFIG, supplied)


def _save_emergency_config(config: dict[str, Any]) -> None:
    from tools.emergencias_guardia.emergencias.config import atomic_write_json
    try:
        atomic_write_json(EMERGENCIAS_CONFIG_FILE, config)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"No se pudo guardar la configuración: {exc}") from exc


def _province_id(name: str) -> str:
    folded = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(char for char in folded if not unicodedata.combining(char))
    return "panel-province-" + re.sub(r"[^a-z0-9]+", "-", ascii_name.casefold()).strip("-")


def probe(tool: ToolDefinition, timeout: float = 2) -> dict[str, Any]:
    request = Request(tool.url.rstrip("/") + tool.health_path, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(64 * 1024)
            return {"reachable": True, "status": response.status, "details": json.loads(raw) if raw else {}}
    except HTTPError as exc:
        return {"reachable": False, "status": exc.code, "error": "respuesta HTTP no válida"}
    except (URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        return {"reachable": False, "status": None, "error": str(exc)}


def execute_action(action: ActionDefinition) -> dict[str, Any]:
    argv = list(action.argv)
    if action.kind == "systemd":
        argv = ["systemctl", action.operation, action.unit]
    try:
        done = subprocess.run(
            argv, cwd=REPO_DIR, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=action.timeout, check=False, stdin=subprocess.DEVNULL,
        )
        stdout, stderr = done.stdout[:MAX_OUTPUT], done.stderr[:MAX_OUTPUT]
        try:
            data = json.loads(stdout) if stdout.strip() else None
        except json.JSONDecodeError:
            data = None
        missing_unit = action.kind == "systemd" and (
            "could not be found" in stderr.casefold() or "not-found" in stdout.casefold()
        )
        if missing_unit:
            data = {
                "instalado": False,
                "unidad": action.unit,
                "explicación": (
                    "El recolector puede seguir enviando avisos mediante el temporizador; "
                    "esta unidad separada solo mantiene la API de consultas disponible."
                    if action.unit == "meshnet-emergencias-api.service" else
                    "La unidad systemd no está instalada en este equipo."
                ),
                "solución": (
                    "Instale systemd/meshnet-emergencias-api.service y ejecute systemctl daemon-reload."
                    if action.unit == "meshnet-emergencias-api.service" else
                    "Instale la unidad y ejecute systemctl daemon-reload."
                ),
            }
        return {"ok": done.returncode == 0, "returncode": done.returncode, "stdout": stdout,
                "stderr": stderr, "data": data,
                "truncated": len(done.stdout) > MAX_OUTPUT or len(done.stderr) > MAX_OUTPUT}
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "returncode": None, "stdout": str(exc.stdout or "")[:MAX_OUTPUT],
                "stderr": "Tiempo de espera agotado", "data": None}
    except OSError as exc:
        return {"ok": False, "returncode": None, "stdout": "", "stderr": str(exc), "data": None}


def create_app(registry: ToolRegistry | None = None, auth_token: str | None = None) -> FastAPI:
    registry = registry or ToolRegistry(Path(os.getenv("CONTROLPANEL_STATE", str(BASE_DIR / "data/state.json"))))
    app = FastAPI(title="MeshNet Control", version="1.0.0")
    configured_token = auth_token if auth_token is not None else os.getenv("CONTROLPANEL_TOKEN", "")

    @app.middleware("http")
    async def require_authentication(request: FastAPIRequest, call_next):
        if not configured_token or request.url.path == "/health":
            return await call_next(request)
        authorization = request.headers.get("authorization", "")
        try:
            scheme, encoded = authorization.split(" ", 1)
            decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
            username, supplied_token = decoded.split(":", 1)
        except (ValueError, UnicodeDecodeError, binascii.Error):
            username, supplied_token, scheme = "", "", ""
        authenticated = (
            scheme.casefold() == "basic"
            and secrets.compare_digest(username, "admin")
            and secrets.compare_digest(supplied_token, configured_token)
        )
        if not authenticated:
            return JSONResponse(
                {"detail": "Autenticación requerida"}, status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="MeshNet ControlPanel", charset="UTF-8"'},
            )
        return await call_next(request)

    def require_enabled(tool_id: str) -> None:
        if not registry.enabled(tool_id):
            raise HTTPException(status_code=409, detail="Habilite la aplicación antes de configurarla")

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> HTMLResponse:
        return HTMLResponse(DASHBOARD, headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        })

    @app.get("/api/tools")
    def list_tools() -> dict[str, Any]:
        return {"tools": registry.items()}

    @app.get("/health")
    def control_panel_health() -> dict[str, Any]:
        tools = registry.items()
        return {
            "ok": True,
            "service": "meshnet-control-panel",
            "version": app.version,
            "authentication": False,
            "tools": {
                "registered": len(tools),
                "enabled": sum(bool(tool["enabled"]) for tool in tools),
            },
        }

    @app.put("/api/tools/{tool_id}/enabled")
    def set_enabled(tool_id: str, payload: EnabledPayload) -> dict[str, Any]:
        try:
            registry.set_enabled(tool_id, payload.enabled)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Aplicación desconocida") from exc
        return {"id": tool_id, "enabled": payload.enabled}

    @app.get("/api/auto-reply")
    def get_auto_reply() -> dict[str, Any]:
        try:
            data = json.loads(AUTO_REPLY_CONFIG_FILE.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            data = {}
        return {
            "enabled": bool(data.get("enabled", False)),
            "template": str(data.get("template") or "Recibido, {message}"),
            "meshcore_channels": data.get("meshcore", {}).get("channels", []),
            "meshtastic_channels": data.get("meshtastic", {}).get("channels", []),
        }

    @app.put("/api/auto-reply")
    def set_auto_reply(payload: AutoReplyPayload) -> dict[str, Any]:
        template = " ".join(payload.template.split()).strip()
        channels = payload.meshcore_channels + payload.meshtastic_channels
        if not template or len(template) > 300:
            raise HTTPException(status_code=422, detail="El texto debe tener entre 1 y 300 caracteres")
        if template.count("{message}") != 1:
            raise HTTPException(status_code=422, detail="El texto debe incluir {message} exactamente una vez")
        if any(channel < 0 or channel > 255 for channel in channels):
            raise HTTPException(status_code=422, detail="Los canales deben estar entre 0 y 255")
        data = {
            "enabled": payload.enabled,
            "template": template,
            "meshcore": {"channels": sorted(set(payload.meshcore_channels))},
            "meshtastic": {"channels": sorted(set(payload.meshtastic_channels))},
        }
        AUTO_REPLY_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = AUTO_REPLY_CONFIG_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(AUTO_REPLY_CONFIG_FILE)
        return {"ok": True, **data}

    @app.get("/api/tools/{tool_id}/health")
    def tool_health(tool_id: str) -> dict[str, Any]:
        try:
            tool = registry.get(tool_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Aplicación desconocida") from exc
        if not registry.enabled(tool_id):
            raise HTTPException(status_code=409, detail="Habilite la aplicación antes de consultarla")
        return {"id": tool_id, **probe(tool)}

    @app.post("/api/tools/{tool_id}/actions/{action_id}")
    def run_action(tool_id: str, action_id: str, payload: ActionPayload) -> dict[str, Any]:
        if not registry.enabled(tool_id):
            raise HTTPException(status_code=409, detail="Habilite la aplicación antes de operarla")
        try:
            action = registry.action(tool_id, action_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Aplicación o acción desconocida") from exc
        if action.confirm and not payload.confirmed:
            raise HTTPException(status_code=409, detail="La acción requiere confirmación")
        return {"id": tool_id, "action": action_id, **execute_action(action)}

    def emergency_filter_action(*arguments: str) -> dict[str, Any]:
        action = ActionDefinition(
            "emergency_filters", "Filtros de propagación", "command",
            (sys.executable, str(REPO_DIR / "tools/emergencias_guardia/emergencias_guardia.py"), *arguments),
        )
        return execute_action(action)

    def validate_channels(payload: CommunicationChannelsPayload, transports: set[str]) -> None:
        if payload.transport not in transports:
            raise HTTPException(status_code=422, detail="Transporte no válido")
        if not all(-1 <= channel <= 255 for channel in (
            payload.meshcore_channel, payload.meshtastic_channel,
        )):
            raise HTTPException(status_code=422, detail="El canal debe estar entre -1 y 255")

    @app.get("/api/emergencias/collection")
    def get_emergency_collection() -> dict[str, Any]:
        require_enabled("emergencias_guardia")
        config = _emergency_config()
        areas = config.get("areas", [])
        radius = next((area for area in areas if area.get("id") == "panel-radius"), None)
        configured_provinces = {
            area.get("name") for area in areas
            if area.get("type") == "province" and area.get("enabled", True)
        }
        key_configured = bool(read_env_values(EMERGENCIAS_ENV_FILE, {"FIRMS_MAP_KEY"}).get("FIRMS_MAP_KEY"))
        return {
            "sources": [{"id": source_id, "enabled": bool(config["sources"].get(source_id, {}).get("enabled"))}
                        for source_id in sorted(EMERGENCY_SOURCE_IDS)],
            "provinces": [{"name": name, "enabled": name in configured_provinces}
                          for name in SPANISH_PROVINCES],
            "categories": [{"name": name, "enabled": name in set(config["filters"].get("categories", []))}
                           for name in sorted(EMERGENCY_CATEGORIES)],
            "radius": {
                "enabled": radius is not None,
                "latitude": float((radius or {}).get("latitude", 41.6488)),
                "longitude": float((radius or {}).get("longitude", -0.8891)),
                "radius_km": float((radius or {}).get("radius_km", 150)),
            },
            "firms_key_configured": key_configured,
        }

    @app.get("/api/emergencias/overview")
    def get_emergency_overview() -> dict[str, Any]:
        require_enabled("emergencias_guardia")
        config = _emergency_config()
        try:
            state = json.loads((EMERGENCIAS_CONFIG_FILE.parent / "state.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            state = {}
        source_states = state.get("sources", {}) if isinstance(state.get("sources", {}), dict) else {}
        enabled = [source_id for source_id, source in config.get("sources", {}).items()
                   if source.get("enabled")]
        successes = [item.get("last_success") for item in source_states.values()
                     if isinstance(item, dict) and item.get("last_success")]
        source_items = []
        for source_id in enabled:
            item = source_states.get(source_id, {})
            source_items.append({"id": source_id, **(item if isinstance(item, dict) else {})})
        incremental = state.get("notifications", {}).get("incremental", {})
        api_health = probe(registry.get("emergencias_guardia"))
        return {
            "api": {"ok": bool(api_health.get("reachable")), "detail": api_health.get("error", "Operativa")},
            "collector": {
                "ok": any(
                    isinstance(source_states.get(source_id), dict)
                    and source_states[source_id].get("ok")
                    for source_id in enabled
                ),
                "last_success": max(successes) if successes else None,
            },
            "sources": {"enabled": len(enabled), "total": len(config.get("sources", {})),
                        "items": source_items},
            "notifications": {"enabled": bool(config.get("notifications", {}).get("enabled")),
                              "pending": len(incremental.get("pending", []))},
            "coverage": {"areas": len([area for area in config.get("areas", []) if area.get("enabled", True)])},
        }

    @app.put("/api/emergencias/collection")
    def set_emergency_collection(payload: EmergencyCollectionPayload) -> dict[str, Any]:
        require_enabled("emergencias_guardia")
        sources, provinces, categories = set(payload.sources), set(payload.provinces), set(payload.categories)
        if not sources.issubset(EMERGENCY_SOURCE_IDS):
            raise HTTPException(status_code=422, detail="Fuente de emergencias no válida")
        if not provinces.issubset(SPANISH_PROVINCES):
            raise HTTPException(status_code=422, detail="Provincia no válida")
        if not categories.issubset(EMERGENCY_CATEGORIES):
            raise HTTPException(status_code=422, detail="Categoría de recogida no válida")
        if sources & {"dgt_datex"} and not provinces and not payload.radius.enabled:
            raise HTTPException(status_code=422, detail="DGT requiere al menos una provincia o un radio")
        if sources & {"ign_earthquakes", "nasa_firms"} and not payload.radius.enabled:
            raise HTTPException(status_code=422, detail="IGN y FIRMS requieren un radio porque sus registros usan coordenadas")
        if payload.radius.enabled and (
            not -90 <= payload.radius.latitude <= 90
            or not -180 <= payload.radius.longitude <= 180
            or not 0 < payload.radius.radius_km <= 1000
        ):
            raise HTTPException(status_code=422, detail="Radio geográfico no válido")
        key = payload.firms_map_key.strip()
        if "nasa_firms" in sources and not key and not read_env_values(
            EMERGENCIAS_ENV_FILE, {"FIRMS_MAP_KEY"}
        ).get("FIRMS_MAP_KEY"):
            raise HTTPException(status_code=422, detail="NASA FIRMS requiere una MAP_KEY")

        config = _emergency_config()
        for source_id in EMERGENCY_SOURCE_IDS:
            config["sources"][source_id]["enabled"] = source_id in sources
        config["filters"]["categories"] = sorted(categories)
        preserved = [area for area in config.get("areas", []) if area.get("type") != "province"
                     and area.get("id") != "panel-radius"]
        province_areas = [{"id": _province_id(name), "type": "province", "name": name, "enabled": True}
                          for name in SPANISH_PROVINCES if name in provinces]
        if payload.radius.enabled:
            preserved.append({
                "id": "panel-radius", "type": "radius", "name": "Radio del ControlPanel",
                "latitude": payload.radius.latitude, "longitude": payload.radius.longitude,
                "radius_km": payload.radius.radius_km, "enabled": True,
            })
        config["areas"] = preserved + province_areas
        if key:
            try:
                update_env_values(EMERGENCIAS_ENV_FILE, {"FIRMS_MAP_KEY": key})
            except OSError as exc:
                raise HTTPException(status_code=500, detail=f"No se pudo guardar la MAP_KEY: {exc}") from exc
        _save_emergency_config(config)
        return {"ok": True, "sources": sorted(sources), "provinces": sorted(provinces),
                "categories": sorted(categories), "restart_required": False}

    @app.get("/api/emergencias/filters")
    def get_emergency_filters() -> dict[str, Any]:
        require_enabled("emergencias_guardia")
        result = emergency_filter_action("filters", "show")
        if not result["ok"]:
            raise HTTPException(status_code=502, detail=result["stderr"] or "No se pudieron leer los filtros")
        return result["data"] or {}

    @app.put("/api/emergencias/filters")
    def set_emergency_filters(payload: EmergencyFiltersPayload) -> dict[str, Any]:
        require_enabled("emergencias_guardia")
        severities = {"low", "medium", "high", "critical"}
        categories = {
            "wildfire", "urban_fire", "industrial_fire", "traffic_collision",
            "road_closed", "lane_closed", "traffic_obstruction", "flood", "storm",
            "snow", "strong_wind", "extreme_temperature", "chemical", "power_outage",
            "water_outage", "gas_outage", "public_safety", "civil_protection",
            "earthquake", "tsunami", "volcanic", "landslide", "other",
        }
        if payload.rules is not None:
            if set(payload.rules) - severities or any(
                not set(values).issubset(categories) for values in payload.rules.values()
            ):
                raise HTTPException(status_code=422, detail="Matriz de propagación no válida")
            rules = {severity: sorted(set(payload.rules.get(severity, [])))
                     for severity in ("low", "medium", "high", "critical")}
            result = emergency_filter_action("filters", "set", "--rules-json", json.dumps(rules))
            if not result["ok"]:
                raise HTTPException(status_code=502, detail=result["stderr"] or result["stdout"])
            return result["data"] or {}
        selected = set(payload.categories)
        selected_severities = set(payload.severities)
        if not selected_severities.issubset(severities) or not selected.issubset(categories):
            raise HTTPException(status_code=422, detail="Filtro de emergencias no válido")
        result = emergency_filter_action(
            "filters", "set", "--severities", ",".join(
                severity for severity in ("low", "medium", "high", "critical")
                if severity in selected_severities
            ),
            "--categories", ",".join(sorted(selected)),
        )
        if not result["ok"]:
            raise HTTPException(status_code=502, detail=result["stderr"] or result["stdout"])
        return result["data"] or {}

    @app.get("/api/emergencias/channels")
    def get_emergency_channels() -> dict[str, Any]:
        require_enabled("emergencias_guardia")
        result = emergency_filter_action("notify", "status")
        if not result["ok"]:
            raise HTTPException(status_code=502, detail=result["stderr"] or "No se pudieron leer los canales")
        data = result["data"] or {}
        routes = data.get("routes", {})
        return {
            "transport": data.get("transport", "meshcore"),
            "enabled": bool(data.get("enabled", False)),
            "routes": {
                route: {
                    "meshcore_channel": int(routes.get(route, {}).get("meshcore_channel", -1)),
                    "meshtastic_channel": int(routes.get(route, {}).get("meshtastic_channel", -1)),
                }
                for route in ("emergencias", "servicios", "meteo")
            },
        }

    @app.put("/api/emergencias/channels/{route}")
    def set_emergency_channels(route: str, payload: RouteChannelsPayload) -> dict[str, Any]:
        require_enabled("emergencias_guardia")
        if route not in {"emergencias", "servicios", "meteo"}:
            raise HTTPException(status_code=404, detail="Ruta de emergencias desconocida")
        if not all(-1 <= channel <= 255 for channel in (
            payload.meshcore_channel, payload.meshtastic_channel,
        )):
            raise HTTPException(status_code=422, detail="El canal debe estar entre -1 y 255")
        commands = (
            ("notify", "set-channel", route, "meshcore", str(payload.meshcore_channel)),
            ("notify", "set-channel", route, "meshtastic", str(payload.meshtastic_channel)),
        )
        for arguments in commands:
            result = emergency_filter_action(*arguments)
            if not result["ok"]:
                raise HTTPException(status_code=502, detail=result["stderr"] or result["stdout"])
        return {"ok": True, "route": route, **payload.as_dict()}

    @app.put("/api/emergencias/transport")
    def set_emergency_transport(payload: TransportPayload) -> dict[str, Any]:
        require_enabled("emergencias_guardia")
        if payload.transport not in {"meshcore", "meshtastic"}:
            raise HTTPException(status_code=422, detail="Transporte no válido")
        result = emergency_filter_action("notify", "set-transport", payload.transport)
        if not result["ok"]:
            raise HTTPException(status_code=502, detail=result["stderr"] or result["stdout"])
        return {"ok": True, "transport": payload.transport}

    @app.get("/api/farmacias/channels")
    def get_pharmacy_channels() -> dict[str, Any]:
        require_enabled("farmacias_guardia")
        values = read_env_values(FARMACIAS_ENV_FILE, CHANNEL_KEYS)
        profile = values.get("RADIO_PROFILE", "").strip().lower()
        transport = values.get("FARMACIAS_BROADCAST_TRANSPORT", "auto").strip().lower()
        effective_transport = transport
        if transport == "auto":
            if profile == "meshcore_only":
                effective_transport = "meshcore"
            elif profile == "meshtastic_a_meshcore_embedded_b":
                effective_transport = "meshtastic"
            else:
                effective_transport = values.get("FARMACIAS_MIXED_PROFILE_BROADCAST", "meshcore")
        elif profile == "meshcore_only" and transport == "meshtastic":
            effective_transport = "meshcore"
        return {
            "transport": transport,
            "effective_transport": effective_transport,
            "radio_profile": profile or None,
            "meshcore_channel": int(values.get("FARMACIAS_MESHCORE_CHANNEL", "-1")),
            "meshtastic_channel": int(values.get("FARMACIAS_MESHTASTIC_CHANNEL", "-1")),
        }

    @app.put("/api/farmacias/channels")
    def set_pharmacy_channels(payload: CommunicationChannelsPayload) -> dict[str, Any]:
        require_enabled("farmacias_guardia")
        validate_channels(payload, {"auto", "meshcore", "meshtastic"})
        values = read_env_values(FARMACIAS_ENV_FILE, CHANNEL_KEYS)
        profile = values.get("RADIO_PROFILE", "").strip().lower()
        if profile == "meshcore_only" and payload.transport == "meshtastic":
            raise HTTPException(
                status_code=422,
                detail="El perfil meshcore_only no permite publicar por Meshtastic; use Automático o MeshCore",
            )
        update_env_values(FARMACIAS_ENV_FILE, {
            "FARMACIAS_BROADCAST_TRANSPORT": payload.transport,
            "FARMACIAS_MESHCORE_CHANNEL": str(payload.meshcore_channel),
            "FARMACIAS_MESHTASTIC_CHANNEL": str(payload.meshtastic_channel),
        })
        return {"ok": True, **payload.as_dict(), "restart_required": True}

    return app


DASHBOARD = """<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>MeshNet Control</title>
<style>
:root{color-scheme:dark;--bg:#07111f;--card:#102238;--line:#29445e;--accent:#32d6a0;--accent2:#53a8ff;--muted:#9cb0c4;--danger:#ff938b}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 50% -20%,#17466e,var(--bg) 48%);font:15px system-ui;color:#f5f8fb;min-height:100vh}
header,main{max-width:1400px;margin:auto;padding:24px}header{display:flex;align-items:center;gap:16px}.logo{background:linear-gradient(135deg,var(--accent),#7af0ca);color:#05251b;border-radius:14px;padding:12px;font-weight:950;box-shadow:0 8px 25px #32d6a044}
h1,h2,h3,p{margin:0 0 10px}.sub,.muted{color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:20px}
.card{background:linear-gradient(145deg,#142b44dd,var(--card));border:1px solid var(--line);border-radius:20px;padding:22px;box-shadow:0 18px 45px #0004}.row{display:flex;justify-content:space-between;gap:15px;align-items:center}
.badge{font-size:.75rem;padding:6px 10px;border-radius:20px;background:#23394d;color:var(--muted)}.badge.on,.pill.ok{background:#124d3d;color:#7bf0c7}.pill.bad{background:#5b2d32;color:#ffd4d1}
button{border:0;border-radius:10px;padding:9px 12px;font-weight:750;cursor:pointer;background:var(--accent);color:#05251b;transition:.15s}button:hover{transform:translateY(-1px)}button.secondary{background:#263f57;color:white}button.danger{background:#5b2d32;color:#ffd4d1}button:disabled{opacity:.4;transform:none}.actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:15px}
.result{display:none;margin-top:18px;border-top:1px solid var(--line);padding-top:16px;max-height:520px;overflow:auto}.result.visible{display:block}.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:9px}.field,.item{background:#091a2a99;border:1px solid #213e57;border-radius:11px;padding:10px}.key{display:block;color:#7f9ab3;font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;margin-bottom:4px}.value{overflow-wrap:anywhere}.list{display:grid;gap:9px}.pill{display:inline-block;border-radius:20px;padding:4px 8px;background:#243d55;margin:2px;font-size:.82rem}
.filterbox{margin-top:18px;padding:15px;background:#091a2a99;border:1px solid var(--line);border-radius:14px}.severity{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.severity select{background:#173149;color:white;border:1px solid #365773;border-radius:8px;padding:8px}.checks{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:7px;margin:12px 0}.checks label{background:#142b44;border-radius:8px;padding:7px}.empty{color:var(--muted);font-style:italic}
.channel-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(135px,1fr));gap:9px;margin:12px 0}.channel-grid label{color:var(--muted);font-size:.82rem}.channel-grid select,.channel-grid input,.channel-grid textarea{display:block;width:100%;margin-top:5px;background:#173149;color:white;border:1px solid #365773;border-radius:8px;padding:8px}.routebox{border-top:1px solid var(--line);padding-top:10px;margin-top:10px}.routebox:first-child{border:0;padding-top:0;margin-top:0}
.config-section{margin:14px 0}.config-section summary{cursor:pointer;font-weight:750;margin-bottom:8px}.config-section input[type=password],.config-section input[type=number]{width:100%;background:#173149;color:white;border:1px solid #365773;border-radius:8px;padding:8px}.hint{font-size:.82rem;color:var(--muted);margin:7px 0}.toolbar{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0}
.matrix-wrap{overflow-x:auto;margin:12px 0}.matrix{width:100%;border-collapse:collapse;min-width:560px}.matrix th,.matrix td{padding:8px;border-bottom:1px solid var(--line);text-align:center}.matrix th:first-child,.matrix td:first-child{text-align:left;position:sticky;left:0;background:#102238}.matrix input{width:18px;height:18px}
.tabs{display:flex;gap:6px;overflow-x:auto;margin:18px 0 8px;padding-bottom:4px}.tab{white-space:nowrap;background:#213b53;color:#dceafa}.tab.active{background:var(--accent);color:#05251b}.tab-panel{display:none}.tab-panel.active{display:block}.status-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:10px}.status-card{padding:13px;border:1px solid var(--line);border-radius:13px;background:#091a2a}.status-card strong{display:block;font-size:1.15rem;margin-top:5px}.dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:#687b8d;margin-right:6px}.dot.ok{background:var(--accent)}.dot.warn{background:#ffc857}.source-status{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:9px;margin-top:12px}.province-search{width:100%;background:#173149;color:white;border:1px solid #365773;border-radius:8px;padding:9px}.chips{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}.chip{background:#24465f;border-radius:20px;padding:5px 9px;font-size:.82rem}.matrix th.sev-low{color:#75c8ff}.matrix th.sev-medium{color:#ffd166}.matrix th.sev-high{color:#ff9f43}.matrix th.sev-critical{color:#ff7770}.matrix tr:hover td{background:#18334a}.toasts{position:fixed;right:18px;bottom:18px;z-index:20;display:grid;gap:8px;max-width:360px}.toast{padding:12px 15px;border-radius:11px;background:#124d3d;color:#d8fff1;box-shadow:0 10px 30px #0008}.toast.bad{background:#5b2d32;color:#ffd4d1}
@media(max-width:520px){.grid{grid-template-columns:1fr}header,main{padding:16px}.card{padding:17px}}
</style></head><body><header><div class="logo">MN</div><div><h1>MeshNet Control</h1><div class="sub">Estado, datos y operación de aplicaciones independientes · UI 2</div></div></header><main><section class="card" style="margin-bottom:20px"><div class="row"><h2>Respuesta automática</h2><span id="auto-reply-state" class="badge">CARGANDO</span></div><p class="sub">Responde en los canales elegidos del mismo transporte. Usa <strong>{message}</strong> para insertar el mensaje recibido.</p><div id="auto-reply" class="filterbox">Cargando configuración…</div></section><div id="tools" class="grid"><div class="card"><h2>Cargando aplicaciones…</h2><p class="muted">Si este mensaje no desaparece, recarga sin caché y revisa la consola del navegador.</p></div></div><noscript><div class="card"><h2>JavaScript deshabilitado</h2><p>El ControlPanel necesita JavaScript para mostrar las aplicaciones.</p></div></noscript></main><div id="toasts" class="toasts"></div>
<script>
function fatalUi(title,message){const box=document.querySelector('#tools');if(!box)return;box.innerHTML='<div class="card"><h2 class="ui-error-title"></h2><p class="pill bad ui-error-message"></p><p class="muted">Recarga con Ctrl+F5. Si continúa, abre /api/tools.</p></div>';box.querySelector('.ui-error-title').textContent=title;box.querySelector('.ui-error-message').textContent=String(message)}
window.addEventListener('error',event=>fatalUi('No se pudo mostrar el panel',event.message||'Error JavaScript'));
window.addEventListener('unhandledrejection',event=>fatalUi('Error cargando el panel',(event.reason&&event.reason.message)||event.reason||'Error desconocido'));
const headers={'Content-Type':'application/json'};
const labels={ok:'Correcto',enabled:'Habilitado',error:'Error',events:'Eventos',sources:'Fuentes',records:'Recibidos',accepted:'Aceptados',last_success:'Último éxito',last_error:'Último error',current_exists:'Datos locales',minimum_severity:'Severidad mínima',categories:'Categorías',changes:'Cambios',new:'Nuevas',updated:'Actualizadas',resolved:'Resueltas',problems:'Problemas',areas:'Áreas',pending:'Pendientes',delivered:'Entregados',observed:'Observados'};
const catLabels={wildfire:'Incendio forestal',urban_fire:'Incendio urbano',industrial_fire:'Incendio industrial',traffic_collision:'Colisión de tráfico',road_closed:'Carretera cortada',lane_closed:'Carril cerrado',traffic_obstruction:'Obstáculo o afección',flood:'Inundación',storm:'Tormenta',snow:'Nieve',strong_wind:'Viento fuerte',extreme_temperature:'Temperatura extrema',chemical:'Riesgo químico',power_outage:'Corte eléctrico',water_outage:'Corte de agua',gas_outage:'Corte de gas',public_safety:'Seguridad pública',civil_protection:'Protección civil',earthquake:'Terremoto',tsunami:'Tsunami',volcanic:'Actividad volcánica',landslide:'Deslizamiento',other:'Otras'};
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const label=k=>esc(labels[k]||catLabels[k]||String(k).replaceAll('_',' '));
function render(v,k=''){
  if(v===null||v===undefined||v==='')return '<span class="empty">Sin datos</span>';
  if(typeof v==='boolean')return `<span class="pill ${v?'ok':'bad'}">${v?'Sí':'No'}</span>`;
  if(Array.isArray(v)){if(!v.length)return '<span class="empty">No hay elementos</span>';return `<div class="list">${v.map((x,i)=>`<div class="item">${typeof x==='object'?render(x):`<span class="pill">${esc(catLabels[x]||x)}</span>`}</div>`).join('')}</div>`}
  if(typeof v==='object')return `<div class="kv">${Object.entries(v).map(([a,b])=>`<div class="field"><span class="key">${label(a)}</span><div class="value">${render(b,a)}</div></div>`).join('')}</div>`;
  return `<span>${esc(v)}</span>`;
}
async function request(url,options={}){const r=await fetch(url,{headers,...options});const d=await r.json();if(!r.ok)throw Error(d.detail||'No se pudo completar la operación');return d}
function emergencyTabsHtml(t){return !t.enabled||t.id!=='emergencias_guardia'?'':`<nav class="tabs"><button class="tab active" onclick="openEmergencyTab('summary',this)">Resumen</button><button class="tab" onclick="openEmergencyTab('collection',this)">Fuentes y cobertura</button><button class="tab" onclick="openEmergencyTab('propagation',this)">Propagación</button></nav>`}
function summaryHtml(t){return !t.enabled||t.id!=='emergencias_guardia'?'':`<section class="filterbox tab-panel active" data-emtab="summary"><h3>Resumen operativo</h3><div id="overview-emergencias_guardia" class="empty">Cargando estado…</div></section>`}
function filterHtml(t){return !t.enabled||t.id!=='emergencias_guardia'?'':`<section class="filterbox tab-panel" data-emtab="propagation"><h3>Matriz de propagación</h3><p class="muted">Elige las combinaciones exactas que podrán enviarse.</p><div id="filters-${t.id}" class="empty">Cargando filtros…</div></section>`}
function collectionHtml(t){return !t.enabled||t.id!=='emergencias_guardia'?'':`<section class="filterbox tab-panel" data-emtab="collection"><h3>Fuentes y cobertura</h3><p class="muted">Configura orígenes, tipos de incidencia y ámbito geográfico.</p><div id="collection-${t.id}" class="empty">Cargando configuración…</div></section>`}
function channelHtml(t){if(!t.enabled||!['emergencias_guardia','farmacias_guardia'].includes(t.id))return '';const tab=t.id==='emergencias_guardia'?' tab-panel':'';const attr=t.id==='emergencias_guardia'?' data-emtab="propagation"':'';return `<section class="filterbox${tab}"${attr}><h3>Canales de comunicación</h3><p class="muted">Consulta y modifica los canales de difusión. Usa -1 para dejar un canal sin configurar.</p><div id="channels-${t.id}" class="empty">Cargando canales…</div></section>`}
async function load(){loadAutoReply();const d=await request('/api/tools');document.querySelector('#tools').innerHTML=d.tools.map(t=>`<article class="card"><div class="row"><h2>${esc(t.name)}</h2><span class="badge ${t.enabled?'on':''}">${t.enabled?'HABILITADA':'DESHABILITADA'}</span></div><p class="sub">${esc(t.description)}</p><div class="actions"><button onclick="toggle('${t.id}',${!t.enabled})">${t.enabled?'Deshabilitar':'Habilitar'}</button><button class="secondary" ${t.enabled?'':'disabled'} onclick="health('${t.id}')">Comprobar salud</button></div><div class="actions">${t.actions.map(a=>`<button class="${a.confirm?'danger':(a.mutating?'':'secondary')}" ${t.enabled?'':'disabled'} onclick="run('${t.id}','${a.id}',${a.confirm},'${esc(a.name)}')">${esc(a.name)}</button>`).join('')}</div>${emergencyTabsHtml(t)}${summaryHtml(t)}${collectionHtml(t)}${channelHtml(t)}${filterHtml(t)}<div class="result" id="r-${t.id}"></div></article>`).join('');if(d.tools.some(t=>t.id==='emergencias_guardia'&&t.enabled)){loadOverview();loadCollection();loadFilters();loadEmergencyChannels()}if(d.tools.some(t=>t.id==='farmacias_guardia'&&t.enabled))loadPharmacyChannels()}
async function toggle(id,enabled){await request(`/api/tools/${id}/enabled`,{method:'PUT',body:JSON.stringify({enabled})});load()}
async function loadAutoReply(){const box=document.querySelector('#auto-reply');try{const d=await request('/api/auto-reply'),state=document.querySelector('#auto-reply-state');state.textContent=d.enabled?'ACTIVA':'DESACTIVADA';state.className='badge '+(d.enabled?'on':'');box.innerHTML=`<label><input id="ar-enabled" type="checkbox" ${d.enabled?'checked':''}> Activar respuestas automáticas</label><div class="channel-grid"><label>Texto de respuesta<textarea id="ar-template" rows="3">${esc(d.template)}</textarea></label><label>Canales MeshCore<input id="ar-meshcore" value="${d.meshcore_channels.join(', ')}" placeholder="0, 2, 5"></label><label>Canales Meshtastic<input id="ar-meshtastic" value="${d.meshtastic_channels.join(', ')}" placeholder="0, 1, 3"></label></div><p class="hint">Separa varios canales con comas. Ejemplo: Recibido, {message}</p><button onclick="saveAutoReply()">Guardar respuesta automática</button>`}catch(e){box.textContent=e.message}}
async function saveAutoReply(){const parse=id=>document.querySelector(id).value.split(',').map(x=>x.trim()).filter(Boolean).map(Number),payload={enabled:document.querySelector('#ar-enabled').checked,template:document.querySelector('#ar-template').value,meshcore_channels:parse('#ar-meshcore'),meshtastic_channels:parse('#ar-meshtastic')};if([...payload.meshcore_channels,...payload.meshtastic_channels].some(x=>!Number.isInteger(x))){toast('Los canales deben ser números enteros',true);return}try{await request('/api/auto-reply',{method:'PUT',body:JSON.stringify(payload)});toast('Respuesta automática guardada');loadAutoReply()}catch(e){toast(e.message,true)}}
async function health(id){show(id,'Comprobando…',true);try{const d=await request(`/api/tools/${id}/health`);show(id,render(d.details??d))}catch(e){show(id,render({error:e.message}))}}
async function run(id,a,needs,name){if(needs&&!confirm(`¿Ejecutar “${name}”?`))return;show(id,'Ejecutando…',true);try{const d=await request(`/api/tools/${id}/actions/${a}`,{method:'POST',body:JSON.stringify({confirmed:true})});show(id,render(d.data??{correcto:d.ok,salida:d.stdout,error:d.stderr}))}catch(e){show(id,render({error:e.message}))}}
function show(id,html,text=false){const n=document.querySelector('#r-'+id);n.classList.add('visible');n.innerHTML=text?`<span class="muted">${esc(html)}</span>`:html}
function toast(message,bad=false){const n=document.createElement('div');n.className='toast'+(bad?' bad':'');n.textContent=message;document.querySelector('#toasts').appendChild(n);setTimeout(()=>n.remove(),4500)}
function openEmergencyTab(name,button){document.querySelectorAll('[data-emtab]').forEach(x=>x.classList.toggle('active',x.dataset.emtab===name));document.querySelectorAll('.tabs .tab').forEach(x=>x.classList.remove('active'));button.classList.add('active')}
const ago=value=>{if(!value)return 'Sin datos';const seconds=Math.max(0,(Date.now()-new Date(value).getTime())/1000);if(seconds<90)return 'Hace menos de 1 min';if(seconds<3600)return `Hace ${Math.floor(seconds/60)} min`;if(seconds<86400)return `Hace ${Math.floor(seconds/3600)} h`;return new Date(value).toLocaleString('es-ES')};
async function loadOverview(){const box=document.querySelector('#overview-emergencias_guardia');if(!box)return;try{const d=await request('/api/emergencias/overview');const cards=[['API de consultas',d.api.ok?'Operativa':'No disponible',d.api.ok],['Recolector',d.collector.ok?'Con datos':'Sin ejecución correcta',d.collector.ok],['Última recogida',ago(d.collector.last_success),Boolean(d.collector.last_success)],['Fuentes',`${d.sources.enabled} de ${d.sources.total} activas`,d.sources.enabled>0],['Avisos pendientes',String(d.notifications.pending),d.notifications.pending===0],['Áreas configuradas',String(d.coverage.areas),d.coverage.areas>0]];const sourceCards=d.sources.items.map(s=>`<div class="status-card"><span class="dot ${s.ok?'ok':'warn'}"></span>${esc(sourceLabels[s.id]||s.id)}<strong>${s.ok?'Operativa':(s.error||'Sin datos')}</strong><span class="hint">${s.last_success?ago(s.last_success):'Nunca consultada'} · ${Number(s.accepted||0)} aceptados</span></div>`).join('');box.innerHTML=`<div class="status-grid">${cards.map(x=>`<div class="status-card"><span class="dot ${x[2]?'ok':'warn'}"></span>${esc(x[0])}<strong>${esc(x[1])}</strong></div>`).join('')}</div><div class="source-status">${sourceCards}</div>`}catch(e){box.textContent=e.message}}
const sourceLabels={municipal_json:'Ayuntamiento de Zaragoza',dgt_datex:'DGT — tráfico y carreteras',ign_earthquakes:'IGN — terremotos',nasa_firms:'NASA FIRMS — focos térmicos'};
function toggleChecks(selector,value){document.querySelectorAll(selector).forEach(x=>x.checked=value);if(selector.includes('collection-province'))updateProvinceChips()}
async function loadCollection(){const box=document.querySelector('#collection-emergencias_guardia');if(!box)return;try{const d=await request('/api/emergencias/collection');const sources=d.sources.map(x=>`<label><input class="collection-source" type="checkbox" value="${x.id}" ${x.enabled?'checked':''}> ${esc(sourceLabels[x.id]||x.id)}</label>`).join('');const provinces=d.provinces.map(x=>`<label class="province-option" data-name="${esc(x.name.toLowerCase())}"><input class="collection-province" onchange="updateProvinceChips()" type="checkbox" value="${esc(x.name)}" ${x.enabled?'checked':''}> ${esc(x.name)}</label>`).join('');const cats=d.categories.map(x=>`<label><input class="collection-category" type="checkbox" value="${x.name}" ${x.enabled?'checked':''}> ${esc(catLabels[x.name]||x.name)}</label>`).join('');box.innerHTML=`<div class="config-section"><strong>Fuentes consultadas</strong><div class="checks">${sources}</div><label>MAP_KEY de NASA FIRMS <input id="firms-key" type="password" autocomplete="new-password" placeholder="${d.firms_key_configured?'Configurada — dejar vacío para conservar':'Introducir MAP_KEY'}"></label><p class="hint">La clave nunca se devuelve al navegador. Déjala vacía para conservar la actual.</p></div><details class="config-section" open><summary>Tipos de incidencia recogidos</summary><div class="toolbar"><button class="secondary" onclick="toggleChecks('.collection-category',true)">Todos</button><button class="secondary" onclick="toggleChecks('.collection-category',false)">Ninguno</button></div><div class="checks">${cats}</div></details><details class="config-section"><summary>Provincias (${d.provinces.filter(x=>x.enabled).length} seleccionadas)</summary><input class="province-search" placeholder="Buscar provincia…" oninput="filterProvinces(this.value)"><div id="province-chips" class="chips"></div><p class="hint">Se aplican a fuentes que informan la provincia, como DGT.</p><div class="toolbar"><button class="secondary" onclick="toggleChecks('.collection-province',true)">Toda España</button><button class="secondary" onclick="toggleChecks('.collection-province',false)">Limpiar</button></div><div class="checks">${provinces}</div></details><div class="config-section"><label><input id="radius-enabled" type="checkbox" ${d.radius.enabled?'checked':''}> Usar también un radio geográfico</label><p class="hint">Necesario para IGN y FIRMS, que proporcionan coordenadas pero no siempre provincia.</p><div class="channel-grid"><label>Latitud<input id="radius-lat" type="number" step="0.0001" value="${d.radius.latitude}"></label><label>Longitud<input id="radius-lon" type="number" step="0.0001" value="${d.radius.longitude}"></label><label>Radio (km)<input id="radius-km" type="number" min="1" max="1000" value="${d.radius.radius_km}"></label></div></div><button onclick="saveCollection()">Guardar recogida</button>`;updateProvinceChips()}catch(e){box.textContent=e.message}}
function filterProvinces(value){const q=value.toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g,'');document.querySelectorAll('.province-option').forEach(x=>x.hidden=!x.dataset.name.normalize('NFD').replace(/[\u0300-\u036f]/g,'').includes(q))}
function updateProvinceChips(){const box=document.querySelector('#province-chips');if(box)box.innerHTML=[...document.querySelectorAll('.collection-province:checked')].map(x=>`<span class="chip">${esc(x.value)}</span>`).join('')||'<span class="hint">Ninguna provincia seleccionada</span>'}
async function saveCollection(){const values=s=>[...document.querySelectorAll(s+':checked')].map(x=>x.value),payload={sources:values('.collection-source'),provinces:values('.collection-province'),categories:values('.collection-category'),firms_map_key:document.querySelector('#firms-key').value,radius:{enabled:document.querySelector('#radius-enabled').checked,latitude:Number(document.querySelector('#radius-lat').value),longitude:Number(document.querySelector('#radius-lon').value),radius_km:Number(document.querySelector('#radius-km').value)}};try{const d=await request('/api/emergencias/collection',{method:'PUT',body:JSON.stringify(payload)});show('emergencias_guardia',render({correcto:true,fuentes:d.sources,provincias:d.provinces,tipos:d.categories}));toast('Configuración de recogida guardada');loadCollection();loadOverview()}catch(e){show('emergencias_guardia',render({error:e.message}))}}
const severityLabels={low:'Baja',medium:'Media',high:'Alta',critical:'Crítica'};
async function loadFilters(){const box=document.querySelector('#filters-emergencias_guardia');try{const d=await request('/api/emergencias/filters'),levels=['low','medium','high','critical'];const head=levels.map(s=>`<th class="sev-${s}">${severityLabels[s]}<br><button class="secondary" onclick="toggleChecks('.rule-'+s,true)">Todo</button></th>`).join('');const rows=d.categories.map(c=>`<tr><td>${esc(catLabels[c.name]||c.name)}</td>${levels.map(s=>`<td><input class="prop-rule rule-${s}" data-severity="${s}" data-category="${c.name}" type="checkbox" ${(d.rules[s]||[]).includes(c.name)?'checked':''}></td>`).join('')}</tr>`).join('');box.innerHTML=`<p class="hint">Marca exactamente qué categoría puede propagarse en cada severidad. Una casilla vacía bloquea esa combinación.</p><div class="matrix-wrap"><table class="matrix"><thead><tr><th>Tipo</th>${head}</tr></thead><tbody>${rows}</tbody></table></div><div class="toolbar"><button class="secondary" onclick="toggleChecks('.prop-rule',false)">Limpiar matriz</button><button onclick="saveFilters()">Guardar matriz</button></div>`}catch(e){box.textContent=e.message}}
async function saveFilters(){const rules={low:[],medium:[],high:[],critical:[]};document.querySelectorAll('.prop-rule:checked').forEach(x=>rules[x.dataset.severity].push(x.dataset.category));try{const d=await request('/api/emergencias/filters',{method:'PUT',body:JSON.stringify({rules})});show('emergencias_guardia',render({correcto:true,matriz:d.rules,nota:d.note}));toast('Matriz de propagación guardada');loadFilters()}catch(e){toast(e.message,true);show('emergencias_guardia',render({error:e.message}))}}
const channelInputs=(prefix,d)=>`<div class="channel-grid"><label>Canal MeshCore<input id="${prefix}-meshcore" type="number" min="-1" max="255" value="${Number(d.meshcore_channel)}"></label><label>Canal Meshtastic<input id="${prefix}-meshtastic" type="number" min="-1" max="255" value="${Number(d.meshtastic_channel)}"></label></div>`;
const transportSelect=(prefix,current,values)=>`<label>Transporte<select id="${prefix}-transport">${values.map(x=>`<option value="${x}" ${current===x?'selected':''}>${x==='auto'?'Automático':x==='meshcore'?'MeshCore':'Meshtastic'}</option>`).join('')}</select></label>`;
async function loadEmergencyChannels(){const box=document.querySelector('#channels-emergencias_guardia');if(!box)return;try{const d=await request('/api/emergencias/channels');const globalTransport=`<div class="routebox"><strong>Transporte global</strong><div class="channel-grid">${transportSelect('em-global',d.transport,['meshcore','meshtastic'])}</div><button onclick="saveEmergencyTransport()">Guardar transporte</button></div>`;box.innerHTML=globalTransport+Object.entries(d.routes).map(([route,c])=>`<div class="routebox"><strong>${esc({emergencias:'Emergencias',servicios:'Servicios',meteo:'Meteorología'}[route])}</strong>${channelInputs('em-'+route,c)}<button onclick="saveEmergencyChannels('${route}')">Guardar canales</button></div>`).join('')}catch(e){box.textContent=e.message}}
async function saveEmergencyTransport(){const transport=document.querySelector('#em-global-transport').value;try{await request('/api/emergencias/transport',{method:'PUT',body:JSON.stringify({transport})});show('emergencias_guardia',render({correcto:true,transporte:transport}));loadEmergencyChannels()}catch(e){show('emergencias_guardia',render({error:e.message}))}}
async function saveEmergencyChannels(route){const prefix='em-'+route,payload={meshcore_channel:Number(document.querySelector('#'+prefix+'-meshcore').value),meshtastic_channel:Number(document.querySelector('#'+prefix+'-meshtastic').value)};try{await request('/api/emergencias/channels/'+route,{method:'PUT',body:JSON.stringify(payload)});show('emergencias_guardia',render({correcto:true,ruta:route,...payload}));loadEmergencyChannels()}catch(e){show('emergencias_guardia',render({error:e.message}))}}
async function loadPharmacyChannels(){const box=document.querySelector('#channels-farmacias_guardia');if(!box)return;try{const d=await request('/api/farmacias/channels');const warning=d.radio_profile==='meshcore_only'&&d.transport==='meshtastic'?'<p class="pill bad">Meshtastic no está disponible con meshcore_only; la publicación usará MeshCore.</p>':'';const profile=d.radio_profile||'No definido en Farmacias (se comprobará con el broker al publicar)';box.innerHTML=`<p class="muted">Perfil: ${esc(profile)} · salida configurada: ${esc(d.effective_transport)}</p>${warning}<div class="channel-grid">${transportSelect('farma',d.transport,['auto','meshcore','meshtastic'])}</div>`+channelInputs('farma',d)+'<button onclick="savePharmacyChannels()">Guardar canales</button>'}catch(e){box.textContent=e.message}}
async function savePharmacyChannels(){const payload={transport:document.querySelector('#farma-transport').value,meshcore_channel:Number(document.querySelector('#farma-meshcore').value),meshtastic_channel:Number(document.querySelector('#farma-meshtastic').value)};try{const d=await request('/api/farmacias/channels',{method:'PUT',body:JSON.stringify(payload)});show('farmacias_guardia',render({correcto:true,...payload,nota:d.restart_required?'Reinicie la API de Farmacias para aplicar el cambio.':''}));loadPharmacyChannels()}catch(e){show('farmacias_guardia',render({error:e.message}))}}
load().catch(e=>document.querySelector('#tools').innerHTML=`<div class="card">${esc(e.message)}</div>`);
</script></body></html>"""


app = create_app()
