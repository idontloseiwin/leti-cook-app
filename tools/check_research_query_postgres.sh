#!/usr/bin/env bash
# Research query SQL on PostgreSQL 17 (tests/research_query_pg.py). The test
# builds its own schema from the baseline and numbered steps, so it needs an
# empty database: one disposable container without the installation bootstrap.
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -d "${LIC_SCHEMA_CI_DEPS:-}" && "$LIC_SCHEMA_CI_DEPS" == /* ]] || { echo 'Prepare locked schema CI dependencies first.' >&2; exit 2; }
source "$root/tools/schema_ci_postgres_image.sh"
schema_ci_postgres_require "$root"
image="$SCHEMA_CI_POSTGRES_IMAGE"
python_image="$(sed -n 's/^FROM \(python:[^ ]*\).*$/\1/p' "$root/application/Dockerfile" | head -n1)"
[[ "$python_image" == python:3.12-*@sha256:* ]] || exit 2
source "$root/tools/schema_ci_resources.sh"
schema_ci_resources_init
secret() { head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; }
export LIC_QUERY_TEST_PASSWORD="$(secret)"

schema_ci_create postgres "$image" --network none --memory 768m --memory-swap 768m --cpus 1.5 --log-driver none --pids-limit 128 \
  -e POSTGRES_USER=postgres -e POSTGRES_DB=leti-cook -e POSTGRES_PASSWORD="$LIC_QUERY_TEST_PASSWORD" \
  -e POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256 --
schema_ci_export_pg "$SCHEMA_CI_LAST_INDEX"
docker start "$SCHEMA_CI_LAST_ID" >/dev/null
# The image's init server listens on the socket only; TCP answers once it is done.
ready=0
for _ in $(seq 1 120); do
  if docker exec "$SCHEMA_CI_LAST_ID" pg_isready -q -h 127.0.0.1 -U postgres -d leti-cook; then ready=1; break; fi
  [[ "$(docker inspect -f '{{.State.Running}}' "$SCHEMA_CI_LAST_ID")" == true ]] || break
  sleep 1
done
[[ "$ready" == 1 ]] || { echo 'The empty research query database did not start.' >&2; exit 1; }
schema_ci_require_pg "$SCHEMA_CI_LAST_ID"

# No port publication/provider network. Source and dependencies are read-only.
schema_ci_create python "$python_image" --network "container:$LIC_SCHEMA_CI_PG_ID" \
  --memory 768m --memory-swap 768m --cpus 1.5 --log-driver none --pids-limit 128 --read-only --tmpfs /tmp:rw,nosuid,noexec,size=32m \
  -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONNOUSERSITE=1 -e PYTHON_DOTENV_DISABLED=1 \
  -e PYTHONPATH=/deps:/repo/application:/repo/tests:/repo \
  -e LIC_RESEARCH_QUERY_DISPOSABLE=1 -e LIC_QUERY_TEST_PORT=5432 -e LIC_QUERY_TEST_PASSWORD \
  -e LIC_DEPLOYMENT_PROFILE=self-hosted -e LIC_DATABASE_TRANSPORT=postgres \
  -v "$root:/repo:ro" -v "$LIC_SCHEMA_CI_DEPS:/deps:ro" -- python -B /repo/tests/research_query_pg.py
timeout --signal=TERM --kill-after=10s 180s docker start --attach "$SCHEMA_CI_LAST_ID"
schema_ci_assert_exited "$SCHEMA_CI_LAST_INDEX"
printf '%s\n' '{"stage":"research_query_postgres","status":"pass"}'
