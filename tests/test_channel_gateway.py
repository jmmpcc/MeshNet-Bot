from __future__ import annotations

import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "source"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

try:
    import pubsub  # noqa: F401
except ModuleNotFoundError:
    pubsub_mod = types.ModuleType("pubsub")

    class _Pub:
        def __init__(self):
            self.subscriptions = []

        def subscribe(self, fn, topic):
            self.subscriptions.append((fn, topic))

    pubsub_mod.pub = _Pub()
    sys.modules["pubsub"] = pubsub_mod

import channel_gateway as cg


class FakeQueue:
    def __init__(self):
        self.items = []

    def offer(self, payload, coalesce=False):
        self.items.append((payload, coalesce))


class FakeIface:
    myInfo = {"my_node_num": 0x12345678}

    def __init__(self):
        self.sent = []

    def sendText(self, *args, **kwargs):
        self.sent.append((args, kwargs))


class FakeMeshCoreEngine:
    enable = True

    def __init__(self):
        self.sent = []

    def enqueue_send_channel(self, channel_idx: int, text: str):
        self.sent.append((channel_idx, text))
        return f"tx-{len(self.sent)}"


def _packet(ch: int, text: str, frm: str = "!87654321", to: str = "^all") -> dict:
    return {
        "fromId": frm,
        "toId": to,
        "channel": ch,
        "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": text},
    }


def _set_profile(monkeypatch, profile: str):
    monkeypatch.setenv("RADIO_PROFILE", profile)
    if profile == "meshcore_only":
        monkeypatch.setenv("MESHCORE_ENABLE", "1")
        monkeypatch.setenv("MESHCORE_MODE", "tcp")
        monkeypatch.setenv("MESHCORE_TCP_HOST", "127.0.0.1")
    else:
        monkeypatch.setenv("MESHCORE_ENABLE", "1")
        monkeypatch.setenv("MESHCORE_MODE", "tcp")
        monkeypatch.setenv("MESHCORE_TCP_HOST", "127.0.0.1")
        monkeypatch.setenv("MESHTASTIC_HOST", "127.0.0.1")


def test_parse_rules_are_transport_scoped():
    assert cg._parse_rule_map("0:2,2:0,4:4,bad", "meshcore") == {
        ("meshcore", 0, 2),
        ("meshcore", 2, 0),
    }


def test_meshtastic_forward_uses_broker_sendq_and_suppresses_echo(tmp_path, monkeypatch):
    _set_profile(monkeypatch, "meshtastic_a_meshcore_embedded_b")
    mgr = cg.ChannelGatewayManager(tmp_path / "state.json")
    mgr.set_enabled(True)
    mgr.add_rule("meshtastic", 0, 2, both=True)
    iface = FakeIface()

    q = FakeQueue()
    main_mod = sys.modules["__main__"]
    old = getattr(main_mod, "SENDQ", None)
    main_mod.SENDQ = q
    try:
        assert mgr.handle_meshtastic_packet(_packet(0, "Hola"), iface) == 1
        payload = q.items[0][0]
        assert payload["channel"] == 2
        assert payload["origin"] == "channel_gateway"
        assert payload["no_bridge"] is True
        assert payload["meta"]["transport"] == "meshtastic"

        assert mgr.handle_meshtastic_packet(
            _packet(2, "Hola", frm="!12345678"), iface
        ) == 0
        assert len(q.items) == 1
        assert mgr.status()["stats"]["echo_suppressed"] == 1
    finally:
        if old is None:
            delattr(main_mod, "SENDQ")
        else:
            main_mod.SENDQ = old


