from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request
from functools import lru_cache
from pathlib import Path
from typing import Any

from .models import Event


# Cartografía provincial local. Se mantiene fuera de la lógica de los
# conectores para que cualquier fuente futura con coordenadas pueda reutilizarla.
PROVINCE_BOUNDARIES_FILE = (
    Path(__file__).resolve().parents[1] / "data" / "provincias_espana.geojson"
)

# v7.0.52: colección oficial de núcleos de población IGN/INE. Se usa sólo
# como enriquecimiento humano de eventos FIRMS ya aceptados. Nunca interviene
# en filtros, deduplicación, severidad ni selección geográfica.
IGN_POPULATION_NUCLEI_API = (
    "https://api-features.ign.es/collections/nuc/items"
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


def _valid_coordinates(latitude: float, longitude: float) -> tuple[float, float] | None:
    """Valida y normaliza un par latitud/longitud WGS84."""
    try:
        lat = float(latitude)
        lon = float(longitude)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return lat, lon


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
    coordinates = _valid_coordinates(latitude, longitude)
    if coordinates is None:
        return None
    lat, lon = coordinates

    path = Path(boundaries_file) if boundaries_file is not None else PROVINCE_BOUNDARIES_FILE
    for boundary in _load_province_boundaries_cached(str(path.resolve())):
        min_lon, min_lat, max_lon, max_lat = boundary["bbox"]
        if not (min_lon <= lon <= max_lon and min_lat <= lat <= max_lat):
            continue
        if _geometry_contains(lon, lat, boundary["geometry"]):
            return str(boundary["name"])
    return None


def _haversine_coordinates(
    latitude_a: float,
    longitude_a: float,
    latitude_b: float,
    longitude_b: float,
) -> float:
    """Calcula distancia geodésica aproximada entre dos puntos WGS84 en km.

    Uso:
        distance = _haversine_coordinates(42.44, -0.76, 42.52, -0.81)

    Parámetros:
        latitude_a, longitude_a: coordenadas del evento.
        latitude_b, longitude_b: coordenadas del núcleo de población.

    Funcionalidad:
        Usa Haversine con radio terrestre medio IUGG. Es suficientemente preciso
        para ordenar núcleos cercanos sin añadir dependencias geoespaciales.
    """
    radius = 6371.0088
    phi_a = math.radians(latitude_a)
    phi_b = math.radians(latitude_b)
    dphi = math.radians(latitude_b - latitude_a)
    dlambda = math.radians(longitude_b - longitude_a)
    value = (
        math.sin(dphi / 2.0) ** 2
        + math.cos(phi_a) * math.cos(phi_b) * math.sin(dlambda / 2.0) ** 2
    )
    value = min(1.0, max(0.0, value))
    return radius * 2.0 * math.atan2(math.sqrt(value), math.sqrt(1.0 - value))


def _search_bbox(
    latitude: float,
    longitude: float,
    radius_km: float,
) -> tuple[float, float, float, float]:
    """Construye un bbox WGS84 que cubre aproximadamente ``radius_km``.

    El ajuste longitudinal tiene en cuenta la latitud para no consultar una zona
    innecesariamente grande. El resultado se usa sólo para preseleccionar puntos;
    la distancia final siempre se calcula con Haversine.
    """
    latitude_delta = radius_km / 111.32
    cosine = max(0.15, abs(math.cos(math.radians(latitude))))
    longitude_delta = radius_km / (111.32 * cosine)
    return (
        longitude - longitude_delta,
        latitude - latitude_delta,
        longitude + longitude_delta,
        latitude + latitude_delta,
    )


@lru_cache(maxsize=256)
def _resolve_nearest_population_ign_cached(
    latitude_key: float,
    longitude_key: float,
    endpoint: str,
    timeout_seconds: float,
    max_radius_km: float,
) -> dict[str, Any] | None:
    """Consulta núcleos IGN/INE y devuelve el más próximo al punto.

    Uso:
        La función se invoca exclusivamente desde ``resolve_nearest_population``.

    Parámetros:
        latitude_key, longitude_key: coordenadas redondeadas para la caché.
        endpoint: colección OGC API-Features ``nuc/items``.
        timeout_seconds: timeout máximo por consulta.
        max_radius_km: distancia máxima aceptada para etiquetar ``CERCA``.

    Funcionalidad:
        - Prueba radios crecientes de 5, 15 y ``max_radius_km`` km.
        - Solicita únicamente atributos; ``skipGeometry=true`` evita descargar
          geometría y reduce drásticamente la respuesta.
        - Calcula localmente Haversine para todos los candidatos válidos.
        - Devuelve sólo el núcleo más cercano si queda dentro del radio máximo.
        - Ante error, timeout, JSON inválido o ausencia de candidatos devuelve
          ``None`` sin afectar al evento ni a ninguna otra fuente.
    """
    radii = [5.0, 15.0, max_radius_km]
    unique_radii: list[float] = []
    for value in radii:
        radius = max(1.0, min(100.0, float(value)))
        if radius not in unique_radii:
            unique_radii.append(radius)

    for radius_km in unique_radii:
        bbox = _search_bbox(latitude_key, longitude_key, radius_km)
        query = urllib.parse.urlencode({
            "f": "json",
            "bbox": ",".join(f"{value:.6f}" for value in bbox),
            "skipGeometry": "true",
            "properties": "nombre,latitud,longitud,habitantes,cpro,codine",
            "limit": "500",
        })
        request = urllib.request.Request(
            f"{endpoint}?{query}",
            headers={
                "Accept": "application/json",
                "User-Agent": "MeshNet-Bot/7.0.52 (+https://github.com/jmmpcc/MeshNet-Bot)",
            },
        )
        maximum_bytes = 1_000_000
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                body = response.read(maximum_bytes + 1)
        except (OSError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
            return None
        if len(body) > maximum_bytes:
            return None

        try:
            payload = json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None

        candidates: list[dict[str, Any]] = []
        for feature in payload.get("features", []):
            if not isinstance(feature, dict):
                continue
            properties = feature.get("properties") or {}
            name = str(properties.get("nombre") or "").strip()
            coordinates = _valid_coordinates(
                properties.get("latitud"),
                properties.get("longitud"),
            )
            if not name or coordinates is None:
                continue
            candidate_latitude, candidate_longitude = coordinates
            distance_km = _haversine_coordinates(
                latitude_key,
                longitude_key,
                candidate_latitude,
                candidate_longitude,
            )
            if distance_km > max_radius_km:
                continue
            inhabitants = properties.get("habitantes")
            try:
                inhabitants_value = None if inhabitants is None else int(inhabitants)
            except (TypeError, ValueError):
                inhabitants_value = None
            candidates.append({
                "name": name,
                "distance_km": distance_km,
                "latitude": candidate_latitude,
                "longitude": candidate_longitude,
                "inhabitants": inhabitants_value,
                "province_code": str(properties.get("cpro") or "").strip(),
                "codine": str(properties.get("codine") or "").strip(),
            })

        if candidates:
            candidates.sort(key=lambda item: (item["distance_km"], -int(item["inhabitants"] or 0), item["name"]))
            return candidates[0]

    return None


def resolve_nearest_population(
    latitude: float,
    longitude: float,
    *,
    endpoint: str | None = None,
    timeout_seconds: float | None = None,
    max_radius_km: float | None = None,
) -> dict[str, Any] | None:
    """Devuelve el núcleo de población IGN/INE más cercano a una coordenada.

    Uso:
        result = resolve_nearest_population(42.4407, -0.7678)
        # {"name": "...", "distance_km": 3.2, ...}

    Parámetros:
        latitude, longitude: coordenadas WGS84 del evento.
        endpoint: endpoint alternativo para pruebas.
        timeout_seconds: timeout HTTP; por defecto 2.5 s.
        max_radius_km: distancia máxima para considerar una población cercana;
            por defecto 30 km.

    Funcionalidad y seguridad:
        La función es best-effort. No modifica ningún Event y devuelve ``None``
        ante cualquier fallo. Puede desactivarse con
        ``EMERGENCIAS_GEO_POPULATION_ENABLED=0``. El endpoint y límites pueden
        ajustarse mediante variables de entorno sin alterar configuración previa.
    """
    enabled = str(os.getenv("EMERGENCIAS_GEO_POPULATION_ENABLED", "1") or "1").strip().casefold()
    if enabled not in {"1", "true", "yes", "on", "si", "sí", "y"}:
        return None

    coordinates = _valid_coordinates(latitude, longitude)
    if coordinates is None:
        return None
    lat, lon = coordinates
    service = str(
        endpoint
        or os.getenv("EMERGENCIAS_GEO_POPULATION_ENDPOINT", IGN_POPULATION_NUCLEI_API)
    ).strip()
    if not service.lower().startswith("https://"):
        return None

    try:
        timeout = float(
            timeout_seconds
            if timeout_seconds is not None
            else os.getenv("EMERGENCIAS_GEO_POPULATION_TIMEOUT_SEC", "2.5")
        )
    except (TypeError, ValueError):
        timeout = 2.5
    timeout = max(0.25, min(10.0, timeout))

    try:
        radius = float(
            max_radius_km
            if max_radius_km is not None
            else os.getenv("EMERGENCIAS_GEO_POPULATION_MAX_RADIUS_KM", "30")
        )
    except (TypeError, ValueError):
        radius = 30.0
    radius = max(1.0, min(100.0, radius))

    return _resolve_nearest_population_ign_cached(
        round(lat, 5),
        round(lon, 5),
        service,
        timeout,
        radius,
    )


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
          derivado local y no debe provocar una falsa notificación ``updated``.
        - Ante ausencia/corrupción de cartografía devuelve False y deja el Event
          exactamente como estaba.
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


def enrich_event_municipality(event: Event) -> bool:
    """Compatibilidad v7.0.52: añade referencia de población cercana a metadata.

    Uso:
        changed = enrich_event_municipality(event)

    Parámetros:
        event: evento FIRMS ya aceptado que dispone de coordenadas.

    Funcionalidad:
        El nombre de esta función se conserva para no alterar el flujo del motor
        ya preparado en la primera iteración v7.0.52. Su comportamiento correcto
        NO rellena ``event.municipality`` porque un núcleo cercano no implica que
        el foco esté dentro de ese municipio. En su lugar guarda exclusivamente:
        - ``nearest_population``
        - ``nearest_population_distance_km``
        - coordenadas/código/habitantes del núcleo cuando estén disponibles.

        Si IGN falla, devuelve False y deja el evento intacto. No modifica
        ``raw_hash``, coordenadas, severidad, estado ni campos de filtrado.
    """
    if event.latitude is None or event.longitude is None:
        return False
    if event.metadata.get("nearest_population"):
        return False

    result = resolve_nearest_population(event.latitude, event.longitude)
    if not result:
        return False

    event.metadata["nearest_population"] = result["name"]
    event.metadata["nearest_population_distance_km"] = round(float(result["distance_km"]), 2)
    event.metadata["nearest_population_latitude"] = result.get("latitude")
    event.metadata["nearest_population_longitude"] = result.get("longitude")
    event.metadata["nearest_population_inhabitants"] = result.get("inhabitants")
    event.metadata["nearest_population_codine"] = result.get("codine")
    event.metadata["nearest_population_resolution_method"] = "ign_api_features_nuc_haversine"
    return True
