#!/usr/bin/env bash
# Dependency preparation is separate from isolated database execution.
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
[[ $# == 1 && "$1" == /* && ! -e "$1" ]] || { echo 'Supply a new absolute dependency directory.' >&2; exit 2; }
mkdir -m 0755 "$1"
image="$(sed -n 's/^FROM \(python:[^ ]*\).*$/\1/p' "$root/application/Dockerfile" | head -n1)"
[[ "$image" == python:3.12-*@sha256:* ]] || exit 2
# This step may download only the repository's hash-locked dependencies. It
# runs before any database container and is never called by the native runner.
source "$root/tools/schema_ci_resources.sh"
schema_ci_resources_init
schema_ci_create dependencies "$image" --user "$(id -u):$(id -g)" --memory 768m --memory-swap 768m --cpus 1.5 --log-driver none --pids-limit 128 \
  -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 -v "$root/application/requirements.lock:/requirements.lock:ro" \
  -v "$1:/deps:rw" -- python -m pip install --no-cache-dir --require-hashes --no-deps -r /requirements.lock --target /deps
docker start --attach "$SCHEMA_CI_LAST_ID"
schema_ci_assert_exited "$SCHEMA_CI_LAST_INDEX"
