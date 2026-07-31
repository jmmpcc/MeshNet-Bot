#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
radio_profile.py v7.0.20

Resolvedor común de perfiles de radio para MeshNet-Bot.

Objetivo
--------
Centralizar la interpretación de ``RADIO_PROFILE`` para que broker, bot,
APRS, email, scheduler y herramientas auxiliares utilicen los mismos nombres,
capacidades y reglas de compatibilidad.

Perfiles canónicos
------------------
``meshcore_only``
    Nodo A MeshCore. Meshtastic queda desactivado.

``meshtastic_a_meshcore_embedded_b``
    Perfil histórico. Nodo A Meshtastic y nodo B MeshCore embebido.

``meshcore_a_meshtastic_embedded_b``
    Perfil invertido. Nodo A MeshCore y nodo B Meshtastic controlado por el
    broker.

Compatibilidad
--------------
Se aceptan nombres históricos como ``meshcore_embedded`` y aliases abreviados.
Un perfil vacío se conserva como modo legacy para no alterar instalaciones que
no hayan adoptado todavía ``RADIO_PROFILE``.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from typing import Mapping, MutableMapping, Optional

VERSION = "v7.0.20"

PROFILE_LEGACY = "legacy"
PROFILE_MESHCORE_ONLY = "meshcore_only"
PROFILE_MESHTASTIC_A_MESHCORE_B = "meshtastic_a_meshcore_embedded_b"
PROFILE_MESHCORE_A_MESHTASTIC_B = "meshcore_a_meshtastic_embedded_b"

CANONICAL_PROFILES = {
    PROFILE_MESHCORE_ONLY,
    PROFILE_MESHTASTIC_A_MESHCORE_B,
    PROFILE_MESHCORE_A_MESHTASTIC_B,
}

# Todos los aliases se normalizan primero a minúsculas y guiones bajos.
PROFILE_ALIASES = {
    "meshcore_only": PROFILE_MESHCORE_ONLY,
    "only_meshcore": PROFILE_MESHCORE_ONLY,
    "meshcore": PROFILE_MESHCORE_ONLY,

    # Nombre histórico usado por el proyecto y variantes documentadas.
    "meshcore_embedded": PROFILE_MESHTASTIC_A_MESHCORE_B,
    "meshtastic_a_meshcore_b": PROFILE_MESHTASTIC_A_MESHCORE_B,
    "meshtastic_a_meshcore_embedded_b": PROFILE_MESHTASTIC_A_MESHCORE_B,
    "meshtastic_meshcore": PROFILE_MESHTASTIC_A_MESHCORE_B,

    # Perfil invertido y aliases abreviados.
    "meshcore_a_meshtastic_b": PROFILE_MESHCORE_A_MESHTASTIC_B,
    "meshcore_a_meshtastic_embedded_b": PROFILE_MESHCORE_A_MESHTASTIC_B,
    "meshcore_meshtastic": PROFILE_MESHCORE_A_MESHTASTIC_B,
}


@dataclass(frozen=True)
class RadioCapabilities:
    """Capacidades efectivas derivadas de un perfil de radio.

    Campos principales:
        profile:
            Nombre canónico o ``legacy`` cuando no se ha definido perfil.
        requested_profile:
            Valor original normalizado antes de aplicar aliases.
        valid:
            False únicamente cuando se recibió un nombre desconocido.
        meshcore_enabled / meshtastic_enabled:
            Indican qué adaptadores deben estar operativos.
        node_a_transport / node_b_transport:
            Distribución lógica de las radios.
        embedded_bridge_enabled:
            Indica si las dos redes forman un perfil combinado dentro del broker.
        default_transport:
            Red preferida para operaciones ``auto``.
        environment_overrides:
            Variables mínimas que el perfil debe imponer sobre el entorno.
    """

    profile: str
    requested_profile: str = ""
    valid: bool = True
    alias_used: bool = False
    meshcore_enabled: bool = False
    meshtastic_enabled: bool = False
    node_a_transport: Optional[str] = None
    node_b_transport: Optional[str] = None
    embedded_bridge_enabled: bool = False
    external_bridge_enabled: bool = False
    default_transport: Optional[str] = None
    legacy_mode: bool = False
    environment_overrides: dict[str, str] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        """Devuelve una copia serializable del resultado."""
        return asdict(self)


def _normalize_token(value: object) -> str:
    """Normaliza un nombre de perfil sin decidir todavía su significado."""
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def normalize_radio_profile(value: object, *, allow_legacy_empty: bool = True) -> str:
    """Devuelve el nombre canónico de un perfil.

    Parámetros:
        value:
            Nombre leído desde ``RADIO_PROFILE`` o desde configuración externa.
        allow_legacy_empty:
            Cuando es True, un valor vacío devuelve ``legacy``. Esto mantiene el
            comportamiento anterior del proyecto. Cuando es False devuelve una
            cadena vacía para que un validador pueda marcarlo expresamente.

    La función no modifica variables de entorno.
    """
    token = _normalize_token(value)
    if not token:
        return PROFILE_LEGACY if allow_legacy_empty else ""
    return PROFILE_ALIASES.get(token, token)


