"""Regresión de permisos al publicar el mapa desde un recolector Docker.

Uso:
    python3 -m unittest tools.emergencias_guardia.tests.test_public_map_file_permissions

No utiliza Docker, systemd, FTPS ni datos productivos.
"""
from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.emergencias_guardia.emergencias import config


class PublicMapFilePermissionsTests(unittest.TestCase):
    """Verifica la escritura atómica y el aislamiento de archivos internos."""

    def test_repeated_current_writes_remain_group_readable(self) -> None:
        """Cada sustitución atómica conserva el grupo y los permisos 0640."""
        with tempfile.TemporaryDirectory() as root:
            data_dir = Path(root)
            current = data_dir / "current.json"
            with patch.object(config, "DATA_DIR", data_dir):
                for index in range(2):
                    config.atomic_write_json(current, {"updated_at": index})
                    self.assertEqual(
                        stat.S_IMODE(current.stat().st_mode), 0o640
                    )
                    self.assertEqual(current.stat().st_gid, data_dir.stat().st_gid)

    def test_state_keeps_private_permissions(self) -> None:
        """No se amplían los permisos del estado ni de otros archivos."""
        with tempfile.TemporaryDirectory() as root:
            data_dir = Path(root)
            state = data_dir / "state.json"
            with patch.object(config, "DATA_DIR", data_dir):
                config.atomic_write_json(state, {"secret": "synthetic"})
                self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
