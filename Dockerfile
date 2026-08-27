# Keep this in step with .python-version and the workflows.
ARG PYTHON_VERSION=3.13

# python:<v>-slim publishes both linux/amd64 and linux/arm64, so the same
# Dockerfile builds on an Intel server, an Apple Silicon Mac and a Raspberry Pi.
FROM python:${PYTHON_VERSION}-slim

# Run as an unprivileged user. The state directories below are chowned to it,
# because a non-root process cannot create directories under /app at runtime.
RUN useradd --create-home --shell /usr/sbin/nologin botuser

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Dependencies first, so a source-only change reuses this layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=botuser:botuser core/ ./core/
COPY --chown=botuser:botuser cogs/ ./cogs/
COPY --chown=botuser:botuser resources/ ./resources/
COPY --chown=botuser:botuser main.py ./

# Mount points for state. archives/ is included: /archive writes there, and
# without this the non-root process cannot create it under /app.
RUN mkdir -p /app/databases /app/logs /app/archives \
    && chown -R botuser:botuser /app/databases /app/logs /app/archives

USER botuser

# Liveness from the heartbeat the bot writes once a minute while its event loop
# is running. Log freshness was the wrong signal: a quiet bot legitimately logs
# nothing for hours, which reported unhealthy and could trigger restart loops.
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import pathlib,sys,time; p=pathlib.Path('/app/logs/heartbeat'); sys.exit(0 if p.is_file() and time.time()-p.stat().st_mtime < 180 else 1)"]

CMD ["python", "-u", "main.py"]