def is_known_radio_profile(value: object, *, allow_legacy_empty: bool = True) -> bool:
    """Indica si ``value`` representa un perfil admitido."""
    profile = normalize_radio_profile(value, allow_legacy_empty=allow_legacy_empty)
    return profile == PROFILE_LEGACY or profile in CANONICAL_PROFILES


def default_transport_for_radio_profile(value: object) -> Optional[str]:
    """Devuelve el transporte principal definido por ``RADIO_PROFILE``.

    A diferencia de comprobar únicamente ``meshcore_only``, este helper también
    conserva MeshCore como salida automática cuando es el nodo A del perfil
    invertido. ``None`` mantiene el comportamiento legacy de los consumidores.
    """
    caps = resolve_radio_profile(value, env={}, strict=False)
    return caps.default_transport if caps.valid and not caps.legacy_mode else None


def radio_profile_enables_transport(value: object, transport: object) -> bool:
    """Indica si un perfil explícito permite utilizar un transporte concreto."""
    caps = resolve_radio_profile(value, env={}, strict=False)
    if not caps.valid:
        return False
    if caps.legacy_mode:
        return True
    normalized = _normalize_token(transport)
    if normalized == "meshcore":
        return caps.meshcore_enabled
    if normalized == "meshtastic":
        return caps.meshtastic_enabled
    return False


def bridge_profile_matches_radio_profile(active_profile: object, configured_profile: object) -> bool:
    """Comprueba si un perfil JSON puede complementar ``RADIO_PROFILE``.

    Reglas:
        - En modo legacy se permite que ``bridge_config.json`` seleccione perfil.
        - Un JSON vacío u ``off`` no contradice ningún perfil explícito.
        - Para perfiles explícitos, ambos deben normalizarse al mismo nombre
          canónico. De este modo el JSON nunca cambia silenciosamente la
          arquitectura elegida mediante ``RADIO_PROFILE``.
    """
    active = normalize_radio_profile(active_profile, allow_legacy_empty=True)
    configured_raw = _normalize_token(configured_profile)
    if active == PROFILE_LEGACY:
        return True
    if configured_raw in {"", "off"}:
        return True
    configured = normalize_radio_profile(configured_raw, allow_legacy_empty=False)
    return configured == active


def resolve_radio_profile(
    value: object | None = None,
    *,
    env: Optional[Mapping[str, str]] = None,
    strict: bool = False,
) -> RadioCapabilities:
    """Resuelve capacidades y overrides para un perfil.

    Uso::

        caps = resolve_radio_profile(env=os.environ)
        if caps.meshcore_enabled:
            ...

    Parámetros:
        value:
            Perfil explícito. Si es ``None``, se lee ``RADIO_PROFILE`` de ``env``.
        env:
            Entorno de referencia. Por defecto ``os.environ``.
        strict:
            Si es True, un perfil desconocido genera ``ValueError``. Si es
            False devuelve ``valid=False`` sin aplicar overrides; este modo es
            más seguro para conservar el arranque legacy.
    """
    env_src = env if env is not None else os.environ
    raw = env_src.get("RADIO_PROFILE", "") if value is None else value
    requested = _normalize_token(raw)
    canonical = normalize_radio_profile(raw, allow_legacy_empty=True)
    alias_used = bool(requested and requested != canonical)

    if canonical == PROFILE_LEGACY:
        return RadioCapabilities(
            profile=PROFILE_LEGACY,
            requested_profile=requested,
            valid=True,
            legacy_mode=True,
            warnings=("RADIO_PROFILE no definido; se conserva el comportamiento legacy del entorno.",),
        )

    if canonical not in CANONICAL_PROFILES:
        message = (
            f"RADIO_PROFILE desconocido: {requested!r}. Valores canónicos: "
            + ", ".join(sorted(CANONICAL_PROFILES))
        )
        if strict:
            raise ValueError(message)
        return RadioCapabilities(
            profile=canonical,
            requested_profile=requested,
            valid=False,
            warnings=(message,),
        )

    if canonical == PROFILE_MESHCORE_ONLY:
        overrides = {
            "RADIO_PROFILE": canonical,
            "MESHCORE_ENABLE": "1",
            "MESHCORE_ONLY": "1",
            "BRIDGE_ENABLED": "0",
            "BRIDGE_DIRECTION_MODE": "off",
            "BBS_ENABLED": "0",
            "BBS_ENABLE": "0",
        }
        return RadioCapabilities(
            profile=canonical,
            requested_profile=requested,
            alias_used=alias_used,
            meshcore_enabled=True,
            meshtastic_enabled=False,
            node_a_transport="meshcore",
            node_b_transport=None,
            embedded_bridge_enabled=False,
            default_transport="meshcore",
            environment_overrides=overrides,
        )

    if canonical == PROFILE_MESHTASTIC_A_MESHCORE_B:
        # No se fuerza BRIDGE_ENABLED: el funcionamiento histórico utiliza el
        # motor MeshCore embebido y sus rutas actuales. Forzar el bridge TCP B
        # heredado podría abrir una segunda conexión y romper un despliegue sano.
        overrides = {
            "RADIO_PROFILE": canonical,
            "MESHCORE_ENABLE": "1",
            "MESHCORE_ONLY": "0",
            "BRIDGE_DIRECTION_MODE": canonical,
        }
        return RadioCapabilities(
            profile=canonical,
            requested_profile=requested,
            alias_used=alias_used,
            meshcore_enabled=True,
            meshtastic_enabled=True,
            node_a_transport="meshtastic",
            node_b_transport="meshcore",
            embedded_bridge_enabled=True,
            default_transport="meshtastic",
            environment_overrides=overrides,
        )

    # PROFILE_MESHCORE_A_MESHTASTIC_B
    overrides = {
        "RADIO_PROFILE": canonical,
        "MESHCORE_ENABLE": "1",
        "MESHCORE_ONLY": "0",
        # El bridge invertido se implementa sobre el motor MeshCore embebido y la
        # interfaz Meshtastic principal ya existente. BRIDGE_ENABLED corresponde
        # al bridge Meshtastic A↔B histórico y debe permanecer apagado aquí.
        "BRIDGE_ENABLED": "0",
        "BRIDGE_DIRECTION_MODE": canonical,
    }
    return RadioCapabilities(
        profile=canonical,
        requested_profile=requested,
        alias_used=alias_used,
        meshcore_enabled=True,
        meshtastic_enabled=True,
        node_a_transport="meshcore",
        node_b_transport="meshtastic",
        embedded_bridge_enabled=True,
        default_transport="meshcore",
        environment_overrides=overrides,
    )


