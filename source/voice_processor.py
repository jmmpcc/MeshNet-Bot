#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
voice_processor.py — STT/TTS offline para MeshNet-Bot
- STT: faster-whisper (offline, CPU, modelos tiny/base/small)
- TTS: piper-tts (offline, alta calidad) con fallback a pyttsx3 + espeak-ng
Los modelos se descargan una sola vez en el directorio VOICE_MODELS_DIR.
"""
import io
import logging
import os
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Config desde entorno ──────────────────────────────────────────────────────
_WHISPER_MODEL    = os.getenv("VOICE_WHISPER_MODEL", "tiny")
_WHISPER_LANG     = os.getenv("VOICE_WHISPER_LANG", "es")
_WHISPER_DEVICE   = os.getenv("VOICE_WHISPER_DEVICE", "cpu")
_WHISPER_COMPUTE  = os.getenv("VOICE_WHISPER_COMPUTE", "int8")
_PIPER_VOICE      = os.getenv("VOICE_PIPER_VOICE", "es_ES-sharvard-medium")
_MODELS_DIR       = Path(os.getenv("VOICE_MODELS_DIR", "/app/bot_data/voice_models"))

# ── Lazy singletons con lock para thread-safety ───────────────────────────────
_whisper_lock  = threading.Lock()
_piper_lock    = threading.Lock()
_pyttsx3_lock  = threading.Lock()
_whisper_model = None          # WhisperModel | False (unavailable)
_piper_voice   = None          # PiperVoice   | False (unavailable)
_pyttsx3_engine = None         # pyttsx3.Engine | False


# ── Conversión de audio ───────────────────────────────────────────────────────

def _run_ffmpeg(args: list, timeout: int = 30) -> None:
    result = subprocess.run(
        ["ffmpeg", "-y"] + args,
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg error: {result.stderr.decode(errors='replace')[:300]}")


def _ogg_to_wav(ogg_bytes: bytes) -> str:
    """Guarda OGG en temporal, convierte a WAV 16 kHz mono. Devuelve ruta WAV."""
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
        f.write(ogg_bytes)
        in_path = f.name
    out_path = in_path.replace(".ogg", ".wav")
    _run_ffmpeg(["-i", in_path, "-ar", "16000", "-ac", "1", out_path])
    os.unlink(in_path)
    return out_path


def _wav_to_ogg(wav_bytes: bytes) -> bytes:
    """Convierte WAV a OGG Opus para nota de voz de Telegram."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(wav_bytes)
        in_path = f.name
    out_path = in_path.replace(".wav", ".ogg")
    _run_ffmpeg(["-i", in_path, "-c:a", "libopus", "-b:a", "24k", out_path])
    os.unlink(in_path)
    with open(out_path, "rb") as f:
        data = f.read()
    os.unlink(out_path)
    return data


# ── STT (voz → texto) ─────────────────────────────────────────────────────────

def _get_whisper():
    global _whisper_model
    if _whisper_model is not None:
        return _whisper_model
    with _whisper_lock:
        if _whisper_model is not None:
            return _whisper_model
        try:
            from faster_whisper import WhisperModel
            logger.info("[voice] Cargando modelo Whisper '%s' (primera vez)…", _WHISPER_MODEL)
            _whisper_model = WhisperModel(
                _WHISPER_MODEL,
                device=_WHISPER_DEVICE,
                compute_type=_WHISPER_COMPUTE,
            )
            logger.info("[voice] Modelo Whisper listo.")
        except Exception as e:
            logger.warning("[voice] faster-whisper no disponible: %s", e)
            _whisper_model = False
    return _whisper_model


def transcribe(ogg_bytes: bytes, language: str = _WHISPER_LANG) -> str:
    """
    Transcribe audio OGG/Opus a texto.
    Lanza RuntimeError si STT no está disponible o falla la transcripción.
    """
    model = _get_whisper()
    if not model:
        raise RuntimeError("faster-whisper no está instalado o no se pudo cargar el modelo")

    wav_path = _ogg_to_wav(ogg_bytes)
    try:
        segments, _ = model.transcribe(
            wav_path,
            language=language,
            beam_size=1,
            vad_filter=True,
        )
        texto = " ".join(seg.text.strip() for seg in segments).strip()
    finally:
        try:
            os.unlink(wav_path)
        except OSError:
            pass
    return texto


