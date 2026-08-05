from __future__ import annotations

import re
import unicodedata

_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_MARKUP_RE = re.compile(r"[`*_#<>\[\]{}|]")
_SPACE_RE = re.compile(r"\s+")
_ROAD_RE = re.compile(r"\b([A-Z]{1,3})[- ](\d{1,4})\b", re.IGNORECASE)
_KM_RE = re.compile(r"\bkm\.?\s*(\d+(?:[.,]\d+)?)\b", re.IGNORECASE)


def _ascii_control_safe(value: str) -> str:
    """Elimina caracteres de control conservando Unicode pronunciable."""
    return "".join(char for char in value if char in "\n\t" or unicodedata.category(char) != "Cc")


def normalize_voice_text(text: str, *, max_chars: int = 700) -> str:
    """Convierte un mensaje técnico en texto seguro para síntesis.

    Parámetros:
        text: mensaje original de la emergencia.
        max_chars: longitud máxima aceptada tras normalizar.

    Funcionalidad:
        - elimina URLs y marcas de formato;
        - expande `km 18` a `kilómetro 18`;
        - separa identificadores de carretera para mejorar pronunciación;
        - compacta espacios;
        - añade apertura y cierre claros de prueba/aviso.
    """
    clean = _ascii_control_safe(str(text or ""))
    clean = _URL_RE.sub("", clean)
    clean = _MARKUP_RE.sub(" ", clean)
    clean = _KM_RE.sub(lambda match: f"kilómetro {match.group(1).replace(',', '.')}", clean)
    clean = _ROAD_RE.sub(lambda match: f"{match.group(1).upper()} {match.group(2)}", clean)
    clean = _SPACE_RE.sub(" ", clean).strip(" .,-")
    if not clean:
        raise ValueError("voice_text_empty")
    clean = clean[: max(1, int(max_chars))].rstrip()
    return clean


def compose_emergency_voice_text(
    text: str,
    *,
    callsign: str = "EB2EAS",
    is_test: bool = False,
    max_chars: int = 700,
) -> str:
    """Compone la locución completa de una emergencia.

    Se llama desde el gateway antes de invocar el motor TTS. `is_test` añade
    una advertencia inequívoca para que una prueba nunca parezca real.
    """
    body = normalize_voice_text(text, max_chars=max_chars)
    prefix = "Atención. Prueba técnica. No existe una emergencia real." if is_test else (
        "Atención. Aviso automático de emergencia."
    )
    suffix = f"Fin del aviso. {str(callsign or 'EB2EAS').strip()}."
    return f"{prefix} {body}. {suffix}"
