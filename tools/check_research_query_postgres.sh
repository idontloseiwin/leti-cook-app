#!/usr/bin/env bash
# Research workshop SQL on PostgreSQL 17: query, frozen, growth, aggregate, inspector, overview, home, channels, fields, graph, graph frozen, saved
# suites (tests/research_*_pg.py), WebSub storage and effects (tests/websub_pg.py), a channel's first
# Short or stream and the WebSub tally (tests/first_of_type_pg.py), one-off provider
# acquisitions (tests/provider_sources_pg.py), the HTTP reader role
# (tests/reader_role_http_pg.py, the reader-route manifest), and the browser wire
# fixtures checked against the current server on the same seeds (tests/library_wire_fixtures_pg.py),
# and channel profile status through cleanup and the HOT release (tests/channel_profile_status_pg.py),
# and quota bucket deferrals, claims and the quota-ledger memory (tests/quota_split_pg.py)
# and a stream-off pass that asks YouTube nothing for streams (tests/streams_off_pg.py)
# and durable discovery admission, exact VIDEO ownership and response clocks
# (tests/discovery_admission_pg.py). Each suite creates cluster
# roles and builds its own schema from the baseline and numbered steps, so each
# needs a new empty database: one disposable container per suite, without the
# installation bootstrap.
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -d "${LIC_SCHEMA_CI_DEPS:-}" && "$LIC_SCHEMA_CI_DEPS" == /* ]] || { echo 'Prepare locked schema CI dependencies first.' >&2; exit 2; }
mode=standard
case "$#:${1:-}" in
  0:) ;;
  1:--large-home) mode=large-home ;;
  *) echo 'Usage: check_research_query_postgres.sh [--large-home]' >&2; exit 2 ;;
esac
source "$root/tools/schema_ci_postgres_image.sh"
schema_ci_postgres_require "$root"
image="$SCHEMA_CI_POSTGRES_IMAGE"
python_image="$(sed -n 's/^FROM \(python:[^ ]*\).*$/\1/p' "$root/application/Dockerfile" | head -n1)"
[[ "$python_image" == python:3.12-*@sha256:* ]] || exit 2
source "$root/tools/schema_ci_resources.sh"
schema_ci_resources_init
secret() { head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; }
export LIC_QUERY_TEST_PASSWORD="$(secret)"

run_suite() {
local suite="$1" pg_index python_index deadline=360s
local -a flags=()
if [[ "$mode" == large-home ]]; then
  flags=(-e LIC_TEST_LARGE_HOME=1)
  deadline=600s
fi
schema_ci_create postgres "$image" --network none --memory 768m --memory-swap 768m --cpus 1.5 --log-driver none --pids-limit 128 \
  -e POSTGRES_USER=postgres -e POSTGRES_DB=leti-cook -e POSTGRES_PASSWORD="$LIC_QUERY_TEST_PASSWORD" \
  -e POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256 --
pg_index="$SCHEMA_CI_LAST_INDEX"
schema_ci_export_pg "$pg_index"
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
  -e PYTHONPATH=/deps:/deps/mcp:/repo/application:/repo/tests:/repo \
  "${flags[@]}" -e LIC_RESEARCH_QUERY_DISPOSABLE=1 -e LIC_QUERY_TEST_PORT=5432 -e LIC_QUERY_TEST_PASSWORD \
  -e LIC_DEPLOYMENT_PROFILE=self-hosted -e LIC_DATABASE_TRANSPORT=postgres \
  -v "$root:/repo:ro" -v "$LIC_SCHEMA_CI_DEPS:/deps:ro" -- python -B "/repo/tests/$suite.py"
python_index="$SCHEMA_CI_LAST_INDEX"
timeout --signal=TERM --kill-after=10s "$deadline" docker start --attach "$SCHEMA_CI_LAST_ID"
schema_ci_assert_exited "$python_index"
schema_ci_remove "$python_index"
schema_ci_remove "$pg_index"
printf '{"stage":"research_postgres","suite":"%s","status":"pass"}\n' "$suite"
}

if [[ "$mode" == large-home ]]; then
  run_suite research_home_pg
  exit 0
fi

for suite in research_query_pg research_frozen_pg research_growth_pg research_aggregate_pg research_inspector_pg research_overview_pg research_home_pg research_channels_pg research_fields_pg research_graph_pg research_graph_frozen_pg research_saved_pg research_edges_pg research_mcp_pg websub_pg first_of_type_pg provider_sources_pg library_wire_fixtures_pg reader_role_http_pg update_copy_lifecycle_pg channel_profile_status_pg quota_split_pg streams_off_pg discovery_admission_pg discovery_control_pg discovery_executor_pg discovery_heads_pg; do
  run_suite "$suite"
done
printf '%s\n' '{"stage":"research_query_postgres","status":"pass"}'
