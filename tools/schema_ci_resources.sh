#!/usr/bin/env bash
# Exact disposable-resource ownership. Callers supply fixed options, never names.
schema_ci_resources_init() {
  SCHEMA_CI_TOKEN="$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  [[ "$SCHEMA_CI_TOKEN" =~ ^[a-f0-9]{32}$ ]] || return 1
  SCHEMA_CI_NAMES=(); SCHEMA_CI_IDS=(); SCHEMA_CI_IMAGES=(); SCHEMA_CI_COMPONENTS=()
  trap 'schema_ci_resources_exit "$?"' EXIT
  trap 'exit 124' TERM
  trap 'exit 130' INT
}
schema_ci_image_id() {
  local identity
  identity="$(docker image inspect --format '{{.Id}}' "$1")" || return 1
  [[ "$identity" =~ ^sha256:[a-f0-9]{64}$ ]] || return 1
  printf '%s' "$identity"
}
schema_ci_find() {
  local index="$1" found
  if [[ -n "${SCHEMA_CI_IDS[$index]}" ]]; then
    found="$(docker container ls -aq --no-trunc --filter "id=${SCHEMA_CI_IDS[$index]}")" || return 1
  else
    found="$(docker container ls -aq --no-trunc --filter "name=^/${SCHEMA_CI_NAMES[$index]}$")" || return 1
  fi
  [[ -z "$found" || "$found" =~ ^[a-f0-9]{64}$ ]] || return 1
  SCHEMA_CI_FOUND="$found"
}
schema_ci_verify() {
  local index="$1" identity="$2" actual expected
  [[ "$identity" =~ ^[a-f0-9]{64}$ ]] || return 1
  actual="$(docker container inspect --format '{{.Id}}|{{.Name}}|{{.Image}}|{{index .Config.Labels "org.leti-cook.schema-ci"}}|{{index .Config.Labels "org.leti-cook.schema-ci.component"}}' "$identity")" || return 1
  expected="$identity|/${SCHEMA_CI_NAMES[$index]}|${SCHEMA_CI_IMAGES[$index]}|$SCHEMA_CI_TOKEN|${SCHEMA_CI_COMPONENTS[$index]}"
  [[ "$actual" == "$expected" ]] || { echo 'Schema CI resource ownership mismatch; refusing mutation.' >&2; return 1; }
  [[ -z "${SCHEMA_CI_IDS[$index]}" || "${SCHEMA_CI_IDS[$index]}" == "$identity" ]] || return 1
  SCHEMA_CI_IDS[$index]="$identity"
}
schema_ci_create() { # component, digest-pinned image or immutable local ID, fixed options, --, command
  local component="$1" image="$2" image_id name present index receipt
  shift 2
  [[ "$component" =~ ^[a-z][a-z-]{1,20}$ ]] || return 1
  [[ "$image" == *@sha256:* || "$image" =~ ^sha256:[a-f0-9]{64}$ ]] || return 1
  image_id="$(schema_ci_image_id "$image")" || { echo 'Cached pinned image required.' >&2; return 1; }
  name="leti-cook-schema-$component-$SCHEMA_CI_TOKEN"
  present="$(docker container ls -aq --no-trunc --filter "name=^/$name$")" || return 1
  [[ -z "$present" ]] || { echo 'Schema CI name already exists; refusing ownership.' >&2; return 1; }
  local options=()
  while [[ $# -gt 0 && "$1" != -- ]]; do
    case "$1" in --name|--name=*|--label|--label=*|--label-file|--label-file=*|--pull|--pull=*|--rm|--rm=*) return 1;; esac
    options+=("$1"); shift
  done
  [[ $# -gt 0 ]] || return 1; shift
  index=${#SCHEMA_CI_NAMES[@]}
  SCHEMA_CI_NAMES+=("$name"); SCHEMA_CI_IDS+=(""); SCHEMA_CI_IMAGES+=("$image_id"); SCHEMA_CI_COMPONENTS+=("$component")
  # Register before create: a lost create ACK is resolved only through matching
  # name/image/token/component; foreign resources are never adopted or removed.
  if ! receipt="$(docker create --pull=never --name "$name" --label "org.leti-cook.schema-ci=$SCHEMA_CI_TOKEN" --label "org.leti-cook.schema-ci.component=$component" "${options[@]}" "$image" "$@")"; then
    return 1
  fi
  schema_ci_verify "$index" "$receipt" || return 1
  SCHEMA_CI_LAST_INDEX="$index"; SCHEMA_CI_LAST_ID="$receipt"; SCHEMA_CI_LAST_NAME="$name"
}
schema_ci_remove() {
  local index="$1" identity volumes volume remaining
  [[ -n "${SCHEMA_CI_NAMES[$index]}" ]] || return 0
  schema_ci_find "$index" || return 1
  identity="$SCHEMA_CI_FOUND"
  [[ -n "$identity" ]] || { SCHEMA_CI_NAMES[$index]=''; return 0; }
  schema_ci_verify "$index" "$identity" || return 1
  volumes="$(docker container inspect --format '{{range .Mounts}}{{if eq .Type "volume"}}{{.Name}}{{println}}{{end}}{{end}}' "$identity")" || return 1
  # Only exact volumes already attached to the verified disposable container.
  while IFS= read -r volume; do
    [[ -z "$volume" || "$volume" =~ ^[a-f0-9]{64}$ ]] || { echo 'Unexpected named volume; refusing cleanup.' >&2; return 1; }
  done <<< "$volumes"
  docker rm -fv "$identity" >/dev/null || return 1
  schema_ci_find "$index" || return 1
  [[ -z "$SCHEMA_CI_FOUND" ]] || return 1
  while IFS= read -r volume; do
    [[ -n "$volume" ]] || continue
    remaining="$(docker volume ls -q --filter "name=^$volume$")" || return 1
    [[ -z "$remaining" ]] || { echo 'Schema CI anonymous volume cleanup failed.' >&2; return 1; }
  done <<< "$volumes"
  SCHEMA_CI_NAMES[$index]=''
}
schema_ci_resources_exit() {
  local result="$1" index failed=0
  trap - EXIT INT TERM
  for ((index=${#SCHEMA_CI_NAMES[@]}-1;index>=0;index--)); do
    schema_ci_remove "$index" || failed=1
  done
  if declare -F schema_ci_files_cleanup >/dev/null; then schema_ci_files_cleanup || failed=1; fi
  if [[ "$failed" != 0 ]]; then echo 'Schema CI cleanup failed; inspect owned resources.' >&2; result=1; fi
  exit "$result"
}

schema_ci_export_pg() {
  local index="$1"
  schema_ci_verify "$index" "${SCHEMA_CI_IDS[$index]}" || return 1
  export LIC_SCHEMA_CI_PG_ID="${SCHEMA_CI_IDS[$index]}" LIC_SCHEMA_CI_PG_NAME="${SCHEMA_CI_NAMES[$index]}"
  export LIC_SCHEMA_CI_PG_IMAGE="${SCHEMA_CI_IMAGES[$index]}" LIC_SCHEMA_CI_PG_TOKEN="$SCHEMA_CI_TOKEN" LIC_SCHEMA_CI_PG_COMPONENT="${SCHEMA_CI_COMPONENTS[$index]}"
}
schema_ci_require_pg() {
  local actual expected
  [[ "${LIC_SCHEMA_CI_PG_ID:-}" =~ ^[a-f0-9]{64}$ && "${LIC_SCHEMA_CI_PG_IMAGE:-}" =~ ^sha256:[a-f0-9]{64}$ && "${LIC_SCHEMA_CI_PG_TOKEN:-}" =~ ^[a-f0-9]{32}$ ]] || return 1
  [[ "${LIC_SCHEMA_CI_PG_COMPONENT:-}" == postgres || "${LIC_SCHEMA_CI_PG_COMPONENT:-}" == base ]] || return 1
  [[ "${LIC_SCHEMA_CI_PG_NAME:-}" == "leti-cook-schema-$LIC_SCHEMA_CI_PG_COMPONENT-$LIC_SCHEMA_CI_PG_TOKEN" && ( "$1" == "$LIC_SCHEMA_CI_PG_ID" || "$1" == "$LIC_SCHEMA_CI_PG_NAME" ) ]] || return 1
  actual="$(docker container inspect --format '{{.Id}}|{{.Name}}|{{.Image}}|{{index .Config.Labels "org.leti-cook.schema-ci"}}|{{index .Config.Labels "org.leti-cook.schema-ci.component"}}|{{.HostConfig.NetworkMode}}' "$LIC_SCHEMA_CI_PG_ID")" || return 1
  expected="$LIC_SCHEMA_CI_PG_ID|/$LIC_SCHEMA_CI_PG_NAME|$LIC_SCHEMA_CI_PG_IMAGE|$LIC_SCHEMA_CI_PG_TOKEN|$LIC_SCHEMA_CI_PG_COMPONENT|none"
  [[ "$actual" == "$expected" ]] || { echo 'Private PG identity mismatch.' >&2; return 1; }
}

schema_ci_assert_exited() {
  local index="$1" state
  schema_ci_verify "$index" "${SCHEMA_CI_IDS[$index]}" || return 1
  state="$(docker container inspect --format '{{.State.Status}}|{{.State.ExitCode}}' "${SCHEMA_CI_IDS[$index]}")" || return 1
  [[ "$state" == 'exited|0' ]] || { echo 'Schema CI process did not exit successfully.' >&2; return 1; }
}
