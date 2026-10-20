"""Pruebas del índice disperso de FETCH_BACKLOG del broker."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import threading


BROKER_PATH = Path(__file__).resolve().parents[1] / "source" / "Meshtastic_Broker.py"

_FUNCTIONS = {
    "_backlog_row_timestamp",
    "_backlog_index_reset_locked",
    "_backlog_index_refresh_locked",
    "_backlog_index_seek_offset",
    "_iter_backlog_jsonl",
}


def _load_runtime(log_path: Path, *, stride: int = 32) -> dict:
    """
    Carga solo los helpers de índice desde Meshtastic_Broker.py.

    Funcionalidad:
        - Evita importar el broker completo y sus dependencias de radio.
        - Compila las funciones reales mediante AST.
        - Inyecta un OFFLINE_LOG_PATH temporal para pruebas deterministas.
    """
    source = BROKER_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)

    selected = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in _FUNCTIONS
    ]

    assert {node.name for node in selected} == _FUNCTIONS

    module = ast.Module(body=selected, type_ignores=[])
    ast.fix_missing_locations(module)

    runtime = {
        "os": os,
        "json": json,
        "threading": threading,
        "OFFLINE_LOG_PATH": str(log_path),
        "_BACKLOG_INDEX_LOCK": threading.Lock(),
        "_BACKLOG_INDEX_STATE": {
            "dev": None,
            "ino": None,
            "size": 0,
            "indexed_offset": 0,
            "max_ts": 0,
            "lines": 0,
            "checkpoints": [(0, 0)],
        },
        "_BACKLOG_INDEX_STRIDE_LINES": int(stride),
    }

    exec(compile(module, str(BROKER_PATH), "exec"), runtime)
    return runtime


def _write_rows(path: Path, timestamps: list[int], *, portnum: str = "TEXT_MESSAGE_APP") -> None:
    """Escribe un JSONL mínimo con timestamps y orden explícitos."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for idx, ts in enumerate(timestamps):
            f.write(
                json.dumps(
                    {
                        "id": idx,
                        "rx_time": ts,
                        "ts": ts,
                        "channel": idx % 2,
                        "portnum": portnum,
                        "text": f"row-{idx}",
                    }
                )
                + "\n"
            )


def _append_rows(path: Path, timestamps: list[int]) -> None:
    """Añade filas al JSONL conservando formato append-only del broker."""
    with path.open("a", encoding="utf-8") as f:
        for ts in timestamps:
            f.write(
                json.dumps(
                    {
                        "rx_time": ts,
                        "ts": ts,
                        "channel": 0,
                        "portnum": "TEXT_MESSAGE_APP",
                        "text": f"row-{ts}",
                    }
                )
                + "\n"
            )


def test_recent_query_preserves_order_and_limit(tmp_path: Path) -> None:
    """Una consulta reciente debe conservar orden ascendente y límite histórico."""
    log = tmp_path / "broker_offline_log.jsonl"
    _write_rows(log, list(range(1_000, 2_000)))

    rt = _load_runtime(log, stride=32)
    rows = list(
        rt["_iter_backlog_jsonl"](
            1_990,
            None,
            None,
            ["TEXT_MESSAGE_APP"],
            5,
        )
    )

    assert [row["rx_time"] for row in rows] == [1990, 1991, 1992, 1993, 1994]


def test_out_of_order_timestamp_is_not_skipped(tmp_path: Path) -> None:
    """
    El índice usa máximo acumulado, por lo que una trama retrasada no permite
    saltar un prefijo que pudiera contener datos válidos.
    """
    log = tmp_path / "broker_offline_log.jsonl"
    _write_rows(log, [100, 200, 300, 50, 400])

    rt = _load_runtime(log, stride=2)
    rows = list(rt["_iter_backlog_jsonl"](250, None, None, None, None))

    assert [row["rx_time"] for row in rows] == [300, 400]


def test_index_updates_only_appended_region(tmp_path: Path) -> None:
    """Al crecer el fichero, el índice avanza desde indexed_offset anterior."""
    log = tmp_path / "broker_offline_log.jsonl"
    _write_rows(log, list(range(100, 201)))

    rt = _load_runtime(log, stride=16)
    first = list(rt["_iter_backlog_jsonl"](195, None, None, None, None))
    first_offset = rt["_BACKLOG_INDEX_STATE"]["indexed_offset"]

    _append_rows(log, [201, 202, 203, 204, 205])

    second = list(rt["_iter_backlog_jsonl"](203, None, None, None, None))
    second_offset = rt["_BACKLOG_INDEX_STATE"]["indexed_offset"]

    assert [row["rx_time"] for row in first] == [195, 196, 197, 198, 199, 200]
    assert [row["rx_time"] for row in second] == [203, 204, 205]
    assert second_offset > first_offset


def test_rotation_invalidates_previous_offsets(tmp_path: Path) -> None:
    """Cambiar inode debe reconstruir índice y nunca reutilizar offsets antiguos."""
    log = tmp_path / "broker_offline_log.jsonl"
    _write_rows(log, list(range(1_000, 1_500)))

    rt = _load_runtime(log, stride=16)
    assert list(rt["_iter_backlog_jsonl"](1_490, None, None, None, None))

    rotated = tmp_path / "broker_offline_log.jsonl.1"
    os.replace(log, rotated)
    _write_rows(log, [2_000, 2_001, 2_002])

    rows = list(rt["_iter_backlog_jsonl"](2_000, None, None, None, None))

    assert [row["rx_time"] for row in rows] == [2000, 2001, 2002]


def test_no_since_keeps_full_scan_semantics(tmp_path: Path) -> None:
    """Sin since_ts se conserva lectura desde byte 0 y filtros existentes."""
    log = tmp_path / "broker_offline_log.jsonl"
    _write_rows(log, [10, 20, 30, 40])

    rt = _load_runtime(log)
    rows = list(rt["_iter_backlog_jsonl"](None, 30, 1, ["TEXT_MESSAGE_APP"], None))

    assert [row["rx_time"] for row in rows] == [20]
