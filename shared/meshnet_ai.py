"""Infraestructura común y opcional de MeshNet Intelligence.

Este módulo NO modifica por sí mismo ningún flujo existente de MeshNet. Su misión
es centralizar la configuración de IA, seleccionar el proveedor, aplicar timeout,
controlar fallos mediante circuit breaker y ofrecer una única función de texto que
siempre puede degradar de forma segura al comportamiento clásico del llamador.

Uso básico::

    from shared.meshnet_ai import MeshNetAI

    ai = MeshNetAI.from_env()
    result = ai.generate_text("Resume este evento")
    if result.ok:
        texto = result.text
    else:
        texto = texto_clasico

Regla de compatibilidad:
- MESHNET_AI_ENABLED ausente o falso => no se realiza ninguna llamada externa.
- Un proveedor mal configurado, un timeout o cualquier error => resultado seguro
  con ``ok=False`` para que el llamador conserve el comportamiento actual.
- Nunca se incluyen claves API en ``public_status()`` ni en las excepciones creadas
  por este módulo.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional


_TRUE_VALUES = {"1", "true", "yes", "on", "si", "sí"}
_FALSE_VALUES = {"", "0", "false", "no", "off"}
_SUPPORTED_PROVIDERS = {"openai", "ollama", "compatible"}


def _env_bool(name: str, default: bool = False, env: Optional[Mapping[str, str]] = None) -> bool:
    """Lee un booleano de entorno sin lanzar excepciones por valores desconocidos.

    Parámetros:
        name: Nombre de la variable.
        default: Valor usado si no existe o contiene un valor no reconocido.
        env: Mapeo alternativo para pruebas; por defecto utiliza ``os.environ``.

    Retorna:
        ``True`` o ``False``. Los valores desconocidos conservan ``default`` para
        no impedir el arranque de MeshNet por una configuración opcional.
    """
    source = os.environ if env is None else env
    raw = str(source.get(name, "")).strip().casefold()
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    return default


def _env_int(
    name: str,
    default: int,
    minimum: int,
    env: Optional[Mapping[str, str]] = None,
) -> int:
    """Lee un entero de entorno y aplica un mínimo seguro."""
    source = os.environ if env is None else env
    try:
        value = int(str(source.get(name, default)).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, value)


@dataclass(frozen=True)
class AIFeatures:
    """Feature flags independientes subordinados al interruptor global de IA."""

    emergencies: bool = False
    summarize: bool = False
    correlation: bool = False
    network: bool = False
    log_analysis: bool = False
    knowledge: bool = False
    radio: bool = False
    pythia: bool = False
    agent: bool = False

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "AIFeatures":
        """Construye los flags por módulo a partir de variables ``MESHNET_AI_*``."""
        return cls(
            emergencies=_env_bool("MESHNET_AI_EMERGENCIES_ENABLED", False, env),
            summarize=_env_bool("MESHNET_AI_SUMMARIZE_ENABLED", False, env),
            correlation=_env_bool("MESHNET_AI_CORRELATION_ENABLED", False, env),
            network=_env_bool("MESHNET_AI_NETWORK_ENABLED", False, env),
            log_analysis=_env_bool("MESHNET_AI_LOG_ANALYSIS_ENABLED", False, env),
            knowledge=_env_bool("MESHNET_AI_KNOWLEDGE_ENABLED", False, env),
            radio=_env_bool("MESHNET_AI_RADIO_ENABLED", False, env),
            pythia=_env_bool("MESHNET_AI_PYTHIA_ENABLED", False, env),
            agent=_env_bool("MESHNET_AI_AGENT_ENABLED", False, env),
        )

    def as_dict(self) -> dict[str, bool]:
        """Devuelve una representación apta para health/status sin secretos."""
        return {
            "emergencies": self.emergencies,
            "summarize": self.summarize,
            "correlation": self.correlation,
            "network": self.network,
            "log_analysis": self.log_analysis,
            "knowledge": self.knowledge,
            "radio": self.radio,
            "pythia": self.pythia,
            "agent": self.agent,
        }


@dataclass(frozen=True)
class AIConfig:
    """Configuración inmutable de MeshNet Intelligence obtenida del entorno."""

    enabled: bool = False
    provider: str = "openai"
    model: str = ""
    api_key: str = field(default="", repr=False)
    base_url: str = ""
    timeout_sec: int = 10
    failure_threshold: int = 3
    cooldown_sec: int = 300
    features: AIFeatures = field(default_factory=AIFeatures)

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "AIConfig":
        """Carga la configuración sin exigir variables cuando la IA está desactivada.

        ``MESHNET_AI_ENABLED`` ausente equivale siempre a ``False``. De esta forma
        una instalación existente puede actualizar MeshNet sin configurar IA y
        mantiene exactamente su comportamiento previo.
        """
        source = os.environ if env is None else env
        provider = str(source.get("MESHNET_AI_PROVIDER", "openai")).strip().casefold() or "openai"
        return cls(
            enabled=_env_bool("MESHNET_AI_ENABLED", False, source),
            provider=provider,
            model=str(source.get("MESHNET_AI_MODEL", "")).strip(),
            api_key=str(source.get("MESHNET_AI_API_KEY", "")).strip(),
            base_url=str(source.get("MESHNET_AI_BASE_URL", "")).strip().rstrip("/"),
            timeout_sec=_env_int("MESHNET_AI_TIMEOUT_SEC", 10, 1, source),
            failure_threshold=_env_int("MESHNET_AI_FAILURE_THRESHOLD", 3, 1, source),
            cooldown_sec=_env_int("MESHNET_AI_COOLDOWN_SEC", 300, 1, source),
            features=AIFeatures.from_env(source),
        )

    def validation_error(self) -> str:
        """Devuelve un error de configuración seguro o cadena vacía si es válida.

        La validación solo es estricta cuando la IA está activada. Ningún mensaje
        incluye la clave API ni otros secretos.
        """
        if not self.enabled:
            return ""
        if self.provider not in _SUPPORTED_PROVIDERS:
            return f"proveedor IA no soportado: {self.provider}"
        if not self.model:
            return "MESHNET_AI_MODEL no configurado"
        if self.provider in {"openai", "compatible"} and not self.api_key:
            return "MESHNET_AI_API_KEY no configurada"
        if self.provider == "compatible" and not self.base_url:
            return "MESHNET_AI_BASE_URL no configurada"
        return ""


@dataclass(frozen=True)
class AIResult:
    """Resultado uniforme de una operación IA; nunca obliga al llamador a usarlo."""

    ok: bool
    text: str = ""
    status: str = "disabled"
    error: str = ""
    duration_ms: int = 0


class CircuitBreaker:
    """Circuit breaker simple y thread-safe para evitar insistir sobre una API caída."""

    def __init__(self, threshold: int, cooldown_sec: int, clock: Callable[[], float] = time.monotonic):
        self.threshold = max(1, int(threshold))
        self.cooldown_sec = max(1, int(cooldown_sec))
        self._clock = clock
        self._failures = 0
        self._opened_at: Optional[float] = None
        self._lock = threading.Lock()

    def allow_request(self) -> bool:
        """Indica si puede intentarse una llamada; tras cooldown permite una prueba."""
        with self._lock:
            if self._opened_at is None:
                return True
            if self._clock() - self._opened_at >= self.cooldown_sec:
                self._failures = 0
                self._opened_at = None
                return True
            return False

    def record_success(self) -> None:
        """Reinicia el contador tras una llamada correcta."""
        with self._lock:
            self._failures = 0
            self._opened_at = None

    def record_failure(self) -> None:
        """Incrementa fallos y abre el circuito al alcanzar el umbral configurado."""
        with self._lock:
            self._failures += 1
            if self._failures >= self.threshold:
                self._opened_at = self._clock()

    @property
    def failures(self) -> int:
        """Número actual de fallos consecutivos."""
        with self._lock:
            return self._failures

    @property
    def is_open(self) -> bool:
        """Estado actual sin alterar el temporizador de recuperación."""
        with self._lock:
            return self._opened_at is not None and self._clock() - self._opened_at < self.cooldown_sec


class MeshNetAI:
    """Fachada única para IA opcional con fallback seguro y sin dependencias externas.

    Los consumidores deben comprobar ``AIResult.ok``. Si es falso, deben ejecutar
    o conservar su ruta clásica. Este módulo deliberadamente no conoce broker,
    radio, Emergencias, BBS ni Telegram, por lo que no puede alterar sus flujos.
    """

    def __init__(self, config: AIConfig):
        self.config = config
        self.breaker = CircuitBreaker(config.failure_threshold, config.cooldown_sec)
        self._last_error = ""
        self._last_success_monotonic: Optional[float] = None

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "MeshNetAI":
        """Crea la fachada desde variables de entorno."""
        return cls(AIConfig.from_env(env))

    def feature_enabled(self, feature: str) -> bool:
        """Comprueba interruptor global y flag concreto sin lanzar excepciones."""
        if not self.config.enabled:
            return False
        return bool(self.config.features.as_dict().get(feature, False))

    def generate_text(self, prompt: str, system: str = "") -> AIResult:
        """Genera texto con el proveedor configurado y devuelve fallo seguro.

        Parámetros:
            prompt: Texto de entrada. Debe estar ya anonimizado por el módulo que
                invoque IA cuando pueda contener información sensible.
            system: Instrucción opcional para el modelo.

        Retorna:
            ``AIResult(ok=True)`` únicamente si existe una respuesta válida.
            Cualquier otro caso devuelve ``ok=False`` para activar el fallback del
            consumidor. Nunca se propaga una excepción de red al núcleo MeshNet.
        """
        if not self.config.enabled:
            return AIResult(ok=False, status="disabled")

        config_error = self.config.validation_error()
        if config_error:
            self._last_error = config_error
            return AIResult(ok=False, status="error", error=config_error)

        if not self.breaker.allow_request():
            return AIResult(ok=False, status="degraded", error="circuit breaker activo")

        started = time.monotonic()
        try:
            if self.config.provider == "ollama":
                text = self._call_ollama(prompt, system)
            else:
                text = self._call_openai_compatible(prompt, system)
            text = text.strip()
            if not text:
                raise ValueError("respuesta IA vacía")
            self.breaker.record_success()
            self._last_error = ""
            self._last_success_monotonic = time.monotonic()
            return AIResult(
                ok=True,
                text=text,
                status="available",
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            )
        except Exception as exc:  # noqa: BLE001 - frontera de aislamiento intencionada.
            self.breaker.record_failure()
            safe_error = self._safe_error(exc)
            self._last_error = safe_error
            return AIResult(
                ok=False,
                status="degraded",
                error=safe_error,
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            )

    def _call_openai_compatible(self, prompt: str, system: str) -> str:
        """Llama a Responses API de OpenAI o a una URL compatible configurable.

        Para ``provider=openai`` usa ``https://api.openai.com/v1/responses`` salvo
        que se configure ``MESHNET_AI_BASE_URL``. Para ``provider=compatible`` la
        base URL es obligatoria. Se usa únicamente biblioteca estándar para no
        añadir dependencias al núcleo actual.
        """
        base = self.config.base_url or "https://api.openai.com"
        url = f"{base}/v1/responses"
        body: dict[str, object] = {"model": self.config.model, "input": prompt}
        if system:
            body["instructions"] = system
        payload = self._post_json(
            url,
            body,
            headers={"Authorization": f"Bearer {self.config.api_key}"},
        )
        output_text = payload.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text

        # Compatibilidad defensiva con respuestas que exponen el texto solo en
        # la colección output/content.
        chunks: list[str] = []
        for item in payload.get("output", []) if isinstance(payload.get("output"), list) else []:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    chunks.append(part["text"])
        return "\n".join(chunks)

    def _call_ollama(self, prompt: str, system: str) -> str:
        """Llama al endpoint nativo ``/api/generate`` de Ollama sin streaming."""
        base = self.config.base_url or "http://127.0.0.1:11434"
        body: dict[str, object] = {
            "model": self.config.model,
            "prompt": prompt,
            "stream": False,
        }
        if system:
            body["system"] = system
        payload = self._post_json(f"{base}/api/generate", body)
        response = payload.get("response")
        return response if isinstance(response, str) else ""

    def _post_json(self, url: str, body: Mapping[str, object], headers: Optional[Mapping[str, str]] = None) -> dict:
        """POST JSON con timeout obligatorio y respuesta limitada a un objeto JSON."""
        request_headers = {"Content-Type": "application/json"}
        if headers:
            request_headers.update(headers)
        request = urllib.request.Request(
            url,
            data=json.dumps(dict(body), ensure_ascii=False).encode("utf-8"),
            headers=request_headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.config.timeout_sec) as response:
            raw = response.read()
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("respuesta IA JSON no válida")
        return parsed

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        """Normaliza errores sin reproducir cuerpos HTTP que pudieran incluir datos."""
        if isinstance(exc, urllib.error.HTTPError):
            return f"HTTP {exc.code} del proveedor IA"
        if isinstance(exc, urllib.error.URLError):
            return "proveedor IA no accesible"
        if isinstance(exc, TimeoutError):
            return "timeout del proveedor IA"
        if isinstance(exc, json.JSONDecodeError):
            return "respuesta IA no es JSON válido"
        text = str(exc).strip()
        return text[:160] if text else exc.__class__.__name__

    def public_status(self) -> dict[str, object]:
        """Devuelve estado seguro para panel/health; jamás incluye ``api_key``.

        Estados:
            disabled: IA desactivada por configuración.
            error: IA activada pero configuración incompleta/incorrecta.
            degraded: circuit breaker abierto o último intento fallido.
            available: configuración válida y sin fallo activo conocido.
        """
        if not self.config.enabled:
            state = "disabled"
        elif self.config.validation_error():
            state = "error"
        elif self.breaker.is_open or self._last_error:
            state = "degraded"
        else:
            state = "available"

        return {
            "enabled": self.config.enabled,
            "state": state,
            "provider": self.config.provider if self.config.enabled else "",
            "model_configured": bool(self.config.model),
            "base_url_configured": bool(self.config.base_url),
            "features": self.config.features.as_dict(),
            "failures": self.breaker.failures,
            "circuit_open": self.breaker.is_open,
            "last_error": self._last_error,
        }
