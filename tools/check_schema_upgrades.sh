#!/usr/bin/env bash
# Compare two independent private PG17 clusters; no installation is inspected.
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
[[ "${LIC_SCHEMA_UPGRADES_MAX_ROWS:-}" == 1000 ]] || { echo 'Authorize the bounded 1000-row upgrade suite.' >&2; exit 2; }
command -v python3 >/dev/null
command -v timeout >/dev/null
source "$root/tools/schema_ci_postgres_image.sh"
schema_ci_postgres_require "$root"
image="$SCHEMA_CI_POSTGRES_IMAGE"
source "$root/tools/schema_ci_resources.sh"
schema_ci_resources_init
umask 077
work="$(mktemp -d "${TMPDIR:-/tmp}/leti-cook-schema-upgrades.XXXXXXXX")"
schema_ci_files_cleanup() {
  [[ -d "$work" && ! -L "$work" && "${work##*/}" == leti-cook-schema-upgrades.* ]] || return 1
  rm -f -- "$work/upgraded.json" "$work/fresh.json"
  rmdir -- "$work"
}
secret() { head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; }
export LIC_SCHEMA_CI_ADMIN_PASSWORD="$(secret)" LIC_SCHEMA_CI_RUNTIME_PASSWORD="$(secret)"
ready() {
  for _ in $(seq 1 120); do
    if docker exec "$SCHEMA_CI_LAST_ID" sh -c 'test -f "$PGDATA/.self-hosted-schema-ready" && pg_isready -q -h 127.0.0.1 -U postgres -d leti-cook'; then
      return 0
    fi
    [[ "$(docker inspect -f '{{.State.Running}}' "$SCHEMA_CI_LAST_ID")" == true ]] || break
    sleep 1
  done
  echo 'Upgrade fixture did not bootstrap.' >&2
  return 1
}
launch() {
  local component="$1" schema="$2" digest
  digest="$(sha256sum "$schema" | cut -d' ' -f1)"
  schema_ci_create "$component" "$image" --network none --memory 768m --memory-swap 768m --cpus 1.5 --log-driver none --pids-limit 128 \
    -e POSTGRES_USER=postgres -e POSTGRES_DB=leti-cook -e POSTGRES_PASSWORD="$LIC_SCHEMA_CI_ADMIN_PASSWORD" \
    -e POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256 \
    -e AUTHENTICATOR_PASSWORD="$LIC_SCHEMA_CI_RUNTIME_PASSWORD" -e SELF_HOSTED_SCHEMA_SHA256="$digest" \
    -v "$root/database/bootstrap.sh:/docker-entrypoint-initdb.d/10-self-hosted.sh:ro" \
    -v "$schema:/opt/leti-cook/schema.sql:ro" --
  schema_ci_export_pg "$SCHEMA_CI_LAST_INDEX"
  docker start "$SCHEMA_CI_LAST_ID" >/dev/null
  ready
  schema_ci_require_pg "$SCHEMA_CI_LAST_ID"
}
launch base "$root/schema/baseline/schema.sql"
first_id="$SCHEMA_CI_LAST_ID"
timeout --signal=TERM --kill-after=15s 270s python3 -B "$root/tests/schema_upgrades/run.py" upgraded --output "$work/upgraded.json"
schema_ci_remove "$SCHEMA_CI_LAST_INDEX"
launch postgres "$root/schema/schema.sql"
[[ "$SCHEMA_CI_LAST_ID" != "$first_id" ]]
timeout --signal=TERM --kill-after=15s 90s python3 -B "$root/tests/schema_upgrades/run.py" fresh --output "$work/fresh.json"
schema_ci_remove "$SCHEMA_CI_LAST_INDEX"
python3 -B "$root/tests/schema_upgrades/run.py" compare --first "$work/upgraded.json" --second "$work/fresh.json"
printf '%s\n' '{"stage":"schema_upgrades_cleanup","status":"pass","remaining_owned_containers":0,"remaining_owned_volumes":0}'
