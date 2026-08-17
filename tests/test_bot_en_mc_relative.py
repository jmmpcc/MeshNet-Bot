#!/usr/bin/env python3
"""Regresión estructural para /en_mc sin importar el bot ni abrir radios/sockets."""
from pathlib import Path
import ast

ROOT = Path(__file__).resolve().parents[1]
BOT = ROOT / "source" / "Telegram_Bot_Broker.py"
SOURCE = BOT.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def function_source(name: str) -> str:
    for node in TREE.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(SOURCE, node) or ""
    raise AssertionError(f"No existe la función {name}")


def test_en_mc_exists_and_reuses_scheduler():
    src = function_source("en_mc_cmd")
    assert "_parse_minutes_list" in src
    assert "_parse_mc_channel_token" in src
    assert "_validate_len_or_block" in src
    assert "_norm_mesh" not in src
    assert "_split_mesh" not in src
    assert "broker_tasks.schedule_message" in src
    assert '"transport": "meshcore"' in src
    assert '"meshcore_mode": "channel"' in src
    assert '"meshcore_channel_idx": int(channel_idx)' in src
    assert '"bot_est_parts": 1' in src
    assert 'destination="meshcore:channel"' in src


def test_en_meshtastic_handler_remains_present_and_separate():
    assert "async def en_cmd(" in SOURCE
    assert 'CommandHandler("en", en_cmd)' in SOURCE
    assert 'CommandHandler("en_mc", en_mc_cmd)' in SOURCE


def test_en_mc_is_not_meshtastic_only():
    start = SOURCE.index("_MESHTASTIC_ONLY_COMMANDS = frozenset({")
    end = SOURCE.index("})", start)
    block = SOURCE[start:end]
    assert '"en"' in block
    assert '"en_mc"' not in block


def test_en_mc_is_advertised_and_documented_for_meshcore_help():
    assert 'BotCommand("en_mc"' in SOURCE
    assert "<code>/en_mc &lt;minutos|m1,m2,...&gt; &lt;chX|X|canal X&gt; &lt;texto&gt;</code>" in SOURCE
    assert "s_diario_mc" in SOURCE


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"OK: {len(tests)} pruebas /en_mc")
