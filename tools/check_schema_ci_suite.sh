#!/usr/bin/env bash
# Acceptance suite for the current fresh installation schema.
# The caller owns the one PostgreSQL container; this adds one Python container.
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
[[ $# == 1 ]] || exit 2
[[ "${LIC_SCHEMA_CI_MAX_ROWS:-}" == 15000 ]] || { echo 'Explicit 15000-row CI fixture authorization is required.' >&2; exit 2; }
[[ -d "${LIC_SCHEMA_CI_DEPS:-}" && "$LIC_SCHEMA_CI_DEPS" == /* ]] || { echo 'Prepare locked schema CI dependencies first.' >&2; exit 2; }
[[ "${LIC_SCHEMA_CI_ADMIN_PASSWORD:-}" =~ ^[a-f0-9]{64}$ && "${LIC_SCHEMA_CI_RUNTIME_PASSWORD:-}" =~ ^[a-f0-9]{64}$ ]] || exit 2
image="$(sed -n 's/^FROM \(python:[^ ]*\).*$/\1/p' "$root/application/Dockerfile" | head -n1)"
[[ "$image" == python:3.12-*@sha256:* ]] || exit 2
source "$root/tools/schema_ci_resources.sh"
schema_ci_require_pg "$1"
schema_ci_resources_init
export LIC_SCHEMA_CI_ADMIN_DSN="host=127.0.0.1 port=5432 dbname=leti-cook user=postgres password=$LIC_SCHEMA_CI_ADMIN_PASSWORD sslmode=disable"
export LIC_SCHEMA_CI_DSN="host=127.0.0.1 port=5432 dbname=leti-cook user=authenticator password=$LIC_SCHEMA_CI_RUNTIME_PASSWORD sslmode=disable"
# The native suites below restore the fixture after every case, so they share
# one database; each declares its own 1000-row budget. /out takes their reports.
command=(sh -c 'python -B /repo/tools/check_schema25_repositories.py && python -B /repo/tests/native_publication_lifecycle.py \
  && export LIC_SCHEMA_CI_MAX_ROWS=1000 \
  && python -B /repo/tests/native_unified_availability.py --run-proof \
  && python -B /repo/tests/native_automatic_type_policy.py \
  && python -B /repo/tests/native_pipeline_trace.py --run-proof \
  && python -B /repo/tests/native_all_type_queue_health.py \
  && python -B /repo/tests/native_health_queue_projection.py \
  && python -B /repo/tests/native_data_lifecycle_consumers.py \
  && python -B /repo/tests/native_quota_batch_packing.py --run-proof')
# No port publication/provider network. Source and dependencies are read-only.
schema_ci_create python "$image" --network "container:$LIC_SCHEMA_CI_PG_ID" \
  --memory 768m --memory-swap 768m --cpus 1.5 --log-driver none --pids-limit 128 --read-only --tmpfs /tmp:rw,nosuid,noexec,size=32m \
  --tmpfs /out:rw,nosuid,noexec,size=32m \
  -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONNOUSERSITE=1 -e PYTHON_DOTENV_DISABLED=1 \
  -e PYTHONPATH=/deps:/repo/application:/repo/tests:/repo -e LIC_SCHEMA_CI_OWNED=schema25_disposable \
  -e LIC_SCHEMA_CI_ADMIN_DSN -e LIC_SCHEMA_CI_DSN -e LIC_SCHEMA_CI_MAX_ROWS \
  -v "$root:/repo:ro" -v "$LIC_SCHEMA_CI_DEPS:/deps:ro" -- "${command[@]}"
timeout --signal=TERM --kill-after=10s 450s docker start --attach "$SCHEMA_CI_LAST_ID"
schema_ci_assert_exited "$SCHEMA_CI_LAST_INDEX"