# ── TTS (texto → voz) ─────────────────────────────────────────────────────────

def _download_piper_model(voice: str, dest: Path) -> None:
    """Descarga modelo Piper desde GitHub releases (solo primera vez)."""
    import urllib.request
    base = "https://github.com/rhasspy/piper/releases/download/v1.2.0"
    for ext in (".onnx", ".onnx.json"):
        url = f"{base}/{voice}{ext}"
        out = dest / f"{voice}{ext}"
        logger.info("[voice] Descargando modelo Piper: %s → %s", url, out)
        urllib.request.urlretrieve(url, str(out))


def _get_piper():
    global _piper_voice
    if _piper_voice is not None:
        return _piper_voice
    with _piper_lock:
        if _piper_voice is not None:
            return _piper_voice
        try:
            from piper.voice import PiperVoice
            _MODELS_DIR.mkdir(parents=True, exist_ok=True)
            model_path  = _MODELS_DIR / f"{_PIPER_VOICE}.onnx"
            config_path = _MODELS_DIR / f"{_PIPER_VOICE}.onnx.json"
            if not model_path.exists() or not config_path.exists():
                _download_piper_model(_PIPER_VOICE, _MODELS_DIR)
            _piper_voice = PiperVoice.load(str(model_path), config_path=str(config_path))
            logger.info("[voice] Modelo Piper '%s' listo.", _PIPER_VOICE)
        except Exception as e:
            logger.warning("[voice] piper-tts no disponible: %s", e)
            _piper_voice = False
    return _piper_voice


def _tts_piper(text: str) -> bytes:
    voice = _get_piper()
    if not voice:
        raise RuntimeError("piper no disponible")
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        voice.synthesize(text, wf)
    return _wav_to_ogg(buf.getvalue())


def _get_pyttsx3():
    global _pyttsx3_engine
    if _pyttsx3_engine is not None:
        return _pyttsx3_engine
    with _pyttsx3_lock:
        if _pyttsx3_engine is not None:
            return _pyttsx3_engine
        try:
            import pyttsx3
            eng = pyttsx3.init()
            eng.setProperty("rate", 145)
            # Intentar seleccionar voz española
            for v in eng.getProperty("voices"):
                if "es" in (v.languages[0] if v.languages else "").lower() or "spanish" in v.name.lower():
                    eng.setProperty("voice", v.id)
                    break
            _pyttsx3_engine = eng
            logger.info("[voice] Motor pyttsx3 listo.")
        except Exception as e:
            logger.warning("[voice] pyttsx3 no disponible: %s", e)
            _pyttsx3_engine = False
    return _pyttsx3_engine


def _tts_pyttsx3(text: str) -> bytes:
    eng = _get_pyttsx3()
    if not eng:
        raise RuntimeError("pyttsx3 no disponible")
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wav_path = f.name
    eng.save_to_file(text, wav_path)
    eng.runAndWait()
    with open(wav_path, "rb") as f:
        wav = f.read()
    os.unlink(wav_path)
    return _wav_to_ogg(wav)


def synthesize(text: str) -> bytes:
    """
    Convierte texto a nota de voz OGG Opus.
    Prueba Piper primero, luego pyttsx3. Lanza RuntimeError si ambos fallan.
    """
    errors = []
    for fn in (_tts_piper, _tts_pyttsx3):
        try:
            return fn(text)
        except Exception as e:
            errors.append(f"{fn.__name__}: {e}")
    raise RuntimeError(f"TTS no disponible: {'; '.join(errors)}")


# ── Helpers de disponibilidad ─────────────────────────────────────────────────

def is_stt_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def is_tts_available() -> bool:
    for mod in ("piper.voice", "pyttsx3"):
        try:
            __import__(mod)
            return True
        except ImportError:
            pass
    return False


def ffmpeg_available() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        return True
    except Exception:
        return False
