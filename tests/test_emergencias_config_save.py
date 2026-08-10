from __future__ import annotations

import json

from tools.emergencias_guardia.emergencias import config as emergency_config


def test_save_config_writes_complete_json_atomically(tmp_path, monkeypatch) -> None:
    """
    Verifica la API pública save_config() utilizada por la CLI de emergencias.

    La prueba redirige CONFIG_FILE a un directorio temporal para no modificar
    la configuración real del proyecto. save_config() debe escribir el
    diccionario completo mediante atomic_write_json() y producir JSON válido.
    """
    config_file = tmp_path / "config.json"
    monkeypatch.setattr(emergency_config, "CONFIG_FILE", config_file)

    payload = {
        "schema_version": 1,
        "areas": [{"id": "zaragoza", "type": "province", "name": "Zaragoza"}],
        "sources": {"dgt_datex": {"enabled": True}},
    }

    emergency_config.save_config(payload)

    assert config_file.exists()
    assert json.loads(config_file.read_text(encoding="utf-8")) == payload


def test_cli_can_import_save_config() -> None:
    """Evita que cli.py vuelva a importar una función inexistente."""
    from tools.emergencias_guardia.emergencias.config import save_config

    assert callable(save_config)
