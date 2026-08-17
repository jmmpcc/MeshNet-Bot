#!/usr/bin/env python3
"""Extensión v7.0.58 de MeshNet Mobile API: autenticación por sesiones.

Esta capa se coloca delante de ``mobile_api_v7054`` sin modificar sus endpoints ni
su middleware Bearer ya validado. La compatibilidad se conserva de esta forma:

- el Bearer fijo ``MESHNET_MOBILE_API_TOKEN`` sigue funcionando exactamente igual;
- login usuario/contraseña emite access + refresh tokens;
- un access token de sesión válido se traduce internamente al Bearer fijo antes de
  delegar la petición a la API histórica;
- usuario/contraseña nunca se propagan a los endpoints existentes;
- los endpoints de lectura y sus respuestas permanecen sin cambios.

Ejecución:
    python3 -m uvicorn tools.MobileAPI.mobile_api_v7058:app --host 0.0.0.0 --port 8791

Preparación de un usuario:
    python3 -m tools.MobileAPI.mobile_auth user-set USUARIO --role viewer

La Fase 2.6.1 exige conservar ``MESHNET_MOBILE_API_TOKEN`` durante la migración. La
retirada del Bearer histórico sólo podrá plantearse después de validar Android.
"""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from tools.MobileAPI import mobile_api_v7054 as legacy
from tools.MobileAPI.mobile_auth import (
    AuthIdentity,
    authenticate_user,
    issue_session,
    refresh_session,
    revoke_token_family,
    users_configured,
    verify_access_token,
)


class LoginRequest(BaseModel):
    """Credenciales de primer acceso recibidas exclusivamente por /auth/login."""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class RefreshRequest(BaseModel):
    """Refresh token opaco utilizado para rotar una sesión persistida."""

    refresh_token: str = Field(min_length=20, max_length=512)


class LogoutRequest(BaseModel):
    """Token de cualquier tipo cuya familia debe revocarse en logout."""

    token: str = Field(min_length=20, max_length=512)


app = FastAPI(
    title="MeshNet Mobile API",
    version="1.2.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


def _configured_legacy_token() -> str:
    """Lee el Bearer histórico mediante el helper existente, sin duplicar su variable."""
    return legacy.base._configured_token()


def _bearer_token(request: Request) -> str:
    """Reutiliza el parser Bearer existente de la API base."""
    return legacy.base._bearer_token(request)


def _replace_authorization_header(request: Request, bearer: str) -> None:
    """Sustituye Authorization en el scope ASGI antes de delegar a la API histórica.

    Args:
        request: petición ya recibida por la capa v7.0.58.
        bearer: Bearer fijo configurado que espera ``mobile_api.py``.

    La modificación sólo vive durante esta petición. No cambia cabeceras del cliente,
    no registra tokens y no modifica ninguna función del backend histórico.
    """
    encoded_name = b"authorization"
    encoded_value = f"Bearer {bearer}".encode("latin-1")
    headers = [
        (name, value)
        for name, value in request.scope.get("headers", [])
        if name.lower() != encoded_name
    ]
    headers.append((encoded_name, encoded_value))
    request.scope["headers"] = headers


@app.middleware("http")
async def translate_session_bearer(request: Request, call_next):
    """Acepta access tokens de sesión sin alterar el middleware Bearer histórico.

    Los endpoints ``/api/v1/auth/*`` pertenecen a esta capa y se procesan aquí. Para
    cualquier otro endpoint, un access token válido se sustituye internamente por el
    Bearer fijo antes de entrar en ``mobile_api_v7054``. Un Bearer fijo ya válido pasa
    intacto y conserva la compatibilidad de clientes actuales.
    """
    path = request.url.path
    if path.startswith("/api/v1/auth/"):
        return await call_next(request)

    supplied = _bearer_token(request)
    configured = _configured_legacy_token()

    if supplied and configured and secrets.compare_digest(supplied, configured):
        return await call_next(request)

    identity = verify_access_token(supplied) if supplied else None
    if identity is not None:
        if not configured:
            raise HTTPException(
                status_code=503,
                detail="MESHNET_MOBILE_API_TOKEN debe mantenerse configurado durante la migración",
            )
        request.state.mobile_auth = identity
        _replace_authorization_header(request, configured)

    return await call_next(request)


@app.post("/api/v1/auth/login")
def auth_login(payload: LoginRequest) -> dict[str, Any]:
    """Valida usuario/contraseña y emite una nueva sesión móvil.

    La respuesta contiene tokens opacos. La contraseña no se conserva ni se devuelve.
    El Bearer histórico debe seguir configurado para garantizar que la sesión pueda
    delegar después en los endpoints actuales sin modificar su autenticación.
    """
    if not _configured_legacy_token():
        raise HTTPException(
            status_code=503,
            detail="MESHNET_MOBILE_API_TOKEN debe mantenerse configurado durante la migración",
        )
    if not users_configured():
        raise HTTPException(status_code=503, detail="No hay usuarios Mobile API configurados")

    identity = authenticate_user(payload.username, payload.password)
    if identity is None:
        raise HTTPException(
            status_code=401,
            detail="Usuario o contraseña no válidos",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return {"ok": True, **issue_session(identity)}


@app.post("/api/v1/auth/refresh")
def auth_refresh(payload: RefreshRequest) -> dict[str, Any]:
    """Rota access y refresh token sin volver a solicitar la contraseña."""
    if not _configured_legacy_token():
        raise HTTPException(
            status_code=503,
            detail="MESHNET_MOBILE_API_TOKEN debe mantenerse configurado durante la migración",
        )
    tokens = refresh_session(payload.refresh_token)
    if tokens is None:
        raise HTTPException(
            status_code=401,
            detail="Refresh token no válido o caducado",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return {"ok": True, **tokens}


@app.post("/api/v1/auth/logout")
def auth_logout(payload: LogoutRequest) -> dict[str, Any]:
    """Revoca la familia completa asociada al token presentado."""
    revoked = revoke_token_family(payload.token)
    return {"ok": True, "revoked": revoked}


@app.get("/api/v1/auth/me")
def auth_me(request: Request) -> dict[str, Any]:
    """Devuelve la identidad correspondiente a un access token de sesión.

    Este endpoint no acepta el Bearer fijo como identidad de usuario porque éste se
    conserva exclusivamente como mecanismo de compatibilidad durante la migración.
    """
    supplied = _bearer_token(request)
    identity: AuthIdentity | None = verify_access_token(supplied)
    if identity is None:
        raise HTTPException(
            status_code=401,
            detail="Sesión no válida o caducada",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return {
        "ok": True,
        "username": identity.username,
        "role": identity.role,
        "authentication": identity.auth_type,
    }


# Todas las rutas no resueltas arriba se entregan a la aplicación existente. FastAPI
# documenta el montaje de subaplicaciones como aplicaciones independientes, por lo que
# esta capa puede evolucionar sin reescribir la API histórica.
app.mount("/", legacy.app)
