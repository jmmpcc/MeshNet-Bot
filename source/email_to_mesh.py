#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
email_to_mesh.py
================

Pasarela independiente correo electrónico -> MeshNet Broker.

Objetivo
--------
Vigilar mediante IMAP una bandeja de correo, aceptar únicamente mensajes de
remitentes autorizados y enviar el asunto del correo a un canal concreto de la
malla utilizando SEND_TEXT para Meshtastic o MESHCORE_SEND para MeshCore.

La integración es deliberadamente independiente del proceso principal:
- No modifica Meshtastic_Broker.py.
- No abre conexiones directas contra el nodo Meshtastic o MeshCore.
- No duplica la cola, el troceo ni los reintentos RF del broker.
- Si IMAP falla, el broker y el resto del sistema continúan funcionando.

Configuración mínima (.env)
---------------------------
EMAIL_TO_MESH_ENABLED=1
EMAIL_IMAP_HOST=imap.example.org
EMAIL_IMAP_PORT=993
EMAIL_IMAP_SSL=1
EMAIL_IMAP_USER=cuenta@example.org
EMAIL_IMAP_PASSWORD=token_o_contrasena_de_aplicacion
EMAIL_ALLOWED_SENDERS=avisos@example.org
EMAIL_MESH_CHANNEL=0
EMAIL_MESH_PREFIX=[EMAIL]

Encaminamiento por asunto
-------------------------
[ch3] Texto   -> canal 3 del nodo Meshtastic embebido/principal
[ch2]M Texto  -> canal 2 del motor MeshCore embebido

La marca ``M`` debe ir pegada al corchete de cierre. Así, ``[ch6] Muy...``
se interpreta correctamente como Meshtastic y no confunde la inicial de ``Muy``
con el selector de MeshCore.

Sin prefijo se utiliza EMAIL_MESH_CHANNEL y, si RADIO_PROFILE=meshcore_only, la red MeshCore; en los demás perfiles se conserva Meshtastic.

Primera ejecución
-----------------
Por seguridad, EMAIL_PROCESS_EXISTING=0 crea una línea base con los mensajes ya
existentes y solo procesa los que lleguen después. Para procesar también el
contenido actual del buzón se debe configurar expresamente:

    EMAIL_PROCESS_EXISTING=1

Estado persistente
------------------
Se guarda de forma atómica en EMAIL_STATE_PATH, por defecto:

    /app/bot_data/email_to_mesh_state.json

