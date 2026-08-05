from __future__ import annotations

import shutil
import subprocess
import uuid
import wave
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from .config import VoiceRfConfig


@dataclass(slots=True)
class SynthesisResult:
    """Resultado verificable de una síntesis de voz local."""

    ok: bool
    engine: str
    output_path: str
    duration_seconds: float
    reason: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def wav_duration(path: Path) -> float:
    """Devuelve la duración real de un WAV PCM mediante su cabecera."""
    with wave.open(str(path), "rb") as handle:
        frames = handle.getnframes()
        rate = handle.getframerate()
    if rate <= 0:
        raise ValueError("wav_invalid_sample_rate")
    return frames / float(rate)


class TtsSynthesizer:
    """Sintetizador local con Piper y fallback eSpeak NG.

    No reproduce audio, no abre ALSA, no controla PTT y no transmite RF.
    Únicamente crea un fichero WAV validado.
    """

    def __init__(self, config: VoiceRfConfig) -> None:
        self.config = config

    def _new_output_path(self, prefix: str = "voice") -> Path:
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        return self.config.output_dir / f"{prefix}_{uuid.uuid4().hex[:12]}.wav"

    def engine_available(self, engine: str) -> tuple[bool, str]:
        """Comprueba binario y modelo requeridos por un motor."""
        normalized = str(engine or "").strip().lower()
        if normalized == "piper":
            binary = Path(self.config.piper_bin)
            model = Path(self.config.piper_model) if self.config.piper_model else None
            if not binary.is_file() and shutil.which(self.config.piper_bin) is None:
                return False, "piper_binary_missing"
            if model is None or not model.is_file():
                return False, "piper_model_missing"
            return True, "ok"
        if normalized in {"espeak", "espeak-ng"}:
            if Path(self.config.espeak_bin).is_file() or shutil.which(self.config.espeak_bin):
                return True, "ok"
            return False, "espeak_binary_missing"
        return False, "unsupported_engine"

    def _run_piper(self, text: str, output: Path) -> None:
        subprocess.run(
            [self.config.piper_bin, "--model", self.config.piper_model, "--output_file", str(output)],
            input=text,
            text=True,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=90,
        )

    def _run_espeak(self, text: str, output: Path) -> None:
        subprocess.run(
            [
                self.config.espeak_bin,
                "-v", self.config.espeak_voice,
                "-s", str(self.config.espeak_speed),
                "-w", str(output),
                text,
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=90,
        )

    def _synthesize_with_engine(self, engine: str, text: str, output: Path) -> SynthesisResult:
        available, reason = self.engine_available(engine)
        if not available:
            return SynthesisResult(False, engine, "", 0.0, reason=reason)
        try:
            if engine == "piper":
                self._run_piper(text, output)
            else:
                self._run_espeak(text, output)
            if not output.is_file() or output.stat().st_size <= 44:
                return SynthesisResult(False, engine, "", 0.0, reason="wav_not_created")
            duration = wav_duration(output)
            if duration > self.config.max_audio_seconds:
                output.unlink(missing_ok=True)
                return SynthesisResult(
                    False, engine, "", duration, reason="audio_too_long"
                )
            return SynthesisResult(True, engine, str(output), duration, reason="generated")
        except subprocess.TimeoutExpired as exc:
            output.unlink(missing_ok=True)
            return SynthesisResult(False, engine, "", 0.0, reason="tts_timeout", error=str(exc))
        except (subprocess.CalledProcessError, OSError, ValueError, wave.Error) as exc:
            output.unlink(missing_ok=True)
            return SynthesisResult(
                False,
                engine,
                "",
                0.0,
                reason="tts_failed",
                error=f"{type(exc).__name__}: {exc}",
            )

    def synthesize(self, text: str, *, prefix: str = "voice") -> SynthesisResult:
        """Genera WAV usando motor principal y fallback, sin reproducirlo."""
        engines: list[str] = []
        for candidate in (self.config.tts_engine, self.config.fallback_engine):
            candidate = str(candidate or "").strip().lower()
            if candidate and candidate not in engines:
                engines.append(candidate)
        last = SynthesisResult(False, "", "", 0.0, reason="no_tts_engine")
        for engine in engines:
            output = self._new_output_path(prefix)
            last = self._synthesize_with_engine(engine, text, output)
            if last.ok:
                return last
        return last
