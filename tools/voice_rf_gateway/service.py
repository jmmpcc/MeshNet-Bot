from __future__ import annotations

import json
import logging
import os
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .config import VoiceRfConfig
from .text_normalizer import compose_emergency_voice_text
from .tts import TtsSynthesizer

LOGGER = logging.getLogger("meshnet.voice_rf")


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


class VoiceRfApplication:
    """Núcleo del servicio Voice RF v7.0.34.

    El servicio acepta solicitudes y puede sintetizar WAV cuando está
    habilitado, pero nunca transmite ni controla PTT en esta fase.
    """

    def __init__(self, config: VoiceRfConfig | None = None) -> None:
        self.config = config or VoiceRfConfig.from_env()
        self.synthesizer = TtsSynthesizer(self.config)

    def health(self) -> dict[str, Any]:
        """Devuelve estado de configuración y disponibilidad de motores."""
        primary_ok, primary_reason = self.synthesizer.engine_available(self.config.tts_engine)
        fallback_ok, fallback_reason = self.synthesizer.engine_available(
            self.config.fallback_engine
        )
        return {
            "ok": True,
            "version": "7.0.34",
            "service_enabled": self.config.service_enabled,
            "transmit_enabled": False,
            "transmit_reason": "not_implemented_safety_lock",
            "tts": {
                "primary": {
                    "engine": self.config.tts_engine,
                    "available": primary_ok,
                    "reason": primary_reason,
                },
                "fallback": {
                    "engine": self.config.fallback_engine,
                    "available": fallback_ok,
                    "reason": fallback_reason,
                },
            },
            "output_dir": str(self.config.output_dir),
        }

    def dispatch(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Procesa una emergencia y devuelve un resultado aislado.

        Requiere `VOICE_RF_SERVICE_ENABLED=1` para sintetizar. Incluso entonces,
        la respuesta siempre indica `sent=false`, porque esta versión no tiene
        implementación de reproducción, PTT ni acceso al transmisor.
        """
        if not self.config.service_enabled:
            return {"ok": True, "generated": False, "sent": False, "reason": "disabled"}
        text = str(payload.get("text") or "").strip()
        if not text:
            return {"ok": False, "generated": False, "sent": False, "reason": "empty_text"}
        callsign = str(os.getenv("VOICE_RF_CALLSIGN", "EB2EAS") or "EB2EAS")
        is_test = bool(payload.get("is_test", False))
        try:
            speech = compose_emergency_voice_text(
                text,
                callsign=callsign,
                is_test=is_test,
                max_chars=self.config.max_text_chars,
            )
        except ValueError as exc:
            return {
                "ok": False,
                "generated": False,
                "sent": False,
                "reason": str(exc),
            }
        result = self.synthesizer.synthesize(
            speech,
            prefix=f"emergency_{str(payload.get('event_id') or 'unknown').replace('/', '_')}",
        )
        response = result.to_dict()
        response.update({
            "generated": result.ok,
            "sent": False,
            "transmit_reason": "not_implemented_safety_lock",
            "event_id": str(payload.get("event_id") or ""),
            "created_at": time.time(),
        })
        if result.ok and not self.config.keep_audio:
            # En solicitudes automáticas de esta fase se elimina el WAV después
            # de validarlo para no llenar el disco de la Raspberry.
            Path(result.output_path).unlink(missing_ok=True)
            response["output_path"] = ""
            response["reason"] = "generated_and_discarded"
        return response


def make_handler(application: VoiceRfApplication):
    """Crea el handler HTTP vinculado a una aplicación concreta."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "MeshNetVoiceRF/7.0.34"

        def log_message(self, fmt: str, *args: object) -> None:
            LOGGER.info("http %s", fmt % args)

        def do_GET(self) -> None:  # noqa: N802 - contrato BaseHTTPRequestHandler
            if self.path.rstrip("/") == "/health":
                _json_response(self, HTTPStatus.OK, application.health())
                return
            _json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "reason": "not_found"})

        def do_POST(self) -> None:  # noqa: N802 - contrato BaseHTTPRequestHandler
            if self.path.rstrip("/") != "/dispatch":
                _json_response(self, HTTPStatus.NOT_FOUND, {"ok": False, "reason": "not_found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0 or length > 65536:
                _json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "reason": "invalid_length"})
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                _json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "reason": "invalid_json"})
                return
            if not isinstance(payload, dict):
                _json_response(self, HTTPStatus.BAD_REQUEST, {"ok": False, "reason": "invalid_payload"})
                return
            result = application.dispatch(payload)
            status = HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_REQUEST
            _json_response(self, status, result)

    return Handler


def serve(config: VoiceRfConfig | None = None) -> None:
    """Inicia el servidor HTTP local hasta recibir SIGTERM/SIGINT."""
    selected = config or VoiceRfConfig.from_env()
    application = VoiceRfApplication(selected)
    server = ThreadingHTTPServer((selected.bind, selected.port), make_handler(application))
    LOGGER.info(
        "Voice RF API escuchando en http://%s:%s; service_enabled=%s; RF bloqueada",
        selected.bind,
        selected.port,
        selected.service_enabled,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
