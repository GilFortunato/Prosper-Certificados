FROM python:3.13-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PROSPER_DB_PATH=/data/certificados.sqlite3

RUN apt-get update && apt-get install -y --no-install-recommends \
      libreoffice-impress fonts-crosextra-carlito fonts-liberation fonts-dejavu-core tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home prosper \
    && install -d -o prosper -g prosper -m 0700 /data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY certificados ./certificados

USER prosper
VOLUME ["/data"]
EXPOSE 8000
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "6", "--timeout", "75", "--umask", "0o077", "--no-control-socket", "certificados:create_app()"]
