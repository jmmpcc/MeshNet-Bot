from __future__ import annotations

import ast
import re
import textwrap
from pathlib import Path

BROKER = Path("source/Meshtastic_Broker.py")


def _runtime_namespace() -> dict:
    """Extrae y ejecuta solo el helper y los métodos del resolvedor MeshCore.

    Evita importar el broker completo y sus dependencias de hardware, pero ejecuta
    código real de las funciones afectadas para detectar NameError y errores de
    normalización que una comprobación AST puramente estructural no detectaría.
    """
    source = BROKER.read_text(encoding="utf-8")
    tree = ast.parse(source)

    norm_node = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "_norm_text"
    )
    alias_key_node = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "_meshcore_alias_key"
    )
    cls = next(
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "MeshCoreEmbeddedBridge"
    )
    remember_node = next(
        n for n in cls.body
        if isinstance(n, ast.FunctionDef) and n.name == "_meshcore_remember_observed_dm_prefix"
    )
    resolve_node = next(
        n for n in cls.body
        if isinstance(n, ast.FunctionDef) and n.name == "resolve_observed_dm_prefix"
    )

    ns = {"re": re}
    exec(compile(ast.Module(body=[norm_node], type_ignores=[]), "<norm>", "exec"), ns)
    exec(compile(ast.Module(body=[alias_key_node], type_ignores=[]), "<alias_key>", "exec"), ns)
    exec(compile(textwrap.dedent(ast.get_source_segment(source, remember_node)), "<remember>", "exec"), ns)
    exec(compile(textwrap.dedent(ast.get_source_segment(source, resolve_node)), "<resolve>", "exec"), ns)
    return ns


def _dummy(observed=None, aliases=None):
    class Dummy:
        pass

    obj = Dummy()
    obj._mc_observed_dm_prefixes_by_alias = observed or {}
    obj.contact_aliases = aliases or {}
    return obj


def test_resolver_returns_real_observed_rf_prefix() -> None:
    ns = _runtime_namespace()
    obj = _dummy()
    ns["_meshcore_remember_observed_dm_prefix"](
        obj, "  EB2EAS   T1000e  ", "8B47FEA70A4B"
    )
    assert ns["resolve_observed_dm_prefix"](obj, "EB2EAS T1000e") == "8b47fea70a4b"


def test_resolver_accepts_configured_alias_prefix() -> None:
    ns = _runtime_namespace()
    obj = _dummy(aliases={"8B47FEA70A4B": " EB2EAS   T1000e "})
    assert ns["resolve_observed_dm_prefix"](obj, "EB2EAS T1000e") == "8b47fea70a4b"


def test_resolver_matches_rx_hyphen_alias_with_contact_space_alias() -> None:
    """Caso real: RX EB2EAS-T1000E debe resolver consulta EB2EAS T1000e."""
    ns = _runtime_namespace()
    obj = _dummy()
    ns["_meshcore_remember_observed_dm_prefix"](
        obj, "EB2EAS-T1000E", "8B47FEA70A4B"
    )
    assert ns["resolve_observed_dm_prefix"](obj, "EB2EAS T1000e") == "8b47fea70a4b"


def test_resolver_matches_configured_underscore_alias_with_space_alias() -> None:
    """Los aliases configurados con '_' comparten la misma clave estable."""
    ns = _runtime_namespace()
    obj = _dummy(aliases={"8B47FEA70A4B": "EB2EAS_T1000E"})
    assert ns["resolve_observed_dm_prefix"](obj, "EB2EAS T1000e") == "8b47fea70a4b"
