FROM python:3.11-slim

ARG DEBIAN_MIRROR=deb.debian.org

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

RUN set -eux; \
    if [ "$DEBIAN_MIRROR" != "deb.debian.org" ]; then \
        for source_file in /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources; do \
            if [ -f "$source_file" ]; then \
                sed -i \
                    -e "s|https\?://deb.debian.org/debian-security|https://${DEBIAN_MIRROR}/debian-security|g" \
                    -e "s|https\?://deb.debian.org/debian|https://${DEBIAN_MIRROR}/debian|g" \
                    "$source_file"; \
            fi; \
        done; \
    fi

RUN set -eux; \
    installed=0; \
    for attempt in 1 2 3; do \
        if apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 update \
            && apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 install -y --no-install-recommends ffmpeg; then \
            installed=1; \
            break; \
        fi; \
        apt-get -o Acquire::Retries=3 -f install -y || true; \
        rm -rf /var/lib/apt/lists/*; \
        sleep $((attempt * 3)); \
    done; \
    test "$installed" = 1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin seeit \
    && mkdir -p /data/uploads /data/models \
    && chown -R seeit:seeit /data

ARG ROCKETMQ_CLIENT_VERSION=2.0.0
ARG ROCKETMQ_CLIENT_SHA256=d8a97b5aed30559a6bffe846835f0de39c6cb3f051b9ef665e461e1111ddd785

COPY vendor /tmp/rocketmq-vendor

RUN set -eux; \
    installed=0; \
    for attempt in 1 2 3; do \
        if apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 update \
            && apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 install -y --no-install-recommends ca-certificates curl; then \
            installed=1; \
            break; \
        fi; \
        apt-get -o Acquire::Retries=3 -f install -y || true; \
        rm -rf /var/lib/apt/lists/*; \
        sleep $((attempt * 3)); \
    done; \
    test "$installed" = 1 \
    && if [ -f /tmp/rocketmq-vendor/rocketmq-client.deb ]; then \
        cp /tmp/rocketmq-vendor/rocketmq-client.deb /tmp/rocketmq-client.deb; \
    else \
        curl --fail --location --retry 3 --retry-all-errors --connect-timeout 10 --max-time 180 \
            --output /tmp/rocketmq-client.deb \
            "https://github.com/apache/rocketmq-client-cpp/releases/download/${ROCKETMQ_CLIENT_VERSION}/rocketmq-client-cpp-${ROCKETMQ_CLIENT_VERSION}.amd64.deb"; \
    fi \
    && echo "${ROCKETMQ_CLIENT_SHA256}  /tmp/rocketmq-client.deb" | sha256sum --check --strict \
    && dpkg --install /tmp/rocketmq-client.deb \
    && ldconfig \
    && rm -f /tmp/rocketmq-client.deb \
    && rm -rf /tmp/rocketmq-vendor \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt requirements-rocketmq.txt ./
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --index-url "$PIP_INDEX_URL" -r requirements-rocketmq.txt
COPY alembic.ini ./
COPY alembic ./alembic
COPY docker-entrypoint.sh ./docker-entrypoint.sh
COPY seeit ./seeit
RUN chmod 755 /app/docker-entrypoint.sh \
    && chown -R seeit:seeit /app

USER seeit
STOPSIGNAL SIGTERM

CMD ["/app/docker-entrypoint.sh"]
