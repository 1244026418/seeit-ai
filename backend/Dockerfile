FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tesseract-ocr tesseract-ocr-chi-sim \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin seeit \
    && mkdir -p /data/uploads \
    && chown -R seeit:seeit /data

ARG ROCKETMQ_CLIENT_VERSION=2.0.0
ARG ROCKETMQ_CLIENT_SHA256=d8a97b5aed30559a6bffe846835f0de39c6cb3f051b9ef665e461e1111ddd785

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && curl --fail --location --retry 3 --retry-all-errors --connect-timeout 10 --max-time 180 \
        --output /tmp/rocketmq-client.deb \
        "https://github.com/apache/rocketmq-client-cpp/releases/download/${ROCKETMQ_CLIENT_VERSION}/rocketmq-client-cpp-${ROCKETMQ_CLIENT_VERSION}.amd64.deb" \
    && echo "${ROCKETMQ_CLIENT_SHA256}  /tmp/rocketmq-client.deb" | sha256sum --check --strict \
    && dpkg --install /tmp/rocketmq-client.deb \
    && ldconfig \
    && rm -f /tmp/rocketmq-client.deb \
    && rm -rf /var/lib/apt/lists/*

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
