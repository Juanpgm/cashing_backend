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

# No --forwarded-allow-ips here: with `always_trust` uvicorn takes the LEFTMOST
# X-Forwarded-For hop as the client address, and a proxy only APPENDS to that
# header — the leftmost hop is fully attacker-controlled. That forged address
# would flow into every get_remote_address-keyed rate limit (auth login/register,
# agent_chat, configuracion, the global default) and into the audit log's
# request.client.host. --proxy-headers is uvicorn's default already, so omitting
# both flags here is a no-op change of behaviour vs before, not a new default.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
