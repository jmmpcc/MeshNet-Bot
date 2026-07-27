import json
from pathlib import Path
from urllib.error import URLError

from tools.ControlPanel import web_admin
from fastapi.testclient import TestClient


def registry(tmp_path: Path) -> web_admin.ToolRegistry:
    tools = (web_admin.ToolDefinition("demo", "Demo", "Servicio de prueba", "http://demo:1"),)
    return web_admin.ToolRegistry(tmp_path / "state.json", tools)


def test_registry_defaults_disabled_and_persists(tmp_path):
    first = registry(tmp_path)
    assert first.items()[0]["enabled"] is False

    first.set_enabled("demo", True)

    second = registry(tmp_path)
    assert second.enabled("demo") is True
    assert json.loads((tmp_path / "state.json").read_text())["enabled"] == {"demo": True}


def test_registry_rejects_unknown_tool(tmp_path):
    item = registry(tmp_path)
    try:
        item.set_enabled("not-allowlisted", True)
    except KeyError:
        pass
    else:
        raise AssertionError("se aceptó una aplicación fuera del registro")


def test_probe_reports_connection_errors(monkeypatch):
    def unavailable(*args, **kwargs):
        raise URLError("offline")

    monkeypatch.setattr(web_admin, "urlopen", unavailable)
    result = web_admin.probe(web_admin.DEFAULT_TOOLS[0])
    assert result["reachable"] is False
    assert "offline" in result["error"]


def test_dashboard_escapes_dynamic_values():
    assert "innerHTML=d.tools.map" in web_admin.DASHBOARD
    assert "const esc=" in web_admin.DASHBOARD


def test_api_can_enable_allowlisted_tool(tmp_path):
    item = registry(tmp_path)
    client = TestClient(web_admin.create_app(item))
    assert client.get("/api/tools").json()["tools"][0]["enabled"] is False

    response = client.put("/api/tools/demo/enabled", json={"enabled": True})

    assert response.status_code == 200
    assert response.json() == {"id": "demo", "enabled": True}


def test_api_blocks_health_for_disabled_tool(tmp_path):
    client = TestClient(web_admin.create_app(registry(tmp_path)))
    response = client.get("/api/tools/demo/health")
    assert response.status_code == 409
