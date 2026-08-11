FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Madrid

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tzdata iputils-ping \
    build-essential python3-dev pkg-config \
    libssl-dev libffi-dev \
    rustc cargo \
    && rm -rf /var/lib/apt/lists/*

# Importante en buildx/multi-arch: asegurar wheel/pip modernos
RUN python -m pip install --upgrade pip setuptools wheel


WORKDIR /app

# v7.0.53: dependencias Python sin reverse_geocoder/SciPy.
# Se mantienen las capas funcionales existentes; la geolocalización de nodos
# reutiliza el resolver IGN/INE ligero incluido en geo_admin.py.
COPY requirements/ /app/requirements/
RUN python -m pip install --no-cache-dir \
    -r /app/requirements/requirements.txt \
    -r /app/requirements/requirements.base.txt \
    -r /app/requirements/requirements.bot.txt

# Código principal del broker/bot.
COPY source/*.py /app/source/
COPY source/*.json /app/source/

# v7.0.53: mínimo subconjunto geográfico compartido con Emergencias.
# Telegram_Bot_Broker.py continúa usando `import reverse_geocoder as rg`, pero
# /app/source/reverse_geocoder.py es ahora un shim ligero que delega en estas
# funciones ya validadas. Se copia también la cartografía provincial para el
# fallback local si el servicio IGN de núcleos no está disponible.
COPY tools/emergencias_guardia/emergencias/__init__.py /app/tools/emergencias_guardia/emergencias/__init__.py
COPY tools/emergencias_guardia/emergencias/models.py /app/tools/emergencias_guardia/emergencias/models.py
COPY tools/emergencias_guardia/emergencias/geo_admin.py /app/tools/emergencias_guardia/emergencias/geo_admin.py
COPY tools/emergencias_guardia/data/provincias_espana.geojson /app/tools/emergencias_guardia/data/provincias_espana.geojson

COPY docker/entrypoint_broker.sh /usr/local/bin/
COPY docker/entrypoint_bot.sh    /usr/local/bin/
RUN chmod +x /usr/local/bin/entrypoint_broker.sh /usr/local/bin/entrypoint_bot.sh

VOLUME ["/app/bot_data"]
#USER nobody

EXPOSE 8765 8766
CMD ["python", "--version"]
