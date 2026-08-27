FROM python:3.13-slim

# Run as an unprivileged user; the previous image ran the bot as root.
RUN useradd --create-home --shell /usr/sbin/nologin botuser

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=botuser:botuser core/ ./core/
COPY --chown=botuser:botuser cogs/ ./cogs/
COPY --chown=botuser:botuser resources/ ./resources/
COPY --chown=botuser:botuser main.py ./

# Mount points for state; owned by botuser so the bot can write to them.
RUN mkdir -p /app/databases /app/logs && chown -R botuser:botuser /app/databases /app/logs

USER botuser

# Fails the container if the process stops writing its log, which is how a
# wedged event loop presents.
HEALTHCHECK --interval=60s --timeout=10s --start-period=45s --retries=3 \
    CMD python -c "import pathlib,time,sys; p=pathlib.Path('/app/logs/bot.log'); sys.exit(0 if p.exists() and time.time()-p.stat().st_mtime < 3600 else 1)"

CMD ["python", "-u", "main.py"]
