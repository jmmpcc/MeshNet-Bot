"""Regresiones del enrutado NASA FIRMS hacia la ruta de emergencias.

Estas pruebas cubren exclusivamente la decisión de ``route_event()``. No
modifican ni simulan el gateway APRS, el broker Mesh, la deduplicación ni la
persistencia. Su objetivo es impedir que una futura refactorización vuelva a
confundir "evento aceptado por el recolector" con "evento autorizado para
propagación".
"""
from __future__ import annotations

from copy import deepcopy

from tools.emergencias_guardia.emergencias.config import DEFAULT_CONFIG
from tools.emergencias_guardia.emergencias.models import Event
from tools.emergencias_guardia.emergencias.notifier import route_event


def _config_with_matrix(*, severity: str = "high", category: str = "wildfire") -> dict:
    """Construye una configuración mínima con autorización matricial explícita.

    Uso:
        config = _config_with_matrix(severity="high", category="wildfire")

    Parámetros:
        severity: nivel cuya casilla se considera marcada en Control Panel.
        category: categoría autorizada para ese nivel.

    Funcionalidad:
        Parte de ``DEFAULT_CONFIG`` para conservar la estructura real y añade
        una matriz exactamente igual a la que usa actualmente el Control Panel.
        ``allow_satellite_detection`` se deja deliberadamente a False para
        verificar la corrección objeto de esta fase.
    """
    config = deepcopy(DEFAULT_CONFIG)
    config["notifications"]["allow_satellite_detection"] = False
    config["notifications"]["propagation_filter"]["rules"] = {
        "low": [],
        "medium": [],
        "high": [],
        "critical": [],
    }
    config["notifications"]["propagation_filter"]["rules"][severity] = [category]
    return config


def _event(
    *,
    source: str = "nasa_firms",
    verification: str = "satellite_detection",
    category: str = "wildfire",
    severity: str = "high",
) -> Event:
    """Crea un evento activo y vigente para pruebas de enrutado.

    Los campos temporales se dejan vacíos para que ``is_current()`` lo trate
    como vigente sin depender de la fecha de ejecución del test.
    """
    return Event(
        event_id=f"{source}:test",
        source=source,
        source_event_id="test",
        category=category,
        status="active",
        verification=verification,
        severity=severity,
        title="Evento de prueba",
    )


def test_firms_matrix_explicitly_allows_satellite_wildfire() -> None:
    """FIRMS marcado en la matriz debe alcanzar la ruta de emergencias."""
    config = _config_with_matrix(severity="high", category="wildfire")

    assert route_event(_event(), config) == "emergencias"


def test_firms_matrix_still_blocks_unchecked_severity() -> None:
    """Una casilla FIRMS no marcada debe seguir bloqueando esa severidad."""
    config = _config_with_matrix(severity="critical", category="wildfire")

    assert route_event(_event(severity="high"), config) is None


def test_firms_legacy_mode_remains_blocked_without_satellite_permission() -> None:
    """Sin matriz se conserva el bloqueo histórico de detecciones satelitales."""
    config = deepcopy(DEFAULT_CONFIG)
    config["notifications"]["allow_satellite_detection"] = False

    assert route_event(_event(), config) is None


def test_firms_legacy_explicit_permission_remains_compatible() -> None:
    """El interruptor histórico sigue autorizando FIRMS fuera de la matriz."""
    config = deepcopy(DEFAULT_CONFIG)
    config["notifications"]["allow_satellite_detection"] = True

    assert route_event(_event(), config) == "emergencias"


def test_other_satellite_source_does_not_inherit_firms_exception() -> None:
    """La excepción matricial se limita a NASA FIRMS y no rebaja otras fuentes."""
    config = _config_with_matrix(severity="high", category="wildfire")

    assert route_event(_event(source="otra_fuente_satelital"), config) is None


def test_official_emergency_routing_is_unchanged() -> None:
    """Los avisos oficiales siguen usando exactamente la ruta histórica."""
    config = _config_with_matrix(severity="high", category="wildfire")

    event = _event(source="fuente_oficial", verification="official")
    assert route_event(event, config) == "emergencias"
