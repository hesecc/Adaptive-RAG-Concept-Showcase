#!/usr/bin/env bash
# Cold snapshot: only an-rag/api is stopped. An EXIT trap restores its state.
set -Eeuo pipefail
umask 077
ROOT=/opt/adaptive-rag
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
COMPOSE=(docker compose --project-name an-rag --env-file "$ROOT/deployment.env" -f "$ROOT/compose.yaml")

fail() { printf '%s\n' "$1" >&2; exit 1; }
[[ $# == 0 ]] || fail 'backup.sh takes no arguments'
[[ -d "$ROOT" && ! -L "$ROOT" && $(realpath -- "$ROOT") == "$ROOT" ]] || fail 'Invalid managed root'
[[ -f "$ROOT/.managed" && ! -L "$ROOT/.managed" ]] || fail 'Missing managed marker'
[[ $(cat -- "$ROOT/.managed") == 'an-rag:standalone:v1' ]] || fail 'Wrong managed marker'
for path in config data secrets tls backups; do
    [[ -d "$ROOT/$path" && ! -L "$ROOT/$path" && $(realpath -- "$ROOT/$path") == "$ROOT/$path" ]] || fail 'Unsafe or missing managed directory'
done
for path in deployment.env compose.yaml; do
    [[ -f "$ROOT/$path" && ! -L "$ROOT/$path" ]] || fail 'Unsafe or missing deployment file'
done
[[ -f "$SCRIPT_DIR/snapshot.py" && ! -L "$SCRIPT_DIR/snapshot.py" ]] || fail 'Missing snapshot helper'

# All release/backup operations must hold this same project-specific lock.
[[ ! -L "$ROOT/.maintenance.lock" && ( ! -e "$ROOT/.maintenance.lock" || -f "$ROOT/.maintenance.lock" ) ]] || fail 'Unsafe maintenance lock'
exec 9>"$ROOT/.maintenance.lock"
flock -n 9 || fail 'Another an-rag maintenance operation is active'

was_running=0
restart_needed=0
finish() {
    local status=$?
    trap - EXIT INT TERM
    if (( restart_needed )); then
        if ! "${COMPOSE[@]}" up -d --no-recreate --wait --wait-timeout 180 api >/dev/null; then
            printf '%s\n' 'CRITICAL: could not restore an-rag/api; run the scoped Compose up --no-recreate command' >&2
            status=1
        fi
    fi
    exit "$status"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

"${COMPOSE[@]}" config --quiet
container_id=$("${COMPOSE[@]}" ps --all --quiet api)
[[ "$container_id" =~ ^[0-9a-f]{12,64}$ ]] || fail 'Expected exactly one existing an-rag/api container'
state=$(docker inspect --format '{{.State.Status}}' "$container_id")
[[ "$state" =~ ^(running|restarting|exited|created|dead)$ ]] || fail 'Unsupported or paused application state'
if [[ "$state" == running || "$state" == restarting ]]; then
    was_running=1
    # Set before stop: even a partial/failed stop must attempt to restore it.
    restart_needed=1
    "${COMPOSE[@]}" stop --timeout 150 api >/dev/null
fi
state=$(docker inspect --format '{{.State.Status}}' "$container_id")
[[ "$state" == exited || "$state" == created || "$state" == dead ]] || fail 'App is still active; no snapshot created'
image_id=$("${COMPOSE[@]}" images -q api)
[[ "$image_id" =~ ^(sha256:)?[0-9a-f]{12,64}$ ]] || fail 'Expected one immutable API image ID'
destination=$(mktemp -d "$ROOT/backups/.partial-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXXXX")
python3 "$SCRIPT_DIR/snapshot.py" create "$destination" --image-id "$image_id"
final="$ROOT/backups/${destination##*/.partial-}"
[[ ! -e "$final" ]] || fail 'Backup target already exists'
mv -T -- "$destination" "$final"
printf 'Backup verified: %s (app_was_running=%s)\n' "$final" "$was_running"
