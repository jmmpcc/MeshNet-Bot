from __future__ import annotations

import ast
from pathlib import Path

BROKER = Path('source/Meshtastic_Broker.py')
BOT = Path('source/Telegram_Bot_Broker.py')


def function_source(path: Path, name: str) -> str:
    source = path.read_text(encoding='utf-8')
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            segment = ast.get_source_segment(source, node)
            assert segment is not None
            return segment
    raise AssertionError(f'{name} not found')


def test_mc_contactos_remains_untouched_by_new_resolver() -> None:
    fn = function_source(BOT, 'mc_contactos_cmd')
    assert 'mc_map[str(idx)] = dm_key' in fn
    assert 'MESHCORE_RESOLVE_CONTACT' not in fn
    assert '_resolve_meshcore_contact_alias_via_ctrl' not in fn


def test_broker_list_contacts_does_not_use_observed_prefix_map() -> None:
    fn = function_source(BROKER, 'list_contacts')
    assert '_mc_observed_dm_prefixes_by_alias' not in fn
    assert 'resolve_observed_dm_prefix' not in fn


def test_rx_learns_only_literal_event_prefix() -> None:
    source = BROKER.read_text(encoding='utf-8')
    assert 'self._meshcore_remember_observed_dm_prefix(' in source
    assert 'alias, str(data.get("pubkey_prefix") or "").strip()' in source
    assert 'self.contact_aliases[pref] = alias' not in source


def test_resolver_avoids_public_key_fallback() -> None:
    fn = function_source(BROKER, 'resolve_observed_dm_prefix')
    assert '_mc_observed_dm_prefixes_by_alias' in fn
    assert 'self.contact_aliases' in fn
    executable = fn.split('\"\"\"', 2)[-1]
    assert 'public_key[:12]' not in executable
    assert '_mc_contacts_cache' not in fn


def test_numeric_dm_resolves_alias_but_manual_prefix_does_not() -> None:
    fn = function_source(BOT, 'enviar_mc_dm_cmd')
    assert 'if not cp and str(text_tokens[0]).isdigit()' in fn
    assert '_resolve_meshcore_contact_alias_via_ctrl, contact_alias, 2.0' in fn
    assert 'if resolved_prefix:' in fn
    assert 'cp = resolved_prefix' in fn
    assert '_extract_mc_contact_prefix_from_text(text_tokens[0])' in fn


def test_dm_mc_and_enviar_mc_dm_share_same_handler() -> None:
    """Ambos comandos deben ejecutar exactamente la misma ruta DM y su resolvedor numérico."""
    fn = function_source(BOT, 'build_application')
    assert 'CommandHandler(["enviar_mc_dm", "dm_mc"], enviar_mc_dm_cmd)' in fn


def test_control_endpoint_is_read_only_and_separate_from_send() -> None:
    source = BROKER.read_text(encoding='utf-8')
    assert 'elif cmd == "MESHCORE_RESOLVE_CONTACT"' in source
    assert 'resolved_prefix = eng.resolve_observed_dm_prefix(alias)' in source
    assert 'elif cmd == "MESHCORE_SEND"' in source


def test_sources_parse() -> None:
    ast.parse(BROKER.read_text(encoding='utf-8'))
    ast.parse(BOT.read_text(encoding='utf-8'))
