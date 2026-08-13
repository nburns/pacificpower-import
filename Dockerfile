# The Playwright Python image ships with Chromium + all system deps preinstalled,
# which sidesteps the Alpine/musl compatibility issues with the HA base images.
# The add-on can still read `/data/options.json` (populated by supervisor) directly.
ARG BUILD_FROM=mcr.microsoft.com/playwright/python:v1.62.0-noble
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends jq curl ca-certificates tzdata gosu \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast package installation.
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && mv /root/.local/bin/uv /usr/local/bin/uv

# Install supercronic for in-container scheduling.
ARG SUPERCRONIC_VERSION=v0.2.48
RUN ARCH="$(dpkg --print-architecture)"; \
    case "${ARCH}" in \
      amd64) SC_ARCH=amd64 ;; \
      arm64) SC_ARCH=arm64 ;; \
      *) echo "unsupported arch ${ARCH}"; exit 1 ;; \
    esac; \
    curl -fsSLo /usr/local/bin/supercronic \
      "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${SC_ARCH}" \
    && chmod +x /usr/local/bin/supercronic

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv pip install --system --no-cache .

COPY run.sh /run.sh
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /run.sh /entrypoint.sh \
    && chown -R pwuser:pwuser /app

# Entrypoint runs as root just long enough to fix /data ownership
# (supervisor mounts it as root) then drops to pwuser via gosu.
CMD ["/entrypoint.sh"]
