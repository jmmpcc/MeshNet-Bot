# broker_tasks_v6.1.1 py
# ─────────────────────────────────────────────────────────────────────────────
# Gestor común de TAREAS programadas de envío para el ecosistema Meshtastic.
# - Reutilizable desde el broker y el bot (sin duplicar código).
# - Persistencia en JSONL (sobrevive reinicios).
# - Scheduler en hilo (thread) con backoff y reconexión opcional.
# - API pública sencilla:
#     configure_sender(func)
#     configure_reconnect(func)
#     init(...), start(), stop()
#     schedule_message(...), cancel(task_id), list_tasks(status)
#
# Importante:
# - NO modifica tus funciones existentes. Solo ofrece “hooks” para llamarlas.
# - Si no configuras un sender propio, intentará autodetectar funciones típicas
#   en `meshtastic_api_adapter` (si existe).
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations
import os, json, time, threading, uuid, logging, re  # ← MOD: añadido re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Callable, Optional, Dict, Any, List
import socket

# Zona horaria por defecto (tu entorno)
DEFAULT_TZ = "Europe/Madrid"
ISO_FMT = "%Y-%m-%d %H:%M:%S"

# ── Zona horaria: zoneinfo (3.9+) o pytz como respaldo ───────────────────────
try:
    import zoneinfo
    ZoneInfo = zoneinfo.ZoneInfo
except Exception:
    ZoneInfo = None

def _tz_get(tz_name: str):
    if ZoneInfo:
        try:
            return ZoneInfo(tz_name)
        except Exception:
            pass
    try:
        import pytz  # type: ignore
        return pytz.timezone(tz_name)
    except Exception:
        return timezone.utc

def _tz_localize(tz, dt_naive: datetime) -> datetime:
    # Compat zoneinfo/pytz
    if hasattr(tz, "localize"):
        return tz.localize(dt_naive)  # pytz
    return dt_naive.replace(tzinfo=tz)  # zoneinfo

# === NUEVO: cálculo DST UE para fallback de Europe/Madrid ===
def _eu_last_sunday(year: int, month: int) -> datetime:
    # Último domingo del mes a las 00:00
    if month == 12:
        next_month = datetime(year + 1, 1, 1)
    else:
        next_month = datetime(year, month + 1, 1)
    last_day = next_month - timedelta(days=1)
    delta = (last_day.weekday() + 1) % 7  # 0=Mon ... 6=Sun → días desde domingo
    return datetime(year, month, last_day.day) - timedelta(days=delta)

# === MODIFICADO: cálculo DST local sin tzinfo para Europe/Madrid ===
def _eu_madrid_offset_hours(dt_naive: datetime) -> int:
    """
    Devuelve el desfase horario de Europe/Madrid en horas (1=CET, 2=CEST) usando reglas UE.
    Compara en hora local (naive) para evitar choques aware/naive en entornos sin tzdata.

    Reglas:
      - Inicio DST: último domingo de marzo a las 02:00 (hora local) → pasa a CEST (UTC+2).
      - Fin   DST: último domingo de octubre a las 03:00 (hora local) → vuelve a CET (UTC+1).
    """
    y = dt_naive.year
    # Límites en hora local (naive)
    start_local = _eu_last_sunday(y, 3).replace(hour=2, minute=0, second=0, microsecond=0)
    end_local   = _eu_last_sunday(y, 10).replace(hour=3, minute=0, second=0, microsecond=0)

    if start_local <= dt_naive < end_local:
        return 2  # CEST (UTC+2)
    return 1      # CET  (UTC+1)

def _tz_get_for_dt(tz_name: str, dt_naive: datetime):
    # 1) zoneinfo si está disponible
    if 'ZoneInfo' in globals() and ZoneInfo:
        try:
            return ZoneInfo(tz_name)
        except Exception:
            pass
    # 2) pytz si existe
    try:
        import pytz  # type: ignore
        return pytz.timezone(tz_name)
    except Exception:
        pass
    # 3) Fallback manual para tz europeas comunes (incluye Europe/Madrid)
    if tz_name in ("Europe/Madrid", "CET", "CEST", "Europe/Paris", "Europe/Berlin"):
        off = _eu_madrid_offset_hours(dt_naive)
        return timezone(timedelta(hours=off))
    # 4) Último recurso: UTC
    return timezone.utc


