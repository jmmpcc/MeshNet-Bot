from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .engine import list_events
from .formatters import byte_chunks
from .storage import CURRENT_FILE, load_state


def query_from_text(text: str) -> dict[str, Any] | None:
    words = text.strip().split()
    if not words or words[0].casefold() not in {"emergencia", "emergencias", "emerg"}:
        return None
    query: dict[str, Any] = {}
    if len(words) > 1:
        term = " ".join(words[1:])
        aliases = {
            "incendios": "wildfire", "incendio": "wildfire",
            "trafico": "traffic_collision", "tráfico": "traffic_collision",
            "carreteras": "road_closed",
        }
        if term.casefold() in aliases:
            query["category"] = aliases[term.casefold()]
        elif term.upper().startswith(("A-", "AP-", "N-", "Z-")):
            query["road"] = term
        else:
            query["text"] = term
    return query


def make_handler(config: dict[str, Any]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "MeshNetEmergencias/0.1"

        def _json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                state = load_state()
                self._json(200, {
                    "ok": CURRENT_FILE.exists(),
                    "sources": state.get("sources", {}),
                })
                return
            if parsed.path == "/events":
                parameters = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
                events = list_events(config, parameters)
                self._json(200, {"events": [event.to_dict() for event in events]})
                return
            self._json(404, {"error": "not_found"})

        def do_POST(self) -> None:
            if urlparse(self.path).path != "/query":
                self._json(404, {"error": "not_found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._json(400, {"error": "invalid_content_length"})
                return
            maximum = int(config["api"]["max_body_bytes"])
            if length <= 0 or length > maximum:
                self._json(413, {"error": "body_size"})
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._json(400, {"error": "invalid_json"})
                return
            query = query_from_text(str(payload.get("text", "")))
            if query is None:
                self._json(200, {"recognized": False, "messages": []})
                return
            events = list_events(config, query)
            limit = max(1, min(int(payload.get("limit", 5)), 20))
            max_bytes = max(80, min(int(payload.get("max_bytes", 140)), 500))
            self._json(200, {
                "recognized": True, "count": len(events),
                "messages": byte_chunks(events[:limit], max_bytes),
            })

        def log_message(self, format: str, *args: Any) -> None:
            print(f"[emergencias-api] {self.address_string()} {format % args}")

    return Handler


def serve(config: dict[str, Any], host: str | None = None, port: int | None = None) -> None:
    api = config["api"]
    address = (host or str(api["host"]), port or int(api["port"]))
    server = ThreadingHTTPServer(address, make_handler(config))
    print(f"API emergencias escuchando en http://{address[0]}:{address[1]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
