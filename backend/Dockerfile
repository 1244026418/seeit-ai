FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tesseract-ocr tesseract-ocr-chi-sim \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin seeit \
    && mkdir -p /data/uploads \
    && chown -R seeit:seeit /data

WORKDIR /app
COPY requirements.txt requirements-rocketmq.txt ./
RUN pip install --no-cache-dir -r requirements-rocketmq.txt
COPY alembic.ini ./
COPY alembic ./alembic
COPY seeit ./seeit
RUN chown -R seeit:seeit /app

USER seeit
STOPSIGNAL SIGTERM

CMD ["sh", "-c", "alembic upgrade head && uvicorn seeit.main:app --host 0.0.0.0 --port 9090"]
