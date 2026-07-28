"""Panel operativo seguro para aplicaciones independientes de MeshNet."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
REPO_DIR = BASE_DIR.parent.parent
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
    minimum_severity: str
    categories: list[str]


class CommunicationChannelsPayload(BaseModel):
    transport: str
    meshcore_channel: int
    meshtastic_channel: int

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump() if hasattr(self, "model_dump") else self.dict()


FARMACIAS_ENV_FILE = Path(
    os.getenv("CONTROLPANEL_FARMACIAS_ENV", str(REPO_DIR / "tools/farmacias_guardia/.env"))
)
CHANNEL_KEYS = {
    "FARMACIAS_BROADCAST_TRANSPORT",
    "FARMACIAS_MESHCORE_CHANNEL",
    "FARMACIAS_MESHTASTIC_CHANNEL",
}


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
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text("\n".join(output) + "\n", encoding="utf-8")
    temporary.replace(path)


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
        return {"ok": done.returncode == 0, "returncode": done.returncode, "stdout": stdout,
                "stderr": stderr, "data": data,
                "truncated": len(done.stdout) > MAX_OUTPUT or len(done.stderr) > MAX_OUTPUT}
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "returncode": None, "stdout": str(exc.stdout or "")[:MAX_OUTPUT],
                "stderr": "Tiempo de espera agotado", "data": None}
    except OSError as exc:
        return {"ok": False, "returncode": None, "stdout": "", "stderr": str(exc), "data": None}


def create_app(registry: ToolRegistry | None = None) -> FastAPI:
    registry = registry or ToolRegistry(Path(os.getenv("CONTROLPANEL_STATE", str(BASE_DIR / "data/state.json"))))
    app = FastAPI(title="MeshNet Control", version="1.0.0")

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> str:
        return DASHBOARD

    @app.get("/api/tools")
    def list_tools() -> dict[str, Any]:
        return {"tools": registry.items()}

    @app.put("/api/tools/{tool_id}/enabled")
    def set_enabled(tool_id: str, payload: EnabledPayload) -> dict[str, Any]:
        try:
            registry.set_enabled(tool_id, payload.enabled)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Aplicación desconocida") from exc
        return {"id": tool_id, "enabled": payload.enabled}

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

    @app.get("/api/emergencias/filters")
    def get_emergency_filters() -> dict[str, Any]:
        result = emergency_filter_action("filters", "show")
        if not result["ok"]:
            raise HTTPException(status_code=502, detail=result["stderr"] or "No se pudieron leer los filtros")
        return result["data"] or {}

    @app.put("/api/emergencias/filters")
    def set_emergency_filters(payload: EmergencyFiltersPayload) -> dict[str, Any]:
        severities = {"low", "medium", "high", "critical"}
        categories = {
            "wildfire", "urban_fire", "industrial_fire", "traffic_collision",
            "road_closed", "lane_closed", "traffic_obstruction", "flood", "storm",
            "snow", "strong_wind", "extreme_temperature", "chemical", "power_outage",
            "water_outage", "gas_outage", "public_safety", "civil_protection", "other",
        }
        selected = set(payload.categories)
        if payload.minimum_severity not in severities or not selected.issubset(categories):
            raise HTTPException(status_code=422, detail="Filtro de emergencias no válido")
        result = emergency_filter_action(
            "filters", "set", "--minimum-severity", payload.minimum_severity,
            "--categories", ",".join(sorted(selected)),
        )
        if not result["ok"]:
            raise HTTPException(status_code=502, detail=result["stderr"] or result["stdout"])
        return result["data"] or {}

    @app.get("/api/emergencias/channels")
    def get_emergency_channels() -> dict[str, Any]:
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
    def set_emergency_channels(route: str, payload: CommunicationChannelsPayload) -> dict[str, Any]:
        if route not in {"emergencias", "servicios", "meteo"}:
            raise HTTPException(status_code=404, detail="Ruta de emergencias desconocida")
        validate_channels(payload, {"meshcore", "meshtastic"})
        commands = (
            ("notify", "set-channel", route, "meshcore", str(payload.meshcore_channel)),
            ("notify", "set-channel", route, "meshtastic", str(payload.meshtastic_channel)),
            ("notify", "set-transport", payload.transport),
        )
        for arguments in commands:
            result = emergency_filter_action(*arguments)
            if not result["ok"]:
                raise HTTPException(status_code=502, detail=result["stderr"] or result["stdout"])
        return {"ok": True, "route": route, **payload.as_dict()}

    @app.get("/api/farmacias/channels")
    def get_pharmacy_channels() -> dict[str, Any]:
        values = read_env_values(FARMACIAS_ENV_FILE, CHANNEL_KEYS)
        return {
            "transport": values.get("FARMACIAS_BROADCAST_TRANSPORT", "auto"),
            "meshcore_channel": int(values.get("FARMACIAS_MESHCORE_CHANNEL", "-1")),
            "meshtastic_channel": int(values.get("FARMACIAS_MESHTASTIC_CHANNEL", "-1")),
        }

    @app.put("/api/farmacias/channels")
    def set_pharmacy_channels(payload: CommunicationChannelsPayload) -> dict[str, Any]:
        validate_channels(payload, {"auto", "meshcore", "meshtastic"})
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
.channel-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(135px,1fr));gap:9px;margin:12px 0}.channel-grid label{color:var(--muted);font-size:.82rem}.channel-grid select,.channel-grid input{display:block;width:100%;margin-top:5px;background:#173149;color:white;border:1px solid #365773;border-radius:8px;padding:8px}.routebox{border-top:1px solid var(--line);padding-top:10px;margin-top:10px}.routebox:first-child{border:0;padding-top:0;margin-top:0}
@media(max-width:520px){.grid{grid-template-columns:1fr}header,main{padding:16px}.card{padding:17px}}
</style></head><body><header><div class="logo">MN</div><div><h1>MeshNet Control</h1><div class="sub">Estado, datos y operación de aplicaciones independientes</div></div></header><main><div id="tools" class="grid"></div></main>
<script>
const headers={'Content-Type':'application/json'};
const labels={ok:'Correcto',enabled:'Habilitado',error:'Error',events:'Eventos',sources:'Fuentes',records:'Recibidos',accepted:'Aceptados',last_success:'Último éxito',last_error:'Último error',current_exists:'Datos locales',minimum_severity:'Severidad mínima',categories:'Categorías',changes:'Cambios',new:'Nuevas',updated:'Actualizadas',resolved:'Resueltas',problems:'Problemas',areas:'Áreas',pending:'Pendientes',delivered:'Entregados',observed:'Observados'};
const catLabels={wildfire:'Incendio forestal',urban_fire:'Incendio urbano',industrial_fire:'Incendio industrial',traffic_collision:'Colisión de tráfico',road_closed:'Carretera cortada',lane_closed:'Carril cerrado',traffic_obstruction:'Obstáculo o afección',flood:'Inundación',storm:'Tormenta',snow:'Nieve',strong_wind:'Viento fuerte',extreme_temperature:'Temperatura extrema',chemical:'Riesgo químico',power_outage:'Corte eléctrico',water_outage:'Corte de agua',gas_outage:'Corte de gas',public_safety:'Seguridad pública',civil_protection:'Protección civil',other:'Otras'};
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
function filterHtml(t){return t.id!=='emergencias_guardia'?'':`<section class="filterbox"><h3>Filtro de propagación</h3><p class="muted">Elige qué alertas podrán enviarse en las próximas comprobaciones.</p><div id="filters-${t.id}" class="empty">Cargando filtros…</div></section>`}
function channelHtml(t){return !['emergencias_guardia','farmacias_guardia'].includes(t.id)?'':`<section class="filterbox"><h3>Canales de comunicación</h3><p class="muted">Consulta y modifica los canales de difusión. Usa -1 para dejar un canal sin configurar.</p><div id="channels-${t.id}" class="empty">Cargando canales…</div></section>`}
async function load(){const d=await request('/api/tools');document.querySelector('#tools').innerHTML=d.tools.map(t=>`<article class="card"><div class="row"><h2>${esc(t.name)}</h2><span class="badge ${t.enabled?'on':''}">${t.enabled?'HABILITADA':'DESHABILITADA'}</span></div><p class="sub">${esc(t.description)}</p><div class="actions"><button onclick="toggle('${t.id}',${!t.enabled})">${t.enabled?'Deshabilitar':'Habilitar'}</button><button class="secondary" ${t.enabled?'':'disabled'} onclick="health('${t.id}')">Comprobar salud</button></div><div class="actions">${t.actions.map(a=>`<button class="${a.confirm?'danger':(a.mutating?'':'secondary')}" ${t.enabled?'':'disabled'} onclick="run('${t.id}','${a.id}',${a.confirm},'${esc(a.name)}')">${esc(a.name)}</button>`).join('')}</div>${channelHtml(t)}${filterHtml(t)}<div class="result" id="r-${t.id}"></div></article>`).join('');if(d.tools.some(t=>t.id==='emergencias_guardia')){loadFilters();loadEmergencyChannels()}if(d.tools.some(t=>t.id==='farmacias_guardia'))loadPharmacyChannels()}
async function toggle(id,enabled){await request(`/api/tools/${id}/enabled`,{method:'PUT',body:JSON.stringify({enabled})});load()}
async function health(id){show(id,'Comprobando…',true);try{const d=await request(`/api/tools/${id}/health`);show(id,render(d.details??d))}catch(e){show(id,render({error:e.message}))}}
async function run(id,a,needs,name){if(needs&&!confirm(`¿Ejecutar “${name}”?`))return;show(id,'Ejecutando…',true);try{const d=await request(`/api/tools/${id}/actions/${a}`,{method:'POST',body:JSON.stringify({confirmed:true})});show(id,render(d.data??{correcto:d.ok,salida:d.stdout,error:d.stderr}))}catch(e){show(id,render({error:e.message}))}}
function show(id,html,text=false){const n=document.querySelector('#r-'+id);n.classList.add('visible');n.innerHTML=text?`<span class="muted">${esc(html)}</span>`:html}
async function loadFilters(){const box=document.querySelector('#filters-emergencias_guardia');try{const d=await request('/api/emergencias/filters');box.innerHTML=`<div class="severity"><label>Severidad mínima <select id="severity">${['low','medium','high','critical'].map(x=>`<option value="${x}" ${d.minimum_severity===x?'selected':''}>${{low:'Baja',medium:'Media',high:'Alta',critical:'Crítica'}[x]}</option>`).join('')}</select></label></div><div class="checks">${d.categories.map(c=>`<label><input type="checkbox" value="${c.name}" ${c.enabled?'checked':''}> ${esc(catLabels[c.name]||c.name)}</label>`).join('')}</div><button onclick="saveFilters()">Guardar filtro</button>`}catch(e){box.textContent=e.message}}
async function saveFilters(){const categories=[...document.querySelectorAll('#filters-emergencias_guardia input:checked')].map(x=>x.value);try{const d=await request('/api/emergencias/filters',{method:'PUT',body:JSON.stringify({minimum_severity:document.querySelector('#severity').value,categories})});show('emergencias_guardia',render({correcto:true,severidad:d.minimum_severity,categorías:d.categories,nota:d.note}));loadFilters()}catch(e){show('emergencias_guardia',render({error:e.message}))}}
const channelFields=(prefix,d,transports)=>`<div class="channel-grid"><label>Transporte<select id="${prefix}-transport">${transports.map(x=>`<option value="${x}" ${d.transport===x?'selected':''}>${x==='auto'?'Automático':x==='meshcore'?'MeshCore':'Meshtastic'}</option>`).join('')}</select></label><label>Canal MeshCore<input id="${prefix}-meshcore" type="number" min="-1" max="255" value="${Number(d.meshcore_channel)}"></label><label>Canal Meshtastic<input id="${prefix}-meshtastic" type="number" min="-1" max="255" value="${Number(d.meshtastic_channel)}"></label></div>`;
async function loadEmergencyChannels(){const box=document.querySelector('#channels-emergencias_guardia');try{const d=await request('/api/emergencias/channels');box.innerHTML=Object.entries(d.routes).map(([route,c])=>`<div class="routebox"><strong>${esc({emergencias:'Emergencias',servicios:'Servicios',meteo:'Meteorología'}[route])}</strong>${channelFields('em-'+route,{...c,transport:d.transport},['meshcore','meshtastic'])}<button onclick="saveEmergencyChannels('${route}')">Guardar canales</button></div>`).join('')}catch(e){box.textContent=e.message}}
async function saveEmergencyChannels(route){const prefix='em-'+route,payload={transport:document.querySelector('#'+prefix+'-transport').value,meshcore_channel:Number(document.querySelector('#'+prefix+'-meshcore').value),meshtastic_channel:Number(document.querySelector('#'+prefix+'-meshtastic').value)};try{await request('/api/emergencias/channels/'+route,{method:'PUT',body:JSON.stringify(payload)});show('emergencias_guardia',render({correcto:true,ruta:route,...payload}));loadEmergencyChannels()}catch(e){show('emergencias_guardia',render({error:e.message}))}}
async function loadPharmacyChannels(){const box=document.querySelector('#channels-farmacias_guardia');try{const d=await request('/api/farmacias/channels');box.innerHTML=channelFields('farma',d,['auto','meshcore','meshtastic'])+'<button onclick="savePharmacyChannels()">Guardar canales</button>'}catch(e){box.textContent=e.message}}
async function savePharmacyChannels(){const payload={transport:document.querySelector('#farma-transport').value,meshcore_channel:Number(document.querySelector('#farma-meshcore').value),meshtastic_channel:Number(document.querySelector('#farma-meshtastic').value)};try{const d=await request('/api/farmacias/channels',{method:'PUT',body:JSON.stringify(payload)});show('farmacias_guardia',render({correcto:true,...payload,nota:d.restart_required?'Reinicie la API de Farmacias para aplicar el cambio.':''}));loadPharmacyChannels()}catch(e){show('farmacias_guardia',render({error:e.message}))}}
load().catch(e=>document.querySelector('#tools').innerHTML=`<div class="card">${esc(e.message)}</div>`);
</script></body></html>"""


app = create_app()
