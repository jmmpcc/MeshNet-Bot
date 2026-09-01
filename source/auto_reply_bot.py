#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Comando Telegram para administrar la configuración compartida de AutoReply."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

DEFAULT_TEMPLATE = "Recibido, {message}"
_TRANSPORT_ALIASES = {
    "meshcore": "meshcore",
    "mc": "meshcore",
    "meshtastic": "meshtastic",
    "mesh": "meshtastic",
    "mt": "meshtastic",
}


def _admin_ids() -> set[int]:
    """Devuelve los usuarios administradores definidos en ADMIN_IDS."""
    return {
        int(value)
        for value in (os.getenv("ADMIN_IDS") or "").replace(";", ",").split(",")
        if value.strip().isdigit()
    }


def _config_path() -> Path:
    """
    Resuelve el mismo fichero compartido que consume AutoReply en el broker.

    AUTO_REPLY_CONFIG permite sobreescribir la ruta para despliegues especiales
    y para pruebas. En Docker, el valor por defecto apunta al volumen bot_data
    que ya comparten broker y bot.
    """
    return Path(
        (os.getenv("AUTO_REPLY_CONFIG") or "/app/bot_data/auto_reply.json").strip()
        or "/app/bot_data/auto_reply.json"
    )


def _radio_profile_context() -> dict[str, Any]:
    """
    Obtiene los transportes habilitados por RADIO_PROFILE sin modificar el entorno.

    Si el resolvedor común no está disponible, no se bloquean consultas de estado,
    pero tampoco se inventa una topología para altas de canales.
    """
    try:
        from radio_profile import resolve_radio_profile

        caps = resolve_radio_profile(env=os.environ, strict=False)
        transports: list[str] = []
        if bool(getattr(caps, "meshtastic_enabled", False)):
            transports.append("meshtastic")
        if bool(getattr(caps, "meshcore_enabled", False)):
            transports.append("meshcore")
        return {
            "profile": str(getattr(caps, "profile", "legacy") or "legacy"),
            "transports": tuple(transports),
        }
    except Exception:
        return {
            "profile": (os.getenv("RADIO_PROFILE") or "legacy").strip() or "legacy",
            "transports": (),
        }


def _load_config(path: Path | None = None) -> dict[str, Any]:
    """
    Carga auto_reply.json conservando todos sus campos.

    Un fichero inexistente se representa mediante una configuración compatible
    con AutoReply. Un JSON corrupto se considera un error y no se sobrescribe.
    """
    target = path or _config_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = {}
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"No se puede leer una configuración válida: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError("La configuración existente no es un objeto JSON.")

    config = dict(raw)
    config.setdefault("enabled", False)
    config.setdefault("template", DEFAULT_TEMPLATE)

    for transport in ("meshcore", "meshtastic"):
        route = config.get(transport)
        if not isinstance(route, dict):
            route = {}
        else:
            route = dict(route)

        channels = route.get("channels")
        if not isinstance(channels, list):
            channels = []
        normalized: list[int] = []
        for value in channels:
            try:
                channel = int(value)
            except (TypeError, ValueError):
                continue
            if channel >= 0 and channel not in normalized:
                normalized.append(channel)
        route["channels"] = sorted(normalized)
        config[transport] = route

    return config


