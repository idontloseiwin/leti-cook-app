#!/usr/bin/env bash
# Retention benchmark (tools/retention_benchmark.py) against a disposable, freshly
# bootstrapped database. Same image, bootstrap and resource ownership as
# tools/check_schema_postgres.sh. No published port, no provider network; every
# container is removed on exit. Arguments go to the benchmark, for example:
#   LIC_SCHEMA_CI_DEPS=<deps> bash tools/retention_benchmark.sh --scenario downtime
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
deps="${LIC_SCHEMA_CI_DEPS:-}"
[[ -n "$deps" && -d "$deps" ]] || { echo 'Prepare dependencies with tools/prepare_schema_ci_python.sh and set LIC_SCHEMA_CI_DEPS.' >&2; exit 2; }
source "$root/tools/schema_ci_postgres_image.sh"
schema_ci_postgres_require "$root"
pg_image="$SCHEMA_CI_POSTGRES_IMAGE"
python_image="$(sed -n 's/^FROM \(python:[^ ]*\).*$/\1/p' "$root/application/Dockerfile" | head -n1)"
[[ "$python_image" == python:3.12-*@sha256:* ]] || exit 2
digest="$(cut -d' ' -f1 "$root/schema/schema.sql.sha256")"
secret() { head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; }
admin_pw="$(secret)"; runtime_pw="$(secret)"
source "$root/tools/schema_ci_resources.sh"
schema_ci_resources_init

schema_ci_create postgres "$pg_image" --network none --memory 768m --memory-swap 768m --cpus 1.5 --log-driver none --pids-limit 128 \
  -e POSTGRES_USER=postgres -e POSTGRES_DB=leti-cook -e POSTGRES_PASSWORD="$admin_pw" \
  -e AUTHENTICATOR_PASSWORD="$runtime_pw" -e SELF_HOSTED_SCHEMA_SHA256="$digest" \
  -v "$root/database/bootstrap.sh:/docker-entrypoint-initdb.d/10-self-hosted.sh:ro" \
  -v "$root/schema/schema.sql:/opt/leti-cook/schema.sql:ro" \
  --
pg_id="$SCHEMA_CI_LAST_ID"; pg_name="$SCHEMA_CI_LAST_NAME"
docker start "$pg_id" >/dev/null
for _ in $(seq 1 180); do
  if docker exec "$pg_name" sh -c 'test -f "$PGDATA/.self-hosted-schema-ready" && pg_isready -q -h 127.0.0.1 -U postgres -d leti-cook'; then break; fi
  sleep 1
done
docker exec "$pg_name" sh -c 'test -f "$PGDATA/.self-hosted-schema-ready"'
# The benchmark accepts only an explicitly disposable database name.
docker exec "$pg_name" psql --no-psqlrc --username postgres --dbname postgres --set ON_ERROR_STOP=1 --quiet \
  --command 'ALTER DATABASE "leti-cook" RENAME TO lic_direct_pg_probe_bench'
echo '{"stage":"disposable_database","status":"ready","database":"lic_direct_pg_probe_bench"}'

export LIC_DIRECT_PG_PROBE_ADMIN_DSN="host=127.0.0.1 port=5432 dbname=lic_direct_pg_probe_bench user=postgres password=$admin_pw sslmode=disable"
export LIC_DIRECT_PG_PROBE_DSN="host=127.0.0.1 port=5432 dbname=lic_direct_pg_probe_bench user=authenticator password=$runtime_pw sslmode=disable"
rc=0
schema_ci_create benchmark "$python_image" --network "container:$pg_id"   --memory 768m --memory-swap 768m --cpus 1.5 --log-driver none --pids-limit 256 --read-only --tmpfs /tmp:rw,nosuid,size=64m   -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONNOUSERSITE=1 -e PYTHON_DOTENV_DISABLED=1   -e PYTHONPATH=/deps:/repo/application:/repo/tests:/repo   -e LIC_DIRECT_PG_PROBE_ADMIN_DSN -e LIC_DIRECT_PG_PROBE_DSN   -v "$root:/repo:ro" -v "$deps:/deps:ro" -w /repo -- python -B tools/retention_benchmark.py "$@"
timeout --signal=TERM --kill-after=10s 1800s docker start --attach "$SCHEMA_CI_LAST_ID" || rc=$?
echo "{\"stage\":\"retention_benchmark\",\"exit\":$rc}"
exit "$rc"
