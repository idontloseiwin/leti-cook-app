#!/usr/bin/env bash
# Explicit dependency-network phase; never called by a native test/controller.
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
[[ $# == 0 && "${LIC_SCHEMA_CI_OFFLINE:-0}" == 0 ]] || exit 2
python_image="$(sed -n 's/^FROM \(python:[^ ]*\).*$/\1/p' "$root/application/Dockerfile" | head -n1)"
source "$root/tools/schema_ci_postgres_image.sh"
schema_ci_postgres_source "$root"
[[ "$python_image" == python:3.12-*@sha256:* ]] || exit 2
docker pull "$python_image"
docker pull "$SCHEMA_CI_POSTGRES_BASE"
schema_ci_postgres_build "$root"
schema_ci_postgres_require "$root"