# ─────────────────────────────────────────────────────────────────────────────
# NUEVO: utilidades para normalizar y trocear mensajes Meshtastic
# ─────────────────────────────────────────────────────────────────────────────
MAX_PAYLOAD_BYTES = 180  # margen seguro típico para Meshtastic (UTF-8 por paquete)

def _normalize_text_for_mesh(s: str) -> str:
    """
    Normaliza comillas tipográficas, guiones largos y espacios no separables.
    Reduce espacios múltiples. Mantiene acentos (UTF-8 OK).
    """
    rep = {
        '“': '"', '”': '"',
        '’': "'", '‘': "'",
        '—': '-', '–': '-',
        '…': '...', '\u00A0': ' ',
    }
    s = s.translate(str.maketrans(rep))
    s = re.sub(r'\s+', ' ', s).strip()
    return s

def _utf8_len(s: str) -> int:
    return len(s.encode('utf-8'))

def split_text_for_meshtastic(text: str, max_bytes: int = MAX_PAYLOAD_BYTES) -> List[str]:
    """
    Divide 'text' en trozos que no superen max_bytes en UTF-8.
    Intenta no romper tokens (URLs/palabras). Si un token supera el límite,
    hace corte duro respetando límites de bytes de un carácter UTF-8.
    """
    s = _normalize_text_for_mesh(text)
    tokens = re.split(r'(\s+)', s)  # conserva separadores (espacios)
    parts: List[str] = []
    cur = ""

    def flush():
        nonlocal cur
        if cur.strip():
            parts.append(cur.strip())
        cur = ""

    for t in tokens:
        if _utf8_len(cur + t) <= max_bytes:
            cur += t
        else:
            if cur:
                flush()
                if _utf8_len(t) <= max_bytes:
                    cur = t
                else:
                    # Corte duro de token larguísimo (p.ej. URL muy larga)
                    b = t.encode('utf-8')
                    start = 0
                    while start < len(b):
                        chunk = b[start:start+max_bytes]
                        # Evitar cortar dentro de multi-byte
                        while chunk and (chunk[-1] & 0xC0) == 0x80:
                            chunk = chunk[:-1]
                        parts.append(chunk.decode('utf-8', 'ignore').strip())
                        start += len(chunk)
                    cur = ""
            else:
                # cur vacío pero token > max_bytes: corte duro directo
                b = t.encode('utf-8')
                start = 0
                while start < len(b):
                    chunk = b[start:start+max_bytes]
                    while chunk and (chunk[-1] & 0xC0) == 0x80:
                        chunk = chunk[:-1]
                    parts.append(chunk.decode('utf-8', 'ignore').strip())
                    start += len(chunk)
                cur = ""
    flush()
    return parts


# ── Datos de tarea ───────────────────────────────────────────────────────────
@dataclass
class ScheduledTask:
    id: str
    created_utc: str
    when_utc: str
    tz_name: str
    channel: int
    message: str
    destination: str = "broadcast"  # "!id", alias, o "broadcast"
    require_ack: bool = False
    status: str = "pending"         # "pending" | "done" | "failed" | "canceled"
    attempts: int = 0
    max_attempts: int = 3
    last_error: Optional[str] = None
    next_try_utc: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None
    last_run_ts: Optional[float] = None  # NUEVO

    @staticmethod
    def now_utc_str() -> str:
        return datetime.now(timezone.utc).strftime(ISO_FMT)

