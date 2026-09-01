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


def test_mc_contactos_muestra_public_key_completa_y_conserva_dm_key() -> None:
    """La UI muestra el ID canónico sin cambiar la clave corta usada para DM."""
    fn = _mc_contactos_source()

    assert "canonical_id = public_key or display_prefix or contact_id or dm_key" in fn
    assert 'ID: <code>[MC:{escape(canonical_id)}]</code>' in fn
    assert 'DM: <code>{escape(dm_key)}</code>' in fn

    # Regresión: la resolución numérica y el callback deben seguir usando dm_key.
    assert 'mc_map[str(idx)] = dm_key' in fn
    assert 'callback_data=f"mc_dm:{idx}:{dm_key[:32]}"' in fn


def test_telegram_bot_broker_sigue_siendo_python_valido() -> None:
    source_path = Path(__file__).resolve().parents[1] / "source" / "Telegram_Bot_Broker.py"
    ast.parse(source_path.read_text(encoding="utf-8"))
