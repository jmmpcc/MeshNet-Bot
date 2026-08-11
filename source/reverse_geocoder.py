"""Compatibilidad ligera con ``reverse_geocoder`` para MeshNet-Bot.

Este módulo sustituye la dependencia externa ``reverse_geocoder`` sin obligar a
modificar los handlers históricos de Telegram.  El bot ya hace imports diferidos
``import reverse_geocoder as rg`` dentro de /ver_nodos y /vecinos; al encontrarse
este fichero junto a ``Telegram_Bot_Broker.py``, Python reutiliza la misma API
``rg.search(...)`` pero la resolución se realiza con la infraestructura geográfica
validada en MeshNet-Bot v7.0.52.

La referencia humana se obtiene mediante el núcleo de población IGN/INE más
cercano y la provincia se resuelve con la cartografía local existente.  Ante
cualquier fallo de red se mantiene el fallback provincial, de modo que una
indisponibilidad del IGN nunca rompe /ver_nodos ni /vecinos.
"""

from __future__ import annotations

import math
import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable


# Cuando el bot se ejecuta como /app/source/Telegram_Bot_Broker.py, sys.path[0]
# es /app/source. Añadimos únicamente la raíz del proyecto para poder reutilizar
# el paquete tools/... que el Dockerfile v7.0.53 copia de forma explícita.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    from tools.emergencias_guardia.emergencias.geo_admin import (
        resolve_nearest_population,
        resolve_province,
    )
except Exception:  # pragma: no cover - fallback defensivo de arranque
    resolve_nearest_population = None  # type: ignore[assignment]
    resolve_province = None  # type: ignore[assignment]


_TRUTHY = {"1", "true", "t", "yes", "y", "on", "si", "sí"}


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    """Lee un float de entorno y lo limita a un intervalo seguro.

    Uso:
        timeout = _env_float("BOT_GEO_LOOKUP_TIMEOUT_SEC", 1.2, 0.25, 10.0)

    Parámetros:
        name: nombre de la variable.
        default: valor usado si falta o no es numérico.
        minimum/maximum: límites de seguridad.
    """
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = float(default)
    return max(float(minimum), min(float(maximum), value))


def _network_lookup_enabled() -> bool:
    """Indica si se permite consultar la población cercana en el IGN.

    ``BOT_GEO_LOOKUP_ENABLED=0`` desactiva sólo la consulta de núcleos. La
    provincia local sigue disponible como fallback y los comandos continúan
    operativos.
    """
    raw = str(os.getenv("BOT_GEO_LOOKUP_ENABLED", "1") or "1").strip().casefold()
    return raw in _TRUTHY


