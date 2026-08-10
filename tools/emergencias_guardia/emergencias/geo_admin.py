from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Any

from .models import Event


# Cartografía provincial local. Se mantiene fuera de la lógica de los
# conectores para que cualquier fuente futura con coordenadas pueda reutilizarla.
PROVINCE_BOUNDARIES_FILE = (
    Path(__file__).resolve().parents[1] / "data" / "provincias_espana.geojson"
)


def _point_on_segment(
    x: float,
    y: float,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    tolerance: float = 1e-10,
) -> bool:
    """Comprueba si un punto está sobre un segmento del contorno.

    Uso:
        _point_on_segment(lon, lat, lon1, lat1, lon2, lat2)

    Parámetros:
        x, y: longitud y latitud del punto.
        x1, y1, x2, y2: extremos del segmento.
        tolerance: tolerancia numérica para puntos situados en una frontera.

    Funcionalidad:
        Evita resultados indeterminados del algoritmo ray-casting cuando una
        coordenada cae exactamente sobre el límite de dos polígonos. En ese
        caso el punto se considera perteneciente al contorno evaluado.
    """
    dx, dy = x2 - x1, y2 - y1
    cross = (x - x1) * dy - (y - y1) * dx
    scale = max(1.0, abs(dx), abs(dy))
    if abs(cross) > tolerance * scale:
        return False
    dot = (x - x1) * (x - x2) + (y - y1) * (y - y2)
    return dot <= tolerance


def _ring_contains(longitude: float, latitude: float, ring: list[list[float]]) -> bool:
    """Determina si una coordenada está dentro de un anillo GeoJSON.

    Uso:
        inside = _ring_contains(longitude, latitude, ring)

    Parámetros:
        longitude, latitude: punto WGS84 a comprobar.
        ring: anillo GeoJSON ``[[lon, lat], ...]``.

    Funcionalidad:
        Implementa ray-casting en Python puro y considera los puntos de borde
        como interiores. No requiere shapely, geopandas, fiona ni pyproj.
    """
    if not isinstance(ring, list) or len(ring) < 3:
        return False

    inside = False
    previous = ring[-1]
    for current in ring:
        try:
            x1, y1 = float(previous[0]), float(previous[1])
            x2, y2 = float(current[0]), float(current[1])
        except (TypeError, ValueError, IndexError):
            previous = current
            continue

        if _point_on_segment(longitude, latitude, x1, y1, x2, y2):
            return True

        crosses = (y1 > latitude) != (y2 > latitude)
        if crosses:
            intersection_x = (x2 - x1) * (latitude - y1) / (y2 - y1) + x1
            if longitude < intersection_x:
                inside = not inside
        previous = current
    return inside


def _polygon_contains(longitude: float, latitude: float, polygon: list[Any]) -> bool:
    """Comprueba un Polygon GeoJSON respetando sus huecos interiores.

    El primer anillo es el contorno exterior y los restantes son huecos. Un
    punto debe estar en el exterior y fuera de todos los huecos.
    """
    if not isinstance(polygon, list) or not polygon:
        return False
    if not _ring_contains(longitude, latitude, polygon[0]):
        return False
    return not any(_ring_contains(longitude, latitude, hole) for hole in polygon[1:])


def _geometry_contains(longitude: float, latitude: float, geometry: dict[str, Any]) -> bool:
    """Comprueba una coordenada contra geometrías Polygon/MultiPolygon.

    Uso:
        _geometry_contains(lon, lat, feature["geometry"])

    Las geometrías desconocidas se ignoran de forma segura y devuelven False.
    """
    geometry_type = str(geometry.get("type") or "")
    coordinates = geometry.get("coordinates")
    if geometry_type == "Polygon":
        return _polygon_contains(longitude, latitude, coordinates)
    if geometry_type == "MultiPolygon" and isinstance(coordinates, list):
        return any(_polygon_contains(longitude, latitude, polygon) for polygon in coordinates)
    return False


