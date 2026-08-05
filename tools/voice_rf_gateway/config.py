from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_TRUTHY = {"1", "true", "yes", "on", "si", "sí", "y"}


def env_bool(name: str, default: bool = False) -> bool:
    """Lee una variable booleana de entorno de forma tolerante.

    Parámetros:
        name: nombre de la variable.
        default: valor usado cuando no existe.

    Retorna:
        True para valores afirmativos conocidos; False en el resto.
    """
    fallback = "1" if default else "0"
    return str(os.getenv(name, fallback) or fallback).strip().lower() in _TRUTHY


def env_int(name: str, default: int, minimum: int = 0) -> int:
    """Lee un entero de entorno aplicando un mínimo seguro."""
    try:
        return max(minimum, int(str(os.getenv(name, default) or default).strip()))
    except (TypeError, ValueError):
        return max(minimum, int(default))


def env_float(name: str, default: float, minimum: float = 0.0) -> float:
    """Lee un decimal de entorno aplicando un mínimo seguro."""
    try:
        return max(minimum, float(str(os.getenv(name, default) or default).strip()))
    except (TypeError, ValueError):
        return max(minimum, float(default))


@dataclass(frozen=True, slots=True)
class VoiceRfConfig:
    """Configuración inmutable del gateway de voz.

    Esta fase permite síntesis local y API de diagnóstico. `transmit_enabled`
    se conserva para futuras fases, pero el código v7.0.34 nunca acciona PTT.
    """

    service_enabled: bool
    bind: str
    port: int
    tts_engine: str
    fallback_engine: str
    piper_bin: str
    piper_model: str
    espeak_bin: str
    espeak_voice: str
    espeak_speed: int
    output_dir: Path
    keep_audio: bool
    max_text_chars: int
    max_audio_seconds: float
    transmit_enabled: bool

    @classmethod
    def from_env(cls) -> "VoiceRfConfig":
        """Construye la configuración a partir del `.env` operativo."""
        data_dir = Path(os.getenv("BOT_DATA_DIR", "/app/bot_data"))
        output_dir = Path(
            os.getenv("VOICE_RF_OUTPUT_DIR", str(data_dir / "voice_rf"))
        )
        return cls(
            service_enabled=env_bool("VOICE_RF_SERVICE_ENABLED", False),
            bind=str(os.getenv("VOICE_RF_SERVICE_BIND", "127.0.0.1") or "127.0.0.1").strip(),
            port=env_int("VOICE_RF_SERVICE_PORT", 8790, 1),
            tts_engine=str(os.getenv("VOICE_RF_TTS_ENGINE", "piper") or "piper").strip().lower(),
            fallback_engine=str(
                os.getenv("VOICE_RF_TTS_FALLBACK_ENGINE", "espeak-ng") or "espeak-ng"
            ).strip().lower(),
            piper_bin=str(os.getenv("VOICE_RF_PIPER_BIN", "/usr/local/bin/piper") or "").strip(),
            piper_model=str(os.getenv("VOICE_RF_PIPER_MODEL", "") or "").strip(),
            espeak_bin=str(os.getenv("VOICE_RF_ESPEAK_BIN", "/usr/bin/espeak-ng") or "").strip(),
            espeak_voice=str(os.getenv("VOICE_RF_ESPEAK_VOICE", "es") or "es").strip(),
            espeak_speed=env_int("VOICE_RF_ESPEAK_SPEED", 145, 80),
            output_dir=output_dir,
            keep_audio=env_bool("VOICE_RF_KEEP_AUDIO", False),
            max_text_chars=env_int("VOICE_RF_MAX_TEXT_CHARS", 700, 80),
            max_audio_seconds=env_float("VOICE_RF_MAX_AUDIO_SECONDS", 40.0, 1.0),
            transmit_enabled=env_bool("VOICE_RF_TRANSMIT_ENABLED", False),
        )
