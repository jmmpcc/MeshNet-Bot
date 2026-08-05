from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterator


@dataclass(slots=True)
class RfLease:
    """Información de una reserva exclusiva del transmisor compartido."""

    owner: str
    priority: int
    acquired_at: float
    lock_path: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class RfManager:
    """Árbitro base para un único transmisor RF.

    En esta fase se entrega la exclusión mutua y el estado observable, pero no
    se conecta todavía al gateway APRS ni a un PTT de voz. Por tanto, instalar
    este módulo no cambia el comportamiento de transmisión actual.
    """

    def __init__(self, lock_path: str | None = None) -> None:
        default_path = os.path.join(
            os.getenv("BOT_DATA_DIR", "/app/bot_data"),
            "rf_tx.lock",
        )
        self.lock_path = Path(lock_path or os.getenv("RF_MANAGER_LOCK_PATH", default_path))
        self.state_path = Path(os.getenv("RF_MANAGER_STATE_PATH", str(self.lock_path) + ".json"))

    def _write_state(self, payload: dict[str, object]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, self.state_path)

    @contextmanager
    def acquire(
        self,
        *,
        owner: str,
        priority: int,
        timeout_seconds: float = 0.0,
    ) -> Iterator[RfLease]:
        """Reserva el transmisor mediante un bloqueo de fichero no reentrante.

        owner identifica al solicitante (`aprs`, `voice`, etc.). priority queda
        registrado para la futura cola priorizada. En esta base no interrumpe
        una transmisión ya iniciada. Lanza TimeoutError si no obtiene el lock.
        """
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+")
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        acquired = False
        try:
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"rf_busy owner={owner}")
                    time.sleep(0.05)
            lease = RfLease(
                owner=str(owner),
                priority=int(priority),
                acquired_at=time.time(),
                lock_path=str(self.lock_path),
            )
            self._write_state({"busy": True, **lease.to_dict()})
            yield lease
        finally:
            if acquired:
                self._write_state({
                    "busy": False,
                    "last_owner": str(owner),
                    "released_at": time.time(),
                    "lock_path": str(self.lock_path),
                })
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