# ── Gestor (singleton a nivel de módulo) ─────────────────────────────────────
class _TaskManager:
    def __init__(self):
        self._logger = logging.getLogger("broker.tasks")
        self._tasks_file = "scheduled_tasks.jsonl"
        self._tz_name = DEFAULT_TZ
        self._poll_interval_sec = 2.0
        self._retry_backoff_sec = [2, 5, 10, 20]
        self._stop = threading.Event()
        self._th: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self._send_callable: Optional[Callable[[int, str, str, bool], Dict[str, Any]]] = None
        self._reconnect_callable: Optional[Callable[[], bool]] = None
        #self._ensure_file()

    # ── Configuración pública ────────────────────────────────────────────────
    def configure_logger(self, logger: logging.Logger):
        self._logger = logger or self._logger

    def configure_sender(self, func: Callable[[int, str, str, bool], Dict[str, Any]]):
        """
        Establece el callback de envío:
        func(channel:int, message:str, destination:str, require_ack:bool) -> {ok, packet_id?, error?}
        """
        self._send_callable = func

    def configure_reconnect(self, func: Optional[Callable[[], bool]]):
        """
        Establece el callback de reconexión (opcional).
        Devuelve True si la reconexión parece correcta.
        """
        self._reconnect_callable = func

    def configure_backoff(self, backoff_list: List[int]):
        if backoff_list and isinstance(backoff_list, list):
            self._retry_backoff_sec = list(backoff_list)

    def init(
        self,
        *,
        data_dir: Optional[str] = None,
        tz_name: str = DEFAULT_TZ,
        tasks_file: Optional[str] = None,
        poll_interval_sec: float = 2.0,
    ):
        """
        Inicializa rutas y parámetros. Debe llamarse antes de start().
        """
        self._tz_name = tz_name or DEFAULT_TZ
        self._poll_interval_sec = float(poll_interval_sec or 2.0)

        if data_dir:
            os.makedirs(data_dir, exist_ok=True)
            self._tasks_file = os.path.join(data_dir, "scheduled_tasks.jsonl")
        elif tasks_file:
            self._tasks_file = tasks_file

        self._ensure_file()

    def start(self):
        if self._th and self._th.is_alive():
            return
        self._stop.clear()
        self._th = threading.Thread(target=self._loop, name="broker-task-scheduler", daemon=True)
        self._th.start()
        self._logger.info("[Tasks] Scheduler iniciado.")

    def stop(self, timeout: Optional[float] = 3.0):
        self._stop.set()
        if self._th:
            self._th.join(timeout)
            self._th = None
            self._logger.info("[Tasks] Scheduler detenido.")

    # ── API de tareas ────────────────────────────────────────────────────────
    def schedule_message(
        self,
        *,
        when_local: str,         # "YYYY-mm-dd HH:MM[:SS]" en self._tz_name
        channel: int,
        message: str,
        destination: str = "broadcast",
        require_ack: bool = False,
        meta: Optional[Dict[str, Any]] = None,
        max_attempts: int = 3,
    ) -> Dict[str, Any]:
        when_local_dt = self._parse_local_dt(when_local)
        when_utc = when_local_dt.astimezone(timezone.utc)

        # ── MOD: normalizar texto antes de persistir (sin romper API)
        orig_msg = str(message)
        norm_msg = _normalize_text_for_mesh(orig_msg)
        _meta = dict(meta or {})
        if norm_msg != orig_msg:
            _meta.setdefault("orig_message", orig_msg)
            _meta["normalized"] = True

        t = ScheduledTask(
            id=str(uuid.uuid4()),
            created_utc=ScheduledTask.now_utc_str(),
            when_utc=when_utc.strftime(ISO_FMT),
            tz_name=self._tz_name,
            channel=int(channel),
            message=norm_msg,  # ← MOD: guardamos normalizado
            destination=str(destination or "broadcast"),
            require_ack=bool(require_ack),
            status="pending",
            attempts=0,
            max_attempts=int(max_attempts) if max_attempts and max_attempts > 0 else 3,
            last_error=None,
            next_try_utc=None,
            meta=_meta,
        )
        self._append(t)
        self._logger.info(f"[Tasks] Tarea creada {t.id} → {when_local} ({self._tz_name}) ch={t.channel} dest={t.destination}")
        return {"ok": True, "task": asdict(t)}

    def cancel(self, task_id: str) -> Dict[str, Any]:
        with self._lock:
            rows = self._read_all()
            changed = False
            for r in rows:
                if r["id"] == task_id and r["status"] in ("pending", "failed"):
                    r["status"] = "canceled"
                    changed = True
            if changed:
                self._rewrite(rows)
        return {"ok": changed, "task_id": task_id}

    def list_tasks(self, status: Optional[str] = None) -> Dict[str, Any]:
        rows = self._read_all()
        if status:
            rows = [r for r in rows if r["status"] == status]
        return {"ok": True, "tasks": rows}

    # ── Bucle del scheduler ─────────────────────────────────────────────────
    def _loop(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:
                self._logger.exception(f"[Tasks] Excepción en ciclo: {e}")
            finally:
                self._stop.wait(self._poll_interval_sec)

    def _tick(self):
        now_utc = datetime.now(timezone.utc)
        with self._lock:
            tasks = [self._to_task(d) for d in self._read_all()]

        due: List[ScheduledTask] = []
        for t in tasks:
            if t.status not in ("pending", "failed"):
                continue
            next_time = self._parse_utc(t.next_try_utc) if t.next_try_utc else self._parse_utc(t.when_utc)
            if next_time and next_time <= now_utc:
                due.append(t)

        for t in due:
            self._process_task(t)

    def _process_task(self, t: ScheduledTask):
        self._logger.info(f"[Tasks] Ejecutando {t.id} ch={t.channel} dest={t.destination} intento={t.attempts+1}")
        ok, packet_id, error = False, None, None
      
        transport = (t.meta or {}).get("transport", "mesh").lower()  # 'mesh' | 'aprs' | 'both'
        aprs_dest = (t.meta or {}).get("aprs_dest") or t.destination

        # 1) Resolver/usar sender
        send = self._send_callable or self._auto_detect_sender()
        if not send:
            error = "No hay sender configurado (configure_sender) y no se pudo autodetectar."
            self._fail_or_retry(t, error)
            return

        # --- SOLO APRS: saltar MESH y mandar por pasarela APRS
        if transport == "aprs":
            try:
                self._aprs_forward_via_udp(dest=aprs_dest, text=t.message)
                # Repetición diaria (si aplica)
                meta = t.meta or {}
                if meta.get("repeat") == "daily":
                    self._reschedule_daily(t)
                else:
                    self._mark_done(t.id, None)
                self._logger.info(f"[Tasks] DONE (APRS-only) {t.id}")
                return
            except Exception as e:
                error = f"APRS send failed: {type(e).__name__}: {e}"
                self._fail_or_retry(t, error)
                return


        # ── MOD: troceo previo del mensaje a payloads seguros
        parts = split_text_for_meshtastic(t.message, MAX_PAYLOAD_BYTES)
        if len(parts) > 1:
            self._logger.info(f"[Tasks] Mensaje troceado en {len(parts)} partes por longitud")

        # 2) Envío por partes (si falla una parte → backoff de la tarea completa)
        try:
            sent_any = False
            for i, part in enumerate(parts, 1):
                label = f" ({i}/{len(parts)})" if len(parts) > 1 else ""
                try:
                    res = send(t.channel, part + label, t.destination, t.require_ack)
                    if not bool(res.get("ok")):
                        error = res.get("error") or "Unknown send error"
                        # Intento de reconexión inmediato para esta parte
                        if self._looks_like_conn_timeout(error):
                            reconn = self._reconnect_callable or self._auto_detect_reconnect()
                            if reconn and reconn():
                                self._logger.warning("[Tasks] Reconexión OK durante envío por partes, reintentando parte…")
                                res2 = send(t.channel, part + label, t.destination, t.require_ack)
                                if not bool(res2.get("ok")):
                                    error = res2.get("error") or "Unknown error after reconnect"
                                    raise RuntimeError(error)
                            else:
                                raise RuntimeError(error)
                        else:
                            raise RuntimeError(error)

                    # Parte OK
                    sent_any = True
                    packet_id = res.get("packet_id") or packet_id
                    # Pausa corta entre partes para no estresar el socket
                    time.sleep(0.8)

                except Exception as e_part:
                    ok = False
                    error = f"{type(e_part).__name__}: {e_part}"
                    raise  # salimos del bucle y tratamos como fallo de tarea

            # Si todas las partes fueron bien
            ok = True

        except Exception as e:
            ok = False
            if not error:
                error = f"{type(e).__name__}: {e}"

        if ok:
            # [NUEVO] Si la tarea es de transporte APRS, reenvía también por UDP a la pasarela
            try:
                if (t.meta or {}).get("transport", "mesh").lower() == "both":
                    self._aprs_forward_via_udp(dest=t.destination, text=t.message)
            except Exception as _e_aprs:
                self._logger.warning(f"[Tasks→APRS] Error en post-send APRS: {type(_e_aprs).__name__}: {_e_aprs}")
       
            # === Repetición diaria, si aplica
            meta = t.meta or {}
            if meta.get("repeat") == "daily":
                self._reschedule_daily(t)
                return

            self._mark_done(t.id, packet_id)
            self._logger.info(f"[Tasks] DONE {t.id} • packet_id={packet_id}")
            return


        # 3) Si hay error de conexión/timeout (no resuelto arriba), reintentar a nivel de tarea
        if self._looks_like_conn_timeout(error):
            reconn = self._reconnect_callable or self._auto_detect_reconnect()
            if reconn:
                try:
                    if reconn():
                        self._logger.warning("[Tasks] Reconexión OK, reintentando envío completo…")
                        # Reintento del mensaje completo (volverá a partir en partes)
                        try:
                            res2_ok = True
                            packet_id = None
                            for i, part in enumerate(parts, 1):
                                label = f" ({i}/{len(parts)})" if len(parts) > 1 else ""
                                r = send(t.channel, part + label, t.destination, t.require_ack)
                                if not bool(r.get("ok")):
                                    res2_ok = False
                                    error = r.get("error") or "Unknown error after reconnect"
                                    break
                                packet_id = r.get("packet_id") or packet_id
                                time.sleep(0.8)
                            if res2_ok:
                                self._mark_done(t.id, packet_id)
                                self._logger.info(f"[Tasks] DONE tras reconexión {t.id} • packet_id={packet_id}")
                                return
                        except Exception as e2:
                            error = f"{type(e2).__name__}: {e2}"
                    else:
                        self._logger.warning("[Tasks] Reconexión fallida (callback devolvió False)")
                except Exception as e:
                    self._logger.exception(f"[Tasks] Excepción reconectando: {e}")

        # 4) Backoff / fail
        self._fail_or_retry(t, error)

    def _aprs_forward_via_udp(self, dest: str, text: str) -> None:
        """
        Envía un control UDP a la pasarela APRS, que hará el troceo y TX.
        Compatible con meshtastic_to_aprs_v5.8.py:
        CONTROL_UDP_HOST = APRS_CTRL_HOST (default 127.0.0.1)
        CONTROL_UDP_PORT = APRS_CTRL_PORT (default 9464)
        payload: {"mode":"aprs","dest":"EA2XXX-xx|broadcast","text":"..."}
        """
        host = os.getenv("APRS_CTRL_HOST", "127.0.0.1").strip()
        port = int(os.getenv("APRS_CTRL_PORT", "9464"))
        try:
            msg = {
                "mode": "aprs",
                "dest": "broadcast" if (dest or "").lower() in ("", "broadcast", "all") else str(dest).upper(),
                "text": str(text or ""),
            }
            data = (json.dumps(msg, ensure_ascii=False)).encode("utf-8")
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.sendto(data, (host, port))
            try: sock.close()
            except Exception: pass
            self._logger.info(f"[Tasks→APRS] UDP enviado dest={msg['dest']} len={len(msg['text'])}")
        except Exception as e:
            self._logger.warning(f"[Tasks→APRS] Fallo al enviar UDP: {type(e).__name__}: {e}")



    # ── Persistencia ─────────────────────────────────────────────────────────
    def _ensure_file(self):
        try:
            if not os.path.exists(self._tasks_file):
                os.makedirs(os.path.dirname(self._tasks_file), exist_ok=True)
                with open(self._tasks_file, "w", encoding="utf-8") as f:
                    pass
        except FileNotFoundError:
            # Si dirname está vacío, se permite crear en cwd
            with open(self._tasks_file, "w", encoding="utf-8") as f:
                pass

    def _append(self, task: ScheduledTask):
        with self._lock, open(self._tasks_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(task), ensure_ascii=False) + "\n")

    def _read_all(self) -> List[Dict[str, Any]]:
        if not os.path.exists(self._tasks_file):
            return []
        rows = []
        with open(self._tasks_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
        return rows

    def _rewrite(self, rows: List[Dict[str, Any]]) -> None:
        """
        Reescribe el JSONL de tareas de forma atómica y segura en concurrencia:
        - Directorio garantizado
        - .tmp único por llamada (evita colisiones entre hilos)
        - flush + fsync antes del replace
        """
        import tempfile, uuid

        base_dir = os.path.dirname(self._tasks_file) or "."
        os.makedirs(base_dir, exist_ok=True)

        # .tmp único en el MISMO directorio (para que os.replace sea atómico en el mismo FS)
        tmp_name = f"{os.path.basename(self._tasks_file)}.{uuid.uuid4().hex}.tmp"
        tmp_path = os.path.join(base_dir, tmp_name)

        try:
            with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
                # Escribe todo el contenido de golpe; también valdría iterativo como tenías
                for r in (rows or []):
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())  # garantiza persistencia antes del replace

            # Reemplazo atómico
            os.replace(tmp_path, self._tasks_file)

        except Exception as e:
            # En caso de fallo, intenta limpiar el .tmp (si existe)
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except Exception:
                pass
            raise


    def _mark_done(self, task_id: str, packet_id: Optional[Any]):
        with self._lock:
            rows = self._read_all()
            for r in rows:
                if r["id"] == task_id:
                    r["status"] = "done"
                    r["meta"] = r.get("meta") or {}
                    r["meta"]["packet_id"] = packet_id
                    r["last_error"] = None
                    r["next_try_utc"] = None
                    r["last_run_ts"] = time.time()
            self._rewrite(rows)

    def _bump_attempt(self, task_id: str, *, attempts: int, error: str, next_try_utc: str):
        with self._lock:
            rows = self._read_all()
            for r in rows:
                if r["id"] == task_id:
                    r["attempts"] = int(attempts)
                    r["last_error"] = error
                    r["next_try_utc"] = next_try_utc
                    if r["status"] != "pending":
                        r["status"] = "pending"
            self._rewrite(rows)

    def _mark_failed(self, task_id: str, error: str, next_try_utc: Optional[str]):
        with self._lock:
            rows = self._read_all()
            for r in rows:
                if r["id"] == task_id:
                    r["status"] = "failed"
                    r["last_error"] = error
                    r["next_try_utc"] = next_try_utc
            self._rewrite(rows)

   
    # ── Utilidades tiempo/zonas ──────────────────────────────────────────────
    def _parse_local_dt(self, s: str) -> datetime:
        s2 = s.strip()
        if len(s2) == 16:
            s2 += ":00"
        dt_naive = datetime.strptime(s2, ISO_FMT)
        tz = _tz_get_for_dt(self._tz_name, dt_naive)

        return _tz_localize(tz, dt_naive)

    @staticmethod
    def _parse_utc(s: Optional[str]) -> Optional[datetime]:
        if not s:
            return None
        return datetime.strptime(s, ISO_FMT).replace(tzinfo=timezone.utc)

    def _reschedule_daily(self, t: ScheduledTask) -> None:
        """
        Reprograma la tarea t al próximo día a la misma HH:MM local indicada.
        Mantiene repeat='daily' y meta['daily_time'].
        """
        try:
            meta = t.meta or {}
            hhmm = (meta.get("daily_time") or "").strip()
            tz = _tz_get(self._tz_name)

            # Si no vino fija, usa la hora/minuto de when_utc actual (pasándola a local)
            if not hhmm or ":" not in hhmm:
                cur_local = self._parse_utc(t.when_utc).astimezone(tz)
                hhmm = f"{cur_local.hour:02d}:{cur_local.minute:02d}"

            h, m = [int(x) for x in hhmm.split(":", 1)]
            now_local = datetime.now(tz)
            next_local = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
            if next_local <= now_local:
                next_local = next_local + timedelta(days=1)

            next_utc = next_local.astimezone(timezone.utc).strftime(ISO_FMT)

            with self._lock:
                rows = self._read_all()
                for r in rows:
                    if r["id"] == t.id:
                        r["last_run_ts"] = time.time()  # ← NUEVO
                        r["when_utc"] = next_utc
                        r["status"] = "pending"
                        r["attempts"] = 0
                        r["last_error"] = None
                        r["next_try_utc"] = None
                        r.setdefault("meta", {})
                        r["meta"]["repeat"] = "daily"
                        r["meta"]["daily_time"] = hhmm
                        r["meta"]["repeat_count"] = int(r["meta"].get("repeat_count") or 0) + 1
                self._rewrite(rows)

            self._logger.info(f"[Tasks] Reprogramada (daily) {t.id} → {next_utc} ({self._tz_name})")
        except Exception as e:
            self._logger.exception(f"[Tasks] _reschedule_daily: {e}")






    # ── Heurística de errores de conexión ────────────────────────────────────
    @staticmethod
    def _looks_like_conn_timeout(error: Optional[str]) -> bool:
        if not error:
            return False
        e = str(error).lower()
        needles = [
            "timed out waiting for connection completion",
            "connection timed out",
            "timeout",
            "unexpected oserror",
            "broken pipe",
            "connection reset",
            "10060",
            "10054",
        ]
        return any(n in e for n in needles)

    # ── Autodetección “suave” de sender/reconnect para no romper nada ────────
    def _auto_detect_sender(self) -> Optional[Callable[[int, str, str, bool], Dict[str, Any]]]:
        """
        Intenta encontrar funciones típicas de envío en `meshtastic_api_adapter`.
        Si no existen, devuelve None (debes configurar un sender con configure_sender).
        """
        try:
            import importlib
            mod = importlib.import_module("meshtastic_api_adapter")

            # Candidatas (se usan si existen). Devuelven dict {ok, packet_id?, error?}
            if hasattr(mod, "send_text_via_api"):
                def _f(channel, message, destination, require_ack):
                    return mod.send_text_via_api(channel=channel, text=message, dest=destination, require_ack=require_ack)
                return _f

            if hasattr(mod, "send_text_message"):
                def _f(channel, message, destination, require_ack):
                    return mod.send_text_message(channel=channel, message=message, destination=destination, require_ack=require_ack)
                return _f

            if hasattr(mod, "send_with_ack_retry"):
                def _f(channel, message, destination, require_ack):
                    # “require_ack” podría ignorarse si esta función ya maneja ACKs internamente
                    return mod.send_with_ack_retry(channel=channel, text=message, dest=destination)
                return _f

            # Como último recurso: si expone una función genérica “broker_send_text”
            if hasattr(mod, "broker_send_text"):
                def _f(channel, message, destination, require_ack):
                    return mod.broker_send_text(channel=channel, text=message, dest=destination, require_ack=require_ack)
                return _f

        except Exception:
            pass
        return None

    def _auto_detect_reconnect(self) -> Optional[Callable[[], bool]]:
        """
        Intenta localizar puntos de reconexión típicos (tcp pool, mesh_reconnect).
        """
        def _wrap(fn):
            def _inner():
                try:
                    fn()
                    return True
                except Exception:
                    return False
            return _inner

        # 1) tcpinterface_persistent.get_pool().close_and_reopen() / .reconnect()
        try:
            import importlib
            tmod = importlib.import_module("tcpinterface_persistent")
            if hasattr(tmod, "get_pool"):
                pool = tmod.get_pool()
                if hasattr(pool, "close_and_reopen"):
                    return _wrap(pool.close_and_reopen)
                if hasattr(pool, "reconnect"):
                    return _wrap(pool.reconnect)
        except Exception:
            pass

        # 2) meshtastic_api_adapter.mesh_reconnect()
        try:
            import importlib
            mod = importlib.import_module("meshtastic_api_adapter")
            if hasattr(mod, "mesh_reconnect"):
                return _wrap(mod.mesh_reconnect)
        except Exception:
            pass

        return None

    # ── Gestión de fallos/reintentos ─────────────────────────────────────────
    def _fail_or_retry(self, t: ScheduledTask, error: Optional[str]):
        t.attempts += 1
        if t.attempts < t.max_attempts:
            backoff_ix = min(t.attempts - 1, len(self._retry_backoff_sec) - 1)
            wait_s = self._retry_backoff_sec[backoff_ix]
            next_try = datetime.now(timezone.utc) + timedelta(seconds=wait_s)
            self._bump_attempt(t.id, attempts=t.attempts, error=str(error or ""), next_try_utc=next_try.strftime(ISO_FMT))
            self._logger.warning(f"[Tasks] Reintento {t.id} en {wait_s}s • error={error!r}")
        else:
            # Dejarla en failed pero con un “next_try_utc” a +2 min para relanzarla manualmente
            next_try = datetime.now(timezone.utc) + timedelta(minutes=2)
            self._mark_failed(t.id, str(error or ""), next_try.strftime(ISO_FMT))
            self._logger.error(f"[Tasks] FAILED {t.id} • error={error!r}")

    # ── Conversión ───────────────────────────────────────────────────────────
    @staticmethod
    def _to_task(d: Dict[str, Any]) -> ScheduledTask:
        return ScheduledTask(**d)

