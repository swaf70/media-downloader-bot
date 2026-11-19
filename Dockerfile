FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    STORAGE_DIR=/data/storage

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg ca-certificates libgomp1 libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot

RUN mkdir -p /data/storage

EXPOSE 10000

CMD ["python", "-m", "bot"]
