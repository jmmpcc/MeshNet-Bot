from __future__ import annotations

import asyncio
import ast
import json
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "source"
sys.path.insert(0, str(SOURCE))

import meshcore_channel_scope as scope_mod
from meshcore_channel_scope import (
    annotate_meshcore_scope,
    bot_tx_scope,
    extract_scope_modifier,
    normalize_scope,
)


@dataclass
class _Type:
    name: str
    value: str = ""


@dataclass
class _Event:
    type: _Type
    payload: object = None


class _Commands:
    def __init__(self, fail_scope: bool = False):
        self.calls = []
        self.fail_scope = fail_scope

    async def set_flood_scope(self, scope):
        self.calls.append(("scope", scope))
        if self.fail_scope:
            return _Event(_Type("ERROR", "command_error"), "scope error")
        return _Event(_Type("OK", "command_ok"))

    async def send_chan_msg(self, chan, msg, timestamp=None):
        self.calls.append(("send", chan, msg, timestamp))
        return _Event(_Type("OK", "command_ok"))


class _MC:
    def __init__(self, fail_scope=False):
        self.commands = _Commands(fail_scope)


class _Factory:
    @classmethod
    async def create_serial(cls, *args, **kwargs):
        return _MC()


def test_sources_parse() -> None:
    for path in SOURCE.glob("*.py"):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_scope_normalization() -> None:
    assert normalize_scope(None) is None
    assert normalize_scope("default") == "0"
    assert normalize_scope("0") == "0"
    assert normalize_scope("*") == "*"
    assert normalize_scope("unscoped") == "*"
    assert normalize_scope("zaragoza") == "#zaragoza"
    assert normalize_scope("#zaragoza") == "#zaragoza"


def test_scope_modifier_is_independent_of_channel_position() -> None:
    clean, scope, error = extract_scope_modifier(
        ["ambos", "ch5", "--scope", "#zaragoza", "aprs", "broadcast", "Hola"]
    )
    assert error is None
    assert scope == "#zaragoza"
    assert clean == ["ambos", "ch5", "aprs", "broadcast", "Hola"]

    clean, scope, error = extract_scope_modifier(["canal", "5", "Hola", "--scope=zgz"])
    assert error is None
    assert scope == "#zgz"
    assert clean == ["canal", "5", "Hola"]


def test_duplicate_or_missing_scope_is_rejected() -> None:
    _, _, error = extract_scope_modifier(["ch5", "--scope"])
    assert error
    _, _, error = extract_scope_modifier(["ch5", "--scope", "a", "--scope", "b", "Hola"])
    assert error


def test_tx_annotation_only_when_scope_context_exists() -> None:
    text = "Envío MeshCore\nTransporte: <b>MESH</b>\nResultado MeshCore: <b>OK</b>"
    assert annotate_meshcore_scope(text) == text
    with bot_tx_scope("#zaragoza"):
        result = annotate_meshcore_scope(text)
    assert "Scope TX: <b>#zaragoza</b>" in result
    assert result.index("Scope TX:") < result.index("Resultado MeshCore:")


def test_rx_is_not_falsely_annotated() -> None:
    text = "📩 3UTB (meshcore) (MeshCore canal mc:5 (Red-Mesh)):\nhola"
    with bot_tx_scope("#zaragoza"):
        assert annotate_meshcore_scope(text) == text


def test_send_wrapper_applies_scope_and_fail_closed() -> None:
    mc = _MC()
    scope_mod._install_on_meshcore_instance(mc)
    token = scope_mod._ACTIVE_TX_SCOPE.set("#zaragoza")
    try:
        asyncio.run(mc.commands.send_chan_msg(5, "hola"))
    finally:
        scope_mod._ACTIVE_TX_SCOPE.reset(token)
    assert mc.commands.calls == [
        ("scope", "#zaragoza"),
        ("send", 5, "hola", None),
    ]

    mc2 = _MC(fail_scope=True)
    scope_mod._install_on_meshcore_instance(mc2)
    token = scope_mod._ACTIVE_TX_SCOPE.set("#zaragoza")
    try:
        result = asyncio.run(mc2.commands.send_chan_msg(5, "no sale"))
    finally:
        scope_mod._ACTIVE_TX_SCOPE.reset(token)
    assert result.type.name == "ERROR"
    assert mc2.commands.calls == [("scope", "#zaragoza")]


