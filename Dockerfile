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
# to that header — the leftmost hop is fully attacker-controlled. That forged
# address would flow into every get_remote_address-keyed rate limit (auth
# login/register, agent_chat, configuracion, the global default) and into the audit
# log's request.client.host.
#
# What omitting the flag actually does (uvicorn defaults to forwarded_allow_ips
# '127.0.0.1' and --proxy-headers is already on): Railway's edge proxy is not the
# container's loopback peer, so ProxyHeadersMiddleware skips, X-Forwarded-For is
# ignored, and request.client.host is the PROXY's IP for every request. Every
# IP-keyed limiter therefore shares one platform-wide bucket and the audit log
# records no client IPs. That is the behaviour production has always had (prod base
# e2af658) — it is NOT a regression introduced here, but it is not correct either.
#
# The correct value is the Railway proxy's address range, not '*': uvicorn walks
# X-Forwarded-For right-to-left skipping trusted hops, which is not spoofable —
# only '*' short-circuits to the leftmost hop. Pending verification of that range
# with Railway, so no value is set here. The FORWARDED_ALLOW_IPS environment
# variable overrides this at runtime and setting it to '*' reinstates the spoof.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
