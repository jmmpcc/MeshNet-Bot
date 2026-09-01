from __future__ import annotations

import ast
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "source" / "Telegram_Bot_Broker.py"


def _source() -> str:
    return SOURCE.read_text(encoding="utf-8")


def _async_function_source(name: str) -> str:
    source = _source()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            segment = ast.get_source_segment(source, node)
            assert segment is not None
            return segment
    raise AssertionError(f"{name} no encontrada")


def test_dm_por_numero_conserva_prefijo_interno_y_usa_alias_visible() -> None:
    """El número sigue resolviendo el DM interno, pero la UI usa el alias."""
    source = _source()
    fn = _async_function_source("enviar_mc_dm_cmd")

    assert 'context.user_data.get("mc_contacts_map")' in fn
    assert 'context.user_data.get("mc_contacts_alias_map")' in fn
    assert '_send_via_broker_meshcore_contact, contact_prefix, out_text, 3.0' in fn
    assert '_meshcore_dm_confirmation(contact_alias, resp.get("len"))' in fn
    assert 'Destino: {contact_prefix}' not in fn
    assert 'context.user_data["mc_contacts_alias_map"] = mc_alias_map' in source
    assert 'mc_map[str(idx)] = dm_key' in source
    assert 'routing_key = public_key or dm_key' not in source


def test_forcereply_tampoco_expone_prefijo() -> None:
    """El envío iniciado desde el botón DM comparte la misma confirmación segura."""
    fn = _async_function_source("on_forcereply_text")

    assert 'context.user_data.pop("await_mc_dm_alias", None)' in fn
    assert '_meshcore_dm_confirmation(contact_alias, resp.get("len"))' in fn
    assert 'Destino: {contact_prefix}' not in fn


def test_helper_no_publica_identificador_si_no_hay_alias() -> None:
    """El fallback confirma el encolado sin revelar el prefijo interno."""
    source = _source()
    assert 'lines = ["DM MeshCore encolado"]' in source
    assert 'lines.append(f"Destino: {clean_alias}")' in source
    assert 'lines.append(f"Destino: {contact_prefix}")' not in source


def test_source_sigue_siendo_python_valido() -> None:
    ast.parse(_source())