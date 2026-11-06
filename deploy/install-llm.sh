#!/usr/bin/env bash
# Install Nova's language model server on the Ubuntu host (not in Docker: Docker Desktop runs containers
# in a VM that can't reach the GPU). llama.cpp's prebuilt Vulkan build, pinned and checksum-verified,
# run by systemd as the service "nova-llm" on 127.0.0.1:8780 (8080 is often taken on a busy server).
#
#   sudo deploy/install-llm.sh                 # from the repo folder; models already in /opt/nova/models
#
# Re-running it is safe (it updates the files and restarts the service).
set -euo pipefail

LLAMA_BUILD=b11681
LLAMA_ASSET="llama-${LLAMA_BUILD}-bin-ubuntu-vulkan-x64.tar.gz"
LLAMA_SHA256=5f4e8b45b27ad60cc328ce45334ffeaf8c27d1a692d324caa1d1fd06071e7241

MODELS_DIR="${NOVA_MODELS_DIR:-/opt/nova/models}"
MODEL_FILE="${NOVA_MODEL_FILE:-Qwen3.5-9B-The-Defiant-Fable-Uncnr-Heretic-plusIQ-TOOLS-NEO-MAX-MTP-Q8_0.gguf}"
MMPROJ_FILE="${NOVA_MMPROJ_FILE:-mmproj-F16.gguf}"
INSTALL_DIR=/opt/nova/llama.cpp
PORT="${NOVA_LLM_PORT:-8780}"
RUN_AS="${SUDO_USER:-$(id -un)}"

[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }
for f in "$MODEL_FILE" "$MMPROJ_FILE"; do
    [[ -f "$MODELS_DIR/$f" ]] || { echo "missing $MODELS_DIR/$f (copy the models first)"; exit 1; }
done

# The port must be free (nova-llm itself may hold it from an earlier run: that's fine)
systemctl stop nova-llm 2>/dev/null || true
if ss -ltn "sport = :$PORT" | grep -q LISTEN; then
    echo "port $PORT is already in use by:"; ss -ltnp "sport = :$PORT"
    echo "pick another: sudo NOVA_LLM_PORT=8790 deploy/install-llm.sh (and set NOVA_LLM_PORT=8790 in .env)"
    exit 1
fi

echo "== Vulkan drivers"
# Ubuntu's own updater may hold the package lock for a while: wait for it rather than fail
while fuser /var/lib/apt/lists/lock /var/lib/dpkg/lock-frontend >/dev/null 2>&1; do
    echo "   waiting for another package install (Software Updater) to finish..."
    sleep 10
done
apt-get -o DPkg::Lock::Timeout=600 update -qq
apt-get -o DPkg::Lock::Timeout=600 install -y -qq libvulkan1 mesa-vulkan-drivers vulkan-tools curl ca-certificates >/dev/null
usermod -aG render,video "$RUN_AS"

echo "== llama.cpp ${LLAMA_BUILD}"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
curl -fsSL -o "$tmp/$LLAMA_ASSET" "https://github.com/ggml-org/llama.cpp/releases/download/${LLAMA_BUILD}/${LLAMA_ASSET}"
echo "${LLAMA_SHA256}  $tmp/$LLAMA_ASSET" | sha256sum -c --quiet
mkdir -p "$tmp/x" && tar -xzf "$tmp/$LLAMA_ASSET" -C "$tmp/x"
server=$(find "$tmp/x" -type f -name llama-server | head -1)
[[ -n "$server" ]] || { echo "llama-server not found in the download"; exit 1; }
rm -rf "$INSTALL_DIR" && mkdir -p "$INSTALL_DIR"
cp -a "$(dirname "$server")"/. "$INSTALL_DIR"/
chmod +x "$INSTALL_DIR/llama-server"

echo "== settings (/etc/nova-llm.env)"
cat > /etc/nova-llm.env <<EOF
# Read by nova-llm.service. Change, then: sudo systemctl restart nova-llm
MODEL=${MODELS_DIR}/${MODEL_FILE}
MMPROJ=${MODELS_DIR}/${MMPROJ_FILE}
# Context is shared by the parallel slots; keep CONTEXT/PARALLEL >= 12288
CONTEXT=49152
PARALLEL=4
# 127.0.0.1 = only this machine. Docker Desktop reaches it as host.docker.internal.
HOST=127.0.0.1
# Multi-token prediction (faster). If the service won't start, set SPEC=none
SPEC=draft-mtp
# If you change it, set NOVA_LLM_PORT to match in ~/nova/.env
PORT=${PORT}
EOF

echo "== service"
sed "s/@USER@/${RUN_AS}/" "$(dirname "$0")/nova-llm.service" > /etc/systemd/system/nova-llm.service
systemctl daemon-reload
systemctl enable --now nova-llm
systemctl restart nova-llm

echo "== waiting for the model to load"
for _ in $(seq 1 60); do
    if curl -fs "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
        echo "OK: nova-llm is up on 127.0.0.1:${PORT}. GPU it found:"
        journalctl -u nova-llm -n 300 --no-pager | grep -i -m3 "vulkan" || echo "  (no Vulkan line: check 'vulkaninfo --summary')"
        exit 0
    fi
    systemctl is-active --quiet nova-llm || break
    sleep 5
done
echo "nova-llm didn't come up. Last log lines:"
journalctl -u nova-llm -n 40 --no-pager
exit 1