El estado contiene UIDVALIDITY, último UID examinado y una lista limitada de
Message-ID recientes. Esto evita reenvíos tras reinicios normales.
"""

from __future__ import annotations

import argparse
import email
import imaplib
import smtplib
import json
import logging
import os
import re
import select
import signal
import socket
import ssl
import tempfile
import threading
import time
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.message import Message
from email.policy import default as default_email_policy
from email.utils import formatdate, make_msgid, parseaddr
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple


APP_NAME = "email-to-mesh"
APP_VERSION = "1.3.0"

_LOG = logging.getLogger(APP_NAME)
_STOP_EVENT = threading.Event()
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]+")
_SPACES_RE = re.compile(r"\s+")
_UIDVALIDITY_RE = re.compile(rb"UIDVALIDITY\s+(\d+)", re.IGNORECASE)
_UIDNEXT_RE = re.compile(rb"UIDNEXT\s+(\d+)", re.IGNORECASE)
# Prefijos de encaminamiento admitidos al principio del asunto:
#   [ch3] Texto   -> canal 3 de Meshtastic
#   [ch2]M Texto  -> canal 2 de MeshCore
_SUBJECT_ROUTE_RE = re.compile(r"^\s*\[\s*ch\s*(\d+)\s*\](m)?(?:\s*[:\-]?\s*)", re.IGNORECASE)


# =============================================================================
# Configuración
# =============================================================================


def _env_bool(name: str, default: bool = False) -> bool:
    """
    Lee una variable de entorno como booleano.

    Parámetros:
        name:
            Nombre de la variable.
        default:
            Valor usado si la variable no existe o está vacía.

    Valores verdaderos admitidos:
        1, true, yes, y, on, si, sí
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return bool(default)
    return raw.strip().lower() in {"1", "true", "yes", "y", "on", "si", "sí"}


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    """
    Lee una variable de entorno como entero y aplica límites seguros.
    """
    try:
        value = int(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        value = int(default)
    return max(minimum, min(maximum, value))


def _csv_addresses(raw: str) -> Set[str]:
    """
    Convierte una lista separada por comas o punto y coma en direcciones
    normalizadas en minúsculas.

    No acepta comodines. La comparación posterior siempre es exacta.
    """
    values: Set[str] = set()
    for item in re.split(r"[,;]", raw or ""):
        address = parseaddr(item.strip())[1].strip().lower()
        if address and "@" in address:
            values.add(address)
    return values


@dataclass(frozen=True)
class SubjectRoute:
    """
    Resultado de interpretar el prefijo opcional de encaminamiento del asunto.

    Campos:
        network:
            ``meshtastic`` o ``meshcore``.
        channel:
            Canal numérico de la red seleccionada.
        text:
            Texto del asunto sin el prefijo de encaminamiento.
        explicit:
            True cuando el asunto incluía un prefijo válido.
    """

    network: str
    channel: int
    text: str
    explicit: bool


@dataclass(frozen=True)
class Config:
    """
    Configuración inmutable de la pasarela.

    Se construye una sola vez con Config.from_env() y se valida antes de abrir
    conexiones de red.
    """

    enabled: bool
    imap_host: str
    imap_port: int
    imap_ssl: bool
    imap_starttls: bool
    imap_user: str
    imap_password: str
    imap_folder: str
    allowed_senders: Set[str]
    poll_interval_sec: int
    imap_idle_enabled: bool
    imap_idle_refresh_sec: int
    imap_idle_verify_sec: int
    imap_idle_fallback_poll_sec: int
    process_existing: bool
    mark_as_read: bool
    delete_after_send: bool
    max_messages_per_cycle: int
    state_path: Path
    recent_message_ids_max: int
    mesh_channel: int
    mesh_dest: Optional[str]
    mesh_ack: bool
    mesh_prefix: str
    mesh_allow_bridge: bool
    max_subject_chars: int
    broker_host: str
    broker_port: int
    broker_timeout_sec: int
    default_network: str
    log_level: str

    @classmethod
    def from_env(cls) -> "Config":
        """
        Crea la configuración desde variables de entorno.

        La función no abre archivos ni conexiones. Las credenciales permanecen
        únicamente en memoria y nunca se imprimen en el log.
        """
        raw_dest = os.getenv("EMAIL_MESH_DEST", "broadcast").strip()
        mesh_dest = None if not raw_dest or raw_dest.lower() == "broadcast" else raw_dest

        imap_ssl = _env_bool("EMAIL_IMAP_SSL", True)
        imap_starttls = _env_bool("EMAIL_IMAP_STARTTLS", False)

        return cls(
            enabled=_env_bool("EMAIL_TO_MESH_ENABLED", False),
            imap_host=os.getenv("EMAIL_IMAP_HOST", "").strip(),
            imap_port=_env_int("EMAIL_IMAP_PORT", 993 if imap_ssl else 143, 1, 65535),
            imap_ssl=imap_ssl,
            imap_starttls=imap_starttls,
            imap_user=os.getenv("EMAIL_IMAP_USER", "").strip(),
            imap_password=os.getenv("EMAIL_IMAP_PASSWORD", ""),
            imap_folder=os.getenv("EMAIL_IMAP_FOLDER", "INBOX").strip() or "INBOX",
            allowed_senders=_csv_addresses(os.getenv("EMAIL_ALLOWED_SENDERS", "")),
            poll_interval_sec=_env_int("EMAIL_POLL_INTERVAL_SEC", 30, 5, 3600),
            imap_idle_enabled=_env_bool("EMAIL_IMAP_IDLE_ENABLED", True),
            imap_idle_refresh_sec=_env_int("EMAIL_IMAP_IDLE_REFRESH_SEC", 1500, 60, 1740),
            imap_idle_verify_sec=_env_int("EMAIL_IMAP_IDLE_VERIFY_SEC", 15, 5, 300),
            imap_idle_fallback_poll_sec=_env_int("EMAIL_IMAP_IDLE_FALLBACK_POLL_SEC", 10, 5, 300),
            process_existing=_env_bool("EMAIL_PROCESS_EXISTING", False),
            mark_as_read=_env_bool("EMAIL_MARK_AS_READ", True),
            delete_after_send=_env_bool("EMAIL_DELETE_AFTER_SEND", False),
            max_messages_per_cycle=_env_int("EMAIL_MAX_MESSAGES_PER_CYCLE", 20, 1, 500),
            state_path=Path(
                os.getenv("EMAIL_STATE_PATH", "/app/bot_data/email_to_mesh_state.json").strip()
                or "/app/bot_data/email_to_mesh_state.json"
            ),
            recent_message_ids_max=_env_int("EMAIL_RECENT_MESSAGE_IDS_MAX", 200, 10, 5000),
            mesh_channel=_env_int("EMAIL_MESH_CHANNEL", 0, 0, 15),
            mesh_dest=mesh_dest,
            mesh_ack=_env_bool("EMAIL_MESH_REQUIRE_ACK", False),
            mesh_prefix=os.getenv("EMAIL_MESH_PREFIX", "[EMAIL]").strip(),
            mesh_allow_bridge=_env_bool("EMAIL_MESH_ALLOW_BRIDGE", True),
            max_subject_chars=_env_int("EMAIL_MAX_SUBJECT_CHARS", 500, 1, 5000),
            broker_host=os.getenv("BROKER_CTRL_HOST", "127.0.0.1").strip() or "127.0.0.1",
            broker_port=_env_int("BROKER_CTRL_PORT", 8766, 1, 65535),
            broker_timeout_sec=_env_int("EMAIL_BROKER_TIMEOUT_SEC", 8, 1, 120),
            default_network=_default_email_network(),
            log_level=os.getenv("EMAIL_LOG_LEVEL", "INFO").strip().upper() or "INFO",
        )

    def validate(self) -> List[str]:
        """
        Devuelve una lista de errores bloqueantes de configuración.

        No lanza excepciones para poder mostrar todos los problemas en una sola
        ejecución.
        """
        errors: List[str] = []
        if self.imap_ssl and self.imap_starttls:
            errors.append("EMAIL_IMAP_SSL y EMAIL_IMAP_STARTTLS no pueden estar activos a la vez")
        if not self.imap_host:
            errors.append("falta EMAIL_IMAP_HOST")
        if not self.imap_user:
            errors.append("falta EMAIL_IMAP_USER")
        if not self.imap_password:
            errors.append("falta EMAIL_IMAP_PASSWORD")
        if not self.allowed_senders:
            errors.append("EMAIL_ALLOWED_SENDERS no contiene ninguna dirección válida")
        if self.mesh_ack and not self.mesh_dest:
            errors.append("EMAIL_MESH_REQUIRE_ACK=1 requiere EMAIL_MESH_DEST unicast")
        return errors


# =============================================================================
# Contactos y envío malla -> correo
# =============================================================================

_MAIL_CMD_RE = re.compile(r"^\s*(?:\[\s*mail\s*\]|/mail\b|mail\b)\s*(.*)$", re.IGNORECASE | re.DOTALL)
_CONTACT_KEY_RE = re.compile(r"[^a-z0-9_.-]+")


def default_contacts_path() -> Path:
    return Path(os.getenv("EMAIL_CONTACTS_PATH", "/app/bot_data/email_contacts.json").strip() or "/app/bot_data/email_contacts.json")


def _contact_key(name: str) -> str:
    key = _CONTACT_KEY_RE.sub("-", (name or "").strip().lower()).strip("-._")
    if not key:
        raise ValueError("nombre de contacto vacío")
    return key


def _valid_email_address(address: str) -> str:
    parsed = parseaddr(address or "")[1].strip().lower()
    if not parsed or "@" not in parsed or parsed.startswith("@") or parsed.endswith("@"):
        raise ValueError(f"correo inválido: {address!r}")
    return parsed


def load_contacts(path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    path = path or default_contacts_path()
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8") or "{}")
    contacts = raw.get("contacts", raw) if isinstance(raw, dict) else {}
    if not isinstance(contacts, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for key, value in contacts.items():
        if not isinstance(value, dict):
            continue
        try:
            ckey = _contact_key(str(value.get("name") or key))
            out[ckey] = {"name": str(value.get("name") or ckey), "email": _valid_email_address(str(value.get("email") or ""))}
        except Exception:
            continue
    return out


def save_contacts(contacts: Dict[str, Dict[str, Any]], path: Optional[Path] = None) -> None:
    path = path or default_contacts_path()
    payload = {"version": 1, "contacts": contacts, "updated_at": int(time.time())}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            handle.flush(); os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def upsert_contact(name: str, address: str, path: Optional[Path] = None) -> Dict[str, Any]:
    contacts = load_contacts(path)
    key = _contact_key(name)
    contacts[key] = {"name": name.strip(), "email": _valid_email_address(address)}
    save_contacts(contacts, path)
    return {"key": key, **contacts[key]}


def delete_contact(name_or_number: str, path: Optional[Path] = None) -> Dict[str, Any]:
    contacts = load_contacts(path)
    key = resolve_contact_key(name_or_number, contacts)
    removed = contacts.pop(key)
    save_contacts(contacts, path)
    return {"key": key, **removed}


def resolve_contact_key(name_or_number: str, contacts: Dict[str, Dict[str, Any]]) -> str:
    token = (name_or_number or "").strip()
    if token.isdigit():
        idx = int(token)
        keys = sorted(contacts)
        if 1 <= idx <= len(keys):
            return keys[idx - 1]
        raise KeyError(f"número de contacto fuera de rango: {idx}")
    key = _contact_key(token)
    if key in contacts:
        return key
    matches = [k for k, v in contacts.items() if k.startswith(key) or str(v.get("name", "")).lower().startswith(token.lower())]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise KeyError("contacto ambiguo: " + ", ".join(sorted(matches)))
    raise KeyError(f"contacto no encontrado: {token}")


def format_contacts(contacts: Dict[str, Dict[str, Any]]) -> str:
    if not contacts:
        return "No hay contactos de correo guardados."
    lines = ["Contactos de correo:"]
    for idx, key in enumerate(sorted(contacts), start=1):
        c = contacts[key]
        lines.append(f"{idx}. {c.get('name') or key} <{c.get('email')}> [{key}]")
    return "\n".join(lines)


def _smtp_config() -> Dict[str, Any]:
    return {
        "host": os.getenv("EMAIL_SMTP_HOST", "").strip(),
        "port": _env_int("EMAIL_SMTP_PORT", 587, 1, 65535),
        "ssl": _env_bool("EMAIL_SMTP_SSL", False),
        "starttls": _env_bool("EMAIL_SMTP_STARTTLS", True),
        "user": os.getenv("EMAIL_SMTP_USER", os.getenv("EMAIL_IMAP_USER", "")).strip(),
        "password": os.getenv("EMAIL_SMTP_PASSWORD", os.getenv("EMAIL_IMAP_PASSWORD", "")),
        "from": os.getenv("EMAIL_FROM", os.getenv("EMAIL_SMTP_USER", os.getenv("EMAIL_IMAP_USER", ""))).strip(),
        "subject_prefix": os.getenv("EMAIL_OUT_SUBJECT_PREFIX", "[Mesh]").strip(),
    }


def send_email_to_contact(contact: Dict[str, Any], body: str, source: str = "mesh") -> None:
    cfg = _smtp_config()
    missing = [k for k in ("host", "user", "password", "from") if not cfg.get(k)]
    if missing:
        raise RuntimeError("faltan variables SMTP: " + ", ".join("EMAIL_FROM" if k == "from" else "EMAIL_SMTP_" + k.upper() for k in missing))
    to_addr = _valid_email_address(str(contact.get("email") or ""))
    subject = f"{cfg['subject_prefix']} mensaje de {source}".strip()
    msg = Message()
    msg["From"] = cfg["from"]; msg["To"] = to_addr; msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True); msg["Message-ID"] = make_msgid(domain=parseaddr(cfg["from"])[1].split("@")[-1])
    msg.set_payload(body, charset="utf-8")
    klass = smtplib.SMTP_SSL if cfg["ssl"] else smtplib.SMTP
    with klass(cfg["host"], int(cfg["port"]), timeout=30) as smtp:
        if cfg["starttls"] and not cfg["ssl"]:
            smtp.starttls(context=ssl.create_default_context())
        smtp.login(cfg["user"], cfg["password"])
        smtp.send_message(msg)


def handle_mesh_mail_command(text: str, source: str = "mesh", contacts_path: Optional[Path] = None) -> Optional[str]:
    m = _MAIL_CMD_RE.match(text or "")
    if not m:
        return None
    rest = (m.group(1) or "").strip()
    contacts = load_contacts(contacts_path)
    if not rest or rest.lower() in {"list", "lista", "contactos", "ls"}:
        return format_contacts(contacts)
    parts = rest.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        return "Uso: [mail] contacto texto mensaje | [mail] lista"
    key = resolve_contact_key(parts[0], contacts)
    contact = contacts[key]
    send_email_to_contact(contact, parts[1].strip(), source=source)
    return f"Correo enviado a {contact.get('name') or key} <{contact.get('email')}>"

# =============================================================================
# Estado persistente
# =============================================================================


def _default_state() -> Dict[str, Any]:
    """Crea un estado vacío compatible con versiones futuras."""
    return {
        "version": 1,
        "uidvalidity": None,
        "last_uid": 0,
        "recent_message_ids": [],
        "updated_at": 0,
    }


def load_state(path: Path) -> Dict[str, Any]:
    """
    Carga el estado persistente.

    Si el archivo no existe, está vacío o está corrupto, registra un aviso y
    devuelve un estado nuevo. No interrumpe el servicio.
    """
    state = _default_state()
    if not path.exists():
        return state

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("el estado no es un objeto JSON")
        state["uidvalidity"] = int(raw["uidvalidity"]) if raw.get("uidvalidity") is not None else None
        state["last_uid"] = max(0, int(raw.get("last_uid", 0)))
        ids = raw.get("recent_message_ids", [])
        state["recent_message_ids"] = [str(x) for x in ids if str(x).strip()] if isinstance(ids, list) else []
        state["updated_at"] = int(raw.get("updated_at", 0) or 0)
    except Exception as exc:
        _LOG.warning("Estado inválido en %s; se inicia uno nuevo: %s", path, exc)
    return state


def save_state(path: Path, state: Dict[str, Any]) -> None:
    """
    Guarda el estado mediante escritura atómica.

    Se escribe primero en un temporal del mismo directorio y después se utiliza
    os.replace(), evitando dejar un JSON parcial si el contenedor se detiene.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(state)
    payload["updated_at"] = int(time.time())
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        except OSError:
            pass


# =============================================================================
# Correo
# =============================================================================


def decode_mime_text(value: Optional[str]) -> str:
    """
    Decodifica una cabecera MIME, incluyendo asuntos fragmentados y UTF-8.

    Ejemplo de llamada:
        subject = decode_mime_text(message.get("Subject"))
    """
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def normalize_subject(value: str, max_chars: int) -> str:
    """
    Limpia el asunto antes de enviarlo por radio.

    Elimina caracteres de control, convierte saltos de línea y tabuladores en
    espacios, colapsa espacios repetidos y limita la longitud configurada.
    """
    text = _CONTROL_CHARS_RE.sub(" ", value or "")
    text = _SPACES_RE.sub(" ", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip()
    return text


def _default_email_network() -> str:
    """Devuelve la red por defecto para asuntos sin prefijo explícito."""
    raw = (os.getenv("EMAIL_MESH_NETWORK") or os.getenv("EMAIL_DEFAULT_NETWORK") or "").strip().lower()
    if raw in {"meshcore", "mc", "mesh", "malla"}:
        return "meshcore"
    if raw in {"meshtastic", "mt", "radio"}:
        return "meshtastic"
    return "meshcore" if (os.getenv("RADIO_PROFILE") or "").strip().lower() == "meshcore_only" else "meshtastic"


def parse_subject_route(subject: str, default_meshtastic_channel: int, default_network: str = "meshtastic") -> SubjectRoute:
    """
    Interpreta el prefijo opcional de red y canal situado al inicio del asunto.

    Formatos admitidos, sin distinguir mayúsculas/minúsculas:
        [ch3] Texto
            Envía ``Texto`` al canal 3 de Meshtastic.

        [ch2]M Texto
            Envía ``Texto`` al canal 2 de MeshCore.

    También tolera espacios internos y un separador opcional ``:`` o ``-``::

        [ CH 3 ]: Texto
        [ch2]M - Texto

    Cuando no existe un prefijo válido se conserva todo el asunto y se utiliza
    EMAIL_MESH_CHANNEL sobre la red por defecto resuelta por configuración. En
    RADIO_PROFILE=meshcore_only esa red por defecto es MeshCore, salvo que se
    fuerce EMAIL_MESH_NETWORK=meshtastic.

    El canal Meshtastic se limita a 0..15 porque es el rango utilizado por el
    broker. Para MeshCore se admite 0..255; la disponibilidad real del índice
    será validada finalmente por el motor MeshCore embebido.
    """
    raw = (subject or "").strip()
    match = _SUBJECT_ROUTE_RE.match(raw)
    if not match:
        network = "meshcore" if str(default_network).strip().lower() == "meshcore" else "meshtastic"
        return SubjectRoute(
            network=network,
            channel=int(default_meshtastic_channel),
            text=raw,
            explicit=False,
        )

    channel = int(match.group(1))
    network = "meshcore" if match.group(2) else "meshtastic"
    max_channel = 255 if network == "meshcore" else 15
    if channel < 0 or channel > max_channel:
        raise ValueError(
            f"canal fuera de rango para {network}: {channel} "
            f"(permitido 0..{max_channel})"
        )

    routed_text = raw[match.end():].strip()
    return SubjectRoute(
        network=network,
        channel=channel,
        text=routed_text,
        explicit=True,
    )


def parse_sender(message: Message) -> str:
    """
    Extrae la dirección real de la cabecera From y la normaliza.

    La comparación se realiza sobre la dirección devuelta por parseaddr(), no
    sobre el nombre visible, evitando aceptar nombres de remitente engañosos.
    """
    raw_from = decode_mime_text(message.get("From"))
    return parseaddr(raw_from)[1].strip().lower()


def _extract_fetch_bytes(fetch_response: Sequence[Any]) -> bytes:
    """
    Extrae los bytes de cabecera de la respuesta heterogénea de imaplib.fetch().
    """
    chunks: List[bytes] = []
    for item in fetch_response:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
            chunks.append(bytes(item[1]))
    return b"\n".join(chunks)


def _parse_mailbox_status(raw_status: Sequence[bytes]) -> Tuple[int, int]:
    """
    Obtiene UIDVALIDITY y UIDNEXT de la respuesta IMAP STATUS.

    Devuelve:
        (uidvalidity, uidnext)
    """
    blob = b" ".join(x for x in raw_status if isinstance(x, bytes))
    valid_match = _UIDVALIDITY_RE.search(blob)
    next_match = _UIDNEXT_RE.search(blob)
    if not valid_match or not next_match:
        raise RuntimeError(f"respuesta STATUS no reconocida: {blob!r}")
    return int(valid_match.group(1)), int(next_match.group(1))


def open_imap(config: Config) -> imaplib.IMAP4:
    """
    Abre, cifra y autentica una sesión IMAP.

    Parámetros:
        config:
            Configuración validada.

    Retorno:
        Instancia IMAP autenticada. El llamador debe ejecutar logout().
    """
    if config.imap_ssl:
        client: imaplib.IMAP4 = imaplib.IMAP4_SSL(
            config.imap_host,
            config.imap_port,
            ssl_context=ssl.create_default_context(),
            timeout=30,
        )
    else:
        client = imaplib.IMAP4(config.imap_host, config.imap_port, timeout=30)
        if config.imap_starttls:
            client.starttls(ssl_context=ssl.create_default_context())

    result, _ = client.login(config.imap_user, config.imap_password)
    if result != "OK":
        raise RuntimeError("el servidor IMAP rechazó la autenticación")
    return client


# =============================================================================
# Broker
# =============================================================================


def _send_broker_request(config: Config, request: Dict[str, Any]) -> Dict[str, Any]:
    """
    Envía una orden JSON al puerto de control del broker y devuelve su respuesta.

    Esta función común evita duplicar el transporte TCP entre SEND_TEXT y
    MESHCORE_SEND. No abre conexiones directas contra ningún nodo de radio.
    """
    wire = (json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8")

    with socket.create_connection(
        (config.broker_host, config.broker_port),
        timeout=config.broker_timeout_sec,
    ) as sock:
        sock.settimeout(config.broker_timeout_sec)
        sock.sendall(wire)
        response = bytearray()
        while b"\n" not in response:
            chunk = sock.recv(65536)
            if not chunk:
                break
            response.extend(chunk)
            if len(response) > 1024 * 1024:
                raise RuntimeError("respuesta del broker excesivamente grande")

    line = bytes(response).split(b"\n", 1)[0].decode("utf-8", errors="replace").strip()
    if not line:
        raise RuntimeError("el broker cerró la conexión sin responder")
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"respuesta JSON inválida del broker: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("respuesta del broker con formato inesperado")
    return parsed


def send_meshtastic_to_broker(
    config: Config,
    text: str,
    channel: int,
    sender: str,
    message_id: str,
) -> Dict[str, Any]:
    """
    Encola texto para el nodo Meshtastic administrado por el broker.

    Funciona igualmente cuando dicho nodo es el nodo principal embebido del
    broker, porque utiliza SEND_TEXT y no abre una segunda conexión TCP contra
    el equipo Meshtastic.
    """
    request = {
        "cmd": "SEND_TEXT",
        "params": {
            "text": text,
            "dest": config.mesh_dest,
            "ch": int(channel),
            "ack": 1 if config.mesh_ack else 0,
            "no_bridge": not config.mesh_allow_bridge,
            "origin": "email",
            "meta": {
                "source": "email",
                "network": "meshtastic",
                "channel": int(channel),
                "sender": sender,
                "message_id": message_id,
            },
        },
    }
    return _send_broker_request(config, request)


def send_meshcore_to_broker(
    config: Config,
    text: str,
    channel: int,
    sender: str,
    message_id: str,
) -> Dict[str, Any]:
    """
    Encola texto para un canal del motor MeshCore embebido en el broker.

    Reutiliza el comando MESHCORE_SEND ya disponible. No crea otro cliente
    MeshCore ni modifica la cola 24/7 existente. Los metadatos de correo no se
    envían porque el contrato actual de MESHCORE_SEND solo necesita canal y texto.
    """
    del sender, message_id  # Validados antes; no forman parte del contrato MeshCore.
    request = {
        "cmd": "MESHCORE_SEND",
        "params": {
            "kind": "chan",
            "channel_idx": int(channel),
            "text": text,
        },
    }
    return _send_broker_request(config, request)


def send_routed_text_to_broker(
    config: Config,
    route: SubjectRoute,
    text: str,
    sender: str,
    message_id: str,
) -> Dict[str, Any]:
    """Selecciona el comando del broker según la red indicada por el asunto."""
    if route.network == "meshcore":
        return send_meshcore_to_broker(
            config=config,
            text=text,
            channel=route.channel,
            sender=sender,
            message_id=message_id,
        )
    return send_meshtastic_to_broker(
        config=config,
        text=text,
        channel=route.channel,
        sender=sender,
        message_id=message_id,
    )


# =============================================================================
# Procesamiento
# =============================================================================


def _remember_message_id(state: Dict[str, Any], message_id: str, maximum: int) -> None:
    """Añade un Message-ID al historial limitado, sin duplicarlo."""
    if not message_id:
        return
    current = [str(x) for x in state.get("recent_message_ids", []) if str(x)]
    current = [x for x in current if x != message_id]
    current.append(message_id)
    state["recent_message_ids"] = current[-maximum:]


def _format_mesh_text(prefix: str, subject: str) -> str:
    """Une prefijo y asunto sin generar espacios sobrantes."""
    return f"{prefix} {subject}".strip() if prefix else subject


def process_mailbox_once(config: Config, state: Dict[str, Any]) -> int:
    """
    Ejecuta un ciclo completo de lectura IMAP.

    Funcionamiento:
        1. Abre la cuenta y selecciona la carpeta.
        2. Comprueba UIDVALIDITY y crea la línea base si corresponde.
        3. Recupera UIDs posteriores a last_uid.
        4. Descarta remitentes no autorizados.
        5. Envía el asunto al broker.
        6. Persiste el UID solamente tras cada decisión definitiva.

    Retorno:
        Número de correos enviados correctamente a la cola del broker.

    Regla crítica:
        Si un correo autorizado no puede encolarse, no se avanza last_uid sobre
        ese mensaje. Se volverá a intentar en el siguiente ciclo.
    """
    client: Optional[imaplib.IMAP4] = None
    sent_count = 0

    try:
        client = open_imap(config)

        status, _ = client.select(config.imap_folder, readonly=False)
        if status != "OK":
            raise RuntimeError(f"no se pudo seleccionar la carpeta {config.imap_folder!r}")

        status, status_data = client.status(config.imap_folder, "(UIDVALIDITY UIDNEXT)")
        if status != "OK" or not status_data:
            raise RuntimeError("no se pudo consultar UIDVALIDITY/UIDNEXT")
        uidvalidity, uidnext = _parse_mailbox_status(status_data)

        stored_uidvalidity = state.get("uidvalidity")
        if stored_uidvalidity != uidvalidity:
            # Buzón nuevo, primer arranque o UIDVALIDITY regenerado por el servidor.
            state["uidvalidity"] = uidvalidity
            state["recent_message_ids"] = []
            state["last_uid"] = 0 if config.process_existing else max(0, uidnext - 1)
            save_state(config.state_path, state)
            _LOG.info(
                "Línea base IMAP establecida: uidvalidity=%s last_uid=%s process_existing=%s",
                uidvalidity,
                state["last_uid"],
                config.process_existing,
            )
            if not config.process_existing:
                return 0

        start_uid = int(state.get("last_uid", 0)) + 1
        status, search_data = client.uid("SEARCH", None, f"UID {start_uid}:*")
        if status != "OK":
            raise RuntimeError("falló la búsqueda incremental de mensajes")

        raw_uids = search_data[0].split() if search_data and search_data[0] else []
        uids = sorted({int(raw) for raw in raw_uids if raw.isdigit()})
        # Algunos servidores pueden devolver el último UID al buscar un rango cuyo
        # inicio es superior. Esta condición elimina cualquier UID ya procesado.
        uids = [uid for uid in uids if uid >= start_uid]
        uids = uids[: config.max_messages_per_cycle]

        if not uids:
            return 0

        recent_ids = set(str(x) for x in state.get("recent_message_ids", []))

        for uid in uids:
            if _STOP_EVENT.is_set():
                break

            status, fetch_data = client.uid(
                "FETCH",
                str(uid),
                "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID DATE)])",
            )
            if status != "OK" or not fetch_data:
                raise RuntimeError(f"no se pudieron leer las cabeceras del UID {uid}")

            raw_headers = _extract_fetch_bytes(fetch_data)
            if not raw_headers:
                raise RuntimeError(f"el UID {uid} no devolvió cabeceras")

            message = email.message_from_bytes(raw_headers, policy=default_email_policy)
            sender = parse_sender(message)
            message_id = decode_mime_text(message.get("Message-ID")).strip()

            if sender not in config.allowed_senders:
                _LOG.info("Correo UID %s descartado: remitente no autorizado %r", uid, sender or "-")
                state["last_uid"] = uid
                save_state(config.state_path, state)
                continue

            if message_id and message_id in recent_ids:
                _LOG.warning("Correo UID %s omitido por Message-ID duplicado", uid)
                state["last_uid"] = uid
                save_state(config.state_path, state)
                continue

            subject = normalize_subject(decode_mime_text(message.get("Subject")), config.max_subject_chars)
            if not subject:
                _LOG.info("Correo UID %s descartado: asunto vacío", uid)
                state["last_uid"] = uid
                _remember_message_id(state, message_id, config.recent_message_ids_max)
                recent_ids.add(message_id) if message_id else None
                save_state(config.state_path, state)
                continue

            try:
                route = parse_subject_route(subject, config.mesh_channel, config.default_network)
            except ValueError as exc:
                _LOG.warning("Correo UID %s descartado: %s", uid, exc)
                state["last_uid"] = uid
                _remember_message_id(state, message_id, config.recent_message_ids_max)
                if message_id:
                    recent_ids.add(message_id)
                save_state(config.state_path, state)
                continue

            if not route.text:
                _LOG.info("Correo UID %s descartado: prefijo de canal sin texto", uid)
                state["last_uid"] = uid
                _remember_message_id(state, message_id, config.recent_message_ids_max)
                if message_id:
                    recent_ids.add(message_id)
                save_state(config.state_path, state)
                continue

            mesh_text = _format_mesh_text(config.mesh_prefix, route.text)
            response = send_routed_text_to_broker(config, route, mesh_text, sender, message_id)
            if not bool(response.get("ok")):
                error = response.get("error") or "error no especificado"
                raise RuntimeError(f"broker rechazó UID {uid}: {error}")

            # Solo después de la aceptación del broker se modifica el correo y se
            # avanza el estado persistente.
            if config.mark_as_read:
                client.uid("STORE", str(uid), "+FLAGS.SILENT", "(\\Seen)")
            if config.delete_after_send:
                client.uid("STORE", str(uid), "+FLAGS.SILENT", "(\\Deleted)")

            state["last_uid"] = uid
            _remember_message_id(state, message_id, config.recent_message_ids_max)
            if message_id:
                recent_ids.add(message_id)
            save_state(config.state_path, state)
            sent_count += 1

            _LOG.info(
                "Correo UID %s encolado: sender=%s network=%s channel=%s dest=%s chars=%s",
                uid,
                sender,
                route.network,
                route.channel,
                (config.mesh_dest or "broadcast") if route.network == "meshtastic" else "meshcore-channel",
                len(mesh_text),
            )

        if config.delete_after_send and sent_count:
            client.expunge()

        return sent_count

    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass



# =============================================================================
# IMAP IDLE 24/7
# =============================================================================


def _imap_has_idle(client: imaplib.IMAP4) -> bool:
    """
    Comprueba si el servidor anuncia la capacidad IMAP IDLE.

    No se presupone soporte: algunos servidores IMAP básicos solo admiten
    consulta periódica. En ese caso el servicio utiliza fallback automático.
    """
    capabilities = getattr(client, "capabilities", ()) or ()
    return any(str(item, "ascii", errors="ignore").upper() == "IDLE" if isinstance(item, bytes)
               else str(item).upper() == "IDLE" for item in capabilities)


def _imap_status_has_new_uid(client: imaplib.IMAP4, config: Config, state: Dict[str, Any]) -> bool:
    """
    Detecta mensajes pendientes mediante UIDNEXT antes de entrar en IDLE.

    Esta comprobación cierra la ventana de carrera entre el ciclo que acaba de
    procesar el buzón y la apertura de la nueva sesión IDLE.
    """
    status, status_data = client.status(config.imap_folder, "(UIDVALIDITY UIDNEXT)")
    if status != "OK" or not status_data:
        raise RuntimeError("no se pudo consultar UIDVALIDITY/UIDNEXT antes de IDLE")
    uidvalidity, uidnext = _parse_mailbox_status(status_data)
    stored_uidvalidity = state.get("uidvalidity")
    if stored_uidvalidity is not None and int(stored_uidvalidity) != uidvalidity:
        return True
    return max(0, uidnext - 1) > int(state.get("last_uid", 0))


def _imap_idle_wait(client: imaplib.IMAP4, timeout_sec: int) -> str:
    """
    Mantiene una sesión IMAP IDLE hasta que llega actividad o vence el tiempo.

    Retorna:
        ``mail`` cuando el servidor notifica EXISTS/RECENT/EXPUNGE.
        ``timeout`` cuando debe renovarse preventivamente la sesión.
        ``stop`` cuando el servicio recibe SIGTERM o SIGINT.

    Implementación:
        Se usa el protocolo IDLE directamente porque versiones de Python previas
        a 3.14 no exponen IMAP4.idle(). Toda la operación ocurre en un único hilo,
        sin concurrencia sobre la conexión IMAP.
    """
    tag = client._new_tag()  # Compatibilidad con Python 3.10-3.13.
    client.send(tag + b" IDLE\r\n")
    continuation = client.readline()
    if not continuation.startswith(b"+"):
        raise RuntimeError(f"el servidor rechazó IMAP IDLE: {continuation!r}")

    deadline = time.monotonic() + timeout_sec
    result = "timeout"
    try:
        while not _STOP_EVENT.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            wait_slice = min(1.0, remaining)
            readable, _, _ = select.select([client.sock], [], [], wait_slice)
            if not readable:
                continue
            line = client.readline()
            if not line:
                raise OSError("el servidor cerró la conexión IMAP durante IDLE")
            upper = line.upper()
            if b" EXISTS" in upper or b" RECENT" in upper or b" EXPUNGE" in upper:
                result = "mail"
                break
        if _STOP_EVENT.is_set():
            result = "stop"
    finally:
        client.send(b"DONE\r\n")
        while True:
            line = client.readline()
            if not line:
                break
            if line.startswith(tag):
                if b" OK" not in line.upper():
                    raise RuntimeError(f"finalización IDLE rechazada: {line!r}")
                break
    return result


def wait_for_imap_event(config: Config, state: Dict[str, Any]) -> str:
    """
    Espera actividad del buzón mediante IMAP IDLE.

    Garantías 24/7:
        - Revisa UIDNEXT antes de bloquearse para no perder correos en carrera.
        - Renueva IDLE antes de 29 minutos mediante el límite configurado.
        - Cierra siempre la sesión con DONE y LOGOUT.
        - Si IDLE no está disponible, retorna ``fallback``.
    """
    client: Optional[imaplib.IMAP4] = None
    try:
        client = open_imap(config)
        status, _ = client.select(config.imap_folder, readonly=False)
        if status != "OK":
            raise RuntimeError(f"no se pudo seleccionar la carpeta {config.imap_folder!r} para IDLE")

        if _imap_status_has_new_uid(client, config, state):
            return "mail"

        if not _imap_has_idle(client):
            return "fallback"

        # Mantiene una única conexión IMAP durante toda la ventana de refresco.
        # Gmail debería despertar IDLE inmediatamente mediante EXISTS, pero en
        # algunas combinaciones de imaplib/OpenSSL la notificación puede quedar
        # retenida en el buffer interno. Para no esperar hasta 25 minutos, se
        # cierra IDLE cada pocos segundos, se consulta UIDNEXT sobre la misma
        # conexión y se vuelve a entrar en IDLE sin repetir login.
        refresh_deadline = time.monotonic() + config.imap_idle_refresh_sec
        _LOG.info(
            "IMAP IDLE activo: aviso inmediato + verificación UID cada %ss; renovación en %ss",
            config.imap_idle_verify_sec,
            config.imap_idle_refresh_sec,
        )

        while not _STOP_EVENT.is_set():
            remaining = refresh_deadline - time.monotonic()
            if remaining <= 0:
                return "timeout"

            event = _imap_idle_wait(
                client,
                min(config.imap_idle_verify_sec, max(1, int(remaining))),
            )
            if event in {"mail", "stop"}:
                return event

            # Watchdog anti-latencia: detecta cualquier UID nuevo aunque la
            # notificación EXISTS no haya despertado correctamente el socket.
            if _imap_status_has_new_uid(client, config, state):
                _LOG.info("Watchdog IMAP detectó correo pendiente mediante UIDNEXT")
                return "mail"

        return "stop"
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass

# =============================================================================
# Servicio 24/7
# =============================================================================


def _configure_logging(level_name: str) -> None:
    """Configura logging uniforme para consola y Docker logs."""
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [email-to-mesh] %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _request_stop(signum: int, _frame: Any) -> None:
    """Manejador SIGTERM/SIGINT que permite detener el contenedor limpiamente."""
    _LOG.info("Señal %s recibida; deteniendo servicio", signum)
    _STOP_EVENT.set()


def run_service(config: Config) -> int:
    """
    Ejecuta la vigilancia 24/7 mediante IMAP IDLE con fallback seguro.

    Secuencia:
        1. Procesa todos los UID pendientes.
        2. Entra en IDLE y espera una notificación inmediata del servidor.
        3. Renueva la sesión periódicamente para evitar cierres por timeout.
        4. Si IDLE no existe, consulta mediante polling controlado.
        5. Ante errores aplica reconexión exponencial limitada.
    """
    state = load_state(config.state_path)
    consecutive_failures = 0
    idle_fallback_logged = False

    _LOG.info(
        "%s v%s iniciado: folder=%s senders=%s default_meshtastic_channel=%s "
        "dest=%s bridge=%s imap_idle=%s refresh=%ss verify=%ss",
        APP_NAME,
        APP_VERSION,
        config.imap_folder,
        len(config.allowed_senders),
        config.mesh_channel,
        config.mesh_dest or "broadcast",
        config.mesh_allow_bridge,
        config.imap_idle_enabled,
        config.imap_idle_refresh_sec,
        config.imap_idle_verify_sec,
    )

    while not _STOP_EVENT.is_set():
        try:
            process_mailbox_once(config, state)
            consecutive_failures = 0

            if _STOP_EVENT.is_set():
                break

            if not config.imap_idle_enabled:
                _STOP_EVENT.wait(config.poll_interval_sec)
                continue

            event = wait_for_imap_event(config, state)
            if event == "mail":
                idle_fallback_logged = False
                continue
            if event == "stop":
                break
            if event == "timeout":
                _LOG.debug("Renovación preventiva de sesión IMAP IDLE")
                continue
            if event == "fallback":
                if not idle_fallback_logged:
                    _LOG.warning(
                        "El servidor no anuncia IMAP IDLE; fallback a polling cada %ss",
                        config.imap_idle_fallback_poll_sec,
                    )
                    idle_fallback_logged = True
                _STOP_EVENT.wait(config.imap_idle_fallback_poll_sec)

        except (imaplib.IMAP4.error, OSError, RuntimeError, socket.error) as exc:
            consecutive_failures += 1
            sleep_seconds = min(300, 5 * (2 ** min(consecutive_failures - 1, 6)))
            _LOG.error("Conexión/ciclo fallido (%s); reconexión en %ss", exc, sleep_seconds)
            _STOP_EVENT.wait(sleep_seconds)
        except Exception:
            consecutive_failures += 1
            sleep_seconds = min(300, max(30, 5 * consecutive_failures))
            _LOG.exception("Error inesperado; reconexión en %ss", sleep_seconds)
            _STOP_EVENT.wait(sleep_seconds)

    _LOG.info("Servicio detenido")
    return 0

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=APP_NAME, description="Pasarela correo ↔ malla y libreta de contactos")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("run", help="ejecuta el servicio IMAP→malla (comportamiento por defecto)")
    p_add = sub.add_parser("contact-add", aliases=["add"], help="añade o actualiza un contacto")
    p_add.add_argument("name"); p_add.add_argument("email")
    p_edit = sub.add_parser("contact-edit", aliases=["edit"], help="edita un contacto existente")
    p_edit.add_argument("name_or_number"); p_edit.add_argument("email")
    p_del = sub.add_parser("contact-del", aliases=["del", "rm"], help="elimina un contacto")
    p_del.add_argument("name_or_number")
    sub.add_parser("contacts", aliases=["list", "ls"], help="lista contactos")
    p_send = sub.add_parser("send", help="envía un correo a un contacto desde CLI")
    p_send.add_argument("name_or_number"); p_send.add_argument("message")
    return parser


def _run_contacts_cli(args: argparse.Namespace) -> int:
    if args.cmd in {"contact-add", "add"}:
        c = upsert_contact(args.name, args.email); print(f"OK añadido/actualizado: {c['name']} <{c['email']}> [{c['key']}]"); return 0
    if args.cmd in {"contact-edit", "edit"}:
        contacts = load_contacts(); key = resolve_contact_key(args.name_or_number, contacts)
        c = upsert_contact(contacts[key].get("name") or key, args.email); print(f"OK editado: {c['name']} <{c['email']}> [{c['key']}]"); return 0
    if args.cmd in {"contact-del", "del", "rm"}:
        c = delete_contact(args.name_or_number); print(f"OK eliminado: {c['name']} <{c['email']}> [{c['key']}]"); return 0
    if args.cmd in {"contacts", "list", "ls"}:
        print(format_contacts(load_contacts())); return 0
    if args.cmd == "send":
        contacts = load_contacts(); key = resolve_contact_key(args.name_or_number, contacts)
        send_email_to_contact(contacts[key], args.message, source="cli"); print(f"OK enviado a {contacts[key].get('name') or key}"); return 0
    return 2


def main() -> int:
    args = _build_arg_parser().parse_args()
    if args.cmd and args.cmd != "run":
        return _run_contacts_cli(args)

    config = Config.from_env()
    _configure_logging(config.log_level)

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    if not config.enabled:
        _LOG.info("EMAIL_TO_MESH_ENABLED=0; servicio cargado pero inactivo")
        while not _STOP_EVENT.wait(3600):
            pass
        return 0

    errors = config.validate()
    if errors:
        for problem in errors:
            _LOG.error("Configuración inválida: %s", problem)
        return 2

    return run_service(config)


if __name__ == "__main__":
    raise SystemExit(main())
