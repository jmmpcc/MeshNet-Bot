#!/usr/bin/env python3
"""Regresión estática de la interfaz Telegram para meshcore_only.

Inspecciona el AST del bot sin importarlo, evitando activar Telegram,
Meshtastic, broker o sockets durante la validación.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOT = ROOT / "source" / "Telegram_Bot_Broker.py"


def top_function(tree: ast.Module, name: str):
    """Localiza una función top-level por nombre o falla explícitamente."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"No existe la función {name}")


def node_source(source: str, node: ast.AST) -> str:
    """Obtiene el segmento exacto de código correspondiente a un nodo AST."""
    segment = ast.get_source_segment(source, node)
    if segment is None:
        raise AssertionError("No se pudo recuperar segmento AST")
    return segment


def profile_if(source: str, tree: ast.Module, function_name: str) -> ast.If:
    """Devuelve el if que selecciona _is_meshcore_only_profile en una función."""
    function = top_function(tree, function_name)
    for node in function.body:
        if isinstance(node, ast.If) and "_is_meshcore_only_profile" in ast.unparse(node.test):
            return node
    raise AssertionError(f"{function_name} no contiene selección meshcore_only")


def branch_source(source: str, nodes: list[ast.stmt]) -> str:
    """Concatena únicamente el código ejecutable de una rama AST."""
    return "\n".join(node_source(source, node) for node in nodes)


class MeshcoreOnlyTelegramProfileTests(unittest.TestCase):
    """Valida la nueva política sin modificar la lógica histórica del bot."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.source = BOT.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)

    def test_source_is_valid_python(self) -> None:
        self.assertIsInstance(self.tree, ast.Module)

    def test_uses_existing_canonical_profile_resolver(self) -> None:
        self.assertIn(
            "from radio_profile import PROFILE_MESHCORE_ONLY, normalize_radio_profile",
            self.source,
        )
        helper = node_source(self.source, top_function(self.tree, "_is_meshcore_only_profile"))
        self.assertIn("normalize_radio_profile", helper)
        self.assertIn("PROFILE_MESHCORE_ONLY", helper)

    def test_meshtastic_command_policy_contains_critical_commands(self) -> None:
        names = set()
        for node in self.tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if any(isinstance(t, ast.Name) and t.id == "_MESHTASTIC_ONLY_COMMANDS" for t in node.targets):
                self.assertIsInstance(node.value, ast.Call)
                names = {ast.literal_eval(item) for item in node.value.args[0].elts}
                break
        expected = {
            "enviar", "enviar_ack", "traceroute", "telemetria", "lora",
            "ver_nodos", "vecinos", "programar", "position", "cobertura",
            "auditoria_red", "canales",
        }
        self.assertTrue(expected.issubset(names))

    def test_inline_menu_hides_meshtastic_only_in_meshcore_branch(self) -> None:
        selector = profile_if(self.source, self.tree, "main_menu_kb")
        mc = branch_source(self.source, selector.body)
        other = branch_source(self.source, selector.orelse)
        self.assertIn('callback_data="escuchar"', mc)
        self.assertIn('callback_data="estado"', mc)
        self.assertNotIn('callback_data="traceroute"', mc)
        self.assertNotIn('callback_data="enviar"', mc)
        self.assertIn('callback_data="traceroute"', other)
        self.assertIn('callback_data="enviar"', other)

    def test_slash_menu_keeps_meshcore_and_filters_meshtastic(self) -> None:
        block = node_source(self.source, top_function(self.tree, "set_bot_menu"))
        self.assertIn('BotCommand("enviar_mc"', block)
        self.assertIn('BotCommand("enviar"', block)
        self.assertIn("if cmd.command not in _MESHTASTIC_ONLY_COMMANDS", block)
        self.assertIn('"baliza_clima": "cada <minutos> meshcore', block)

    def test_callbacks_are_blocked_before_historical_dispatch(self) -> None:
        block = node_source(self.source, top_function(self.tree, "on_cb"))
        guard = block.index("if _is_meshcore_only_profile() and data in _MESHTASTIC_ONLY_CALLBACKS:")
        dispatch = block.index('if data == "ver_nodos":')
        self.assertLess(guard, dispatch)

    def test_manual_commands_are_intercepted_before_original_handlers(self) -> None:
        block = node_source(self.source, top_function(self.tree, "build_application"))
        guard = block.index("CommandHandler(sorted(_MESHTASTIC_ONLY_COMMANDS), _meshtastic_unavailable_cmd)")
        original = block.index('CommandHandler("traceroute", traceroute_cmd)')
        self.assertLess(guard, original)
        self.assertIn("if _is_meshcore_only_profile():", block[:guard])

    def test_dynamic_vecinos_guard_matches_numeric_suffixes(self) -> None:
        """Comprueba que /vecinos6, /vecinos10, etc. queden interceptados."""
        function = top_function(self.tree, "build_application")
        patterns = []
        for node in ast.walk(function):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "Regex" or not node.args:
                continue
            value = node.args[0]
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                patterns.append(value.value)
        self.assertIn(r"^/vecinos\d+(?:@\w+)?(?:\s|$)", patterns)

    def test_start_meshcore_branch_has_no_meshtastic_host(self) -> None:
        selector = profile_if(self.source, self.tree, "start")
        mc = branch_source(self.source, selector.body)
        other = branch_source(self.source, selector.orelse)
        self.assertIn("MeshNet Bot listo.", mc)
        self.assertIn("PROFILE_MESHCORE_ONLY", mc)
        self.assertNotIn("MESHTASTIC_HOST", mc)
        self.assertIn("Meshtastic Bot listo.", other)
        self.assertIn("MESHTASTIC_HOST", other)

    def test_help_assembles_only_meshcore_capabilities_in_profile_branch(self) -> None:
        selector = profile_if(self.source, self.tree, "ayuda")
        mc = branch_source(self.source, selector.body)
        other = branch_source(self.source, selector.orelse)
        for kept in ("s_mensajeria_mc", "s_diario_mc", "s_clima_mc_only", "s_aemet_mc_only"):
            self.assertIn(kept, mc)
        for hidden in (
            "s_mensajeria_mesh,", "s_programacion,", "s_diario,",
            "s_nodos,", "s_rutas,", "s_posicion,", "s_auditorias,",
        ):
            self.assertNotIn(hidden, mc)
            self.assertIn(hidden, other)
        self.assertNotIn("/traceroute", mc)

    def test_post_startup_remains_untouched_by_profile_phase(self) -> None:
        block = node_source(self.source, top_function(self.tree, "post_startup"))
        self.assertNotIn("_is_meshcore_only_profile", block)
        self.assertIn("Prefetch inicial", block)


if __name__ == "__main__":
    unittest.main()
