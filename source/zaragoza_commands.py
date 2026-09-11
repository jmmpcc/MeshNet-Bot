#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Comandos AGENDA/NOTICIAS procesados directamente por MeshNet-Broker.

Este módulo NO controla radios ni crea conexiones MeshCore/Meshtastic.
Su única responsabilidad es:

1. Reconocer los comandos Zaragoza soportados.
2. Validar el origen permitido.
3. Aplicar un límite de consultas por contacto.
4. Consultar por HTTP la API ZaragozaNoticias (zaragoza_api.py).
5. Entregar al broker los textos de respuesta mediante un callback.

El broker conserva la propiedad exclusiva de la transmisión de radio mediante
``enqueue_send_contact()`` (MeshCore) o la cola TX ya existente (Meshtastic).

Variables de entorno principales:
    ZARAGOZA_COMMAND_ENABLED=0|1
    ZARAGOZA_SERVICE_URL=http://192.168.1.30:8792/query
    ZARAGOZA_SERVICE_TIMEOUT_SECONDS=8
    ZARAGOZA_MAX_TEXT_BYTES=140
    ZARAGOZA_MAX_RESULTS_PER_QUERY=4
    ZARAGOZA_DM_MAX_MESSAGES_PER_RESPONSE=4
    ZARAGOZA_DM_INTER_MESSAGE_DELAY_SECONDS=1
    ZARAGOZA_MAX_REQUESTS_PER_HOUR=10
    ZARAGOZA_MESHCORE_CHANNEL=-1
    ZARAGOZA_MESHTASTIC_CHANNEL=-1

Uso desde el broker:
    ctx = ZaragozaCommandContext(
        network="meshcore",
        source_id=pubkey_prefix,
        text=text_msg,
        channel=chan_idx,
        is_direct=True,
        packet_id=packet_id,
    )

    handle_zaragoza_command(
        ctx,
        lambda message: meshcore_engine.enqueue_send_contact(
            pubkey_prefix,
            message,
        ),
    )
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo


def _env_bool(name: str, default: str = "0") -> bool:
    """
    Interpreta una variable de entorno como valor booleano.

    Parámetros:
        name:
            Nombre de la variable.
        default:
            Valor utilizado cuando la variable no existe.

    Retorno:
        ``True`` para 1/true/yes/on/si/sí/y; ``False`` en cualquier otro caso.
    """
    return str(os.getenv(name, default) or default).strip().lower() in {
        "1", "true", "yes", "on", "si", "sí", "y",
    }


def _normalized_command(text: str) -> str:
    """
    Normaliza espacios y Unicode sin eliminar acentos ni alterar el contenido.

    Se usa para comparar comandos y para enviar a ZaragozaNoticias una cadena
    estable aunque la radio haya introducido espacios repetidos.
    """
    value = unicodedata.normalize("NFKC", str(text or "")).strip()
    return " ".join(value.split())


@dataclass(frozen=True)
class ZaragozaCommandContext:
    """
    Contexto mínimo de una consulta Zaragoza recibida por MeshNet-Broker.

    Parámetros:
        network:
            Red de origen. Actualmente ``meshcore`` o ``meshtastic``.
        source_id:
            Identificador del remitente. En MeshCore es el pubkey_prefix.
        text:
            Texto íntegro recibido.
        channel:
            Índice de canal si procede; ``None`` para DM MeshCore.
        is_direct:
            ``True`` cuando la consulta es un mensaje directo.
        packet_id:
            Identificador opcional del paquete, utilizado para deduplicación.
    """

    network: str
    source_id: str
    text: str
    channel: int | None
    is_direct: bool
    packet_id: str | int | None = None


