"""Panel web de administración para las aplicaciones independientes de MeshNet."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel


@dataclass(frozen=True)
class ToolDefinition:
    id: str
    name: str
    description: str
    url: str
    health_path: str = "/health"


DEFAULT_TOOLS = (
    ToolDefinition(
        "farmacias_guardia",
        "Farmacias de guardia",
        "Consultas y avisos de farmacias de guardia.",
        os.getenv("FARMACIAS_PANEL_URL", "http://127.0.0.1:8788"),
    ),
    ToolDefinition(
        "emergencias_guardia",
        "Emergencias",
        "Incidencias oficiales, carreteras y emergencias normalizadas.",
        os.getenv("EMERGENCIAS_PANEL_URL", "http://127.0.0.1:8789"),
    ),
)


class ToolRegistry:
    """Registro allowlist con estado persistente; nunca ejecuta órdenes arbitrarias."""

    def __init__(self, path: Path, tools: tuple[ToolDefinition, ...] = DEFAULT_TOOLS):
        self.path = path
        self.tools = {tool.id: tool for tool in tools}
        self._lock = threading.Lock()
        self._enabled = self._load()

    def _load(self) -> dict[str, bool]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            values = raw.get("enabled", {})
            if not isinstance(values, dict):
                raise ValueError("enabled debe ser un objeto")
            return {key: bool(values.get(key, False)) for key in self.tools}
        except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
            return {key: False for key in self.tools}

    def set_enabled(self, tool_id: str, enabled: bool) -> None:
        if tool_id not in self.tools:
            raise KeyError(tool_id)
        with self._lock:
            self._enabled[tool_id] = enabled
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps({"enabled": self._enabled}, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            temporary.replace(self.path)

    def items(self) -> list[dict[str, Any]]:
        return [{**asdict(tool), "enabled": self._enabled[tool.id]} for tool in self.tools.values()]

    def get(self, tool_id: str) -> ToolDefinition:
        try:
            return self.tools[tool_id]
        except KeyError as exc:
            raise KeyError(tool_id) from exc

    def enabled(self, tool_id: str) -> bool:
        return self._enabled.get(tool_id, False)


class EnabledPayload(BaseModel):
    enabled: bool


def probe(tool: ToolDefinition, timeout: float = 2.0) -> dict[str, Any]:
    request = Request(tool.url.rstrip("/") + tool.health_path, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(64 * 1024)
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            return {"reachable": True, "status": response.status, "details": payload}
    except HTTPError as exc:
        return {"reachable": False, "status": exc.code, "error": "respuesta HTTP no válida"}
    except (URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        return {"reachable": False, "status": None, "error": str(exc)}


def create_app(registry: ToolRegistry | None = None) -> FastAPI:
    registry = registry or ToolRegistry(Path(os.getenv("CONTROLPANEL_STATE", str(Path(__file__).parent / "data" / "state.json"))))
    app = FastAPI(title="MeshNet Control", version="0.1.0")

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
            raise HTTPException(status_code=409, detail="Active la aplicación en el panel antes de consultarla")
        return {"id": tool_id, **probe(tool)}

    return app


app = create_app()


DASHBOARD = """<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MeshNet Control</title><style>
:root{color-scheme:dark;--bg:#07111f;--card:#102238;--line:#29445e;--accent:#32d6a0;--muted:#9cb0c4}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at top,#123252,var(--bg) 48%);font:16px system-ui;color:#f5f8fb;min-height:100vh}
header,main{max-width:1050px;margin:auto;padding:28px}header{display:flex;align-items:center;gap:16px}.logo{background:var(--accent);color:#05251b;border-radius:12px;padding:10px;font-weight:900}.sub{color:var(--muted)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));gap:18px}.card{background:linear-gradient(145deg,#142b44,var(--card));border:1px solid var(--line);border-radius:18px;padding:22px;box-shadow:0 15px 35px #0005}.row{display:flex;justify-content:space-between;gap:15px;align-items:center}h1,h2,p{margin:0 0 10px}.badge{font-size:.78rem;padding:5px 9px;border-radius:20px;background:#23394d;color:var(--muted)}.badge.on{background:#124d3d;color:#7bf0c7}
button{border:0;border-radius:9px;padding:10px 14px;font-weight:700;cursor:pointer;background:var(--accent);color:#05251b}button.secondary{background:#263f57;color:white}button:disabled{opacity:.45;cursor:not-allowed}.actions{display:flex;gap:9px;margin-top:20px}.result{margin-top:15px;color:var(--muted);font-size:.9rem;min-height:22px}footer{max-width:1050px;margin:auto;padding:28px;color:var(--muted);font-size:.85rem}
</style></head><body><header><div class="logo">MN</div><div><h1>MeshNet Control</h1><div class="sub">Aplicaciones independientes del ecosistema</div></div></header><main><div id="tools" class="grid"></div></main><footer>La activación habilita el acceso desde este panel; el proceso de cada aplicación se administra de forma independiente.</footer>
<script>
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function load(){const r=await fetch('/api/tools');const d=await r.json();document.querySelector('#tools').innerHTML=d.tools.map(t=>`<article class="card"><div class="row"><h2>${esc(t.name)}</h2><span class="badge ${t.enabled?'on':''}">${t.enabled?'ACTIVA':'INACTIVA'}</span></div><p class="sub">${esc(t.description)}</p><div class="actions"><button onclick="toggle('${t.id}',${!t.enabled})">${t.enabled?'Desactivar':'Activar'}</button><button class="secondary" ${t.enabled?'':'disabled'} onclick="health('${t.id}')">Comprobar</button></div><div class="result" id="r-${t.id}"></div></article>`).join('')}
async function toggle(id,enabled){await fetch(`/api/tools/${id}/enabled`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled})});load()}
async function health(id){const out=document.querySelector(`#r-${id}`);out.textContent='Comprobando…';const r=await fetch(`/api/tools/${id}/health`);const d=await r.json();out.textContent=r.ok?(d.reachable?'Servicio disponible':'Sin conexión: '+(d.error||d.status)):(d.detail||'Error')}
load().catch(()=>document.querySelector('#tools').textContent='No se pudo cargar el registro.');
</script></body></html>"""
