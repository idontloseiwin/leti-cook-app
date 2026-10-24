#!/usr/bin/env bash
set -euo pipefail
umask 077
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# Package only the reviewed file selection, excluding this checkout's .git/state.
# The installer copies every file it needs, so the temporary bundle is removed
# after a successful or cancelled installation. On any other exit, including a
# packaging failure or Ctrl+C, it is preserved and its path printed for diagnosis.
release_parent="$(mktemp -d -t leti-cook-release.XXXXXXXX)"
stage=package
finish() {
    local status=$?
    if [[ $status -eq 0 || ( $status -eq 2 && $stage == install ) ]]; then
        rm -rf -- "$release_parent"
    else
        printf 'Release bundle kept for diagnosis: %s\n' "$release_parent" >&2
    fi
    exit "$status"
}
trap finish EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
python3 -B "$repo_dir/tools/package_release.py" --output "$release_parent/release"
stage=install
bash "$release_parent/release/install.sh" "$@"