# ── Instancia singleton y funciones de módulo (API estable) ──────────────────
_mgr = _TaskManager()

def configure_logger(logger: logging.Logger): _mgr.configure_logger(logger)
def configure_sender(func: Callable[[int, str, str, bool], Dict[str, Any]]): _mgr.configure_sender(func)
def configure_reconnect(func: Optional[Callable[[], bool]]): _mgr.configure_reconnect(func)
def configure_backoff(backoff_list: List[int]): _mgr.configure_backoff(backoff_list)

def init(*, data_dir: Optional[str] = None, tz_name: str = DEFAULT_TZ, tasks_file: Optional[str] = None, poll_interval_sec: float = 2.0):
    _mgr.init(data_dir=data_dir, tz_name=tz_name, tasks_file=tasks_file, poll_interval_sec=poll_interval_sec)

def start(): _mgr.start()
def stop(timeout: Optional[float] = 3.0): _mgr.stop(timeout)

def schedule_message(*, when_local: str, channel: int, message: str, destination: str = "broadcast", require_ack: bool = False, meta: Optional[Dict[str, Any]] = None, max_attempts: int = 3) -> Dict[str, Any]:
    return _mgr.schedule_message(when_local=when_local, channel=channel, message=message, destination=destination, require_ack=require_ack, meta=meta, max_attempts=max_attempts)

def cancel(task_id: str) -> Dict[str, Any]: return _mgr.cancel(task_id)
def list_tasks(status: Optional[str] = None) -> Dict[str, Any]: return _mgr.list_tasks(status)