class SlidingWindowRateLimiter:
    """
    Limitador persistente de consultas Zaragoza por ``red + contacto``.

    Es deliberadamente independiente de Farmacias y Emergencias para que cada
    servicio mantenga sus propios límites y su propio fichero de estado.
    """

    def __init__(self) -> None:
        """Carga configuración y recupera del disco las consultas aún vigentes."""
        self.limit = max(
            1,
            int(os.getenv("ZARAGOZA_MAX_REQUESTS_PER_HOUR", "10")),
        )
        self.window = max(
            60,
            int(os.getenv("ZARAGOZA_RATE_LIMIT_WINDOW_SECONDS", "3600")),
        )
        self.duplicate_window = max(
            1,
            int(os.getenv("ZARAGOZA_DUPLICATE_WINDOW_SECONDS", "20")),
        )
        self.save_interval = max(
            10,
            int(os.getenv("ZARAGOZA_RATE_LIMIT_SAVE_SECONDS", "60")),
        )

        default_path = os.getenv("BOT_DATA_DIR", "/app/bot_data")
        self.path = Path(
            os.getenv(
                "ZARAGOZA_RATE_LIMIT_FILE",
                str(Path(default_path) / "zaragoza_rate_limit.json"),
            )
        )

        self._entries: dict[str, deque[float]] = defaultdict(deque)
        self._duplicates: dict[str, float] = {}
        self._lock = threading.RLock()
        self._last_save = 0.0
        self._load()

    def _key(self, network: str, source_id: str) -> str:
        """Construye la clave persistente que identifica al solicitante."""
        return f"{str(network).lower()}:{str(source_id).strip()}"

    def _duplicate_key(self, ctx: ZaragozaCommandContext) -> str:
        """
        Construye la clave de deduplicación.

        Si existe ``packet_id`` se usa directamente; en caso contrario se utiliza
        texto normalizado más una ventana temporal.
        """
        packet = str(ctx.packet_id or "").strip()
        if packet:
            return f"{self._key(ctx.network, ctx.source_id)}:pkt:{packet}"

        bucket = int(time.time() // self.duplicate_window)
        command = _normalized_command(ctx.text).casefold()
        return (
            f"{self._key(ctx.network, ctx.source_id)}:"
            f"txt:{command}:{bucket}"
        )

    def _prune(self, now: float) -> None:
        """Elimina consultas y marcas de duplicado que ya han caducado."""
        cutoff = now - self.window
        for key in list(self._entries):
            queue = self._entries[key]
            while queue and queue[0] <= cutoff:
                queue.popleft()
            if not queue:
                self._entries.pop(key, None)

        duplicate_cutoff = now - self.duplicate_window
        self._duplicates = {
            key: timestamp
            for key, timestamp in self._duplicates.items()
            if timestamp > duplicate_cutoff
        }

    def check_and_record(
        self,
        ctx: ZaragozaCommandContext,
    ) -> tuple[bool, int, bool]:
        """
        Comprueba y registra atómicamente una consulta.

        Retorno:
            ``(permitida, espera_segundos, duplicada)``.
        """
        now = time.time()

        with self._lock:
            self._prune(now)

            duplicate_key = self._duplicate_key(ctx)
            if duplicate_key in self._duplicates:
                return False, 0, True
            self._duplicates[duplicate_key] = now

            key = self._key(ctx.network, ctx.source_id)
            queue = self._entries[key]

            if len(queue) >= self.limit:
                retry = max(
                    1,
                    int(math.ceil(self.window - (now - queue[0]))),
                )
                self._save_if_due(now)
                return False, retry, False

            queue.append(now)
            self._save_if_due(now)
            return True, 0, False

    def _load(self) -> None:
        """Recupera del fichero las entradas no caducadas del rate limiter."""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            entries = raw.get("entries", {}) if isinstance(raw, dict) else {}
            cutoff = time.time() - self.window

            for key, values in entries.items():
                valid = [
                    float(value)
                    for value in values
                    if float(value) > cutoff
                ]
                if valid:
                    self._entries[str(key)] = deque(sorted(valid))

        except FileNotFoundError:
            return
        except Exception as exc:
            print(
                f"[zaragoza] rate-limit load WARN: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )

    def _save_if_due(self, now: float) -> None:
        """Persiste periódicamente el rate limiter mediante escritura atómica."""
        if now - self._last_save < self.save_interval:
            return

        self._last_save = now

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            payload = {
                "version": 1,
                "saved_at": now,
                "entries": {
                    key: list(values)
                    for key, values in self._entries.items()
                },
            }
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, self.path)

        except Exception as exc:
            print(
                f"[zaragoza] rate-limit save WARN: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )


class ZaragozaServiceClient:
    """
    Cliente HTTP para ``zaragoza_api.py`` usando únicamente biblioteca estándar.

    No realiza scraping ni llama a Ollama. La API Windows es responsable de
    consultar el snapshot diario mediante ``zaragoza_query.py``.
    """

    def __init__(self) -> None:
        """Lee URL y timeout desde variables de entorno."""
        self.url = os.getenv(
            "ZARAGOZA_SERVICE_URL",
            "http://192.168.1.30:8792/query",
        ).strip()
        self.timeout = max(
            0.5,
            float(os.getenv("ZARAGOZA_SERVICE_TIMEOUT_SECONDS", "8")),
        )

    def query(self, ctx: ZaragozaCommandContext) -> list[str]:
        """
        Consulta ZaragozaNoticias y devuelve exclusivamente los mensajes LoRa.

        Parámetros enviados:
            text:
                Orden AGENDA/NOTICIAS recibida.
            reference_date:
                Fecha actual de Zaragoza (Europe/Madrid).
            network/source_id/channel/is_direct:
                Metadatos de origen.
            limit:
                Número máximo de resultados lógicos.
            max_bytes:
                Tamaño objetivo de cada texto generado por la API.

        Excepciones:
            ``RuntimeError`` cuando la API no está disponible, excede el timeout
            o devuelve JSON inválido.
        """
        max_bytes = max(
            80,
            min(
                int(os.getenv("ZARAGOZA_MAX_TEXT_BYTES", "140")),
                500,
            ),
        )
        result_limit = max(
            1,
            min(
                int(os.getenv("ZARAGOZA_MAX_RESULTS_PER_QUERY", "4")),
                20,
            ),
        )

        reference_date = datetime.now(
            ZoneInfo("Europe/Madrid")
        ).date().isoformat()

        payload = json.dumps(
            {
                "text": _normalized_command(ctx.text),
                "reference_date": reference_date,
                "network": ctx.network,
                "source_id": ctx.source_id,
                "channel": ctx.channel,
                "is_direct": ctx.is_direct,
                "limit": result_limit,
                "max_bytes": max_bytes,
            },
            ensure_ascii=False,
        ).encode("utf-8")

        request = urllib.request.Request(
            self.url,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=self.timeout,
            ) as response:
                data = json.loads(
                    response.read().decode(
                        "utf-8",
                        errors="replace",
                    )
                )
        except (
            urllib.error.URLError,
            TimeoutError,
            ValueError,
        ) as exc:
            raise RuntimeError(
                f"servicio no disponible: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        if not isinstance(data, dict):
            return []

        if not data.get("recognized", False):
            return []

        messages = data.get("messages") or []
        return [
            str(message).strip()
            for message in messages
            if str(message).strip()
        ]


_LIMITER = SlidingWindowRateLimiter()
_CLIENT = ZaragozaServiceClient()


def is_zaragoza_command(text: str) -> bool:
    """
    Reconoce únicamente los namespaces reservados ``AGENDA`` y ``NOTICIAS``.

    Ejemplos reconocidos:
        AGENDA
        AGENDA HOY
        AGENDA MAÑANA
        AGENDA FINDE
        AGENDA GRATIS
        AGENDA INFANTIL
        AGENDA CONCIERTOS
        NOTICIAS
        NOTICIAS HOY
        NOTICIAS MOVILIDAD

    También se acepta ``NOTICIA`` singular como alias explícito.

    No captura palabras que solo empiecen igual, como ``agendado`` o
    ``noticiaste``.
    """
    command = _normalized_command(text).casefold()
    prefixes = ("agenda", "noticia", "noticias")
    return any(
        command == prefix or command.startswith(prefix + " ")
        for prefix in prefixes
    )


def is_allowed_origin(ctx: ZaragozaCommandContext) -> bool:
    """
    Valida si Zaragoza puede atender el origen.

    Reglas:
        - El servicio debe estar habilitado explícitamente.
        - Todos los mensajes directos son válidos.
        - Los mensajes públicos solo se aceptan en el canal configurado para la
          red correspondiente. El valor por defecto ``-1`` los deshabilita.
    """
    if not _env_bool("ZARAGOZA_COMMAND_ENABLED", "0"):
        return False

    if ctx.is_direct:
        return True

    variable = (
        "ZARAGOZA_MESHCORE_CHANNEL"
        if ctx.network == "meshcore"
        else "ZARAGOZA_MESHTASTIC_CHANNEL"
    )

    try:
        expected = int(os.getenv(variable, "-1"))
        return (
            expected >= 0
            and ctx.channel is not None
            and int(ctx.channel) == expected
        )
    except (TypeError, ValueError):
        return False


def handle_zaragoza_command(
    ctx: ZaragozaCommandContext,
    enqueue_direct: Callable[[str], None],
) -> bool:
    """
    Procesa una consulta AGENDA/NOTICIAS y encola la respuesta como DM.

    Parámetros:
        ctx:
            Contexto completo de la solicitud.
        enqueue_direct:
            Callback suministrado por el broker. Recibe un único texto y lo
            encola como DM al contacto de origen.

    Retorno:
        ``True`` cuando el mensaje pertenece al namespace Zaragoza y ha sido
        consumido (incluso si era duplicado o la API estaba temporalmente caída).
        ``False`` cuando no corresponde a Zaragoza o el origen no está permitido.

    Importante:
        Esta función NO fragmenta mensajes y NO transmite por radio. Esa lógica
        permanece en ``Meshtastic_Broker.py``.
    """
    if not _env_bool("ZARAGOZA_COMMAND_ENABLED", "0"):
        return False

    if not is_zaragoza_command(ctx.text):
        return False

    if not is_allowed_origin(ctx):
        return False

    allowed, retry_after, duplicate = _LIMITER.check_and_record(ctx)

    if duplicate:
        return True

    if not allowed:
        minutes = max(1, int(math.ceil(retry_after / 60)))
        enqueue_direct(
            f"Límite de consultas Zaragoza alcanzado. "
            f"Disponible en {minutes} min."
        )
        return True

    normalized = _normalized_command(ctx.text)

    print(
        f"[zaragoza] request network={ctx.network} "
        f"source={ctx.source_id} "
        f"direct={ctx.is_direct} "
        f"channel={ctx.channel} "
        f"raw={ctx.text!r} "
        f"normalized={normalized!r} "
        f"packet_id={ctx.packet_id}",
        flush=True,
    )

    try:
        messages = _CLIENT.query(ctx)

        if not messages:
            if normalized.casefold().startswith("agenda"):
                messages = [
                    "AGENDA: sin resultados para esa consulta."
                ]
            else:
                messages = [
                    "NOTICIAS: sin resultados para esa consulta."
                ]

    except Exception as exc:
        print(
            f"[zaragoza] query WARN: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        messages = [
            "Servicio de agenda/noticias no disponible temporalmente."
        ]

    maximum = max(
        1,
        int(
            os.getenv(
                "ZARAGOZA_DM_MAX_MESSAGES_PER_RESPONSE",
                "4",
            )
        ),
    )

    if len(messages) > maximum:
        selected = (
            messages[:max(0, maximum - 1)]
            + [f"Respuesta truncada a {maximum} mensajes."]
        )
    else:
        selected = messages

    delay = max(
        0.0,
        float(
            os.getenv(
                "ZARAGOZA_DM_INTER_MESSAGE_DELAY_SECONDS",
                "1",
            )
        ),
    )

    print(
        f"[zaragoza] response normalized={normalized!r} "
        f"parts_generated={len(messages)} "
        f"parts_enqueued={len(selected)} "
        f"complete={len(messages) <= maximum}",
        flush=True,
    )

    for index, message in enumerate(selected):
        enqueue_direct(message)

        if delay > 0 and index + 1 < len(selected):
            time.sleep(delay)

    return True
