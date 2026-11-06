# Text to speech: pocket-tts on CPU (about 1 s a sentence). Voices download on first use into hf-cache.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/cache/hf HF_HUB_DISABLE_TELEMETRY=1
# Exact versions from deploy/locks (CPU torch from PyTorch's own index), prebuilt wheels only
COPY deploy/locks/tts.txt /tmp/lock.txt
RUN pip install --only-binary=:all: --extra-index-url https://download.pytorch.org/whl/cpu -r /tmp/lock.txt

RUN useradd --create-home --uid 1000 tts && mkdir -p /cache/hf && chown -R tts /cache
USER tts
EXPOSE 8002
CMD ["pocket-tts", "serve", "--host", "0.0.0.0", "--port", "8002"]
