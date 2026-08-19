from __future__ import annotations

import unittest
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1]
SYSTEMD_DIR = APP_DIR / "systemd"
INSTALLER = APP_DIR / "install_public_map.sh"
PUBLIC_MAP = APP_DIR / "emergencias" / "public_map.py"


class PublicEmergencyMapSystemdTests(unittest.TestCase):
    """Regresiones del disparo automático del mapa público sin ejecutar systemd ni FTPS."""

    def test_path_keeps_immediate_current_json_watch(self) -> None:
        """El observador inmediato continúa vigilando exclusivamente current.json."""
        content = (SYSTEMD_DIR / "meshnet-emergencias-public-map.path").read_text(encoding="utf-8")

        self.assertIn("PathChanged=/home/meshnet/MeshNet-Bot/tools/emergencias_guardia/data/current.json", content)
        self.assertIn("Unit=meshnet-emergencias-public-map.service", content)

    def test_timer_provides_thirty_second_failsafe(self) -> None:
        """El timer recupera cambios perdidos por el .path sin crear otro publicador."""
        content = (SYSTEMD_DIR / "meshnet-emergencias-public-map.timer").read_text(encoding="utf-8")

        self.assertIn("OnBootSec=30s", content)
        self.assertIn("OnUnitActiveSec=30s", content)
        self.assertIn("Unit=meshnet-emergencias-public-map.service", content)
        self.assertIn("WantedBy=timers.target", content)

    def test_installer_enables_path_and_timer_together(self) -> None:
        """Una instalación normal deja activos el disparo inmediato y su respaldo."""
        content = INSTALLER.read_text(encoding="utf-8")

        self.assertIn('TIMER_NAME="meshnet-emergencias-public-map.timer"', content)
        self.assertIn('install -m 0644 "$SOURCE_DIR/$TIMER_NAME" "$SYSTEMD_DIR/$TIMER_NAME"', content)
        self.assertIn('systemctl enable --now "$PATH_NAME" "$TIMER_NAME"', content)

    def test_failsafe_still_uses_revision_deduplication(self) -> None:
        """El timer puede comprobar a menudo porque publish_if_changed evita FTPS sin cambios."""
        content = PUBLIC_MAP.read_text(encoding="utf-8")

        self.assertIn("def publish_if_changed(current_file: Path, state_file: Path)", content)
        self.assertIn('previous.get("revision") == payload["revision"]', content)
        self.assertIn('"unchanged": True', content)


if __name__ == "__main__":
    unittest.main()
