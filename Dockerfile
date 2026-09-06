# ── Production image ──
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    libmagic1 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libffi-dev \
    libcairo2 \
    libarchive-tools && \
    rm -rf /var/lib/apt/lists/*

RUN groupadd -r cashin && useradd -r -g cashin -d /app -s /sbin/nologin cashin

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chown -R cashin:cashin /app

USER cashin

EXPOSE 8000

# No --forwarded-allow-ips here, and never '*': with `always_trust` uvicorn takes
# the LEFTMOST X-Forwarded-For hop as the client address, and a proxy only APPENDS
# to that header — the leftmost hop is fully attacker-controlled.
#
# The real client IP is resolved in-app instead: `app/core/client_ip.py`
# (`TrustedProxyClientMiddleware`) reads X-Real-IP first, then walks
# X-Forwarded-For RIGHT TO LEFT for the first public address — spoof-proof
# under both "Railway strips-and-sets the header" and "Railway appends to it"
# hypotheses. It activates automatically under `RAILWAY_ENVIRONMENT` (which
# Railway injects into every deployment) via `TRUST_PROXY_HEADERS`; local dev
# stays off. The `FORWARDED_ALLOW_IPS` environment variable must stay unset —
# uvicorn's own proxy-header handling is not used at all.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
