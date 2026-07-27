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
    client = TestClient(web_admin.create_app(item, token="secret"))
    headers = {"Authorization": "Bearer secret"}
    assert client.get("/api/tools", headers=headers).json()["tools"][0]["enabled"] is False

    response = client.put("/api/tools/demo/enabled", json={"enabled": True}, headers=headers)

    assert response.status_code == 200
    assert response.json() == {"id": "demo", "enabled": True}


def test_api_blocks_health_for_disabled_tool(tmp_path):
    client = TestClient(web_admin.create_app(registry(tmp_path)))
    response = client.get("/api/tools/demo/health")
    assert response.status_code == 409


def test_api_rejects_writes_without_configured_token(tmp_path):
    client = TestClient(web_admin.create_app(registry(tmp_path), token=""))
    response = client.put("/api/tools/demo/enabled", json={"enabled": True})
    assert response.status_code == 503


def test_manifest_loader_discovers_future_tools(tmp_path):
    (tmp_path / "future.json").write_text(json.dumps({
        "id": "future_tool", "name": "Future", "url": "http://127.0.0.1:9999",
        "actions": [{"id": "inspect", "name": "Inspect", "kind": "command",
                     "argv": ["program", "status"]}],
    }))
    tools = web_admin.load_tools(tmp_path)
    assert tools[0].id == "future_tool"
    assert tools[0].actions[0].argv == ("program", "status")


def test_action_uses_only_server_side_allowlist(tmp_path, monkeypatch):
    action = web_admin.ActionDefinition("inspect", "Inspect", "command", ("program", "status"))
    tools = (web_admin.ToolDefinition("demo", "Demo", "Test", "http://demo", actions=(action,)),)
    item = web_admin.ToolRegistry(tmp_path / "state.json", tools)
    item.set_enabled("demo", True)
    seen = {}

    class Result:
        returncode, stdout, stderr = 0, '{"ok": true}', ""

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return Result()

    monkeypatch.setattr(web_admin.subprocess, "run", fake_run)
    client = TestClient(web_admin.create_app(item, token="secret"))
    response = client.post(
        "/api/tools/demo/actions/inspect",
        json={"confirmed": False, "argv": ["evil"]},
        headers={"Authorization": "Bearer secret"},
    )
    assert response.status_code == 200
    assert seen["argv"] == ["program", "status"]


def test_confirmed_action_requires_confirmation(tmp_path, monkeypatch):
    action = web_admin.ActionDefinition(
        "stop", "Stop", "systemd", unit="demo.service", operation="stop",
        mutating=True, confirm=True,
    )
    tools = (web_admin.ToolDefinition("demo", "Demo", "Test", "http://demo", actions=(action,)),)
    item = web_admin.ToolRegistry(tmp_path / "state.json", tools)
    item.set_enabled("demo", True)
    client = TestClient(web_admin.create_app(item, token="secret"))
    response = client.post(
        "/api/tools/demo/actions/stop", json={"confirmed": False},
        headers={"Authorization": "Bearer secret"},
    )
    assert response.status_code == 409
