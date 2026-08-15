import os
import sys
from pathlib import Path


SOURCE_DIR = Path(__file__).resolve().parents[1] / "source"
if str(SOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIR))

import channel_gateway_bot as cgb


def _ctx(profile: str, transports, node_a=None, node_b=None, embedded=False):
    return {
        "profile": profile,
        "valid": True,
        "legacy_mode": profile == "legacy",
        "transports": tuple(transports),
        "node_a_transport": node_a,
        "node_b_transport": node_b,
        "embedded_bridge_enabled": embedded,
    }


def test_meshcore_only_allows_implicit_transport():
    ctx = _ctx("meshcore_only", ("meshcore",), node_a="meshcore")
    transport, idx = cgb._resolve_transport_for_command(["add", "0", "2"], ctx)
    assert transport == "meshcore"
    assert idx == 1


def test_meshcore_only_rejects_meshtastic_explicit_transport():
    ctx = _ctx("meshcore_only", ("meshcore",), node_a="meshcore")
    transport, idx = cgb._resolve_transport_for_command(
        ["add", "meshtastic", "0", "2"], ctx
    )
    assert transport is None
    assert idx == -2


def test_combined_profile_requires_explicit_transport():
    ctx = _ctx(
        "meshtastic_a_meshcore_embedded_b",
        ("meshtastic", "meshcore"),
        node_a="meshtastic",
        node_b="meshcore",
        embedded=True,
    )
    transport, idx = cgb._resolve_transport_for_command(["add", "0", "2"], ctx)
    assert transport is None
    assert idx == -3


def test_combined_profile_accepts_each_valid_transport():
    ctx = _ctx(
        "meshcore_a_meshtastic_embedded_b",
        ("meshtastic", "meshcore"),
        node_a="meshcore",
        node_b="meshtastic",
        embedded=True,
    )
    assert cgb._resolve_transport_for_command(
        ["add", "meshcore", "0", "2"], ctx
    ) == ("meshcore", 2)
    assert cgb._resolve_transport_for_command(
        ["add", "meshtastic", "1", "3"], ctx
    ) == ("meshtastic", 2)


def test_contextual_help_meshcore_only_hides_invalid_meshtastic_syntax():
    ctx = _ctx("meshcore_only", ("meshcore",), node_a="meshcore")
    text = cgb._format_contextual_help(ctx)
    assert "Perfil: meshcore_only" in text
    assert "Transporte válido: meshcore" in text
    assert "/channel_gateway add meshcore" in text
    assert "/channel_gateway add meshtastic" not in text


def test_contextual_help_combined_lists_both_and_embedded_role():
    ctx = _ctx(
        "meshtastic_a_meshcore_embedded_b",
        ("meshtastic", "meshcore"),
        node_a="meshtastic",
        node_b="meshcore",
        embedded=True,
    )
    text = cgb._format_contextual_help(ctx)
    assert "Nodo A: Meshtastic" in text
    assert "Nodo B: Meshcore embebido" in text
    assert "/channel_gateway add meshtastic" in text
    assert "/channel_gateway add meshcore" in text
    assert "obligatorio indicar el transporte" in text


def test_legacy_profile_does_not_guess_transport():
    ctx = _ctx("legacy", ())
    transport, idx = cgb._resolve_transport_for_command(["add", "0", "2"], ctx)
    assert transport is None
    assert idx == -1
    text = cgb._format_contextual_help(ctx)
    assert "No se puede determinar de forma segura" in text