def test_next_unscoped_tx_restores_default_after_explicit_scope() -> None:
    mc = _MC()
    scope_mod._install_on_meshcore_instance(mc)
    token = scope_mod._ACTIVE_TX_SCOPE.set("#zaragoza")
    try:
        asyncio.run(mc.commands.send_chan_msg(5, "regional"))
    finally:
        scope_mod._ACTIVE_TX_SCOPE.reset(token)

    token = scope_mod._ACTIVE_TX_SCOPE.set(None)
    try:
        asyncio.run(mc.commands.send_chan_msg(5, "normal"))
    finally:
        scope_mod._ACTIVE_TX_SCOPE.reset(token)

    assert mc.commands.calls == [
        ("scope", "#zaragoza"),
        ("send", 5, "regional", None),
        ("scope", "0"),
        ("send", 5, "normal", None),
    ]


def test_bridge_enqueue_persists_scope_in_dst_and_normalizer_restores_it() -> None:
    # Replica solo las dependencias que usa el enqueue estable del broker.
    def split_parts(msg, max_b):
        return [msg] if len(msg.encode()) <= max_b else [msg[:max_b], msg[max_b:]]

    def max_bytes():
        return 140

    def original_enqueue(self, *args, **kwargs):  # pragma: no cover - reemplazado
        raise AssertionError("debe reemplazarse")

    original_enqueue.__globals__["_split_meshcore_send_parts"] = split_parts
    original_enqueue.__globals__["_safe_meshcore_max_text_bytes"] = max_bytes

    def normalize(self, item, default_max_retries=None):
        return tuple(item)

    class Bridge:
        enqueue_send_channel = original_enqueue
        _normalize_tx_spool_item = normalize

        def __init__(self):
            self.enable = True
            self._retry_spool_lock = threading.Lock()
            self._connected = False
            self._loop = None
            self._tx_q = None
            self._tx_max_retries = 3
            self.log_enqueue = False
            self.spool = []

        def _spool_append(self, item, why=""):
            self.spool.append((item, why))

    scope_mod._patch_bridge_class(Bridge)
    scope_mod._REQUEST_STATE.scope_present = True
    scope_mod._REQUEST_STATE.scope = "#zaragoza"
    scope_mod._REQUEST_STATE.channel_idx = 5
    scope_mod._REQUEST_STATE.captured_at = time.monotonic()

    bridge = Bridge()
    tx_id = bridge.enqueue_send_channel(5, "hola")
    assert tx_id
    item, why = bridge.spool[0]
    assert why == "enqueue_chan_deferred"
    assert item[0] == {"kind": "chan", "channel_idx": 5, "scope": "#zaragoza"}

    normalized = bridge._normalize_tx_spool_item(item)
    assert normalized[0]["scope"] == "#zaragoza"
    assert scope_mod._ACTIVE_TX_SCOPE.get() == "#zaragoza"


def test_request_capture_reads_scope_only_for_channel_send() -> None:
    original = json.loads
    try:
        scope_mod._install_request_scope_capture()
        json.loads('{"cmd":"MESHCORE_SEND","params":{"kind":"chan","channel_idx":5,"text":"hola","scope":"zaragoza"}}')
        assert scope_mod._REQUEST_STATE.scope_present is True
        assert scope_mod._REQUEST_STATE.scope == "#zaragoza"
        assert scope_mod._REQUEST_STATE.channel_idx == 5

        json.loads('{"cmd":"MESHCORE_SEND","params":{"kind":"contact","contact_prefix":"abc","text":"hola","scope":"zaragoza"}}')
        assert scope_mod._REQUEST_STATE.scope_present is False
    finally:
        json.loads = original


def test_stale_or_wrong_channel_scope_is_not_consumed() -> None:
    scope_mod._REQUEST_STATE.scope_present = True
    scope_mod._REQUEST_STATE.scope = "#zaragoza"
    scope_mod._REQUEST_STATE.channel_idx = 5
    scope_mod._REQUEST_STATE.captured_at = time.monotonic()
    assert scope_mod._consume_pending_request_scope(6) == (False, None)

    scope_mod._REQUEST_STATE.scope_present = True
    scope_mod._REQUEST_STATE.scope = "#zaragoza"
    scope_mod._REQUEST_STATE.channel_idx = 5
    scope_mod._REQUEST_STATE.captured_at = time.monotonic() - 3.0
    assert scope_mod._consume_pending_request_scope(5) == (False, None)


def test_runtime_constructor_patch() -> None:
    class Factory(_Factory):
        pass

    assert scope_mod._install_meshcore_constructor_wrappers(Factory) is True
    mc = asyncio.run(Factory.create_serial("/dev/test"))
    token = scope_mod._ACTIVE_TX_SCOPE.set("#zgz")
    try:
        asyncio.run(mc.commands.send_chan_msg(1, "hola"))
    finally:
        scope_mod._ACTIVE_TX_SCOPE.reset(token)
    assert mc.commands.calls[:2] == [("scope", "#zgz"), ("send", 1, "hola", None)]