def _geometry_bbox(geometry: dict[str, Any]) -> tuple[float, float, float, float] | None:
    """Calcula el bounding-box de una geometría provincial una sola vez.

    Devuelve ``(min_lon, min_lat, max_lon, max_lat)``. El bounding-box permite
    descartar casi todas las provincias antes de ejecutar point-in-polygon.
    """
    points: list[tuple[float, float]] = []

    def collect(value: Any) -> None:
        if (
            isinstance(value, list)
            and len(value) >= 2
            and isinstance(value[0], (int, float))
            and isinstance(value[1], (int, float))
        ):
            points.append((float(value[0]), float(value[1])))
            return
        if isinstance(value, list):
            for child in value:
                collect(child)

    collect(geometry.get("coordinates"))
    if not points:
        return None
    longitudes = [point[0] for point in points]
    latitudes = [point[1] for point in points]
    return min(longitudes), min(latitudes), max(longitudes), max(latitudes)


@lru_cache(maxsize=4)
def _load_province_boundaries_cached(path_text: str) -> tuple[dict[str, Any], ...]:
    """Carga y prepara la cartografía provincial, con caché por ruta.

    Uso:
        boundaries = _load_province_boundaries_cached(str(path))

    Funcionalidad:
        Lee el FeatureCollection local una sola vez por proceso, valida las
        geometrías admitidas y añade un bounding-box en memoria. Si el fichero
        falta, está corrupto o tiene un formato inesperado devuelve una tupla
        vacía: el filtrado existente por radio/provincia continúa funcionando.
    """
    try:
        payload = json.loads(Path(path_text).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError, UnicodeError):
        return ()
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        return ()

    prepared: list[dict[str, Any]] = []
    for feature in payload.get("features", []):
        if not isinstance(feature, dict):
            continue
        properties = feature.get("properties") or {}
        geometry = feature.get("geometry") or {}
        name = str(properties.get("name") or "").strip()
        if not name or geometry.get("type") not in {"Polygon", "MultiPolygon"}:
            continue
        bbox = _geometry_bbox(geometry)
        if bbox is None:
            continue
        prepared.append({"name": name, "bbox": bbox, "geometry": geometry})
    return tuple(prepared)


def resolve_province(
    latitude: float,
    longitude: float,
    boundaries_file: Path | str | None = None,
) -> str | None:
    """Resuelve la provincia española que contiene una coordenada WGS84.

    Uso:
        province = resolve_province(41.6488, -0.8891)

    Parámetros:
        latitude, longitude: coordenadas WGS84 del evento.
        boundaries_file: fichero GeoJSON alternativo, útil para pruebas.

    Funcionalidad:
        Usa exclusivamente la cartografía local. Primero aplica bounding-box y
        después point-in-polygon. Si no puede resolver la coordenada devuelve
        None; nunca realiza peticiones de red ni impide el filtrado por radio.
    """
    try:
        lat = float(latitude)
        lon = float(longitude)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None

    path = Path(boundaries_file) if boundaries_file is not None else PROVINCE_BOUNDARIES_FILE
    for boundary in _load_province_boundaries_cached(str(path.resolve())):
        min_lon, min_lat, max_lon, max_lat = boundary["bbox"]
        if not (min_lon <= lon <= max_lon and min_lat <= lat <= max_lat):
            continue
        if _geometry_contains(lon, lat, boundary["geometry"]):
            return str(boundary["name"])
    return None


def enrich_event_province(
    event: Event,
    boundaries_file: Path | str | None = None,
) -> bool:
    """Añade provincia a un Event que solo dispone de coordenadas.

    Uso:
        changed = enrich_event_province(event)

    Parámetros:
        event: Event ya normalizado por su conector.
        boundaries_file: cartografía alternativa para pruebas.

    Funcionalidad:
        - No modifica eventos que ya traen ``province`` (DGT, Zaragoza, etc.).
        - Solo actúa si existen latitud y longitud.
        - Añade metadata de auditoría cuando la resolución es satisfactoria.
        - Mantiene ``raw_hash`` intacto deliberadamente: la provincia es un dato
          derivado local y no debe provocar una falsa notificación ``updated``
          cuando el contenido original de la fuente no ha cambiado.
        - Ante ausencia/corrupción de cartografía devuelve False y deja el Event
          exactamente como estaba, preservando el filtrado por radio existente.
    """
    if event.province or event.latitude is None or event.longitude is None:
        return False
    province = resolve_province(event.latitude, event.longitude, boundaries_file)
    if not province:
        return False
    event.province = province
    event.metadata["province_resolved_from_coordinates"] = True
    event.metadata["province_resolution_method"] = "local_ign_boundaries"
    return True
