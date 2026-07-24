import ast
import hashlib
import os
import time
import unittest
from pathlib import Path


BROKER_PATH = Path(__file__).resolve().parents[1] / "source" / "Meshtastic_Broker.py"


def _load_chunk_helpers():
    tree = ast.parse(BROKER_PATH.read_text(encoding="utf-8"))
    wanted = {"_safe_meshcore_max_text_bytes", "_split_meshcore_send_parts"}
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    normalize_method = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_normalize_tx_spool_item"
    )
    nodes.append(normalize_method)
    module = ast.Module(body=nodes, type_ignores=[])
    namespace = {"os": os, "hashlib": hashlib, "time": time}
    exec(compile(ast.fix_missing_locations(module), str(BROKER_PATH), "exec"), namespace)
    return namespace


HELPERS = _load_chunk_helpers()


class MeshCoreChunkingTests(unittest.TestCase):
    def test_default_limit_is_stable_and_conservative(self):
        previous = os.environ.pop("MESHCORE_MAX_TEXT_BYTES", None)
        try:
            self.assertEqual(HELPERS["_safe_meshcore_max_text_bytes"](), 140)
        finally:
            if previous is not None:
                os.environ["MESHCORE_MAX_TEXT_BYTES"] = previous

    def test_parts_include_prefix_and_preserve_full_utf8_text(self):
        text = (
            "FARMA ZARAGOZA\nBarrios/sectores: Avda. Cataluña-Barrio La Jota · "
            "Centro · Delicias · Gran Vía · Las Fuentes · Romareda"
        )
        parts = HELPERS["_split_meshcore_send_parts"](text, 80)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(part.encode("utf-8")) <= 80 for part in parts))
        bodies = [part.split(" ", 1)[1] for part in parts]
        self.assertEqual("".join(bodies), text)
        self.assertEqual(
            [part.split(" ", 1)[0] for part in parts],
            [f"({idx}/{len(parts)})" for idx in range(1, len(parts) + 1)],
        )

    def test_same_text_always_produces_identical_parts(self):
        text = "áéíóú " * 80
        expected = HELPERS["_split_meshcore_send_parts"](text, 140)
        for _retry in range(4):
            self.assertEqual(HELPERS["_split_meshcore_send_parts"](text, 140), expected)

    def test_retry_item_preserves_parts_tx_id_and_pending_index(self):
        class Dummy:
            _tx_max_retries = 3

        parts = tuple(HELPERS["_split_meshcore_send_parts"]("mensaje largo " * 20, 80))
        item = ("abc123", "mensaje largo " * 20, 2, 3, "tx-fixed", parts, 1)
        normalized = HELPERS["_normalize_tx_spool_item"](Dummy(), item)
        self.assertEqual(normalized[2], 2)
        self.assertEqual(normalized[4], "tx-fixed")
        self.assertEqual(normalized[5], parts)
        self.assertEqual(normalized[6], 1)


if __name__ == "__main__":
    unittest.main()
