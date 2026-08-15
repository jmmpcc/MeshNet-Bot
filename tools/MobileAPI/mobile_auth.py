#!/usr/bin/env python3
"""Autenticación de usuario y sesiones para MeshNet Mobile API.

Esta capa amplía la autenticación Bearer histórica sin sustituirla. Su objetivo es
permitir que MeshNet-Mobile obtenga tokens de sesión mediante usuario/contraseña,
manteniendo el Bearer fijo existente como vía de compatibilidad durante la migración.

Persistencia:
    - usuarios: JSON local no versionado con hash scrypt y salt aleatorio;
    - sesiones: SQLite local con hashes SHA-256 de tokens, nunca los tokens en claro.

Uso administrativo:
    python3 -m tools.MobileAPI.mobile_auth user-set USUARIO
    python3 -m tools.MobileAPI.mobile_auth user-disable USUARIO
    python3 -m tools.MobileAPI.mobile_auth user-enable USUARIO
    python3 -m tools.MobileAPI.mobile_auth user-list

Variables opcionales:
    MESHNET_MOBILE_AUTH_USERS_FILE
    MESHNET_MOBILE_AUTH_DB
    MESHNET_MOBILE_ACCESS_TTL_SECONDS
    MESHNET_MOBILE_REFRESH_TTL_SECONDS

Seguridad:
    - las contraseñas no se guardan ni se registran;
    - los tokens entregados al cliente sólo existen en claro en la respuesta y cliente;
    - la base SQLite conserva únicamente SHA-256(token);
    - refresh rota la familia completa anterior;
    - logout revoca la familia completa del token presentado.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
import re
import secrets
import sqlite3
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MODULE_DIR = Path(__file__).resolve().parent
DEFAULT_USERS_FILE = MODULE_DIR / "data" / "mobile_users.json"
DEFAULT_SESSIONS_DB = MODULE_DIR / "data" / "mobile_auth.db"
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_MAXMEM = 64 * 1024 * 1024
DEFAULT_ACCESS_TTL_SECONDS = 60 * 60
DEFAULT_REFRESH_TTL_SECONDS = 30 * 24 * 60 * 60


@dataclass(frozen=True)
class AuthIdentity:
    """Identidad validada asociada a una sesión móvil.

    Args:
        username: nombre de usuario tal como figura en el almacén local.
        role: rol informativo preparado para autorización posterior.
        auth_type: origen de autenticación; actualmente ``session``.
    """

    username: str
    role: str
    auth_type: str = "session"


def _users_file() -> Path:
    """Devuelve el fichero de usuarios configurado para este proceso."""
    configured = os.getenv("MESHNET_MOBILE_AUTH_USERS_FILE", "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_USERS_FILE


def _sessions_db() -> Path:
    """Devuelve la base SQLite de sesiones configurada para este proceso."""
    configured = os.getenv("MESHNET_MOBILE_AUTH_DB", "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_SESSIONS_DB


def _ttl_from_env(name: str, default: int, *, minimum: int, maximum: int) -> int:
    """Lee un TTL entero acotado, conservando un valor seguro ante errores."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(value, maximum))


def access_ttl_seconds() -> int:
    """Duración de un access token, por defecto una hora."""
    return _ttl_from_env(
        "MESHNET_MOBILE_ACCESS_TTL_SECONDS",
        DEFAULT_ACCESS_TTL_SECONDS,
        minimum=300,
        maximum=24 * 60 * 60,
    )


def refresh_ttl_seconds() -> int:
    """Duración de un refresh token, por defecto treinta días."""
    return _ttl_from_env(
        "MESHNET_MOBILE_REFRESH_TTL_SECONDS",
        DEFAULT_REFRESH_TTL_SECONDS,
        minimum=60 * 60,
        maximum=180 * 24 * 60 * 60,
    )


def _b64encode(value: bytes) -> str:
    """Codifica bytes en Base64 ASCII para persistencia JSON."""
    return base64.b64encode(value).decode("ascii")


