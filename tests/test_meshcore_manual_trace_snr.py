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


@pytest.mark.parametrize("payload,expected_error", [
    ({"path": [{"hash": "aabb", "snr": 4.5}]}, "hash_width_mismatch"),
    ({"path": [{"snr": 4.5}]}, "without_hops"),
    ({"path": []}, "without_hops"),
    ({"path": [{"hash": "aa", "snr": 1}, {"snr": 2}, {"hash": "bb", "snr": 3}]}, "hash_after_destination"),
    ({"path": [{"hash": "aa", "snr": 1}, {"snr": 2}, {"snr": 3}]}, "multiple_destinations"),
    ({"path": [{"hash": "xx", "snr": 1}]}, "hash_width_mismatch"),
])
def test_trace_rejects_malformed_response_without_inventing_hops(payload, expected_error):
    """Una respuesta incompleta o ambigua no debe convertirse en una medición válida."""
    with _active_loop() as loop:
        engine = _Engine(loop, payload, {"aa": [{"name": "A"}]})
        with pytest.raises(RuntimeError, match=expected_error):
            _trace_function()(
                engine, "cfe25e30b3ec",
                manual_path_hex="aa", manual_hash_width=1, timeout=5,
            )
        assert len(engine._mc.commands.calls) == 1


def test_manual_two_byte_route_keeps_width_and_snr():
    """Las rutas de hashes de dos bytes conservan su ancho y orden."""
    payload = {"path": [{"hash": "7f15", "snr": 3.25}, {"hash": "f64e", "snr": -2.5}, {"snr": 6.0}]}
    cache = {"7f15": [{"name": "REP UTEBO 1"}], "f64e": [{"name": "QSP1"}]}
    with _active_loop() as loop:
        engine = _Engine(loop, payload, cache)
        result = _trace_function()(
            engine, "cfe25e30b3ec",
            manual_path_hex="7f15f64e", manual_hash_width=2, timeout=5,
        )
    assert result["ok"] is True
    assert result["path_hash_width"] == 2
    assert result["path_snrs"] == [3.25, -2.5, 6.0]
    assert [h["hash"] for h in result["hops"]] == ["7f15", "f64e"]
    assert engine._mc.commands.calls[0][2] == 1
    assert engine._mc.commands.calls[0][3] == bytes.fromhex("7f15f64e")


def test_manual_trace_control_requires_explicit_opt_in():
    """Comprueba la barrera de habilitación sin recorrer reiteradamente 12.000 líneas.

    Cómo se ejecuta:
        pytest -q tests/test_meshcore_manual_trace_snr.py

    La comprobación utiliza una única extracción del bloque de control JSONL
    para asegurar que la rama manual exige opt-in y devuelve el rechazo.
    Evita ast.get_source_segment dentro de ast.walk (coste cuadrático).
    """
    source = BROKER.read_text(encoding="utf-8")
    start = source.index('elif cmd == "MESHCORE_TRACE_PATH":')
    end = source.index('elif cmd == "MESHCORE_CONTACTS":', start)
    handler_source = source[start:end]
    assert 'MESHCORE_TRACE_MANUAL_ENABLED' in handler_source
    assert 'meshcore_trace_manual_disabled' in handler_source
    assert 'eng.trace_contact(' in handler_source
    assert handler_source.index('meshcore_trace_manual_disabled') < handler_source.rindex('manual_path_hex=manual_path_hex')


