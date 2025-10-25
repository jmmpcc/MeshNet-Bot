FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Madrid

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tzdata iputils-ping \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Instala dependencias por capas (mejor caché)
COPY requirements.base.txt /app/
COPY requirements.bot.txt  /app/
RUN pip install --no-cache-dir -r /app/requirements.base.txt -r /app/requirements.bot.txt

# Código
COPY *.py /app/
COPY docker/entrypoint_broker.sh /usr/local/bin/
COPY docker/entrypoint_bot.sh    /usr/local/bin/
RUN chmod +x /usr/local/bin/entrypoint_broker.sh /usr/local/bin/entrypoint_bot.sh

VOLUME ["/app/bot_data"]
#USER nobody

EXPOSE 8765 8766
CMD ["python", "--version"]

