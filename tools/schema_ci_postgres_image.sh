#!/usr/bin/env bash
# One build identity for the COPY-free installation Dockerfile. No network here.
schema_ci_postgres_source() {
  local root="$1"
  SCHEMA_CI_POSTGRES_SOURCE_SHA="$(sha256sum "$root/database/Dockerfile" | cut -d' ' -f1)" || return 1
  [[ "$SCHEMA_CI_POSTGRES_SOURCE_SHA" =~ ^[a-f0-9]{64}$ ]] || return 1
  SCHEMA_CI_POSTGRES_BASE="$(sed -nE 's/^FROM (postgres:[^[:space:]]+@sha256:[0-9a-f]{64})$/\1/p' "$root/database/Dockerfile")"
  [[ "$SCHEMA_CI_POSTGRES_BASE" =~ ^postgres:17\.[^[:space:]]+@sha256:[a-f0-9]{64}$ ]] || return 1
  # Additional build inputs must be added to this identity before they are used.
  ! grep -Eiq '^[[:space:]]*(COPY|ADD|ARG)[[:space:]]' "$root/database/Dockerfile" || return 1
  SCHEMA_CI_POSTGRES_TAG="leti-cook-postgres-ci:$SCHEMA_CI_POSTGRES_SOURCE_SHA"
}
schema_ci_postgres_build() {
  schema_ci_postgres_source "$1" || return 1
  docker build --network none --pull=false --tag "$SCHEMA_CI_POSTGRES_TAG" \
    --label "org.leti-cook.postgres-source-sha=$SCHEMA_CI_POSTGRES_SOURCE_SHA" "$1/database"
}
schema_ci_postgres_require() {
  local actual identity
  schema_ci_postgres_source "$1" || return 1
  actual="$(docker image inspect --format '{{.Id}}|{{index .Config.Labels "org.leti-cook.postgres-source-sha"}}' "$SCHEMA_CI_POSTGRES_TAG")" || return 1
  identity="${actual%%|*}"
  [[ "$identity" =~ ^sha256:[a-f0-9]{64}$ && "$actual" == "$identity|$SCHEMA_CI_POSTGRES_SOURCE_SHA" ]] || {
    echo 'Prepare the PostgreSQL image from this exact Dockerfile first.' >&2; return 1;
  }
  # Callers pin the container/scan to this immutable local build, not a tag.
  SCHEMA_CI_POSTGRES_IMAGE="$identity"
}