def _b64decode(value: str) -> bytes:
    """Decodifica Base64 validando la entrada persistida."""
    return base64.b64decode(value.encode("ascii"), validate=True)


def hash_password(password: str) -> dict[str, Any]:
    """Genera un registro scrypt irreversible para una contraseña.

    Args:
        password: contraseña introducida por el administrador. No se persiste.

    Returns:
        Diccionario JSON-serializable con algoritmo, parámetros, salt y hash.

    Raises:
        ValueError: si la contraseña está vacía o supera 1024 caracteres.
    """
    if not password:
        raise ValueError("La contraseña no puede estar vacía")
    if len(password) > 1024:
        raise ValueError("La contraseña supera la longitud máxima permitida")

    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        maxmem=SCRYPT_MAXMEM,
        dklen=SCRYPT_DKLEN,
    )
    return {
        "algorithm": "scrypt",
        "n": SCRYPT_N,
        "r": SCRYPT_R,
        "p": SCRYPT_P,
        "dklen": SCRYPT_DKLEN,
        "salt": _b64encode(salt),
        "hash": _b64encode(derived),
    }


def verify_password(password: str, record: dict[str, Any]) -> bool:
    """Verifica una contraseña frente a un registro scrypt sin comparación temporal simple."""
    try:
        if record.get("algorithm") != "scrypt":
            return False
        salt = _b64decode(str(record["salt"]))
        expected = _b64decode(str(record["hash"]))
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(record["n"]),
            r=int(record["r"]),
            p=int(record["p"]),
            maxmem=SCRYPT_MAXMEM,
            dklen=int(record["dklen"]),
        )
    except (KeyError, TypeError, ValueError, OSError):
        return False
    return secrets.compare_digest(derived, expected)


def _load_users_document() -> dict[str, Any]:
    """Carga el documento local de usuarios o devuelve una estructura vacía válida."""
    path = _users_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "users": {}}
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"No se pudo leer {path}") from exc

    users = data.get("users") if isinstance(data, dict) else None
    if not isinstance(users, dict):
        raise RuntimeError(f"Formato de usuarios no válido en {path}")
    return {"version": 1, "users": users}


def _save_users_document(document: dict[str, Any]) -> None:
    """Guarda usuarios mediante reemplazo atómico para evitar ficheros parciales."""
    path = _users_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def normalize_username(username: str) -> str:
    """Valida y normaliza un nombre de usuario sin aceptar caracteres ambiguos."""
    value = username.strip()
    if not USERNAME_RE.fullmatch(value):
        raise ValueError("Usuario no válido: usa letras, números, punto, guion, _, @")
    return value


def set_user(username: str, password: str, *, role: str = "viewer", enabled: bool = True) -> None:
    """Crea o actualiza un usuario conservando sólo el hash irreversible.

    Args:
        username: identificador de acceso.
        password: contraseña nueva en claro, sólo durante esta llamada.
        role: rol preparado para futuras capacidades; por defecto ``viewer``.
        enabled: permite deshabilitar la cuenta sin borrarla.
    """
    normalized = normalize_username(username)
    role_value = role.strip().casefold() or "viewer"
    if role_value not in {"viewer", "operator", "admin"}:
        raise ValueError("Rol no válido: viewer, operator o admin")

    document = _load_users_document()
    document["users"][normalized] = {
        "enabled": bool(enabled),
        "role": role_value,
        "password": hash_password(password),
        "updated_at": int(time.time()),
    }
    _save_users_document(document)


def set_user_enabled(username: str, enabled: bool) -> None:
    """Habilita o deshabilita un usuario ya existente sin cambiar su contraseña."""
    normalized = normalize_username(username)
    document = _load_users_document()
    record = document["users"].get(normalized)
    if not isinstance(record, dict):
        raise KeyError(f"El usuario {normalized} no existe")
    record["enabled"] = bool(enabled)
    record["updated_at"] = int(time.time())
    _save_users_document(document)
    if not enabled:
        revoke_user_sessions(normalized)


