FROM python:3.11.8-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Madrid

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tzdata iputils-ping \
    build-essential python3-dev gfortran pkg-config \
    libopenblas-dev liblapack-dev \
    libssl-dev libffi-dev \
    rustc cargo \
    && rm -rf /var/lib/apt/lists/*

# Importante en buildx/multi-arch: asegurar wheel/pip modernos
RUN python -m pip install --upgrade pip setuptools wheel


WORKDIR /app

# Instala dependencias por capas (mejor caché)
# Capa GEO independiente (solo se recompila si cambias este archivo)
# Instala dependencias por capas (mejor caché)
ARG INSTALL_GEO=0

COPY requirements/ /app/requirements/

# 1) Base (rápido y estable)
RUN python -m pip install --no-cache-dir \
    -r /app/requirements/requirements.txt \
    -r /app/requirements/requirements.base.txt \
    -r /app/requirements/requirements.bot.txt

# 2) GEO (lento, opcional)
RUN if [ "$INSTALL_GEO" = "1" ]; then \
      python -m pip install --no-cache-dir -r /app/requirements/requirements.geo.txt ; \
    else \
      echo "Skipping GEO requirements (INSTALL_GEO=0)" ; \
    fi


# Código
COPY source/*.py /app/source/
COPY docker/entrypoint_broker.sh /usr/local/bin/
COPY docker/entrypoint_bot.sh    /usr/local/bin/
RUN chmod +x /usr/local/bin/entrypoint_broker.sh /usr/local/bin/entrypoint_bot.sh

VOLUME ["/app/bot_data"]
#USER nobody

EXPOSE 8765 8766
CMD ["python", "--version"]

