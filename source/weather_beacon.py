#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
weather_beacon.py v7.0.12— Baliza meteorológica dinámica para MeshNet Bot.

Objetivo:
- Construir, en el momento exacto de ejecución de una tarea programada, un texto
  corto con hora local, temperatura, humedad relativa y estado meteorológico.
- No envía por RF. Solo devuelve texto. El envío lo realiza broker_task.py con
  el transporte ya existente: Meshtastic, MeshCore o APRS.
- Sin dependencias externas: usa urllib de la librería estándar.

Uso desde broker_task.py:
    from weather_beacon import build_weather_beacon_text_from_meta
    text = build_weather_beacon_text_from_meta(task.meta)

Campos meta esperados:
    task_type: "weather_beacon"
    city: "Zaragoza"
    lat: 41.6488
    lon: -0.8891
    timezone: "Europe/Madrid"
    template: opcional

Variables .env opcionales:
    WEATHER_BEACON_TIMEOUT_SEC=8
    WEATHER_BEACON_CACHE_SEC=600
    WEATHER_BEACON_DEFAULT_CITY=Zaragoza
    WEATHER_BEACON_DEFAULT_LAT=41.6488
    WEATHER_BEACON_DEFAULT_LON=-0.8891
    WEATHER_BEACON_TZ=Europe/Madrid
    WEATHER_BEACON_TEMPLATE=Son las {hora} h. La temperatura en {ciudad} es de {temp} °C, humedad {humedad}% y {estado}.
    WEATHER_BEACON_LOCATIONS=Zaragoza:41.6488,-0.8891;Madrid:40.4168,-3.7038
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


@dataclass(frozen=True)
class WeatherBeaconData:
    """Datos normalizados listos para componer el mensaje de baliza."""
    city: str
    temperature_c: Optional[float]
    relative_humidity_pct: Optional[int]
    weather_code: Optional[int]
    weather_state_es: str
    observed_time: str
    local_time_hhmm: str


_CACHE: Dict[str, Tuple[float, WeatherBeaconData]] = {}


def _env_float(name: str, default: float) -> float:
    """Lee un float desde entorno con fallback seguro."""
    try:
        return float((os.getenv(name, str(default)) or str(default)).strip())
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    """Lee un entero desde entorno con fallback seguro."""
    try:
        return int((os.getenv(name, str(default)) or str(default)).strip())
    except Exception:
        return int(default)


def _safe_float(v: Any) -> Optional[float]:
    """Convierte valores numéricos del JSON/API a float o None."""
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


def _safe_int(v: Any) -> Optional[int]:
    """Convierte valores numéricos del JSON/API a int o None."""
    try:
        if v is None:
            return None
        return int(round(float(v)))
    except Exception:
        return None


def _normalizar_decimal_es(v: Optional[float], digits: int = 1) -> str:
    """Formatea decimales con coma española. Si no hay dato, devuelve '?' ."""
    if v is None:
        return "?"
    return f"{float(v):.{digits}f}".replace(".", ",")


def _weather_code_to_es(code: Optional[int]) -> str:
    """
    Traduce códigos WMO usados por Open-Meteo a texto breve en español.
    Se mantiene deliberadamente corto para no malgastar airtime LoRa.
    """
    table = {
        0: "despejado",
        1: "mayormente despejado",
        2: "parcialmente nuboso",
        3: "cubierto",
        45: "niebla",
        48: "niebla con escarcha",
        51: "llovizna débil",
        53: "llovizna",
        55: "llovizna fuerte",
        56: "llovizna helada débil",
        57: "llovizna helada fuerte",
        61: "lluvia débil",
        63: "lluvia",
        65: "lluvia fuerte",
        66: "lluvia helada débil",
        67: "lluvia helada fuerte",
        71: "nieve débil",
        73: "nieve",
        75: "nieve fuerte",
        77: "granizo de nieve",
        80: "chubascos débiles",
        81: "chubascos",
        82: "chubascos fuertes",
        85: "chubascos de nieve débiles",
        86: "chubascos de nieve fuertes",
        95: "tormenta",
        96: "tormenta con granizo débil",
        99: "tormenta con granizo fuerte",
    }
    if code is None:
        return "estado no disponible"
    return table.get(int(code), f"código meteo {int(code)}")


def _parse_locations_env() -> Dict[str, Tuple[float, float]]:
    """
    Lee WEATHER_BEACON_LOCATIONS.

    Formato:
        Zaragoza:41.6488,-0.8891;Madrid:40.4168,-3.7038

    Devuelve claves en minúsculas para búsqueda tolerante.
    """
    raw = (os.getenv("WEATHER_BEACON_LOCATIONS", "") or "").strip()
    out: Dict[str, Tuple[float, float]] = {
        "zaragoza": (41.6488, -0.8891),
    }
    if not raw:
        return out

    for item in raw.split(";"):
        item = (item or "").strip()
        if not item or ":" not in item or "," not in item:
            continue
        name, coords = item.split(":", 1)
        lat_s, lon_s = coords.split(",", 1)
        try:
            out[name.strip().lower()] = (float(lat_s.strip()), float(lon_s.strip()))
        except Exception:
            continue
    return out


