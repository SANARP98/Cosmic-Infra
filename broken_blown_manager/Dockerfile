FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LIBRARY_DIR=/library \
    PROJECTS_DIR=/projects \
    STATE_DIR=/state \
    HEARTBEAT_TTL=30 \
    STOP_GRACE_PERIOD=5 \
    STATIC_DIR=/app/app/static \
    PORT=8002

RUN apt-get update && apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy backend + static UI
COPY app /app/app

RUN pip install --no-cache-dir \
    fastapi \
    uvicorn[standard] \
    pydantic \
    python-multipart

RUN mkdir -p /library /projects /state /state/backups /state/snapshots \
    && chmod -R 755 /state

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8002/api/health').read()" || exit 1

EXPOSE 8002

# Run the modular FastAPI app via uvicorn
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8002"]
