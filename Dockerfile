# syntax=docker/dockerfile:1

# Keep this in step with .python-version, docker-compose.yml and the workflows.
ARG PYTHON_VERSION=3.13

# Alpine rather than Debian slim. Debian marks perl Essential, so it cannot be
# removed, and it carries two CRITICAL advisories with no upstream fix. Nothing
# here invokes perl, but the finding is permanent and unfixable on that base.
# Alpine has no perl at all. See docs/SECURITY.md for the full comparison.

# ---------------------------------------------------------------------------
# Stage 1: build the dependency tree.
#
# aiohttp and discord.py ship native extensions. musllinux wheels exist for
# both, but build-base is here so an architecture without them still builds.
# Either way the toolchain never reaches the runtime image.
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-alpine AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apk add --no-cache build-base

# A self-contained virtualenv is the unit copied into the runtime stage.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Fail the build rather than the deploy if a dependency did not install.
# zoneinfo is exercised because the bot schedules against America/New_York.
RUN python -c "import discord, aiohttp, aiosqlite, feedparser, dotenv, zoneinfo; \
zoneinfo.ZoneInfo('America/New_York'); \
print('dependency check OK:', discord.__version__)"

# Strip the installer toolchain from the venv that ships. The application
# imports none of it and it carries its own advisories: setuptools (path
# traversal) plus the msgpack that pip vendors (use-after-free). A runtime image
# has no business holding a package manager. Done after the import check so a
# genuine dependency failure is still caught above.
RUN pip uninstall --yes pip setuptools wheel || true
RUN find /opt/venv -maxdepth 4 \
      \( -name 'pip' -o -name 'pip-*' \
      -o -name 'setuptools' -o -name 'setuptools-*' \
      -o -name 'pkg_resources' \
      -o -name 'wheel' -o -name 'wheel-*' \) \
      -exec rm -rf {} + || true

# Prove the application still imports with the toolchain removed.
RUN python -c "import discord, aiohttp, aiosqlite, feedparser, dotenv; \
import importlib.util; \
assert importlib.util.find_spec('pip') is None, 'pip survived the strip'; \
assert importlib.util.find_spec('setuptools') is None, 'setuptools survived the strip'; \
print('runtime venv is clean')"


# ---------------------------------------------------------------------------
# Stage 2: runtime. No compilers, no package manager, no build-stage source.
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-alpine AS runtime

LABEL org.opencontainers.image.title="19 Bot" \
      org.opencontainers.image.description="Moderation and utility Discord bot for The Submission Server" \
      org.opencontainers.image.source="https://github.com/HadithCritic/19--Bot" \
      org.opencontainers.image.licenses="NOASSERTION"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# Apply outstanding Alpine security patches. The published base image lags its
# own security updates by days. This trades byte-for-byte reproducibility for
# being patched, which is the right way round for a long-running service. Pin
# the base image by digest instead if you need bit-identical rebuilds.
RUN apk upgrade --no-cache

# The base image ships its own pip in the system site-packages, plus the wheels
# ensurepip bundles. Both are dead weight once /opt/venv exists, and both are
# scanned: pip vendors the flagged msgpack, and the bundled wheels are where the
# flagged setuptools comes from. The interpreter is untouched; only creating new
# virtualenvs inside the container stops working, which nothing does.
RUN rm -rf /usr/local/lib/python3.13/site-packages/pip \
           /usr/local/lib/python3.13/site-packages/pip-*.dist-info \
           /usr/local/lib/python3.13/ensurepip \
    && rm -f /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.13

# Unprivileged user. The state directories are chowned to it below, because a
# non-root process cannot create directories under /app at runtime.
RUN adduser --disabled-password --no-create-home --uid 10001 botuser

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
