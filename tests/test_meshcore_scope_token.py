from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "source"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from meshcore_scope_token import extract_scope_modifier


def test_source_parses() -> None:
    path = SOURCE / "meshcore_scope_token.py"
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_scope_hash_region_is_extracted_without_touching_channel_or_text() -> None:
    clean, scope, error = extract_scope_modifier(
        ["canal", "5", "scope#utebo", "Prueba", "envio", "a", "utebo"]
    )
    assert error is None
    assert scope == "#utebo"
    assert clean == ["canal", "5", "Prueba", "envio", "a", "utebo"]


def test_scope_hash_is_position_independent() -> None:
    clean, scope, error = extract_scope_modifier(
        ["ambos", "ch5", "aprs", "broadcast", "scope#zaragoza", "Aviso"]
    )
    assert error is None
    assert scope == "#zaragoza"
    assert clean == ["ambos", "ch5", "aprs", "broadcast", "Aviso"]


def test_scope_hash_special_values() -> None:
    clean, scope, error = extract_scope_modifier(["ch5", "scope#0", "Hola"])
    assert error is None
    assert scope == "0"
    assert clean == ["ch5", "Hola"]

    clean, scope, error = extract_scope_modifier(["ch5", "scope#*", "Hola"])
    assert error is None
    assert scope == "*"
    assert clean == ["ch5", "Hola"]


def test_scope_hash_empty_or_duplicate_is_rejected() -> None:
    _, _, error = extract_scope_modifier(["ch5", "scope#", "Hola"])
    assert error == "Falta el valor después de scope#."

    _, _, error = extract_scope_modifier(
        ["ch5", "scope#utebo", "scope#zaragoza", "Hola"]
    )
    assert error == "Solo puede indicarse un scope por envío."


def test_new_and_legacy_scope_cannot_be_mixed() -> None:
    _, _, error = extract_scope_modifier(
        ["ch5", "scope#utebo", "--scope", "#zaragoza", "Hola"]
    )
    assert error == "Solo puede indicarse un scope por envío."


def test_legacy_scope_stays_compatible() -> None:
    clean, scope, error = extract_scope_modifier(
        ["canal", "5", "--scope", "#utebo", "Hola"]
    )
    assert error is None
    assert scope == "#utebo"
    assert clean == ["canal", "5", "Hola"]


def test_no_scope_keeps_historical_arguments_byte_for_byte() -> None:
    original = ["canal", "5", "Prueba", "normal"]
    clean, scope, error = extract_scope_modifier(original)
    assert error is None
    assert scope is None
    assert clean == original
