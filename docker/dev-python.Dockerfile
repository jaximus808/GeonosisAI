# TDM-3: dev image for the Python services (uv projects, Python 3.11).
# Source is bind-mounted at /app; the venv lives in a named volume, so the
# host's macOS .venv is never touched.
FROM python:3.11-slim-bookworm
RUN apt-get update \
 && apt-get install -y --no-install-recommends make curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir "uv>=0.9"
COPY docker/dev-keepalive.sh /usr/local/bin/dev-keepalive
RUN chmod +x /usr/local/bin/dev-keepalive
ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.11 \
    PYTHONUNBUFFERED=1
WORKDIR /app
ENTRYPOINT ["dev-keepalive"]
