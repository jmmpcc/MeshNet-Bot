"""Pruebas del visor histórico de trazas RX MeshCore."""
from __future__ import annotations

import json
from pathlib import Path

from tools.ControlPanel.meshcore_rx_trace_map import _find_trace, _render_trace_html, _trace_points


def test_find_trace_and_map_points(tmp_path: Path) -> None:
    trace_id = "0123456789abcdefabcd"
    path = tmp_path / "backlog.jsonl"
    item = {
        "meshcore_trace_id": trace_id,
        "text": "<mensaje>",
        "meshcore_path_text": "RPT-A -> RPT-B",
        "meshcore_path_len": 2,
        "rx_rssi": -91,
        "rx_snr": 4.5,
        "meshcore_from_name": "ORIGEN",
        "meshcore_from_lat": 41.60,
        "meshcore_from_lon": -0.90,
        "meshcore_repeaters": [
            {"name": "RPT-A", "lat": 41.61, "lon": -0.89},
            {"name": "RPT-B"},
        ],
        "meshcore_receiver_name": "MeshNet",
        "meshcore_receiver_lat": 41.64,
        "meshcore_receiver_lon": -0.88,
    }
    path.write_text(json.dumps(item) + "\n", encoding="utf-8")

    loaded = _find_trace(trace_id, path)
    assert loaded == item
    points = _trace_points(loaded)
    assert [point["name"] for point in points] == ["ORIGEN", "RPT-A", "MeshNet"]

    rendered = _render_trace_html(loaded)
    assert "RPT-A -&gt; RPT-B" in rendered
    assert "1/2 repetidores localizados" in rendered
    assert "RSSI -91 dBm" in rendered
    assert "SNR 4.5 dB" in rendered
    assert "&lt;mensaje&gt;" in rendered
    assert "<mensaje>" not in rendered


def test_invalid_trace_id_is_not_searched(tmp_path: Path) -> None:
    assert _find_trace("../etc/passwd", tmp_path / "missing.jsonl") is None
