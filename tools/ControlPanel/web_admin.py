"""Panel operativo seguro para aplicaciones independientes de MeshNet."""
from __future__ import annotations

import hmac
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

from fastapi import FastAPI, Header, HTTPException
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
        if action.mutating:
            argv.insert(0, "sudo")
            argv.insert(1, "-n")
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


def create_app(registry: ToolRegistry | None = None, token: str | None = None) -> FastAPI:
    registry = registry or ToolRegistry(Path(os.getenv("CONTROLPANEL_STATE", str(BASE_DIR / "data/state.json"))))
    configured_token = os.getenv("CONTROLPANEL_TOKEN", "") if token is None else token
    app = FastAPI(title="MeshNet Control", version="1.0.0")

    def authorize(authorization: str | None, write: bool = False) -> None:
        if not configured_token:
            if write:
                raise HTTPException(status_code=503, detail="Configure CONTROLPANEL_TOKEN para operar")
            return
        expected = f"Bearer {configured_token}"
        if not authorization or not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="No autorizado")

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> str:
        return DASHBOARD

    @app.get("/api/tools")
    def list_tools(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        authorize(authorization)
        return {"tools": registry.items()}

    @app.put("/api/tools/{tool_id}/enabled")
    def set_enabled(tool_id: str, payload: EnabledPayload, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        authorize(authorization, write=True)
        try:
            registry.set_enabled(tool_id, payload.enabled)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Aplicación desconocida") from exc
        return {"id": tool_id, "enabled": payload.enabled}

    @app.get("/api/tools/{tool_id}/health")
    def tool_health(tool_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        authorize(authorization)
        try:
            tool = registry.get(tool_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Aplicación desconocida") from exc
        if not registry.enabled(tool_id):
            raise HTTPException(status_code=409, detail="Habilite la aplicación antes de consultarla")
        return {"id": tool_id, **probe(tool)}

    @app.post("/api/tools/{tool_id}/actions/{action_id}")
    def run_action(tool_id: str, action_id: str, payload: ActionPayload,
                   authorization: str | None = Header(default=None)) -> dict[str, Any]:
        authorize(authorization, write=True)
        if not registry.enabled(tool_id):
            raise HTTPException(status_code=409, detail="Habilite la aplicación antes de operarla")
        try:
            action = registry.action(tool_id, action_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Aplicación o acción desconocida") from exc
        if action.confirm and not payload.confirmed:
            raise HTTPException(status_code=409, detail="La acción requiere confirmación")
        return {"id": tool_id, "action": action_id, **execute_action(action)}

    return app


DASHBOARD = """<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>MeshNet Control</title>
<style>:root{color-scheme:dark;--bg:#07111f;--card:#102238;--line:#29445e;--accent:#32d6a0;--muted:#9cb0c4}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at top,#123252,var(--bg) 48%);font:16px system-ui;color:#f5f8fb;min-height:100vh}header,main{max-width:1200px;margin:auto;padding:28px}header{display:flex;gap:16px}.logo{background:var(--accent);color:#05251b;border-radius:12px;padding:10px;font-weight:900}.sub{color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(350px,1fr));gap:18px}.card{background:linear-gradient(145deg,#142b44,var(--card));border:1px solid var(--line);border-radius:18px;padding:22px}.row{display:flex;justify-content:space-between;gap:15px}h1,h2,p{margin:0 0 10px}.badge{font-size:.78rem;padding:5px 9px;border-radius:20px;background:#23394d;color:var(--muted)}.badge.on{background:#124d3d;color:#7bf0c7}button{border:0;border-radius:9px;padding:9px 12px;font-weight:700;cursor:pointer;background:var(--accent);color:#05251b}button.secondary{background:#263f57;color:white}button.danger{background:#5b2d32;color:#ffd4d1}button:disabled{opacity:.45}.actions{display:flex;flex-wrap:wrap;gap:9px;margin-top:16px}.result{margin-top:15px;color:var(--muted);font:13px ui-monospace;white-space:pre-wrap;overflow:auto;max-height:320px;min-height:22px}</style></head>
<body><header><div class="logo">MN</div><div><h1>MeshNet Control</h1><div class="sub">Operación de aplicaciones independientes</div></div></header><main><div id="tools" class="grid"></div></main><script>
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));let token=sessionStorage.getItem('token')||'';if(!token)token=prompt('Token de ControlPanel')||'';sessionStorage.setItem('token',token);const headers={'Content-Type':'application/json','Authorization':'Bearer '+token};
async function load(){const r=await fetch('/api/tools',{headers}),d=await r.json();if(!r.ok)throw Error(d.detail);document.querySelector('#tools').innerHTML=d.tools.map(t=>`<article class="card"><div class="row"><h2>${esc(t.name)}</h2><span class="badge ${t.enabled?'on':''}">${t.enabled?'HABILITADA':'DESHABILITADA'}</span></div><p class="sub">${esc(t.description)}</p><div class="actions"><button onclick="toggle('${t.id}',${!t.enabled})">${t.enabled?'Deshabilitar':'Habilitar'}</button><button class="secondary" ${t.enabled?'':'disabled'} onclick="health('${t.id}')">Salud HTTP</button></div><div class="actions">${t.actions.map(a=>`<button class="${a.mutating?'danger':'secondary'}" ${t.enabled?'':'disabled'} onclick="run('${t.id}','${a.id}',${a.confirm},'${esc(a.name)}')">${esc(a.name)}</button>`).join('')}</div><div class="result" id="r-${t.id}"></div></article>`).join('')}
async function toggle(id,enabled){await fetch(`/api/tools/${id}/enabled`,{method:'PUT',headers,body:JSON.stringify({enabled})});load()}async function health(id){show(id,'Comprobando…');showResponse(id,await fetch(`/api/tools/${id}/health`,{headers}))}async function run(id,a,needs,name){if(needs&&!confirm(`¿Ejecutar “${name}”?`))return;show(id,'Ejecutando…');showResponse(id,await fetch(`/api/tools/${id}/actions/${a}`,{method:'POST',headers,body:JSON.stringify({confirmed:true})}))}async function showResponse(id,r){const d=await r.json();show(id,JSON.stringify(d.data??d.details??d,null,2)+(d.stderr?'\\n'+d.stderr:'')+(d.stdout&&!d.data?'\\n'+d.stdout:''))}function show(id,text){document.querySelector('#r-'+id).textContent=text}load().catch(e=>document.querySelector('#tools').textContent='No se pudo cargar: '+e.message);
</script></body></html>"""


app = create_app()
