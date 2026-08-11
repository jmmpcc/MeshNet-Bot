from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SHIM_PATH = ROOT / "source" / "reverse_geocoder.py"


def _load_shim():
    """Carga el shim por ruta para no depender de un paquete externo instalado."""
    spec = importlib.util.spec_from_file_location("meshnet_reverse_geocoder_v7053", SHIM_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"No se puede cargar {SHIM_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._lookup_cached.cache_clear()
    return module


class NodeGeolocationV7053Tests(unittest.TestCase):
    """Regresiones del reemplazo ligero de reverse_geocoder en v7.0.53."""

    def test_search_accepts_single_tuple_used_by_legacy_helper(self):
        """Conserva ``rg.search((lat, lon))`` usado por _get_province_offline."""
        rg = _load_shim()
        with (
            patch.object(rg, "resolve_province", return_value="Huesca"),
            patch.object(
                rg,
                "resolve_nearest_population",
                return_value={
                    "name": "Salinas de Jaca",
                    "distance_km": 3.6,
                    "latitude": 42.412645,
                    "longitude": -0.789788,
                    "inhabitants": 20,
                    "codine": "22173000601",
                },
            ),
        ):
            result = rg.search((42.4407287, -0.7678461))

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "Salinas de Jaca")
        self.assertEqual(result[0]["admin2"], "Huesca")
        self.assertAlmostEqual(result[0]["distance_km"], 3.6)

    def test_search_accepts_list_used_by_ver_nodos_and_vecinos(self):
        """Conserva ``rg.search([(lat, lon)])`` usado por _place_of."""
        rg = _load_shim()
        with (
            patch.object(rg, "resolve_province", return_value="Zaragoza"),
            patch.object(
                rg,
                "resolve_nearest_population",
                return_value={
                    "name": "Zaragoza",
                    "distance_km": 1.25,
                    "latitude": 41.6488,
                    "longitude": -0.8891,
                    "inhabitants": 0,
                    "codine": "",
                },
            ),
        ):
            result = rg.search([(41.65, -0.89)])

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "Zaragoza")
        self.assertEqual(result[0]["admin2"], "Zaragoza")

    def test_network_failure_keeps_local_province_fallback(self):
        """Si IGN no responde, /ver_nodos y /vecinos siguen mostrando provincia."""
        rg = _load_shim()
        with (
            patch.object(rg, "resolve_province", return_value="Huesca"),
            patch.object(rg, "resolve_nearest_population", return_value=None),
        ):
            result = rg.search([(42.44, -0.76)])

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "")
        self.assertEqual(result[0]["admin2"], "Huesca")

    def test_lookup_can_disable_network_without_disabling_location(self):
        """BOT_GEO_LOOKUP_ENABLED=0 conserva el resolver provincial local."""
        rg = _load_shim()
        with (
            patch.dict(os.environ, {"BOT_GEO_LOOKUP_ENABLED": "0"}, clear=False),
            patch.object(rg, "resolve_province", return_value="Teruel"),
            patch.object(rg, "resolve_nearest_population") as nearest_mock,
        ):
            result = rg.search([(40.34, -1.10)])

        nearest_mock.assert_not_called()
        self.assertEqual(result[0]["admin2"], "Teruel")

    def test_cache_avoids_repeating_same_node_lookup(self):
        """La misma posición no vuelve a consultar IGN en comandos consecutivos."""
        rg = _load_shim()
        with (
            patch.object(rg, "resolve_province", return_value="Huesca") as province_mock,
            patch.object(
                rg,
                "resolve_nearest_population",
                return_value={
                    "name": "Salinas de Jaca",
                    "distance_km": 3.6,
                    "latitude": 42.412645,
                    "longitude": -0.789788,
                },
            ) as nearest_mock,
        ):
            first = rg.search([(42.4407287, -0.7678461)])
            second = rg.search([(42.4407287, -0.7678461)])

        self.assertEqual(first, second)
        province_mock.assert_called_once()
        nearest_mock.assert_called_once()

    def test_dockerfile_drops_reverse_geocoder_compilation_stack(self):
        """El contenedor no instala reverse_geocoder ni su stack SciPy/Fortran."""
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertNotIn("requirements.geo.txt", dockerfile)
        self.assertNotIn("gfortran", dockerfile)
        self.assertNotIn("libopenblas-dev", dockerfile)
        self.assertNotIn("liblapack-dev", dockerfile)
        self.assertIn("source/*.py", dockerfile)
        self.assertIn("emergencias/geo_admin.py", dockerfile)
        self.assertIn("provincias_espana.geojson", dockerfile)

    def test_external_requirement_file_is_removed(self):
        """La dependencia compilada no debe reaparecer como requirements.geo.txt."""
        self.assertFalse((ROOT / "requirements" / "requirements.geo.txt").exists())


if __name__ == "__main__":
    unittest.main()
