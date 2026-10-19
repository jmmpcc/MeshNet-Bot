"""Pruebas offline de trazas MeshCore explícitas y SNR por salto.

Ejecución: python -m pytest -q tests/test_meshcore_manual_trace_snr.py
No se abre ningún puerto ni conexión RF. La función productiva se extrae
del AST para evitar iniciar el broker y se ejercita con un motor simulado.
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
import os
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


BROKER = Path(__file__).resolve().parents[1] / "source" / "Meshtastic_Broker.py"
BOT = Path(__file__).resolve().parents[1] / "source" / "Telegram_Bot_Broker.py"


def _trace_function():
    """Extrae solo trace_contact del broker existente, sin importar servicios."""
    tree = ast.parse(BROKER.read_text(encoding="utf-8"))
    bridge = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "MeshCoreEmbeddedBridge"
    )
    method = next(
        node for node in bridge.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "trace_contact"
    )
    namespace = {
        "asyncio": asyncio,
        "os": os,
        "re": re,
        "time": time,
        "_MCEventType": SimpleNamespace(
            ERROR="ERROR", MSG_SENT="MSG_SENT", TRACE_DATA="TRACE_DATA",
            PATH_RESPONSE="PATH_RESPONSE",
        ),
    }
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(BROKER), "exec"), namespace)
    return namespace["trace_contact"]


@contextlib.contextmanager
def _active_loop():
    """Crea y cierra un bucle de pruebas ajeno a cualquier equipo de radio."""
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def run():
        asyncio.set_event_loop(loop)
        ready.set()
        loop.run_forever()
        loop.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(2)
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=3)
        assert not thread.is_alive()


class _RadioCommands:
    """Captura las tramas TRACE sin transmitir por RF."""

    def __init__(self, payload):
        self.calls = []
        self.payload = payload

    async def send_trace(self, auth_code, tag, flags, path):
        self.calls.append((auth_code, tag, flags, bytes(path)))
        return SimpleNamespace(type="MSG_SENT", payload={})


class _Radio:
    def __init__(self, payload):
        self.commands = _RadioCommands(payload)

    def get_contact_by_key_prefix(self, prefix):
        if prefix == "cfe25e30b3ec":
            return {"name": "RPT_URZ_SAGASTA", "public_key": prefix, "out_path": ""}
        return None

    async def wait_for_event(self, event_type, **kwargs):
        return SimpleNamespace(
            type=event_type,
            payload=self.commands.payload,
        )


class _Engine:
    """Sustituye los campos del motor que necesita trace_contact."""

    def __init__(self, loop, payload, cache):
        self._mc = _Radio(payload)
        self._loop = loop
        self._connected = True
        self._mc_path_prefix_cache = cache

    def _meshcore_remember_contact(self, contact):
        return None

    @staticmethod
    def _meshcore_path_bytes(value):
        if isinstance(value, bytes):
            return value
        if isinstance(value, str):
            return bytes.fromhex(value)
        return b""

    @staticmethod
    def _meshcore_path_geometry(value, hash_mode=None, path_len=None):
        value = _Engine._meshcore_path_bytes(value)
        width = int(hash_mode or 0) + 1
        return value, width, len(value) // width

    @staticmethod
    def _meshcore_resolve_trace_hops(path_hashes, path_snrs, hash_width=1):
        raw = _Engine._meshcore_path_bytes(path_hashes)
        return [
            {"hash": raw[i:i + hash_width].hex(), "snr": path_snrs[i // hash_width]}
            for i in range(0, len(raw), hash_width)
        ]


def test_manual_trace_preserves_snr_positions_when_one_is_missing():
    """El SNR no disponible no debe asignarse al siguiente repetidor."""
    payload = {
        "path": [
            {"hash": "aa", "snr": None},
            {"hash": "bb", "snr": 4.5},
            {"snr": -1.25},
        ],
    }
    cache = {"aa": [{"name": "A"}], "bb": [{"name": "B"}]}
    with _active_loop() as loop:
        engine = _Engine(loop, payload, cache)
        result = _trace_function()(
            engine, "cfe25e30b3ec",
            manual_path_hex="aabb", manual_hash_width=1, timeout=5,
        )
    assert result["ok"] is True
    assert result["manual_path_used"] is True
    assert result["discovery_used"] is False
    assert result["path_hex"] == "aabb"
    assert result["path_snrs"] == [None, 4.5, -1.25]
    assert result["hops"][0]["snr"] is None
    assert result["hops"][1]["snr"] == 4.5
    assert len(engine._mc.commands.calls) == 1
    assert engine._mc.commands.calls[0][3] == bytes.fromhex("aabb")


@pytest.mark.parametrize("route,width", [
    ("xy", 1), ("abc", 1), ("aabb", 3), ("", 1),
    ("aa" * 9, 1), ("aa", 2),
])
def test_invalid_manual_trace_rejected_without_tx(route, width):
    """Las rutas inválidas deben fallar antes de enviar TRACE_DATA."""
    with _active_loop() as loop:
        engine = _Engine(loop, {}, {"aa": [{"name": "A"}]})
        with pytest.raises(ValueError):
            _trace_function()(
                engine, "cfe25e30b3ec",
                manual_path_hex=route, manual_hash_width=width, timeout=5,
            )
        assert engine._mc.commands.calls == []


def test_ambiguous_hop_rejected_without_tx():
    """No seleccionar arbitrariamente un contacto si el hash colisiona."""
    with _active_loop() as loop:
        engine = _Engine(loop, {}, {"f6": [{"name": "A"}, {"name": "B"}]})
        with pytest.raises(RuntimeError, match="unresolved_or_ambiguous"):
            _trace_function()(
                engine, "cfe25e30b3ec",
                manual_path_hex="f6", manual_hash_width=1, timeout=5,
            )
        assert engine._mc.commands.calls == []


def test_telegram_reuses_broker_rpc_and_restricts_admin():
    """Contrato estructural para no duplicar sesiones del dispositivo."""
    source = BOT.read_text(encoding="utf-8")
    tree = ast.parse(source)
    handler = next(
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "traza_mc_cmd"
    )
    segment = ast.get_source_segment(source, handler)
    assert "is_admin(user.id)" in segment
    assert "asyncio.to_thread(" in segment
    assert '"MESHCORE_TRACE_PATH"' in segment
    assert 'CommandHandler("traza_mc", traza_mc_cmd)' in source
