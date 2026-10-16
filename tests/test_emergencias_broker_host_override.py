"""Regresión del host de broker en instalaciones Docker y systemd.

Ejecutar con: python -m pytest -q tests/test_emergencias_broker_host_override.py
"""
from __future__ import annotations

import json
from unittest.mock import patch

from tools.emergencias_guardia.emergencias import config as emergency_config


def test_docker_broker_host_overrides_persisted_loopback(tmp_path):
    """La variable de Docker prevalece en memoria, sin reescribir config.json."""
    path = tmp_path / "config.json"
    original = {"notifications": {"broker": {"host": "127.0.0.1", "port": 8766}}}
    path.write_text(json.dumps(original), encoding="utf-8")
    with patch.object(emergency_config, "CONFIG_FILE", path), patch.dict(
        "os.environ", {"BROKER_CTRL_HOST": "broker"}
    ):
        result = emergency_config.load_config(create=False)
    assert result["notifications"]["broker"]["host"] == "broker"
    assert result["notifications"]["broker"]["port"] == 8766
    assert json.loads(path.read_text(encoding="utf-8")) == original


def test_systemd_without_env_preserves_explicit_host(tmp_path):
    """Sin override se conserva el host existente en la instalación legacy."""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"notifications": {"broker": {"host": "192.0.2.10"}}}),
        encoding="utf-8",
    )
    with patch.object(emergency_config, "CONFIG_FILE", path), patch.dict(
        "os.environ", {}, clear=True
    ):
        result = emergency_config.load_config(create=False)
    assert result["notifications"]["broker"]["host"] == "192.0.2.10"


def test_no_config_uses_docker_host_only_in_memory(tmp_path):
    """El primer arranque Docker admite broker sin escribir un host temporal."""
    path = tmp_path / "config.json"
    with patch.object(emergency_config, "CONFIG_FILE", path), patch.dict(
        "os.environ", {"BROKER_CTRL_HOST": "broker"}
    ):
        result = emergency_config.load_config(create=False)
    assert result["notifications"]["broker"]["host"] == "broker"
    assert not path.exists()