def resolve_location_from_meta(meta: Dict[str, Any]) -> Tuple[str, float, float, str]:
    """
    Resuelve ciudad, latitud, longitud y zona horaria desde meta/env.

    Prioridad:
      1. meta['lat']/meta['lon']
      2. WEATHER_BEACON_LOCATIONS por nombre de ciudad
      3. WEATHER_BEACON_DEFAULT_LAT/LON
      4. Zaragoza por defecto
    """
    meta = meta or {}
    city = str(
        meta.get("city")
        or meta.get("location")
        or os.getenv("WEATHER_BEACON_DEFAULT_CITY", "Zaragoza")
        or "Zaragoza"
    ).strip() or "Zaragoza"

    tz_name = str(meta.get("timezone") or os.getenv("WEATHER_BEACON_TZ", "Europe/Madrid") or "Europe/Madrid").strip()

    lat = _safe_float(meta.get("lat") or meta.get("latitude"))
    lon = _safe_float(meta.get("lon") or meta.get("longitude"))
    if lat is not None and lon is not None:
        return city, float(lat), float(lon), tz_name

    locs = _parse_locations_env()
    found = locs.get(city.lower())
    if found:
        return city, float(found[0]), float(found[1]), tz_name

    default_lat = _env_float("WEATHER_BEACON_DEFAULT_LAT", 41.6488)
    default_lon = _env_float("WEATHER_BEACON_DEFAULT_LON", -0.8891)
    return city, default_lat, default_lon, tz_name


def _local_hhmm(tz_name: str) -> str:
    """Devuelve HH:MM local con fallback seguro si falta zoneinfo."""
    try:
        if ZoneInfo:
            return datetime.now(ZoneInfo(tz_name)).strftime("%H:%M")
    except Exception:
        pass
    return datetime.now().strftime("%H:%M")


def fetch_weather_now(city: str, lat: float, lon: float, tz_name: str) -> WeatherBeaconData:
    """
    Consulta Open-Meteo y devuelve datos actuales normalizados.

    Parámetros:
      city: nombre legible que aparecerá en el mensaje.
      lat/lon: coordenadas decimales.
      tz_name: zona horaria IANA, por ejemplo Europe/Madrid.

    Variables consultadas:
      - temperature_2m
      - relative_humidity_2m
      - weather_code
    """
    cache_sec = max(0, _env_int("WEATHER_BEACON_CACHE_SEC", 600))
    cache_key = f"{city.lower()}|{lat:.5f}|{lon:.5f}|{tz_name}"
    now = time.time()
    cached = _CACHE.get(cache_key)
    if cached and cache_sec > 0 and (now - cached[0]) <= cache_sec:
        return cached[1]

    params = {
        "latitude": f"{float(lat):.6f}",
        "longitude": f"{float(lon):.6f}",
        "current": "temperature_2m,relative_humidity_2m,weather_code",
        "timezone": tz_name,
    }
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
    timeout = max(2, _env_int("WEATHER_BEACON_TIMEOUT_SEC", 8))

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "MeshNetBot-WeatherBeacon/1.0"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec - URL fija de API pública configurable solo en query
        raw = resp.read(128 * 1024).decode("utf-8", errors="replace")
    data = json.loads(raw)
    current = data.get("current") or {}

    temp = _safe_float(current.get("temperature_2m"))
    hum = _safe_int(current.get("relative_humidity_2m"))
    code = _safe_int(current.get("weather_code"))
    observed_time = str(current.get("time") or "")

    result = WeatherBeaconData(
        city=city,
        temperature_c=temp,
        relative_humidity_pct=hum,
        weather_code=code,
        weather_state_es=_weather_code_to_es(code),
        observed_time=observed_time,
        local_time_hhmm=_local_hhmm(tz_name),
    )
    _CACHE[cache_key] = (now, result)
    return result


def build_weather_beacon_text_from_meta(meta: Dict[str, Any]) -> str:
    """
    Construye el texto final de la baliza desde los metadatos de la tarea.

    Plantilla disponible:
      {hora}      HH:MM local
      {ciudad}    ciudad
      {temp}      temperatura con coma española
      {humedad}   humedad relativa entera
      {estado}    texto meteorológico breve

    Ejemplo:
      Son las 12:00 h. La temperatura en Zaragoza es de 24,3 °C, humedad 38% y despejado.
    """
    meta = meta or {}
    city, lat, lon, tz_name = resolve_location_from_meta(meta)
    weather = fetch_weather_now(city, lat, lon, tz_name)

    template = str(
        meta.get("template")
        or os.getenv(
            "WEATHER_BEACON_TEMPLATE",
            "Son las {hora} h. La temperatura en {ciudad} es de {temp} °C, humedad {humedad}% y {estado}.",
        )
    )

    temp_txt = _normalizar_decimal_es(weather.temperature_c, digits=1)
    humedad_txt = "?" if weather.relative_humidity_pct is None else str(int(weather.relative_humidity_pct))

    text = template.format(
        hora=weather.local_time_hhmm,
        ciudad=weather.city,
        temp=temp_txt,
        humedad=humedad_txt,
        estado=weather.weather_state_es,
    )

    # Normalización ligera de espacios, sin tocar acentos.
    return " ".join(str(text).replace("\r", " ").replace("\n", " ").split()).strip()


if __name__ == "__main__":
    # Prueba manual:
    #   python weather_beacon.py
    print(build_weather_beacon_text_from_meta({"city": "Zaragoza"}))