def _as_float(value: Any) -> float | None:
    """Normaliza una coordenada finita a float o devuelve ``None``."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _normalise_points(geo_coords: Any) -> list[tuple[float, float]]:
    """Acepta las dos formas usadas por ``reverse_geocoder.search``.

    Uso:
        _normalise_points((41.65, -0.88))
        _normalise_points([(41.65, -0.88), (42.44, -0.76)])

    Funcionalidad:
        El código histórico del bot usa tanto una tupla simple ``(lat, lon)``
        como una lista de tuplas. Esta función conserva ambas formas sin exigir
        cambios en /ver_nodos ni /vecinos.
    """
    if isinstance(geo_coords, (tuple, list)) and len(geo_coords) == 2:
        lat = _as_float(geo_coords[0])
        lon = _as_float(geo_coords[1])
        if lat is not None and lon is not None:
            return [(lat, lon)]

    points: list[tuple[float, float]] = []
    if not isinstance(geo_coords, Iterable) or isinstance(geo_coords, (str, bytes)):
        return points

    for item in geo_coords:
        if not isinstance(item, (tuple, list)) or len(item) < 2:
            continue
        lat = _as_float(item[0])
        lon = _as_float(item[1])
        if lat is None or lon is None:
            continue
        points.append((lat, lon))
    return points


@lru_cache(maxsize=1024)
def _lookup_cached(latitude_key: float, longitude_key: float) -> dict[str, Any]:
    """Resuelve y cachea una referencia humana para una coordenada.

    Uso:
        result = _lookup_cached(round(lat, 5), round(lon, 5))

    Funcionalidad:
        1. Resuelve provincia con el GeoJSON local de MeshNet-Bot.
        2. Si está permitido, consulta el núcleo IGN/INE más cercano usando la
           función ya validada en v7.0.52.
        3. Si el servicio no responde, devuelve igualmente la provincia local.
        4. Mantiene las claves principales que esperaba ``reverse_geocoder``:
           ``name``, ``admin2`` y ``admin1``.

    La caché evita repetir consultas cuando Telegram vuelve a mostrar los mismos
    nodos o cuando /ver_nodos y /vecinos se ejecutan consecutivamente.
    """
    lat = float(latitude_key)
    lon = float(longitude_key)

    province = ""
    if callable(resolve_province):
        try:
            province = str(resolve_province(lat, lon) or "").strip()
        except Exception:
            province = ""

    nearest: dict[str, Any] | None = None
    if _network_lookup_enabled() and callable(resolve_nearest_population):
        timeout = _env_float("BOT_GEO_LOOKUP_TIMEOUT_SEC", 1.2, 0.25, 10.0)
        radius = _env_float("BOT_GEO_LOOKUP_MAX_RADIUS_KM", 30.0, 1.0, 100.0)
        try:
            nearest = resolve_nearest_population(
                lat,
                lon,
                timeout_seconds=timeout,
                max_radius_km=radius,
            )
        except Exception:
            nearest = None

    name = ""
    result_lat = lat
    result_lon = lon
    distance_km: float | None = None
    inhabitants: int | None = None
    codine = ""

    if isinstance(nearest, dict):
        name = str(nearest.get("name") or "").strip()
        result_lat = _as_float(nearest.get("latitude")) or lat
        result_lon = _as_float(nearest.get("longitude")) or lon
        distance_km = _as_float(nearest.get("distance_km"))
        try:
            raw_inhabitants = nearest.get("inhabitants")
            inhabitants = None if raw_inhabitants is None else int(raw_inhabitants)
        except (TypeError, ValueError):
            inhabitants = None
        codine = str(nearest.get("codine") or "").strip()

    # reverse_geocoder devolvía strings en lat/lon y los handlers actuales sólo
    # consumen name/admin2/admin1. Conservamos ese contrato para máxima
    # compatibilidad y añadimos metadatos útiles sin romper consumidores.
    return {
        "lat": f"{result_lat:.6f}",
        "lon": f"{result_lon:.6f}",
        "name": name,
        "admin1": "",
        "admin2": province,
        "cc": "ES" if province or name else "",
        "distance_km": distance_km,
        "inhabitants": inhabitants,
        "codine": codine,
    }


def search(geo_coords: Any, mode: int = 1, verbose: bool = False) -> list[dict[str, Any]]:
    """API compatible con ``reverse_geocoder.search`` usada por el bot.

    Uso histórico conservado:
        ``rg.search((lat, lon))``
        ``rg.search([(lat, lon)])``

    Parámetros:
        geo_coords: una coordenada o iterable de coordenadas ``(lat, lon)``.
        mode: aceptado por compatibilidad; no altera la resolución.
        verbose: aceptado por compatibilidad; no genera salida adicional.

    Retorno:
        Lista de diccionarios. ``name`` es el núcleo de población IGN/INE más
        cercano; ``admin2`` es la provincia local. Si no se puede resolver una
        entrada, se devuelve un diccionario vacío compatible en vez de lanzar.
    """
    del mode, verbose

    out: list[dict[str, Any]] = []
    for lat, lon in _normalise_points(geo_coords):
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            continue
        try:
            out.append(_lookup_cached(round(lat, 5), round(lon, 5)))
        except Exception:
            out.append({
                "lat": f"{lat:.6f}",
                "lon": f"{lon:.6f}",
                "name": "",
                "admin1": "",
                "admin2": "",
                "cc": "",
                "distance_km": None,
                "inhabitants": None,
                "codine": "",
            })
    return out
