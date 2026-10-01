#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Scope MeshCore por transmisión, sin acoplarlo al canal.

Objetivo
========
Permitir que un cliente del broker solicite un flood scope para un TX concreto
mediante ``MESHCORE_SEND.params.scope``. El canal continúa siendo únicamente el
destino lógico; el scope pertenece a la transmisión.

La integración se realiza como capa de compatibilidad sobre el runtime existente
para no modificar la cola, el troceado, los reintentos ni el código estable del
broker principal.
"""
from __future__ import annotations

import builtins
import contextvars
import hashlib
import json
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterable


_PATCH_MARKER = "_meshnet_tx_scope_runtime"
_ORIGINAL_BUILD_CLASS = builtins.__build_class__
_ORIGINAL_JSON_LOADS = json.loads

# El BacklogServer/control puede procesar peticiones desde hilos. Este estado se
# consume inmediatamente dentro de enqueue_send_channel(), por lo que no se
# comparte entre peticiones concurrentes.
_REQUEST_STATE = threading.local()

# La cola MeshCore se procesa dentro de una task asyncio. _normalize_tx_spool_item
# selecciona el scope del item actual y send_chan_msg lo consulta después para
# todas las partes del mismo TX, incluidos reintentos.
_ACTIVE_TX_SCOPE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "meshnet_active_meshcore_tx_scope", default=None
)

# En el proceso del bot se usa otro ContextVar para que el normalizador visual
# pueda añadir a la confirmación el scope solicitado por /enviar_mc.
_BOT_TX_SCOPE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "meshnet_bot_meshcore_tx_scope", default=None
)


def normalize_scope(value: object) -> str | None:
    """Normaliza un scope introducido por el usuario.

    Parámetros:
        value: valor recibido desde ``--scope`` o desde ``MESHCORE_SEND``.

    Devuelve:
        - ``None``: no se ha solicitado scope explícito; se conserva el flujo
          histórico hasta que exista que restaurar un override previo.
        - ``"0"``: usar el default scope configurado en el nodo.
        - ``"*"``: enviar explícitamente sin scope.
        - ``"#nombre"``: región MeshCore explícita.
        - Una clave hexadecimal/raw ya válida se conserva sin transformar.

    Alias admitidos:
        ``default`` -> ``0``
        ``none``, ``unscoped``, ``global`` -> ``*``

    Los nombres de región sin ``#`` se convierten en ``#nombre`` porque
    ``meshcore_py.set_flood_scope`` admite el formato hash ``#name``.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    low = text.casefold()
    if low in {"0", "default", "defecto"}:
        return "0"
    if low in {"*", "none", "null", "unscoped", "sin-scope", "sinscope", "global"}:
        return "*"
    if text.startswith("#"):
        return text

    # Las claves raw de TransportKey pueden proporcionarse directamente. No se
    # transforman si parecen una clave hexadecimal suficientemente larga.
    raw_hex = text.removeprefix("0x")
    if len(raw_hex) >= 16 and all(ch in "0123456789abcdefABCDEF" for ch in raw_hex):
        return text

    return f"#{text}"


def extract_scope_modifier(args: Iterable[object]) -> tuple[list[str], str | None, str | None]:
    """Extrae ``--scope`` sin alterar el resto de argumentos de /enviar_mc.

    Formas válidas:
        ``--scope #zaragoza``
        ``--scope=#zaragoza``
        ``--scope zaragoza``
        ``--scope 0``
        ``--scope *``

    Devuelve ``(args_limpios, scope, error)``. Si no existe modificador,
    ``scope`` es ``None`` y el comando conserva exactamente su sintaxis previa.
    Solo se permite un modificador por TX para evitar ambigüedades.
    """
    tokens = [str(v) for v in args]
    cleaned: list[str] = []
    found: str | None = None
    i = 0
    while i < len(tokens):
        token = tokens[i]
        low = token.casefold()
        candidate: str | None = None
        consumed = 1

        if low == "--scope":
            if i + 1 >= len(tokens):
                return tokens, None, "Falta el valor después de --scope."
            candidate = tokens[i + 1]
            consumed = 2
        elif low.startswith("--scope="):
            candidate = token.split("=", 1)[1]

        if candidate is None:
            cleaned.append(token)
            i += 1
            continue

        if found is not None:
            return tokens, None, "Solo puede indicarse un --scope por envío."
        normalized = normalize_scope(candidate)
        if normalized is None:
            return tokens, None, "El valor de --scope está vacío."
        found = normalized
        i += consumed

    return cleaned, found, None


@contextmanager
def bot_tx_scope(scope: str | None):
    """Activa temporalmente el scope visible durante un /enviar_mc."""
    token = _BOT_TX_SCOPE.set(scope)
    try:
        yield
    finally:
        _BOT_TX_SCOPE.reset(token)


