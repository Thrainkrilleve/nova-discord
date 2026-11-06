#!/bin/sh
# Fetch the whisper model once, then serve. The EVE vocabulary prompt comes from Nova with each request.
set -eu
MODEL="/models/${WHISPER_MODEL}"
if [ ! -f "$MODEL" ]; then
    echo "downloading ${WHISPER_MODEL}..."
    curl -fL --retry 3 -o "$MODEL.part" "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/${WHISPER_MODEL}"
    mv "$MODEL.part" "$MODEL"
fi
exec whisper-server -m "$MODEL" -l auto -ng -t "${WHISPER_THREADS}" --host 0.0.0.0 --port 8003
