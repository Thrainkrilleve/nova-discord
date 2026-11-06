# Nova herself: the Discord bot and the chat-app API in one process.
# Built from the repo root:  docker compose -f deploy/docker-compose.yml build nova
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

# Dependencies first, at the exact versions in deploy/locks (prebuilt wheels only: nothing compiles),
# so code changes don't reinstall them. imageio-ffmpeg brings its own ffmpeg.
COPY deploy/locks/nova.txt /tmp/lock.txt
RUN pip install --only-binary=:all: -r /tmp/lock.txt
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-deps .

# Config comes in as a mounted nova.toml; data (memory, files, SDE) lives in the /app/var volume
RUN useradd --create-home --uid 1000 nova && mkdir -p /app/var && chown nova /app/var
USER nova

EXPOSE 8740
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s CMD \
    python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8740/health', timeout=4)" || exit 1
CMD ["nova", "run"]
