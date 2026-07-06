# Use Microsoft Playwright base image — already has Chromium + system deps.
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8090

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Persistent volume for browser session/cookies (mounted at runtime).
RUN mkdir -p /data && chmod 0777 /data
ENV SECOP_STORAGE_STATE_PATH=/data/storage_state.json

EXPOSE 8090

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8090} --workers 1"]