def _write_config(config: dict[str, Any], path: Path | None = None) -> None:
    """
    Guarda auto_reply.json de forma atómica y preserva el modo del fichero.

    La escritura se hace en un temporal dentro del mismo directorio y termina con
    replace(), evitando que broker o Control Panel puedan observar JSON parcial.
    """
    target = path or _config_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    try:
        existing_mode = target.stat().st_mode & 0o777
    except FileNotFoundError:
        existing_mode = 0o600

    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            if hasattr(os, "fchmod"):
                os.fchmod(handle.fileno(), existing_mode)
            else:
                os.chmod(temporary, existing_mode)
            json.dump(config, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _normalize_transport(value: object) -> str | None:
    """Normaliza aliases mt/mc/mesh a los nombres usados en auto_reply.json."""
    return _TRANSPORT_ALIASES.get(str(value or "").strip().lower())


def _format_status(config: dict[str, Any]) -> str:
    """Formatea el estado actual en texto compacto para Telegram."""
    enabled = "ACTIVADA" if bool(config.get("enabled")) else "DESACTIVADA"
    template = str(config.get("template") or DEFAULT_TEMPLATE)
    lines = [
        "Autorespuesta",
        f"Estado: {enabled}",
        "",
        "MeshCore: " + (
            ", ".join(str(x) for x in config["meshcore"]["channels"])
            if config["meshcore"]["channels"]
            else "(sin canales)"
        ),
        "Meshtastic: " + (
            ", ".join(str(x) for x in config["meshtastic"]["channels"])
            if config["meshtastic"]["channels"]
            else "(sin canales)"
        ),
        "",
        f"Plantilla: {template}",
    ]
    return "\n".join(lines)


def contextual_help() -> str:
    """
    Genera la ayuda de ``/autorespuesta`` adaptada al RADIO_PROFILE activo.

    Funcionalidad:
        - Siempre muestra consulta de estado y activación/desactivación.
        - Solo muestra altas/bajas de transportes realmente habilitados.
        - En perfiles con una sola radio evita enseñar comandos incompatibles.
        - En perfiles desconocidos no inventa transportes y lo indica expresamente.

    Retorno:
        Texto listo para ``reply_text()`` o para integrarlo en ``/ayuda``.
    """
    profile_ctx = _radio_profile_context()
    profile = str(profile_ctx.get("profile") or "legacy")
    allowed = tuple(profile_ctx.get("transports") or ())

    lines = [
        "AUTORESPUESTA POR CANAL",
        f"Perfil: {profile}",
        "",
        "/autorespuesta",
        "  Muestra el estado actual.",
        "/autorespuesta status",
        "  Muestra la configuración actual.",
        "/autorespuesta on",
        "  Activa las respuestas automáticas.",
        "/autorespuesta off",
        "  Desactiva las respuestas automáticas.",
    ]

    if "meshcore" in allowed:
        lines.extend([
            "/autorespuesta add mc <canal>",
            "  Añade un canal MeshCore.",
            "/autorespuesta del mc <canal>",
            "  Elimina un canal MeshCore.",
        ])

    if "meshtastic" in allowed:
        lines.extend([
            "/autorespuesta add mt <canal>",
            "  Añade un canal Meshtastic.",
            "/autorespuesta del mt <canal>",
            "  Elimina un canal Meshtastic.",
        ])

    if not allowed:
        lines.extend([
            "",
            "No se muestran comandos add/del porque el RADIO_PROFILE activo",
            "no permite determinar de forma segura los transportes disponibles.",
        ])

    lines.extend([
        "/autorespuesta texto <plantilla>",
        "  Configura el texto de respuesta; debe contener {message}.",
        "  Ejemplo: /autorespuesta texto Recibido: {message}",
    ])
    return "\n".join(lines)


def _format_help() -> str:
    """Alias interno para mantener una única fuente de ayuda contextual."""
    return contextual_help()


def _transport_allowed(transport: str) -> tuple[bool, str]:
    """Valida que el transporte solicitado exista en el RADIO_PROFILE activo."""
    profile = _radio_profile_context()
    allowed = tuple(profile.get("transports") or ())
    if not allowed:
        return False, (
            "No se puede determinar de forma segura el transporte habilitado "
            f"para RADIO_PROFILE={profile.get('profile')}."
        )
    if transport not in allowed:
        return False, (
            f"El transporte {transport} no está habilitado para "
            f"RADIO_PROFILE={profile.get('profile')}."
        )
    return True, ""


async def auto_reply_cmd(update, context) -> None:
    """
    Administra AutoReply desde Telegram reutilizando auto_reply.json.

    Uso:
        /autorespuesta
        /autorespuesta status
        /autorespuesta on|off
        /autorespuesta add <mc|mt> <canal>
        /autorespuesta del <mc|mt> <canal>
        /autorespuesta texto <plantilla con {message}>

    Permisos:
        - Estado y ayuda: cualquier usuario autorizado para hablar con el bot.
        - Cambios: exclusivamente usuarios incluidos en ADMIN_IDS.

    No transmite mensajes ni modifica AutoReply; solo actualiza de forma atómica
    la configuración compartida que AutoReply ya recarga automáticamente.
    """
    message = update.effective_message
    user = update.effective_user
    args = [str(value).strip() for value in (getattr(context, "args", None) or []) if str(value).strip()]

    if not args:
        try:
            await message.reply_text(_format_status(_load_config()))
        except ValueError as exc:
            await message.reply_text(f"Error de autorespuesta: {exc}")
        return

    action = args[0].lower()
    if action in {"help", "ayuda"}:
        await message.reply_text(_format_help())
        return

    if action in {"status", "estado", "list", "listar"}:
        try:
            await message.reply_text(_format_status(_load_config()))
        except ValueError as exc:
            await message.reply_text(f"Error de autorespuesta: {exc}")
        return

    uid = int(getattr(user, "id", 0) or 0)
    if uid not in _admin_ids():
        await message.reply_text("Solo disponible para administradores.")
        return

    try:
        config = _load_config()
    except ValueError as exc:
        await message.reply_text(f"Error de autorespuesta: {exc}")
        return

    if action in {"on", "activar", "enable"}:
        config["enabled"] = True
    elif action in {"off", "desactivar", "disable"}:
        config["enabled"] = False
    elif action in {"add", "añadir", "anadir", "del", "delete", "borrar", "remove"}:
        if len(args) != 3:
            await message.reply_text(_format_help())
            return
        transport = _normalize_transport(args[1])
        if transport is None:
            await message.reply_text("Transporte no válido. Usa mc o mt.")
            return
        allowed, reason = _transport_allowed(transport)
        if not allowed:
            await message.reply_text(reason)
            return
        try:
            channel = int(args[2])
        except ValueError:
            await message.reply_text("El canal debe ser un número entero.")
            return
        if channel < 0:
            await message.reply_text("El canal debe ser >= 0.")
            return

        channels = list(config[transport]["channels"])
        if action in {"add", "añadir", "anadir"}:
            if channel not in channels:
                channels.append(channel)
                channels.sort()
        else:
            channels = [value for value in channels if value != channel]
        config[transport]["channels"] = channels
    elif action in {"texto", "template", "plantilla"}:
        template = " ".join(args[1:]).strip()
        if not template:
            await message.reply_text("Debes indicar una plantilla.")
            return
        if "{message}" not in template:
            await message.reply_text("La plantilla debe contener {message}.")
            return
        if len(template) > 500:
            await message.reply_text("La plantilla no puede superar 500 caracteres.")
            return
        config["template"] = template
    else:
        await message.reply_text(_format_help())
        return

    try:
        _write_config(config)
    except OSError as exc:
        await message.reply_text(f"No se pudo guardar la configuración: {exc}")
        return

    await message.reply_text(_format_status(config))