def users_configured() -> bool:
    """Indica si existe al menos un usuario habilitado para autenticación móvil."""
    try:
        document = _load_users_document()
    except RuntimeError:
        return False
    return any(
        isinstance(record, dict) and bool(record.get("enabled"))
        for record in document["users"].values()
    )


def authenticate_user(username: str, password: str) -> AuthIdentity | None:
    """Valida credenciales locales y devuelve identidad sin revelar el motivo del fallo."""
    try:
        normalized = normalize_username(username)
        document = _load_users_document()
    except (ValueError, RuntimeError):
        return None

    record = document["users"].get(normalized)
    if not isinstance(record, dict) or not record.get("enabled"):
        return None
    password_record = record.get("password")
    if not isinstance(password_record, dict) or not verify_password(password, password_record):
        return None
    role = str(record.get("role") or "viewer").strip().casefold() or "viewer"
    return AuthIdentity(username=normalized, role=role)


def _connect_db() -> sqlite3.Connection:
    """Abre la base de sesiones y garantiza su esquema mínimo."""
    path = _sessions_db()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10.0)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK(kind IN ('access', 'refresh')),
            username TEXT NOT NULL,
            role TEXT NOT NULL,
            family_id TEXT NOT NULL,
            issued_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            revoked INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_mobile_auth_family ON sessions(family_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_mobile_auth_user ON sessions(username)"
    )
    connection.commit()
    return connection