def test_meshcore_forward_reuses_embedded_engine_and_suppresses_echo(tmp_path, monkeypatch):
    _set_profile(monkeypatch, "meshcore_only")
    mgr = cg.ChannelGatewayManager(tmp_path / "state.json")
    mgr.set_enabled(True)
    mgr.add_rule("meshcore", 0, 2, both=True)

    main_mod = sys.modules["__main__"]
    old = getattr(main_mod, "MESHCORE_ENGINE", None)
    engine = FakeMeshCoreEngine()
    main_mod.MESHCORE_ENGINE = engine
    try:
        assert mgr.handle_meshcore_message({
            "channel_idx": 0,
            "text": "Prueba MC",
            "pubkey_prefix": "abcd1234",
        }) == 1
        assert engine.sent == [(2, "Prueba MC")]

        # Eco de la TX del gateway en CH2: no debe activar 2 -> 0.
        assert mgr.handle_meshcore_message({
            "channel_idx": 2,
            "text": "Prueba MC",
            "pubkey_prefix": "local",
        }) == 0
        assert engine.sent == [(2, "Prueba MC")]
        assert mgr.status()["stats"]["echo_suppressed"] == 1
    finally:
        if old is None:
            delattr(main_mod, "MESHCORE_ENGINE")
        else:
            main_mod.MESHCORE_ENGINE = old


def test_meshcore_only_rejects_meshtastic_rule(tmp_path, monkeypatch):
    _set_profile(monkeypatch, "meshcore_only")
    mgr = cg.ChannelGatewayManager(tmp_path / "state.json")
    try:
        mgr.add_rule("meshtastic", 0, 2)
    except ValueError as exc:
        assert "no permitido" in str(exc)
    else:
        raise AssertionError("Meshtastic no debe aceptarse en meshcore_only")


def test_combined_profile_keeps_rules_separated_by_transport(tmp_path, monkeypatch):
    _set_profile(monkeypatch, "meshtastic_a_meshcore_embedded_b")
    mgr = cg.ChannelGatewayManager(tmp_path / "state.json")
    mgr.add_rule("meshtastic", 0, 2)
    mgr.add_rule("meshcore", 0, 1)
    status = mgr.status()
    assert {
        (item["transport"], item["source"], item["destination"])
        for item in status["rules"]
    } == {
        ("meshtastic", 0, 2),
        ("meshcore", 0, 1),
    }
    assert status["active_rule_count"] == 2


def test_v7055_state_migrates_only_when_profile_is_unambiguous(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "enabled": True,
        "rules": [{"source": 0, "destination": 2, "enabled": True}],
    }), encoding="utf-8")

    _set_profile(monkeypatch, "meshcore_only")
    mgr = cg.ChannelGatewayManager(path)
    assert ("meshcore", 0, 2) in mgr.rules

    path2 = tmp_path / "combined.json"
    path2.write_text(json.dumps({
        "enabled": True,
        "rules": [{"source": 0, "destination": 2, "enabled": True}],
    }), encoding="utf-8")
    _set_profile(monkeypatch, "meshtastic_a_meshcore_embedded_b")
    combined = cg.ChannelGatewayManager(path2)
    status = combined.status()
    assert status["rules"][0]["transport"] == ""
    assert status["rules"][0]["active_for_profile"] is False


def test_direct_meshtastic_message_is_not_forwarded_by_default(tmp_path, monkeypatch):
    _set_profile(monkeypatch, "meshtastic_a_meshcore_embedded_b")
    mgr = cg.ChannelGatewayManager(tmp_path / "state.json")
    mgr.set_enabled(True)
    mgr.add_rule("meshtastic", 0, 2)
    assert mgr.handle_packet(_packet(0, "privado", to="!11111111"), FakeIface()) == 0
    assert mgr.status()["stats"]["ignored_direct"] == 1


def test_control_rpc_requires_transport_and_persists(tmp_path, monkeypatch):
    _set_profile(monkeypatch, "meshcore_only")
    mgr = cg.ChannelGatewayManager(tmp_path / "state.json")
    server = cg.ChannelGatewayControlServer(mgr)

    result = server._handle_request({
        "cmd": "CHANNEL_GATEWAY_ADD",
        "params": {
            "transport": "meshcore",
            "source": 0,
            "destination": 2,
            "both": True,
        },
    })
    assert result["ok"] is True
    assert result["rule_count"] == 2
    assert result["active_rule_count"] == 2

    again = cg.ChannelGatewayManager(tmp_path / "state.json")
    assert {
        (item["transport"], item["source"], item["destination"])
        for item in again.status()["rules"]
    } == {
        ("meshcore", 0, 2),
        ("meshcore", 2, 0),
    }
