#!/usr/bin/env bash
# Load Nova's memory (made by scripts/export-for-server.ps1) into the stack's nova-data volume.
# Run from the repo folder after the images are built, before the first `up`:
#
#   deploy/import-data.sh ~/nova-transfer
#
# The transfer folder must be under your home folder (Docker Desktop only shares /home by default).
# Refuses to overwrite a memory that's already there unless FORCE=1.
set -euo pipefail

SRC="$(realpath "${1:?usage: $0 /path/to/nova-transfer}")"
[[ -f "$SRC/var/nova.db" ]] || { echo "no var/nova.db in $SRC"; exit 1; }
cd "$(dirname "$0")/.."
compose=(docker compose --env-file .env -f deploy/docker-compose.yml)

# Runs as Nova's own user in her own container definition, so the files land with the right owner
"${compose[@]}" run --rm --no-deps -v "$SRC/var:/in:ro" -e FORCE="${FORCE:-0}" --entrypoint sh nova -c '
    set -e
    if [ -f /app/var/nova.db ] && [ "$FORCE" != "1" ]; then
        echo "Nova already has a memory in this volume; FORCE=1 to replace it"; exit 1
    fi
    rm -f /app/var/nova.db-wal /app/var/nova.db-shm
    cp -R /in/. /app/var/
    python -c "import sqlite3; c=sqlite3.connect(\"/app/var/nova.db\"); print(\"memory check:\", c.execute(\"pragma integrity_check\").fetchone()[0], \"-\", c.execute(\"select count(*) from messages\").fetchone()[0], \"messages\")"
'
echo "Imported. Start the stack: ${compose[*]} up -d"