def _token_hash(token: str) -> str:
    """Transforma un token opaco en la huella irreversible persistida."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _cleanup_sessions(connection: sqlite3.Connection, now: int) -> None:
    """Elimina sesiones caducadas o revocadas antiguas sin afectar sesiones activas."""
    retention_cutoff = now - 7 * 24 * 60 * 60
    connection.execute(
        "DELETE FROM sessions WHERE expires_at < ? OR (revoked = 1 AND issued_at < ?)",
        (now, retention_cutoff),
    )


def issue_session(identity: AuthIdentity) -> dict[str, Any]:
    """Crea una pareja access/refresh persistiendo únicamente sus huellas.

    Args:
        identity: usuario previamente validado.

    Returns:
        Tokens opacos y metadatos necesarios para MeshNet-Mobile.
    """
    now = int(time.time())
    access_ttl = access_ttl_seconds()
    refresh_ttl = refresh_ttl_seconds()
    family_id = uuid.uuid4().hex
    access_token = secrets.token_urlsafe(32)
    refresh_token = secrets.token_urlsafe(48)

    with _connect_db() as connection:
        _cleanup_sessions(connection, now)
        connection.executemany(
            """
            INSERT INTO sessions(
                token_hash, kind, username, role, family_id, issued_at, expires_at, revoked
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0)
            """,
            [
                (
                    _token_hash(access_token),
                    "access",
                    identity.username,
                    identity.role,
                    family_id,
                    now,
                    now + access_ttl,
                ),
                (
                    _token_hash(refresh_token),
                    "refresh",
                    identity.username,
                    identity.role,
                    family_id,
                    now,
                    now + refresh_ttl,
                ),
            ],
        )
        connection.commit()

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "expires_in": access_ttl,
        "refresh_expires_in": refresh_ttl,
        "username": identity.username,
        "role": identity.role,
    }


def _active_session(token: str, kind: str) -> sqlite3.Row | None:
    """Busca una sesión activa del tipo solicitado y limpia caducadas oportunísticamente."""
    if not token:
        return None
    now = int(time.time())
    with _connect_db() as connection:
        _cleanup_sessions(connection, now)
        row = connection.execute(
            """
            SELECT token_hash, kind, username, role, family_id, issued_at, expires_at, revoked
            FROM sessions
            WHERE token_hash = ? AND kind = ? AND revoked = 0 AND expires_at > ?
            """,
            (_token_hash(token), kind, now),
        ).fetchone()
        connection.commit()
        return row


def verify_access_token(token: str) -> AuthIdentity | None:
    """Valida un access token persistido y devuelve su identidad asociada."""
    row = _active_session(token, "access")
    if row is None:
        return None
    return AuthIdentity(username=str(row["username"]), role=str(row["role"]))


def refresh_session(refresh_token: str) -> dict[str, Any] | None:
    """Rota una sesión usando un refresh token válido.

    La familia anterior se revoca completa antes de emitir una nueva pareja. Así un
    refresh token utilizado una vez no puede reutilizarse posteriormente.
    """
    row = _active_session(refresh_token, "refresh")
    if row is None:
        return None

    identity = AuthIdentity(username=str(row["username"]), role=str(row["role"]))
    with _connect_db() as connection:
        connection.execute(
            "UPDATE sessions SET revoked = 1 WHERE family_id = ?",
            (str(row["family_id"]),),
        )
        connection.commit()
    return issue_session(identity)


def revoke_token_family(token: str) -> bool:
    """Revoca access y refresh pertenecientes a la familia de un token conocido."""
    if not token:
        return False
    digest = _token_hash(token)
    with _connect_db() as connection:
        row = connection.execute(
            "SELECT family_id FROM sessions WHERE token_hash = ?",
            (digest,),
        ).fetchone()
        if row is None:
            return False
        connection.execute(
            "UPDATE sessions SET revoked = 1 WHERE family_id = ?",
            (str(row["family_id"]),),
        )
        connection.commit()
        return True


def revoke_user_sessions(username: str) -> int:
    """Revoca todas las sesiones de un usuario, útil al deshabilitar una cuenta."""
    normalized = normalize_username(username)
    with _connect_db() as connection:
        cursor = connection.execute(
            "UPDATE sessions SET revoked = 1 WHERE username = ? AND revoked = 0",
            (normalized,),
        )
        connection.commit()
        return int(cursor.rowcount)


def list_users() -> list[dict[str, Any]]:
    """Devuelve metadatos no secretos de usuarios para la utilidad administrativa."""
    document = _load_users_document()
    rows: list[dict[str, Any]] = []
    for username, record in sorted(document["users"].items(), key=lambda item: item[0].casefold()):
        if not isinstance(record, dict):
            continue
        rows.append(
            {
                "username": username,
                "enabled": bool(record.get("enabled")),
                "role": str(record.get("role") or "viewer"),
            }
        )
    return rows


def main() -> None:
    """CLI administrativa para gestionar usuarios sin editar hashes manualmente."""
    parser = argparse.ArgumentParser(description="Usuarios de MeshNet Mobile API")
    subparsers = parser.add_subparsers(dest="command", required=True)

    set_parser = subparsers.add_parser("user-set", help="Crear o cambiar contraseña de un usuario")
    set_parser.add_argument("username")
    set_parser.add_argument("--role", choices=("viewer", "operator", "admin"), default="viewer")

    for command in ("user-enable", "user-disable"):
        action = subparsers.add_parser(command)
        action.add_argument("username")

    subparsers.add_parser("user-list", help="Listar usuarios sin mostrar hashes")
    args = parser.parse_args()

    if args.command == "user-set":
        password = getpass.getpass("Contraseña: ")
        confirmation = getpass.getpass("Repite la contraseña: ")
        if password != confirmation:
            raise SystemExit("Las contraseñas no coinciden")
        set_user(args.username, password, role=args.role, enabled=True)
        print(f"Usuario {normalize_username(args.username)} guardado correctamente.")
    elif args.command == "user-enable":
        set_user_enabled(args.username, True)
        print(f"Usuario {normalize_username(args.username)} habilitado.")
    elif args.command == "user-disable":
        set_user_enabled(args.username, False)
        print(f"Usuario {normalize_username(args.username)} deshabilitado y sesiones revocadas.")
    else:
        for row in list_users():
            state = "enabled" if row["enabled"] else "disabled"
            print(f"{row['username']}\t{row['role']}\t{state}")


if __name__ == "__main__":
    main()
