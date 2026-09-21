#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regresión del contrato de resolución de rutas RX MeshCore.

Estas pruebas no importan el broker completo porque contiene dependencias de
hardware/red. Extraen únicamente los helpers ya existentes implicados en la
resolución de rutas y los ejecutan contra datos sintéticos.

Objetivo:
- conservar el fallback actual cuando MeshCore solo entrega path_len;
- resolver nombres cuando una futura extensión RX aporte los hashes path;
- no escoger nombres arbitrarios cuando un hash corto colisiona.
"""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BROKER = ROOT / "source" / "Meshtastic_Broker.py"

GLOBAL_HELPERS = {
    "_meshcore_path_chunks_from_payload",
    "_meshcore_format_repeater_path",
}
CLASS_METHODS = {
    "_meshcore_remember_contact",
    "_meshcore_contact_display",
    "_meshcore_enrich_path_info",
}


def _runtime_namespace() -> tuple[dict, type]:
    """Carga solo los helpers de ruta y crea una clase mínima de pruebas.

    Cómo se usa:
        namespace, bridge_type = _runtime_namespace()

    Funcionalidad:
        - parsea el broker actual mediante AST;
        - compila únicamente los dos helpers globales necesarios;
        - extrae los tres métodos ya existentes del resolvedor MeshCore;
        - evita ejecutar inicialización de radio, sockets o threads.
    """
    tree = ast.parse(BROKER.read_text(encoding="utf-8"), filename=str(BROKER))

    global_nodes = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in GLOBAL_HELPERS
    ]
    if {node.name for node in global_nodes} != GLOBAL_HELPERS:
        raise AssertionError("No se localizaron todos los helpers globales MeshCore")

    namespace = {"re": re}
    exec(compile(ast.Module(body=global_nodes, type_ignores=[]), str(BROKER), "exec"), namespace)

    bridge_node = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "MeshCoreEmbeddedBridge"
        ),
        None,
    )
    if bridge_node is None:
        raise AssertionError("No se localizó MeshCoreEmbeddedBridge")

    method_nodes = [
        node
        for node in bridge_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in CLASS_METHODS
    ]
    if {node.name for node in method_nodes} != CLASS_METHODS:
        raise AssertionError("No se localizaron todos los métodos de resolución MeshCore")

    method_ns = dict(namespace)
    exec(compile(ast.Module(body=method_nodes, type_ignores=[]), str(BROKER), "exec"), method_ns)

    bridge_type = type(
        "MeshCoreEmbeddedBridgeForTest",
        (),
        {name: method_ns[name] for name in CLASS_METHODS},
    )
    return namespace, bridge_type


class MeshCoreRepeaterPathContractTest(unittest.TestCase):
    """Valida el contrato que consumirá la futura extensión RX de MeshCore-EAS."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.ns, cls.bridge_type = _runtime_namespace()

    def _bridge(self):
        """Crea un resolvedor mínimo sin abrir ninguna conexión MeshCore."""
        bridge = self.bridge_type()
        bridge._mc = None
        bridge._mc_contacts_cache = {}
        bridge._mc_path_prefix_cache = {}
        return bridge

    def test_path_len_only_keeps_current_safe_fallback(self) -> None:
        """Sin hashes RX no se inventan nombres de repetidor."""
        formatter = self.ns["_meshcore_format_repeater_path"]
        self.assertEqual(
            formatter({"path_len": 3}),
            "3 repetidor(es), nombres no disponibles",
        )

    def test_future_rx_path_resolves_repeater_names_from_contacts(self) -> None:
        """Los hashes RX se convierten en nombres usando la caché ya existente."""
        bridge = self._bridge()

        bridge._meshcore_remember_contact(
            {
                "public_key": "aa11223344556677889900aabbccddeeff",
                "name": "RPT-NORTE",
            }
        )
        bridge._meshcore_remember_contact(
            {
                "public_key": "bb11223344556677889900aabbccddeeff",
                "name": "RPT-SUR",
            }
        )

        enriched = bridge._meshcore_enrich_path_info(
            {
                "path_len": 2,
                "path_hash_size": 1,
                "path": "aabb",
            }
        )

        self.assertEqual(
            [item["name"] for item in enriched["meshcore_repeaters"]],
            ["RPT-NORTE", "RPT-SUR"],
        )
        self.assertTrue(all(item["resolved"] for item in enriched["meshcore_repeaters"]))

        formatter = self.ns["_meshcore_format_repeater_path"]
        self.assertEqual(formatter(enriched), "RPT-NORTE -> RPT-SUR")

    def test_unknown_hash_is_preserved_in_route(self) -> None:
        """Un salto desconocido conserva su hash y no altera los saltos resueltos."""
        bridge = self._bridge()
        bridge._meshcore_remember_contact(
            {
                "public_key": "aa11223344556677889900aabbccddeeff",
                "name": "RPT-CONOCIDO",
            }
        )

        enriched = bridge._meshcore_enrich_path_info(
            {
                "path_len": 2,
                "path_hash_size": 1,
                "path": "aacc",
            }
        )

        self.assertEqual(enriched["meshcore_repeaters"][0]["name"], "RPT-CONOCIDO")
        self.assertEqual(enriched["meshcore_repeaters"][1]["name"], "cc")
        self.assertFalse(enriched["meshcore_repeaters"][1]["resolved"])

    def test_short_hash_collision_never_selects_arbitrary_repeater(self) -> None:
        """Una colisión de hash corto se marca ambigua en vez de falsear la ruta."""
        bridge = self._bridge()
        bridge._meshcore_remember_contact(
            {
                "public_key": "cc11223344556677889900aabbccddeeff",
                "name": "RPT-UNO",
            }
        )
        bridge._meshcore_remember_contact(
            {
                "public_key": "ccffeeddccbbaa00998877665544332211",
                "name": "RPT-DOS",
            }
        )

        enriched = bridge._meshcore_enrich_path_info(
            {
                "path_len": 1,
                "path_hash_size": 1,
                "path": "cc",
            }
        )

        hop = enriched["meshcore_repeaters"][0]
        self.assertFalse(hop["resolved"])
        self.assertTrue(hop["ambiguous"])
        self.assertIn("prefijo ambiguo: 2 contactos", hop["name"])
        self.assertNotEqual(hop["name"], "RPT-UNO")
        self.assertNotEqual(hop["name"], "RPT-DOS")


if __name__ == "__main__":
    unittest.main()
