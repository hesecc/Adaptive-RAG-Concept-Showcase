#!/usr/bin/env bash
# Invoked by the instance unit; only immutable container IDs are ever restarted.
set -eu -o pipefail
: "${STATE_DIRECTORY:?}" "${RAG_PROJECT:?}" "${RAG_COMPOSE_FILE:?}" "${RAG_DEPLOY_ENV:?}" "${RAG_TOPOLOGY:?}"
case "$RAG_TOPOLOGY" in
  local) writers=(api);;
  remote) writers=(api asset-worker);;
  *) exit 2;;
esac
compose=(docker compose -p "$RAG_PROJECT" --env-file "$RAG_DEPLOY_ENV" -f "$RAG_COMPOSE_FILE" -f "/etc/rag-retention/compose.$RAG_TOPOLOGY.yaml")
record="$STATE_DIRECTORY/restart-ids"
exec 9>"$STATE_DIRECTORY/runner.lock"
flock -n 9 || exit 1
recover() {
  if [[ -e "$record" ]]; then
    while read -r id; do
      [[ "$id" =~ ^[a-f0-9]{64}$ ]] || return 1
      docker start "$id" >/dev/null || return 1
    done <"$record"
    rm -- "$record"
  fi
}
case "${1:-}" in
  recover) recover; exit;;
  run) recover;;
  *) exit 2;;
esac
# Require the expected foreground writer in each role, already fenced. Abort on
# topology drift, missing deployment or replica count changes.
ids=()
for service in "${writers[@]}"; do
  short=$("${compose[@]}" ps --status running -q "$service")
  [[ "$short" =~ ^[0-9a-f]{12,64}$ ]] || exit 1
  # Compose v2 can hash the env_file declaration rather than the effective
  # container environment. Pipe the resolved model directly; never log it.
  expected=$("${compose[@]}" config --format json 2>/dev/null |
    docker compose -p "$RAG_PROJECT" --project-directory "$(dirname "$RAG_COMPOSE_FILE")" \
      -f - config --no-interpolate --hash "$service" 2>/dev/null) || {
    echo 'Cannot verify resolved configuration: refusing maintenance' >&2; exit 1; }
  actual=$(docker inspect --format '{{index .Config.Labels "com.docker.compose.config-hash"}}' "$short")
  [[ "$actual" =~ ^[a-f0-9]{64}$ && "$expected" == "$service $actual" ]] || {
    echo 'Configuration changed since deployment: refusing maintenance' >&2; exit 1; }
  entry=$(docker inspect --format '{{json .Config.Entrypoint}}' "$short")
  [[ "$entry" == '["python","-m","adaptive_rag.retention_cli","writer"]' ]] || {
    echo 'Unfenced writer: refusing maintenance' >&2; exit 1; }
  id=$(docker inspect --format '{{.Id}}' "$short")
  [[ "$id" =~ ^[a-f0-9]{64}$ ]] || exit 1
  ids+=("$id")
done
printf '%s\n' "${ids[@]}" > "$record.tmp"
mv -- "$record.tmp" "$record"
trap recover EXIT
# Graceful drain first. The exclusive nonblocking directory lock inside the
# collector is the final fence, also against an independently wrapped CLI.
docker stop -t 150 "${ids[@]}" >/dev/null
"${compose[@]}" run --rm --no-deps -T --entrypoint python api -m adaptive_rag.retention_cli purge --exclusive-deployment
