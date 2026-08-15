from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "source"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

# El entorno normal instala pypubsub. Este stub permite que la prueba unitaria
# pura siga siendo ejecutable también en entornos mínimos de CI/desarrollo.
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


def _packet(ch: int, text: str, frm: str = "!87654321", to: str = "^all") -> dict:
    return {
        "fromId": frm,
        "toId": to,
        "channel": ch,
        "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": text},
    }


def test_parse_rules_discards_invalid_and_same_channel():
    assert cg._parse_rule_map("0:2,2:0,4:4,bad") == {(0, 2), (2, 0)}


def test_forward_uses_broker_sendq_and_suppresses_bidirectional_echo(tmp_path):
    mgr = cg.ChannelGatewayManager(tmp_path / "state.json")
    mgr.set_enabled(True)
    mgr.add_rule(0, 2, both=True)
    iface = FakeIface()

    q = FakeQueue()
    main_mod = sys.modules["__main__"]
    old = getattr(main_mod, "SENDQ", None)
    main_mod.SENDQ = q
    try:
        assert mgr.handle_packet(_packet(0, "Hola"), iface) == 1
        assert len(q.items) == 1
        payload = q.items[0][0]
        assert payload["channel"] == 2
        assert payload["origin"] == "channel_gateway"
        assert payload["no_bridge"] is True
        assert payload["meta"]["source_channel"] == 0
        assert payload["meta"]["destination_channel"] == 2

        # Eco de la TX local del gateway. No debe activar la regla 2 -> 0.
        assert mgr.handle_packet(_packet(2, "Hola", frm="!12345678"), iface) == 0
        assert len(q.items) == 1
        assert mgr.status()["stats"]["echo_suppressed"] == 1
    finally:
        if old is None:
            delattr(main_mod, "SENDQ")
        else:
            main_mod.SENDQ = old


def test_direct_message_is_not_forwarded_by_default(tmp_path):
    mgr = cg.ChannelGatewayManager(tmp_path / "state.json")
    mgr.set_enabled(True)
    mgr.add_rule(0, 2)

    assert mgr.handle_packet(_packet(0, "privado", to="!11111111"), FakeIface()) == 0
    assert mgr.status()["stats"]["ignored_direct"] == 1


def test_state_is_persistent(tmp_path):
    path = tmp_path / "state.json"
    mgr = cg.ChannelGatewayManager(path)
    mgr.set_enabled(True)
    mgr.add_rule(1, 3, both=True)

    again = cg.ChannelGatewayManager(path)
    status = again.status()
    assert status["enabled"] is True
    assert {
        (item["source"], item["destination"])
        for item in status["rules"]
    } == {(1, 3), (3, 1)}


def test_control_commands_change_runtime_and_persistent_state(tmp_path):
    mgr = cg.ChannelGatewayManager(tmp_path / "state.json")
    server = cg.ChannelGatewayControlServer(mgr)

    result = server._handle_request({
        "cmd": "CHANNEL_GATEWAY_ADD",
        "params": {"source": 0, "destination": 2, "both": True},
    })
    assert result["ok"] is True
    assert result["rule_count"] == 2

    assert server._handle_request({"cmd": "CHANNEL_GATEWAY_ON"})["enabled"] is True
    assert server._handle_request({"cmd": "CHANNEL_GATEWAY_OFF"})["enabled"] is False
