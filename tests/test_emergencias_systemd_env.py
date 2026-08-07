from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "tools" / "emergencias_guardia" / "systemd" / "meshnet-emergencias-check.service"

class EmergenciasSystemdEnvTests(unittest.TestCase):
    def test_main_env_is_loaded_before_local_env(self):
        text = SERVICE.read_text(encoding="utf-8")
        main = "EnvironmentFile=-/home/meshnet/MeshNet-Bot/.env"
        local = "EnvironmentFile=-/home/meshnet/MeshNet-Bot/tools/emergencias_guardia/.env"
        self.assertIn(main, text)
        self.assertIn(local, text)
        self.assertLess(text.index(main), text.index(local))

    def test_notify_changes_command_is_preserved(self):
        text = SERVICE.read_text(encoding="utf-8")
        self.assertIn("emergencias_guardia.py check --notify-changes", text)

if __name__ == "__main__":
    unittest.main()
