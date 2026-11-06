# NLLB-200 translation service (services/nllb). CPU is fast enough (~2 s a message) and keeps the
# GPU for Nova's model. The model (~2.5 GB) downloads on first start into the hf-cache volume.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/cache/hf HF_HUB_DISABLE_TELEMETRY=1 \
    REQUIRE_GPU=false TRANSLATOR_API_HOST=0.0.0.0 TRANSLATOR_API_PORT=8001
WORKDIR /srv

# Exact versions from deploy/locks (CPU torch from PyTorch's own index), prebuilt wheels only
COPY deploy/locks/nllb.txt /tmp/lock.txt
RUN pip install --only-binary=:all: --extra-index-url https://download.pytorch.org/whl/cpu -r /tmp/lock.txt
COPY services/nllb/translator_api.py .

RUN useradd --create-home --uid 1000 nllb && mkdir -p /cache/hf && chown -R nllb /cache
USER nllb
EXPOSE 8001
CMD ["python", "translator_api.py"]
