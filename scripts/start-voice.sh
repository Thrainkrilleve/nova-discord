#!/usr/bin/env bash
# Start Nova's voice services (Ubuntu server). Ctrl+C stops both.
#   speech to text: whisper.cpp server on :8003   text to speech: pocket-tts on :8002
# One-time setup:
#   whisper.cpp: git clone https://github.com/ggml-org/whisper.cpp && cmake -B build && cmake --build build -j
#                (add -DGGML_HIP=ON for the 7800 XT; -ng below keeps it on CPU, which is plenty for chat clips)
#   model:       curl -L -o ~/.nova/models/whisper/ggml-small-q5_1.bin \
#                  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small-q5_1.bin
#   pocket-tts:  pip install pocket-tts   (in the same venv as the NLLB service; CPU torch is fine)
set -euo pipefail

WHISPER="${WHISPER_SERVER:-whisper-server}"
MODEL="${WHISPER_MODEL:-$HOME/.nova/models/whisper/ggml-small-q5_1.bin}"
POCKET="${POCKET_TTS:-pocket-tts}"

"$WHISPER" -m "$MODEL" -l auto -ng -t "${WHISPER_THREADS:-8}" --host 127.0.0.1 --port 8003 &
STT=$!
"$POCKET" serve --host 127.0.0.1 --port 8002 &
TTS=$!
trap 'kill $STT $TTS 2>/dev/null' EXIT INT TERM
echo "whisper (pid $STT) on :8003, pocket-tts (pid $TTS) on :8002"
wait
