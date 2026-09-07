#!/usr/bin/env python3
"""Servicio mínimo de estado para MeshNet Intelligence (Fase IA-0).

No integra todavía IA con ninguna función operativa. Expone únicamente estado
seguro en HTTP para futuras integraciones con Control Panel y Mobile API.

Uso:
    python3 tools/intelligence/meshnet_intelligence.py --status
    python3 tools/intelligence/meshnet_intelligence.py --serve
    python3 tools/intelligence/meshnet_intelligence.py --serve --host 127.0.0.1 --port 8792

Por seguridad el bind por defecto es exclusivamente localhost. El JSON nunca
incluye MESHNET_AI_API_KEY.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# Permite ejecutar el fichero directamente desde tools/intelligence sin instalar
# MeshNet como paquete Python.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.meshnet_ai import MeshNetAI  # noqa: E402


class IntelligenceStatus:
    """Mantiene una instancia IA para que el circuit breaker conserve su estado."""

    def __init__(self) -> None:
        self.ai = MeshNetAI.from_env()

    def health(self) -> dict[str, Any]:
        """Devuelve estado público, estable y sin secretos para supervisión."""
        status = self.ai.public_status()
        return {
            "service": "meshnet-intelligence",
            "phase": "IA-0",
            **status,
        }


class IntelligenceHandler(BaseHTTPRequestHandler):
    """HTTP handler con un único endpoint GET /health y sin escritura."""

    server_version = "MeshNetIntelligence/IA-0"

    def do_GET(self) -> None:  # noqa: N802 - nombre exigido por BaseHTTPRequestHandler.
        if self.path.rstrip("/") != "/health":
            self._write_json(404, {"error": "not_found"})
            return
        state: IntelligenceStatus = self.server.intelligence_state  # type: ignore[attr-defined]
        self._write_json(200, state.health())

    def _write_json(self, status_code: int, payload: dict[str, Any]) -> None:
        """Serializa una respuesta JSON UTF-8 con cabeceras mínimas."""
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt: str, *args: object) -> None:
        """Conserva log HTTP mínimo sin volcar configuración ni parámetros sensibles."""
        sys.stderr.write("[meshnet-intelligence] %s\n" % (fmt % args))


class IntelligenceHTTPServer(ThreadingHTTPServer):
    """Servidor HTTP que comparte el estado IA entre peticiones."""

    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], state: IntelligenceStatus):
        super().__init__(server_address, IntelligenceHandler)
        self.intelligence_state = state


def serve(host: str, port: int) -> None:
    """Arranca el health server local hasta recibir interrupción del proceso."""
    state = IntelligenceStatus()
    server = IntelligenceHTTPServer((host, port), state)
    print(f"[meshnet-intelligence] health http://{host}:{port}/health", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> None:
    """CLI de diagnóstico/servicio; no ejecuta operaciones IA por sí misma."""
    parser = argparse.ArgumentParser(description="MeshNet Intelligence - Fase IA-0")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--status", action="store_true", help="muestra estado seguro y termina")
    mode.add_argument("--serve", action="store_true", help="expone GET /health")
    parser.add_argument(
        "--host",
        default=os.getenv("MESHNET_AI_HEALTH_HOST", "127.0.0.1"),
        help="bind del health server; por defecto 127.0.0.1",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("MESHNET_AI_HEALTH_PORT", "8792")),
        help="puerto del health server; por defecto 8792",
    )
    args = parser.parse_args()

    if args.status:
        print(json.dumps(IntelligenceStatus().health(), ensure_ascii=False, indent=2, sort_keys=True))
        return

    if not 1 <= args.port <= 65535:
        parser.error("--port debe estar entre 1 y 65535")
    serve(args.host, args.port)


if __name__ == "__main__":
    main()
