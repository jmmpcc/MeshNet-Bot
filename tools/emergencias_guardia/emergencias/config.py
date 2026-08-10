from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .models import VALID_CATEGORIES


APP_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.getenv("EMERGENCIAS_DATA_DIR", APP_DIR / "data")).resolve()
CONFIG_FILE = Path(os.getenv("EMERGENCIAS_CONFIG_FILE", DATA_DIR / "config.json")).resolve()

DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": 1,
    "api": {"host": "127.0.0.1", "port": 8789, "max_body_bytes": 65536},
    "fetch": {
        "timeout_seconds": 15,
        "max_response_bytes": 10_000_000,
        "resolve_after_missing_fetches": 2,
        "user_agent": "MeshNet-Emergencias/0.1",
    },
    "filters": {"minimum_severity": "low", "categories": sorted(VALID_CATEGORIES)},
    "areas": [],
    "notifications": {
        "enabled": False,
        "transport": "meshcore",
        "max_bytes": 140,
        "max_events_per_broadcast": 3,
        "inter_message_delay_seconds": 8,
        "allow_satellite_detection": False,
        "propagation_filter": {
            "minimum_severity": "low",
            "categories": sorted(VALID_CATEGORIES),
        },
        "incremental": {
            "batch_window_seconds": {
                "emergencias": 0,
                "servicios": 300,
                "meteo": 300,
            },
            "retry_base_seconds": 60,
            "retry_max_seconds": 3600,
            "max_pending": 200,
        },
        "broker": {"host": "127.0.0.1", "port": 8766, "timeout_seconds": 10},
        "routes": {
            "emergencias": {
                "meshcore_channel": -1,
                "meshtastic_channel": -1,
            },
            "servicios": {
                "meshcore_channel": -1,
                "meshtastic_channel": -1,
            },
            "meteo": {
                "meshcore_channel": -1,
                "meshtastic_channel": -1,
            },
        },
    },
    "sources": {
        "dgt_datex": {
            "type": "datex2", "enabled": False,
            "url": "https://nap.dgt.es/datex2/v3/dgt/SituationPublication/datex2_v37.xml",
            "verification": "official", "require_areas": True,
        },
        "municipal_json": {
            "type": "json", "enabled": False,
            "url": "https://www.zaragoza.es/sede/servicio/via-publica/incidencia.json?rows=1000&srsname=wgs84",
            "verification": "official", "default_province": "Zaragoza",
            "default_municipality": "Zaragoza",
            "records_path": "result",
            "mapping": {
                "description": "motivo",
                "category": "tipo.title",
                "road": "calle",
                "started_at": "inicio",
                "updated_at": "lastUpdated",
                "expected_end": "fin",
                "source_url": "uri",
            },
        },
        "ign_earthquakes": {
            "type": "rss", "enabled": False,
            "url": "https://www.ign.es/ign/RssTools/sismologia.xml",
            "profile": "ign_earthquakes", "verification": "official",
            "require_areas": True,
        },
        "nasa_firms": {
            "type": "firms", "enabled": False,
            "url_template": "https://firms.modaps.eosdis.nasa.gov/api/area/csv/{map_key}/{source}/{bbox}/{days}",
            "api_key_env": "FIRMS_MAP_KEY", "dataset": "VIIRS_SNPP_NRT",
            "bbox": [-9.4, 35.8, 4.4, 43.9], "days": 1,
            "verification": "satellite_detection", "require_areas": True,
        },
        "aemet_cap": {
            "type": "aemet_cap", "enabled": False,
            "url": "https://opendata.aemet.es/opendata/api/avisos_cap/ultimoelaborado/area/esp",
            "api_key_env": "AEMET_API_KEY", "verification": "official",
            "require_areas": True,
            "disabled_if_env_enabled": "AEMET_ALERTS_ENABLED",
            "disabled_if_env_default": True,
            "source_url": "https://www.aemet.es/es/eltiempo/prediccion/avisos",
        },
        "che_saih": {
            "type": "che_rss", "enabled": False,
            "url": "https://cph.chebro.es/es/notas-de-prensa-rss",
            "verification": "official", "require_areas": True,
        },
    },
}


def _merge(default: Any, supplied: Any) -> Any:
    if isinstance(default, dict) and isinstance(supplied, dict):
        return {key: _merge(value, supplied.get(key, value)) for key, value in default.items()} | {
            key: value for key, value in supplied.items() if key not in default
        }
    return supplied


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def load_config(create: bool = True) -> dict[str, Any]:
    try:
        supplied = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        supplied = {}
    except (json.JSONDecodeError, OSError):
        supplied = {}
    config = _merge(DEFAULT_CONFIG, supplied)
    if create and not CONFIG_FILE.exists():
        atomic_write_json(CONFIG_FILE, config)
    return config
