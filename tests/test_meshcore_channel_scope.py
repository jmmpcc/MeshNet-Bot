#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regresión del flood scope por channel_idx en MeshCore."""
from __future__ import annotations

import asyncio
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "source"
HELPER = SOURCE / "meshcore_channel_scope.py"
BROKER_LAUNCHER = SOURCE / "Meshtastic_Broker_ChannelGateway.py"
BOT_LAUNCHER = SOURCE / "Telegram_Bot_ChannelGateway_RxHeader.py"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from meshcore_channel_scope import (
    annotate_meshcore_scope,
    configured_scope_for_channel,
    install_meshcore_channel_scope_runtime,
    parse_meshcore_channel_scope_map,
)


@dataclass
class _Type:
    name: str
    value: str


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
            return _Event(_Type("ERROR", "command_error"), "bad scope")
        return _Event(_Type("OK", "command_ok"))

    async def send_chan_msg(self, chan, msg, timestamp=None):
        self.calls.append(("send", chan, msg, timestamp))
        return _Event(_Type("OK", "command_ok"))


class _MC:
    def __init__(self, fail_scope: bool = False):
        self.commands = _Commands(fail_scope=fail_scope)


class _MeshCoreFactory:
    @classmethod
    async def create_serial(cls, *args, **kwargs):
        return _MC()

    @classmethod
    async def create_tcp(cls, *args, **kwargs):
        return _MC()

    @classmethod
    async def create_ble(cls, *args, **kwargs):
        return _MC()


def test_sources_parse() -> None:
    for path in (HELPER, BROKER_LAUNCHER, BOT_LAUNCHER):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_parser_and_lookup_are_backward_safe() -> None:
    env = {"MESHCORE_CHANNEL_SCOPE_MAP": "5:#zaragoza, 2=#huesca, bad, -1:#no, 8:*"}
    assert parse_meshcore_channel_scope_map(env["MESHCORE_CHANNEL_SCOPE_MAP"]) == {
        5: "#zaragoza",
        2: "#huesca",
        8: "*",
    }
    assert configured_scope_for_channel(5, env) == "#zaragoza"
    assert configured_scope_for_channel(3, env) is None


def test_runtime_applies_scope_immediately_before_channel_tx() -> None:
    class Factory(_MeshCoreFactory):
        pass

    env = {"MESHCORE_CHANNEL_SCOPE_MAP": "5:#zaragoza"}
    assert install_meshcore_channel_scope_runtime(Factory, env=env) is True
    mc = asyncio.run(Factory.create_serial("/dev/test"))
    asyncio.run(mc.commands.send_chan_msg(5, "hola", 123))
    assert mc.commands.calls == [
        ("scope", "#zaragoza"),
        ("send", 5, "hola", 123),
    ]


def test_runtime_resets_override_for_unmapped_channel() -> None:
    class Factory(_MeshCoreFactory):
        pass

    env = {"MESHCORE_CHANNEL_SCOPE_MAP": "5:#zaragoza"}
    install_meshcore_channel_scope_runtime(Factory, env=env)
    mc = asyncio.run(Factory.create_serial("/dev/test"))
    asyncio.run(mc.commands.send_chan_msg(3, "legacy"))
    assert mc.commands.calls[:2] == [
        ("scope", "0"),
        ("send", 3, "legacy", None),
    ]


def test_scope_error_blocks_radio_send() -> None:
    class Factory:
        @classmethod
        async def create_serial(cls, *args, **kwargs):
            return _MC(fail_scope=True)

    env = {"MESHCORE_CHANNEL_SCOPE_MAP": "5:#zaragoza"}
    install_meshcore_channel_scope_runtime(Factory, env=env)
    mc = asyncio.run(Factory.create_serial("/dev/test"))
    result = asyncio.run(mc.commands.send_chan_msg(5, "no debe salir"))
    assert result.type.name == "ERROR"
    assert mc.commands.calls == [("scope", "#zaragoza")]


def test_missing_scope_api_fails_closed() -> None:
    class CommandsWithoutScope:
        async def send_chan_msg(self, chan, msg, timestamp=None):
            raise AssertionError("send_chan_msg no debe quedar operativo sin set_flood_scope")

    class Factory:
        @classmethod
        async def create_serial(cls, *args, **kwargs):
            mc = type("MC", (), {})()
            mc.commands = CommandsWithoutScope()
            return mc

    env = {"MESHCORE_CHANNEL_SCOPE_MAP": "5:#zaragoza"}
    install_meshcore_channel_scope_runtime(Factory, env=env)
    with pytest.raises(RuntimeError, match="meshcore_set_flood_scope_unavailable"):
        asyncio.run(Factory.create_serial("/dev/test"))


def test_visual_rx_says_configured_not_received_scope() -> None:
    env = {"MESHCORE_CHANNEL_SCOPE_MAP": "5:#zaragoza"}
    text = "📩 3UTB (meshcore) (MeshCore canal mc:5 (Red-Mesh)):\nhola"
    result = annotate_meshcore_scope(text, env=env)
    assert result == (
        "📩 3UTB (meshcore) (MeshCore canal mc:5 (Red-Mesh) · "
        "scope canal configurado: #zaragoza):\nhola"
    )
    assert annotate_meshcore_scope(result, env=env) == result


def test_visual_tx_reports_scope_used_by_runtime_map() -> None:
    env = {"MESHCORE_CHANNEL_SCOPE_MAP": "5:#zaragoza"}
    text = (
        "Envío MeshCore\n"
        "Transporte: <b>MESH</b>\n"
        "Malla MeshCore → Canal (channel_idx): <b>5</b>\n"
        "Resultado MeshCore: <b>OK</b>"
    )
    result = annotate_meshcore_scope(text, env=env)
    assert "Scope TX: <b>#zaragoza</b>" in result
    assert result.index("Scope TX:") < result.index("Resultado MeshCore:")


def test_empty_map_keeps_current_runtime_untouched() -> None:
    class Factory(_MeshCoreFactory):
        pass

    assert install_meshcore_channel_scope_runtime(Factory, env={}) is False
    mc = asyncio.run(Factory.create_serial("/dev/test"))
    asyncio.run(mc.commands.send_chan_msg(5, "legacy"))
    assert mc.commands.calls == [("send", 5, "legacy", None)]
