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
    assert "Token de ControlPanel" not in web_admin.DASHBOARD
    assert "JSON.stringify(d.data" not in web_admin.DASHBOARD


def test_dashboard_reserves_danger_style_for_confirmed_actions():
    assert "a.confirm?'danger':(a.mutating?'':'secondary')" in web_admin.DASHBOARD


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
    client = TestClient(web_admin.create_app(item))
    response = client.post(
        "/api/tools/demo/actions/inspect",
        json={"confirmed": False, "argv": ["evil"]},
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
    client = TestClient(web_admin.create_app(item))
    response = client.post(
        "/api/tools/demo/actions/stop", json={"confirmed": False},
    )
    assert response.status_code == 409


def test_env_channel_update_preserves_unrelated_values(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# configuración\nSECRET=keep-me\nFARMACIAS_MESHCORE_CHANNEL=1\n")

    web_admin.update_env_values(path, {
        "FARMACIAS_BROADCAST_TRANSPORT": "meshtastic",
        "FARMACIAS_MESHCORE_CHANNEL": "4",
        "FARMACIAS_MESHTASTIC_CHANNEL": "7",
    })

    text = path.read_text()
    assert "SECRET=keep-me" in text
    assert "FARMACIAS_MESHCORE_CHANNEL=4" in text
    assert web_admin.read_env_values(path, web_admin.CHANNEL_KEYS) == {
        "FARMACIAS_BROADCAST_TRANSPORT": "meshtastic",
        "FARMACIAS_MESHCORE_CHANNEL": "4",
        "FARMACIAS_MESHTASTIC_CHANNEL": "7",
    }


def test_pharmacy_channels_api_reads_and_updates_env(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("FARMACIAS_BROADCAST_TRANSPORT=auto\nFARMACIAS_MESHCORE_CHANNEL=2\n")
    monkeypatch.setattr(web_admin, "FARMACIAS_ENV_FILE", path)
    client = TestClient(web_admin.create_app(registry(tmp_path)))

    assert client.get("/api/farmacias/channels").json() == {
        "transport": "auto", "meshcore_channel": 2, "meshtastic_channel": -1,
    }
    response = client.put("/api/farmacias/channels", json={
        "transport": "meshcore", "meshcore_channel": 5, "meshtastic_channel": 3,
    })

    assert response.status_code == 200
    assert response.json()["restart_required"] is True
    assert "FARMACIAS_MESHCORE_CHANNEL=5" in path.read_text()


def test_pharmacy_channels_api_rejects_invalid_channel(tmp_path, monkeypatch):
    monkeypatch.setattr(web_admin, "FARMACIAS_ENV_FILE", tmp_path / ".env")
    client = TestClient(web_admin.create_app(registry(tmp_path)))
    response = client.put("/api/farmacias/channels", json={
        "transport": "meshcore", "meshcore_channel": 300, "meshtastic_channel": 1,
    })
    assert response.status_code == 422
