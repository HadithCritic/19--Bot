# syntax=docker/dockerfile:1

# Keep this in step with .python-version, docker-compose.yml and the workflows.
ARG PYTHON_VERSION=3.13

# ---------------------------------------------------------------------------
# Stage 1: build the dependency tree.
#
# aiohttp and discord.py ship native extensions. Wheels normally exist, but on
# an architecture without them pip falls back to compiling, which needs a C
# toolchain. Doing that here keeps gcc out of the final image.
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# build-essential is only present in this stage.
RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential \
    && rm -rf /var/lib/apt/lists/*

# A self-contained virtualenv is the unit copied into the runtime stage.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Fail the build rather than the deploy if a dependency did not install.
RUN python -c "import discord, aiohttp, aiosqlite, feedparser, dotenv, zoneinfo; \
zoneinfo.ZoneInfo('America/New_York'); \
print('dependency check OK:', discord.__version__)"


# ---------------------------------------------------------------------------
# Stage 2: runtime. No compilers, no pip cache, no source of the build stage.
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

LABEL org.opencontainers.image.title="19 Bot" \
      org.opencontainers.image.description="Moderation and utility Discord bot for The Submission Server" \
      org.opencontainers.image.source="https://github.com/HadithCritic/19--Bot" \
      org.opencontainers.image.licenses="NOASSERTION"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# Unprivileged user. The state directories are chowned to it below, because a
# non-root process cannot create directories under /app at runtime.
RUN useradd --create-home --shell /usr/sbin/nologin --uid 10001 botuser

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

COPY --chown=botuser:botuser core/ ./core/
COPY --chown=botuser:botuser cogs/ ./cogs/
COPY --chown=botuser:botuser resources/ ./resources/
COPY --chown=botuser:botuser main.py ./

# Mount points for state. archives/ is included because /archive writes there.
RUN mkdir -p /app/databases /app/logs /app/archives \
    && chown -R botuser:botuser /app/databases /app/logs /app/archives

USER botuser

# Liveness from the heartbeat the bot writes once a minute while its event loop
# is running. Log freshness was the wrong signal: a quiet bot legitimately logs
# nothing for hours, which reported unhealthy and could trigger restart loops.
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import pathlib,sys,time; p=pathlib.Path('/app/logs/heartbeat'); sys.exit(0 if p.is_file() and time.time()-p.stat().st_mtime < 180 else 1)"]

# No ports: the bot is an outbound gateway client and listens for nothing.
CMD ["python", "-u", "main.py"]
