#!/usr/bin/env bash
# Bootstrap the fresh schema exactly as an installation does (the pinned
# derived PostgreSQL image from database/Dockerfile running database/bootstrap.sh),
# then run the current SQL assertions and Python repository regressions. Needs Docker; uses a disposable
# container with generated passwords and no application data.
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
source "$root/tools/schema_ci_postgres_image.sh"
schema_ci_postgres_require "$root"
image="$SCHEMA_CI_POSTGRES_IMAGE"
digest="$(cut -d' ' -f1 "$root/schema/schema.sql.sha256")"
secret() { head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; }
[[ "${LIC_SCHEMA_CI_MAX_ROWS:-}" == 15000 && -d "${LIC_SCHEMA_CI_DEPS:-}" ]] || { echo "Prepare dependencies and authorize the large CI fixtures." >&2; exit 2; }
export LIC_SCHEMA_CI_ADMIN_PASSWORD="$(secret)" LIC_SCHEMA_CI_RUNTIME_PASSWORD="$(secret)"
source "$root/tools/schema_ci_resources.sh"
schema_ci_resources_init

schema_ci_create postgres "$image" --network none --memory 768m --memory-swap 768m --cpus 1.5 --log-driver none --pids-limit 128 \
  -e POSTGRES_USER=postgres -e POSTGRES_DB=leti-cook -e POSTGRES_PASSWORD="$LIC_SCHEMA_CI_ADMIN_PASSWORD" \
  -e AUTHENTICATOR_PASSWORD="$LIC_SCHEMA_CI_RUNTIME_PASSWORD" -e SELF_HOSTED_SCHEMA_SHA256="$digest" \
  -v "$root/database/bootstrap.sh:/docker-entrypoint-initdb.d/10-self-hosted.sh:ro" \
  -v "$root/schema/schema.sql:/opt/leti-cook/schema.sql:ro" \
  --
name="$SCHEMA_CI_LAST_NAME"
schema_ci_export_pg "$SCHEMA_CI_LAST_INDEX"
docker start "$SCHEMA_CI_LAST_ID" >/dev/null

# Check both first bootstrap and a restart of the same initialized volume.
ready() {
  for _ in $(seq 1 180); do
    if docker exec "$name" sh -c 'test -f "$PGDATA/.self-hosted-schema-ready" && pg_isready -q -h 127.0.0.1 -U postgres -d leti-cook'; then
      return 0
    fi
    [[ "$(docker inspect -f '{{.State.Running}}' "$name")" == true ]] || break
    sleep 1
  done
  echo 'The schema did not bootstrap or restart.' >&2
  return 1
}
check_postgres_process() {
  # docker exec's default HOME belongs to the image, not PID1. Inspect the
  # actual postgres process as its own user, without printing its environment.
  docker exec --user postgres "$name" bash -euc '
    uid="$(id -u postgres)"; gid="$(id -g postgres)"
    test "$uid" -ne 0
    test "$(cat /proc/1/comm)" = postgres
    test "$(awk '\''/^Uid:/{print $2" "$3" "$4" "$5}'\'' /proc/1/status)" = "$uid $uid $uid $uid"
    test "$(awk '\''/^Gid:/{print $2" "$3" "$4" "$5}'\'' /proc/1/status)" = "$gid $gid $gid $gid"
    for cap in CapEff CapPrm CapAmb CapInh; do
      value="$(awk -v key="$cap:" '\''$1==key {print $2}'\'' /proc/1/status)"
      test -n "$value"
      test -z "${value//0/}"
    done
    home="$(getent passwd postgres | cut -d: -f6)"
    test -n "$home"
    tr "\0" "\n" < /proc/1/environ | grep -Fxq "HOME=$home"
    if command -v gosu >/dev/null; then exit 1; fi
    test ! -e /usr/local/bin/gosu
    command -v setpriv >/dev/null
  '
}
ready
check_postgres_process
# One synthetic row proves ordinary pg_dump/pg_restore and volume persistence.
docker exec "$name" createdb --username postgres schema_ci_runtime
docker exec "$name" psql --no-psqlrc --username postgres --dbname schema_ci_runtime --set ON_ERROR_STOP=1 --quiet \
  --command 'CREATE TABLE probe (id integer PRIMARY KEY, value text NOT NULL); INSERT INTO probe VALUES (1, '\''retained'\'');'
docker exec "$name" pg_dump --username postgres --format custom --file /tmp/schema-ci-runtime.dump schema_ci_runtime
docker exec "$name" dropdb --username postgres schema_ci_runtime
docker exec "$name" createdb --username postgres schema_ci_runtime
docker exec "$name" pg_restore --username postgres --exit-on-error --dbname schema_ci_runtime /tmp/schema-ci-runtime.dump
schema_ci_verify "$SCHEMA_CI_LAST_INDEX" "$SCHEMA_CI_LAST_ID"
docker stop --signal SIGTERM --timeout 30 "$SCHEMA_CI_LAST_ID" >/dev/null
schema_ci_assert_exited "$SCHEMA_CI_LAST_INDEX"
docker start "$SCHEMA_CI_LAST_ID" >/dev/null
ready
check_postgres_process
[[ "$(docker exec "$name" psql --no-psqlrc --username postgres --dbname schema_ci_runtime --tuples-only --no-align \
  --command 'SELECT id::text || '\''|'\'' || value FROM probe')" == '1|retained' ]]
docker exec "$name" dropdb --username postgres schema_ci_runtime
docker exec "$name" rm -- /tmp/schema-ci-runtime.dump
printf '%s\n' '{"stage":"postgres_image_runtime","status":"pass","checks":["nonroot_uid_gid","no_process_capabilities","postgres_home","gosu_absent","dump_restore","term_shutdown","existing_volume_restart"]}'

bash "$root/tools/check_schema_ci_suite.sh" "$name"
echo "Fresh schema passes the complete 10 SQL + 21 Python suite."