def apply_radio_profile_to_environment(
    value: object | None = None,
    *,
    env: Optional[MutableMapping[str, str]] = None,
    strict: bool = False,
) -> RadioCapabilities:
    """Resuelve un perfil y aplica sus overrides al entorno indicado.

    Un perfil vacío o desconocido no modifica el entorno en modo no estricto.
    Esto evita alterar instalaciones legacy y evita aplicar parcialmente una
    configuración mal escrita.
    """
    target = env if env is not None else os.environ
    caps = resolve_radio_profile(value, env=target, strict=strict)
    if caps.valid and not caps.legacy_mode:
        for key, item in caps.environment_overrides.items():
            target[str(key)] = str(item)
    return caps


def validate_radio_profile_environment(
    value: object | None = None,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> dict:
    """Valida requisitos mínimos del perfil sin abrir conexiones de red.

    Devuelve ``ok``, ``errors``, ``warnings`` y las capacidades resueltas.
    Los hosts se comprueban solo cuando el perfil requiere Meshtastic.
    """
    env_src = env if env is not None else os.environ
    caps = resolve_radio_profile(value, env=env_src, strict=False)
    errors: list[str] = []
    warnings = list(caps.warnings)

    if not caps.valid:
        errors.extend(caps.warnings)
    elif caps.meshtastic_enabled:
        host = str(
            env_src.get("MESHTASTIC_B_HOST")
            or env_src.get("MESHTASTIC_HOST")
            or env_src.get("MESH_NODE_HOST")
            or ""
        ).strip()
        if not host:
            errors.append(f"El perfil {caps.profile!r} requiere un host Meshtastic.")

    if caps.meshcore_enabled:
        mode = str(env_src.get("MESHCORE_MODE") or "tcp").strip().lower()
        if mode == "tcp" and not str(env_src.get("MESHCORE_TCP_HOST") or "").strip():
            errors.append(f"El perfil {caps.profile!r} requiere MESHCORE_TCP_HOST para MESHCORE_MODE=tcp.")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "capabilities": caps.to_dict(),
    }


def get_radio_capabilities(value: object | None = None, *, env: Optional[Mapping[str, str]] = None) -> dict:
    """Atajo compatible para obtener capacidades como diccionario."""
    return resolve_radio_profile(value, env=env, strict=False).to_dict()


def get_default_transport(value: object | None = None, *, env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Devuelve el transporte predeterminado del perfil o ``None`` en legacy/error."""
    caps = resolve_radio_profile(value, env=env, strict=False)
    return caps.default_transport if caps.valid else None


__all__ = [
    "VERSION",
    "PROFILE_LEGACY",
    "PROFILE_MESHCORE_ONLY",
    "PROFILE_MESHTASTIC_A_MESHCORE_B",
    "PROFILE_MESHCORE_A_MESHTASTIC_B",
    "CANONICAL_PROFILES",
    "PROFILE_ALIASES",
    "RadioCapabilities",
    "normalize_radio_profile",
    "is_known_radio_profile",
    "bridge_profile_matches_radio_profile",
    "resolve_radio_profile",
    "apply_radio_profile_to_environment",
    "validate_radio_profile_environment",
    "get_radio_capabilities",
    "get_default_transport",
]
