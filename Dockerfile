FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Madrid

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tzdata iputils-ping \
    build-essential python3-dev gfortran pkg-config \
    libopenblas-dev liblapack-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Instala dependencias por capas (mejor caché)
# Capa GEO independiente (solo se recompila si cambias este archivo)
COPY requirements/ /app/requirements/
RUN python -m pip install --no-cache-dir \
    -r /app/requirements/requirements.txt \
    -r /app/requirements/requirements.geo.txt \
    -r /app/requirements/requirements.base.txt \
    -r /app/requirements/requirements.bot.txt

# Código
COPY source/*.py /app/source
COPY docker/entrypoint_broker.sh /usr/local/bin/
COPY docker/entrypoint_bot.sh    /usr/local/bin/
RUN chmod +x /usr/local/bin/entrypoint_broker.sh /usr/local/bin/entrypoint_bot.sh

VOLUME ["/app/bot_data"]
#USER nobody

EXPOSE 8765 8766
CMD ["python", "--version"]

