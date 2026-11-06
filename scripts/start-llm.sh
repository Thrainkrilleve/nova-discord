#!/usr/bin/env bash
# Start llama-server with a GGUF model for Nova (Ubuntu server, ROCm or Vulkan build).
#   scripts/start-llm.sh /path/to/model.gguf [context]
# MTP=1 turns on multi-token-prediction speculative decoding for models that ship an MTP head.
set -euo pipefail

MODEL="${1:?usage: $0 /path/to/model.gguf [context]}"
CONTEXT="${2:-16384}"
SERVER="${LLAMA_SERVER:-llama-server}"

# --jinja enables the model's own chat template, which tool calling needs.
# -ngl 99 puts every layer on the GPU; lower it if the model does not fit in 16 GB.
ARGS=(-m "$MODEL" --jinja -ngl 99 -c "$CONTEXT" --host 127.0.0.1 --port "${PORT:-8080}")
if [[ "${MTP:-0}" == "1" ]]; then
    ARGS+=(--spec-type draft-mtp --spec-draft-n-max 2)
fi
# MMPROJ=/path/to/mmproj-F16.gguf enables image input
if [[ -n "${MMPROJ:-}" ]]; then
    ARGS+=(--mmproj "$MMPROJ")
fi
exec "$SERVER" "${ARGS[@]}"
