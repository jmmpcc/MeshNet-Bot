from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from ..config import atomic_write_json
from ..storage import cache_paths, load_json


@dataclass(slots=True)
class FetchResult:
    body: bytes
    not_modified: bool
    metadata: dict[str, Any]


class SourceError(RuntimeError):
    pass


class HttpSource:
    def __init__(self, source_id: str, source_config: dict[str, Any], app_config: dict[str, Any]):
        self.source_id = source_id
        self.config = source_config
        self.app_config = app_config

    def fetch_bytes(self) -> FetchResult:
        url = str(self.config.get("url", "")).strip()
        if not url:
            raise SourceError("URL no configurada")
        if not url.lower().startswith(("https://", "http://", "file://")):
            raise SourceError("solo se admiten URL http, https o file")
        body_path, metadata_path = cache_paths(self.source_id)
        metadata = load_json(metadata_path, {})
        headers = {"User-Agent": self.app_config["fetch"]["user_agent"], "Accept-Encoding": "identity"}
        if metadata.get("etag"):
            headers["If-None-Match"] = metadata["etag"]
        if metadata.get("last_modified"):
            headers["If-Modified-Since"] = metadata["last_modified"]
        request = urllib.request.Request(url, headers=headers)
        timeout = float(self.app_config["fetch"]["timeout_seconds"])
        maximum = int(self.app_config["fetch"]["max_response_bytes"])
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                length = response.headers.get("Content-Length")
                if length and int(length) > maximum:
                    raise SourceError("respuesta mayor que el límite configurado")
                body = response.read(maximum + 1)
                if len(body) > maximum:
                    raise SourceError("respuesta mayor que el límite configurado")
                new_metadata = {
                    "url": url, "etag": response.headers.get("ETag", ""),
                    "last_modified": response.headers.get("Last-Modified", ""),
                    "content_type": response.headers.get("Content-Type", ""),
                }
                body_path.parent.mkdir(parents=True, exist_ok=True)
                body_path.write_bytes(body)
                atomic_write_json(metadata_path, new_metadata)
                return FetchResult(body, False, new_metadata)
        except urllib.error.HTTPError as exc:
            if exc.code == 304 and body_path.exists():
                return FetchResult(body_path.read_bytes(), True, metadata)
            raise SourceError(f"HTTP {exc.code}") from exc
        except (OSError, urllib.error.URLError) as exc:
            raise SourceError(str(exc)) from exc

    def parse(self, body: bytes) -> list:
        raise NotImplementedError

    def fetch(self) -> tuple[list, bool]:
        result = self.fetch_bytes()
        return self.parse(result.body), result.not_modified

    def decode_json(self, body: bytes) -> Any:
        try:
            return json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceError(f"JSON no válido: {exc}") from exc