def test_real_repeater_hop_resolution_preserves_missing_snr():
    """Ejercita el resolvedor REAL, no el simulador de los otros casos."""
    source = BROKER.read_text(encoding="utf-8")
    tree = ast.parse(source)
    bridge = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "MeshCoreEmbeddedBridge"
    )
    method = next(
        node for node in bridge.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_meshcore_resolve_trace_hops"
    )
    namespace = {}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(BROKER), "exec"), namespace)
    resolver = namespace["_meshcore_resolve_trace_hops"]

    class Engine:
        _mc_path_prefix_cache = {
            "7f15": [{"name": "REP UTEBO 1", "public_key": "7f15" + "0" * 60}],
            "f64e": [{"name": "QSP1", "public_key": "f64e" + "0" * 60}],
        }

        @staticmethod
        def _meshcore_path_bytes(value):
            return value if isinstance(value, bytes) else bytes.fromhex(value)

        @staticmethod
        def _meshcore_contact_display(contact, fallback):
            return contact.get("name", fallback)

    hops = resolver(
        Engine(), bytes.fromhex("7f15f64e"), [None, 4.5, -1.25], hash_width=2
    )
    assert [hop["name"] for hop in hops] == ["REP UTEBO 1", "QSP1", "Destino"]
    assert [hop["snr"] for hop in hops] == [None, 4.5, -1.25]
    assert hops[-1]["destination"] is True


@pytest.mark.parametrize("width,path_hex,expected", [
    (1, "aabbcc", "aabbccbbaa"),
    (2, "7f15f64ebf27", "7f15f64ebf27f64e7f15"),
    (1, "aa", "aa"),
])
def test_official_style_manual_trace_round_trip(width, path_hex, expected):
    """Compone ida+vuelta sin repetir el punto de giro, sin PATH_RESPONSE.

    Llamada: pytest -q tests/test_meshcore_manual_trace_snr.py
    Se usan dobles de `send_trace`; jamás transmite un paquete radio.
    """
    labels = [path_hex[pos:pos + width * 2] for pos in range(0, len(path_hex), width * 2)]
    cache = {h: [{"name": "Repeater " + h}] for h in labels}
    trace_nodes = [
        {"hash": h, "snr": float(i + 1)}
        for i, h in enumerate([
            expected[pos:pos + width * 2]
            for pos in range(0, len(expected), width * 2)
        ])
    ] + [{"snr": -1.0}]
    with _active_loop() as loop:
        engine = _Engine(loop, {"path": trace_nodes}, cache)
        response = _trace_function()(
            engine, "ruta", manual_path_hex=path_hex,
            manual_hash_width=width, manual_return=True, timeout=5,
        )
    assert response["ok"] is True
    assert response["manual_return_used"] is True
    assert response["manual_forward_hops"] == len(labels)
    assert response["discovery_used"] is False
    assert engine._mc.commands.calls[0][3] == bytes.fromhex(expected)
    assert len(engine._mc.commands.calls) == 1


def test_manual_trace_without_contact_does_not_require_discovery():
    """La traza manual no requiere contacto, PATH_RESPONSE ni nombre remoto."""
    with _active_loop() as loop:
        engine = _Engine(
            loop, {"path": [{"hash": "aa", "snr": 5.0}, {"snr": 0.5}]},
            {"aa": [{"name": "RPT-A"}]},
        )
        response = _trace_function()(
            engine, "ruta", manual_path_hex="aa", timeout=5,
        )
    assert response["contact"]["name"] == "Traza manual"
    assert response["path_snrs"] == [5.0, 0.5]


def test_manual_trace_rejects_return_without_manual_path():
    """No hay forma segura de construir el regreso desde una ruta no declarada."""
    with _active_loop() as loop:
        engine = _Engine(loop, {}, {})
        with pytest.raises(ValueError, match="return_requires_manual_path"):
            _trace_function()(engine, "ruta", manual_return=True, timeout=5)
        assert not engine._mc.commands.calls


def test_telegram_official_manual_command_structure():
    """El bot separa la traza manual del descubrimiento de contacto."""
    source = BOT.read_text(encoding="utf-8")
    begin = source.index("async def traza_mc_cmd(")
    end = source.index("async def mc_contactos_cmd(", begin)
    handler_source = source[begin:end]
    assert '"contact_prefix": "ruta"' in handler_source
    assert '"manual_return": len(args) == 4' in handler_source
    assert 'direction = " [ida]" if index <= forward else " [vuelta]"' in handler_source
