from __future__ import annotations

import ast
from pathlib import Path


def _mc_contactos_source() -> str:
    source_path = Path(__file__).resolve().parents[1] / "source" / "Telegram_Bot_Broker.py"
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "mc_contactos_cmd":
            segment = ast.get_source_segment(source, node)
            assert segment is not None
            return segment
    raise AssertionError("mc_contactos_cmd no encontrada")


def test_mc_contactos_muestra_solo_numero_y_alias_y_conserva_dm_key() -> None:
    """La UI no expone IDs y la selección numérica conserva la resolución DM existente."""
    fn = _mc_contactos_source()

    assert 'lines.append(f"<b>{idx:02d}.</b> 📡 <b>{escape(name)}</b>")' in fn
    assert 'ID: <code>[MC:' not in fn
    assert 'DM: <code>' not in fn
    assert 'meta.append(f"id:' not in fn
    assert "_format_meshcore_last_seen(ls)" not in fn

    # Regresión: /dm_mc N conserva el dm_key/prefijo operativo que la ruta
    # MeshCore ya acepta y que coincide con el prefix observado en RX real.
    assert 'mc_map[str(idx)] = dm_key' in fn
    assert 'routing_key = public_key or dm_key' not in fn
    assert 'callback_data=f"mc_dm:{idx}:{dm_key[:32]}"' not in fn
    assert 'keyboard.append([' not in fn
    assert 'reply_markup=InlineKeyboardMarkup' not in fn


def test_telegram_bot_broker_sigue_siendo_python_valido() -> None:
    source_path = Path(__file__).resolve().parents[1] / "source" / "Telegram_Bot_Broker.py"
    ast.parse(source_path.read_text(encoding="utf-8"))
