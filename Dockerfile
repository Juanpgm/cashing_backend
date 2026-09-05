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

# --proxy-headers / --forwarded-allow-ips: Railway terminates TLS in front of the
# container, so without these every request looks like it came from the platform
# proxy — client IPs are lost from the audit log and IP-keyed rate limits collapse
# into a single global bucket. Trusting "*" is the documented setup when the only
# reachable path to the container is through the platform's own proxy.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
