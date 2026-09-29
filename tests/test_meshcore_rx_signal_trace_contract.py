"""Contrato de metadatos RF y enlace de mapa para RX MeshCore."""
from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BROKER = ROOT / "source" / "Meshtastic_Broker.py"
BOT = ROOT / "source" / "Telegram_Bot_Broker.py"


def _emit_runtime():
    """Compila aislada la función de emisión sin arrancar radio ni sockets."""
    tree = ast.parse(BROKER.read_text(encoding="utf-8"), filename=str(BROKER))
    wanted = {
        "_meshcore_path_chunks_from_payload",
        "_meshcore_format_repeater_path",
        "emit_meshcore_rx_to_hub_and_log",
    }
    nodes = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted
    ]
    assert {node.name for node in nodes} == wanted

    captured = {"offline": []}

    class Hub:
        def __init__(self):
            self.lines = []

        def broadcast_line(self, line):
            self.lines.append(line)

    hub = Hub()
    namespace = {
        "hashlib": hashlib,
        "os": os,
        "CHANNEL_NAME_BY_INDEX": {4: "PRUEBAS"},
        "BROKER_HUB": hub,
        "_now_s": lambda: 1234.56789,
        "_json_dumps": lambda value: json.dumps(value, ensure_ascii=False),
        "append_offline_log": captured["offline"].append,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(BROKER), "exec"), namespace)
    return namespace["emit_meshcore_rx_to_hub_and_log"], hub, captured


def test_meshcore_rx_publishes_real_signal_and_trace_metadata(monkeypatch) -> None:
    emit, hub, captured = _emit_runtime()
    monkeypatch.setenv("MESHCORE_TRACE_MAP_BASE_URL", "http://meshnet.local:8790/")
    monkeypatch.setenv("HOME_LAT", "41.64")
    monkeypatch.setenv("HOME_LON", "-0.88")
    monkeypatch.setenv("MESHCORE_LOCAL_NAME", "RPT-LOCAL")

    emit(
        ch=4,
        text="[MC] prueba",
        pubkey_prefix="aabbcc",
        kind="chan",
        chan_idx=5,
        chan_tag="RED",
        from_alias="ORIGEN",
        path_info={
            "path_len": 1,
            "path_hash_size": 1,
            "path": "aa",
            "rssi": -91,
            "snr": 4.5,
            "from_name": "ORIGEN",
            "from_lat": 41.60,
            "from_lon": -0.90,
            "meshcore_repeaters": [
                {"hash": "aa", "name": "RPT-A", "lat": 41.61, "lon": -0.89}
            ],
        },
    )

    event = json.loads(hub.lines[0])
    packet = event["packet"]
    assert packet["rxRssi"] == -91.0
    assert packet["rxSnr"] == 4.5
    assert len(packet["meshcore_trace_id"]) == 20
    assert packet["meshcore_trace_url"].endswith(
        "/meshcore/trace/" + packet["meshcore_trace_id"]
    )
    assert packet["meshcore_receiver_name"] == "RPT-LOCAL"

    offline = captured["offline"][0]
    assert offline["rx_rssi"] == -91.0
    assert offline["rx_snr"] == 4.5
    assert offline["meshcore_trace_id"] == packet["meshcore_trace_id"]
    assert offline["meshcore_repeaters"][0]["name"] == "RPT-A"


def test_meshcore_rx_accepts_uppercase_metrics_from_meshcore_py(monkeypatch) -> None:
    """Conserva RSSI/SNR correlacionados por meshcore_py en CHANNEL_MSG_RECV."""
    emit, hub, captured = _emit_runtime()
    monkeypatch.delenv("MESHCORE_TRACE_MAP_BASE_URL", raising=False)

    emit(
        ch=4,
        text="[MC] uppercase metrics",
        kind="chan",
        chan_idx=5,
        path_info={
            "path_len": 1,
            "path_hash_size": 1,
            "path": "aa",
            "RSSI": -103,
            "SNR": -2.25,
        },
    )

    packet = json.loads(hub.lines[0])["packet"]
    assert packet["rxRssi"] == -103.0
    assert packet["rxSnr"] == -2.25
    assert captured["offline"][0]["rx_rssi"] == -103.0
    assert captured["offline"][0]["rx_snr"] == -2.25


def test_telegram_listener_surfaces_trace_url_without_changing_send_mode() -> None:
    source = BOT.read_text(encoding="utf-8")
    assert 'pkt.get("meshcore_trace_url")' in source
    assert '🗺 Ver traza en mapa: {mc_trace_url}' in source
    # Se mantiene send_message sin introducir parse_mode, por lo que Telegram
    # autoenlaza la URL y no cambia el escapado de mensajes ya existente.
    assert "await context.bot.send_message(chat_id=chat_id, text=text_out)" in source
