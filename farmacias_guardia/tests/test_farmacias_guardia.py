import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "farmacias_guardia.py"
spec = importlib.util.spec_from_file_location("farmacias_guardia_app", MODULE_PATH)
app = importlib.util.module_from_spec(spec)
import sys
sys.modules["farmacias_guardia_app"] = app
spec.loader.exec_module(app)


class FarmaciasAppTests(unittest.TestCase):
    def test_normalize_generic_api_record(self):
        item = app.normalize_record({
            "title": "Farmacia Ejemplo",
            "streetAddress": "Avenida de Madrid, 120",
            "telephone": "976123456",
            "addressLocality": "Zaragoza",
            "sector": "Delicias",
            "horarioGuardia": "09:15 a 09:15",
            "id": "zgz-1",
        }, "2026-07-24")
        self.assertIsNotNone(item)
        self.assertEqual(item.locality, "Zaragoza")
        self.assertEqual(item.area, "Delicias")
        self.assertEqual(item.identity, "zgz-1")

    def test_byte_chunks_respect_utf8_limit(self):
        lines = ["DELICIAS", "Av Madrid 120 · 976123456", "C/ Ramón y Cajal 4 · 976000000"]
        chunks = app.byte_chunks(lines, "GUARDIA ZARAGOZA 24/07", 90)
        self.assertGreaterEqual(len(chunks), 2)
        self.assertTrue(all(len(chunk.encode("utf-8")) <= 90 for chunk in chunks))

    def test_byte_chunks_truncates_single_oversized_line(self):
        chunks = app.byte_chunks(["C/ " + "Ramón " * 40 + "· 976000000"], "GUARDIA ZARAGOZA 24/07", 90)
        self.assertEqual(len(chunks), 1)
        self.assertTrue(chunks[0].endswith("…"))
        self.assertTrue(all(len(chunk.encode("utf-8")) <= 90 for chunk in chunks))

    def test_byte_chunks_honors_tiny_byte_limit(self):
        chunks = app.byte_chunks(["C/ Ramón y Cajal 4"], "GUARDIA ZARAGOZA 24/07", 10)
        self.assertTrue(chunks)
        self.assertTrue(all(len(chunk.encode("utf-8")) <= 10 for chunk in chunks))

    def test_byte_chunks_renumbers_after_late_split(self):
        lines = ["A" * 28, "B" * 28, "C" * 28]
        chunks = app.byte_chunks(lines, "HDR", 45)
        self.assertEqual(
            [chunk.split("\n", 1)[0] for chunk in chunks],
            ["HDR [1/3]", "HDR [2/3]", "HDR [3/3]"],
        )
        self.assertTrue(all(len(chunk.encode("utf-8")) <= 45 for chunk in chunks))

    def test_canonical_hash_ignores_input_order(self):
        a = app.Pharmacy("A", "C/ Uno 1", "1", "Utebo", "Utebo", "G", "2026-07-24", "1")
        b = app.Pharmacy("B", "C/ Dos 2", "2", "Zaragoza", "Centro", "G", "2026-07-24", "2")
        self.assertEqual(app.canonical_hash([a, b]), app.canonical_hash([b, a]))


if __name__ == "__main__":
    unittest.main()