def current_bot_tx_scope() -> str | None:
    """Devuelve el scope explícito del /enviar_mc que está en ejecución."""
    return _BOT_TX_SCOPE.get()


def annotate_meshcore_scope(text: object) -> object:
    """Añade el scope explícito a la confirmación TX de Telegram.

    Solo modifica la respuesta conocida que empieza por ``Envío MeshCore`` y
    únicamente mientras el comando actual tenga un ``--scope`` explícito.
    Mensajes RX no se etiquetan: en la versión actual de ``meshcore_py`` el
    evento ``CHANNEL_MSG_RECV`` no expone el scope real recibido, y mostrar un
    valor inferido sería engañoso.
    """
    if not isinstance(text, str) or not text.startswith("Envío MeshCore"):
        return text
    if "Scope TX:" in text:
        return text
    scope = current_bot_tx_scope()
    if scope is None:
        return text

    safe = (
        scope.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    lines = text.splitlines()
    insert_at = next(
        (idx for idx, line in enumerate(lines) if line.startswith("Resultado MeshCore:")),
        len(lines),
    )
    lines.insert(insert_at, f"Scope TX: <b>{safe}</b>")
    return "\n".join(lines)


def _event_is_error(result: Any) -> bool:
    """Detecta respuestas ERROR de meshcore_py de forma compatible."""
    event_type = getattr(result, "type", None)
    if event_type is None:
        return False
    name = str(getattr(event_type, "name", "") or "").upper()
    value = str(getattr(event_type, "value", "") or "").lower()
    text = str(event_type).lower()
    return (
        name == "ERROR"
        or value == "command_error"
        or text.endswith(".error")
        or "command_error" in text
    )


def _install_on_meshcore_instance(mc: Any) -> Any:
    """Envuelve send_chan_msg para aplicar el scope del item de cola actual.

    El wrapper se ejecuta para cada parte de un mensaje largo, por lo que el
    scope permanece correcto durante todas las partes y también al reintentar.
    Si ``set_flood_scope`` falla, esa parte no se transmite (fail-closed).
    """
    commands = getattr(mc, "commands", None)
    if commands is None:
        return mc
    current = getattr(commands, "send_chan_msg", None)
    set_scope = getattr(commands, "set_flood_scope", None)
    if not callable(current) or not callable(set_scope):
        raise RuntimeError("meshcore_py_no_set_flood_scope")
    if getattr(current, _PATCH_MARKER, False):
        return mc

    original_send = current

    async def send_chan_msg_scoped(chan: int, msg: str, timestamp: int | None = None):
        scope = _ACTIVE_TX_SCOPE.get()

        if scope is not None:
            scope_result = await set_scope(scope)
            if _event_is_error(scope_result):
                return scope_result
            # Un scope explícito distinto de default deja override temporal en
            # el Companion; se marca para restaurarlo antes del próximo TX sin
            # scope explícito.
            setattr(commands, "_meshnet_scope_override_dirty", scope != "0")
        elif bool(getattr(commands, "_meshnet_scope_override_dirty", False)):
            # El siguiente TX sin --scope vuelve al default del nodo. Así un
            # envío regional explícito nunca contamina un envío posterior.
            reset_result = await set_scope("0")
            if _event_is_error(reset_result):
                return reset_result
            setattr(commands, "_meshnet_scope_override_dirty", False)

        return await original_send(chan, msg, timestamp)

    setattr(send_chan_msg_scoped, _PATCH_MARKER, True)
    commands.send_chan_msg = send_chan_msg_scoped
    return mc


def _install_meshcore_constructor_wrappers(meshcore_cls: Any) -> bool:
    """Aplica el wrapper anterior a create_serial/create_tcp/create_ble."""
    if getattr(meshcore_cls, _PATCH_MARKER, False):
        return True

    patched = False
    for creator_name in ("create_serial", "create_tcp", "create_ble"):
        creator = getattr(meshcore_cls, creator_name, None)
        if not callable(creator):
            continue

        async def creator_scoped(*args: Any, __creator=creator, **kwargs: Any):
            mc = await __creator(*args, **kwargs)
            return _install_on_meshcore_instance(mc)

        setattr(creator_scoped, _PATCH_MARKER, True)
        setattr(meshcore_cls, creator_name, staticmethod(creator_scoped))
        patched = True

    if patched:
        setattr(meshcore_cls, _PATCH_MARKER, True)
    return patched


def _clear_pending_request_scope() -> None:
    """Limpia cualquier scope pendiente del hilo de control actual."""
    _REQUEST_STATE.scope_present = False
    _REQUEST_STATE.scope = None
    _REQUEST_STATE.channel_idx = None
    _REQUEST_STATE.captured_at = 0.0


def _consume_pending_request_scope(channel_idx: object) -> tuple[bool, str | None]:
    """Consume una sola vez el scope del MESHCORE_SEND actual.

    La captura incluye ``channel_idx`` y una ventana temporal corta. Así una
    petición inválida nunca puede dejar un scope pendiente que alcance a otro
    envío posterior del mismo hilo.
    """
    present = bool(getattr(_REQUEST_STATE, "scope_present", False))
    scope = getattr(_REQUEST_STATE, "scope", None)
    expected_channel = getattr(_REQUEST_STATE, "channel_idx", None)
    captured_at = float(getattr(_REQUEST_STATE, "captured_at", 0.0) or 0.0)
    _clear_pending_request_scope()

    if not present or scope is None:
        return False, None
    try:
        same_channel = int(expected_channel) == int(channel_idx)
    except (TypeError, ValueError):
        same_channel = False
    if not same_channel or (time.monotonic() - captured_at) > 2.0:
        return False, None
    return True, scope


def _patch_bridge_class(cls: type) -> type:
    """Añade scope a los items de cola sin reescribir el broker principal.

    Se modifican únicamente dos métodos de ``MeshCoreEmbeddedBridge``:

    - ``enqueue_send_channel``: conserva la función existente y añade el scope
      capturado a ``dst`` dentro del item de cola.
    - ``_normalize_tx_spool_item``: selecciona ese scope en un ContextVar justo
      antes de que el bucle TX procese todas las partes del item.

    La implementación de enqueue reproduce la función estable actual para poder
    añadir un único campo a ``dst`` manteniendo cola, spool, split y retries.
    """
    if getattr(cls, _PATCH_MARKER, False):
        return cls

    original_enqueue = getattr(cls, "enqueue_send_channel", None)
    original_normalize = getattr(cls, "_normalize_tx_spool_item", None)
    if not callable(original_enqueue) or not callable(original_normalize):
        return cls

    broker_globals = getattr(original_enqueue, "__globals__", {})
    split_parts = broker_globals.get("_split_meshcore_send_parts")
    max_bytes = broker_globals.get("_safe_meshcore_max_text_bytes")
    if not callable(split_parts) or not callable(max_bytes):
        return cls

    def enqueue_send_channel_scoped(
        self: Any,
        channel_idx: int,
        text: str,
        tx_id: str | None = None,
        max_retries: int | None = None,
    ) -> str | None:
        """Encola el TX existente añadiendo únicamente ``dst['scope']``."""
        scope_present, scope = _consume_pending_request_scope(channel_idx)

        if not getattr(self, "enable", False):
            return None

        msg = (text or "").strip()
        if not msg:
            return None

        tx_id = (
            tx_id
            or hashlib.sha1(
                f"{time.time()}|chan|{channel_idx}|{msg}".encode(
                    "utf-8", errors="ignore"
                )
            ).hexdigest()[:12]
        )

        try:
            with self._retry_spool_lock:
                healthy = bool(self._connected)
                loop = self._loop
                tx_q = self._tx_q

            dst: dict[str, Any] = {"kind": "chan", "channel_idx": int(channel_idx)}
            if scope_present and scope is not None:
                dst["scope"] = scope

            item_max_retries = (
                self._tx_max_retries
                if max_retries is None
                else max(0, int(max_retries))
            )
            send_parts = tuple(split_parts(msg, max_bytes()))
            item = (dst, msg, 0, item_max_retries, tx_id, send_parts, 0)

            if (not healthy) or (not loop) or (not tx_q):
                self._spool_append(item, why="enqueue_chan_deferred")
                if self.log_enqueue:
                    print(
                        f"[meshcore] enqueue deferred -> chan_idx={int(channel_idx)} "
                        f"tx_id={tx_id} (sesión no activa)",
                        flush=True,
                    )
                return tx_id

            if self.log_enqueue:
                try:
                    n = len(msg.encode("utf-8", errors="ignore"))
                except Exception:
                    n = len(msg)
                scope_log = f" scope={scope}" if scope_present and scope else ""
                print(
                    f"[meshcore] enqueue -> chan_idx={int(channel_idx)} len={n} "
                    f"tx_id={tx_id}{scope_log}",
                    flush=True,
                )

            loop.call_soon_threadsafe(tx_q.put_nowait, item)
            return tx_id

        except Exception:
            try:
                dst = {"kind": "chan", "channel_idx": int(channel_idx)}
                if scope_present and scope is not None:
                    dst["scope"] = scope
                item_max_retries = (
                    self._tx_max_retries
                    if max_retries is None
                    else max(0, int(max_retries))
                )
                send_parts = tuple(split_parts(msg, max_bytes()))
                self._spool_append(
                    (dst, msg, 0, item_max_retries, tx_id, send_parts, 0),
                    why="enqueue_chan_fallback",
                )
                return tx_id
            except Exception:
                return None

    def normalize_tx_spool_item_scoped(
        self: Any,
        item: object,
        default_max_retries: int | None = None,
    ):
        """Conserva el normalizador existente y activa el scope de ese item."""
        normalized = original_normalize(self, item, default_max_retries)
        scope: str | None = None
        if normalized is not None:
            dst = normalized[0]
            if isinstance(dst, dict) and str(dst.get("kind") or "").lower() in {
                "chan",
                "channel",
            }:
                raw = dst.get("scope") if "scope" in dst else None
                scope = normalize_scope(raw) if raw is not None else None
        _ACTIVE_TX_SCOPE.set(scope)
        return normalized

    setattr(enqueue_send_channel_scoped, _PATCH_MARKER, True)
    setattr(normalize_tx_spool_item_scoped, _PATCH_MARKER, True)
    cls.enqueue_send_channel = enqueue_send_channel_scoped
    cls._normalize_tx_spool_item = normalize_tx_spool_item_scoped
    setattr(cls, _PATCH_MARKER, True)
    return cls


def _install_request_scope_capture() -> None:
    """Captura ``params.scope`` cuando el broker decodifica MESHCORE_SEND."""
    if getattr(json.loads, _PATCH_MARKER, False):
        return

    original_loads = json.loads

    def loads_with_scope(*args: Any, **kwargs: Any):
        # Cada nueva decodificación invalida un estado pendiente anterior. La
        # llamada MESHCORE_SEND válida vuelve a armarlo justo antes de que el
        # handler invoque enqueue_send_channel().
        _clear_pending_request_scope()
        obj = original_loads(*args, **kwargs)
        try:
            if isinstance(obj, dict) and str(obj.get("cmd") or "").upper() == "MESHCORE_SEND":
                params = obj.get("params") or {}
                if isinstance(params, dict):
                    kind = str(params.get("kind") or "").strip().lower()
                    ch_raw = params.get("channel_idx", params.get("ch"))
                    is_channel = kind not in {"contact", "dm"} and (
                        ch_raw is not None or not kind
                    )
                    if is_channel and "scope" in params:
                        scope = normalize_scope(params.get("scope"))
                        if scope is None:
                            raise ValueError("meshcore_scope_empty")
                        _REQUEST_STATE.scope_present = True
                        _REQUEST_STATE.scope = scope
                        _REQUEST_STATE.channel_idx = int(ch_raw)
                        _REQUEST_STATE.captured_at = time.monotonic()
        except Exception:
            _clear_pending_request_scope()
            raise
        return obj

    setattr(loads_with_scope, _PATCH_MARKER, True)
    json.loads = loads_with_scope


def _install_bridge_class_hook() -> None:
    """Parchea MeshCoreEmbeddedBridge en el momento en que el broker la define."""
    current = builtins.__build_class__
    if getattr(current, _PATCH_MARKER, False):
        return

    original = current

    def build_class_with_scope(func: Any, name: str, *bases: Any, **kwargs: Any):
        cls = original(func, name, *bases, **kwargs)
        if name == "MeshCoreEmbeddedBridge":
            _patch_bridge_class(cls)
            # El objetivo ya está parcheado. Restaurar reduce al mínimo la
            # superficie de intervención durante el resto del arranque.
            if builtins.__build_class__ is build_class_with_scope:
                builtins.__build_class__ = original
        return cls

    setattr(build_class_with_scope, _PATCH_MARKER, True)
    builtins.__build_class__ = build_class_with_scope


def install_meshcore_tx_scope_runtime(meshcore_cls: Any) -> bool:
    """Instala el soporte de scope por TX antes de ejecutar el broker.

    Debe llamarse desde ``Meshtastic_Broker_ChannelGateway.py`` después de cargar
    el entorno y antes de ``runpy.run_path``. No requiere ninguna variable nueva.
    Si el usuario nunca envía ``--scope``, los items antiguos se generan sin el
    campo ``scope`` y el comportamiento previo queda intacto.
    """
    if not _install_meshcore_constructor_wrappers(meshcore_cls):
        return False
    _install_request_scope_capture()
    _install_bridge_class_hook()
    return True


__all__ = [
    "annotate_meshcore_scope",
    "bot_tx_scope",
    "current_bot_tx_scope",
    "extract_scope_modifier",
    "install_meshcore_tx_scope_runtime",
    "normalize_scope",
]
