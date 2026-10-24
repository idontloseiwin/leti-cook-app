#!/usr/bin/env bash
# Run from the unpacked reviewed release bundle; interactive local folder/port selection.
# Checks the chosen folder and port before asking for the hidden API key.
set -euo pipefail
umask 077
bundle_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# This wrapper installs no host packages implicitly. Ubuntu package setup is
# explicit; existing Docker installations must never be replaced or upgraded.
if ! command -v python3 >/dev/null || ! command -v docker >/dev/null; then
    printf '%s\n' 'Prerequisites: Python 3.10+ and Docker Engine with Compose 2.24.4+.' >&2
    printf '%s\n' 'On a new Ubuntu 24.04 host: sudo apt-get install python3 docker.io docker-compose-v2' >&2
    exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    printf '%s\n' 'Python 3.10 or newer is required.' >&2
    exit 1
fi
exec python3 "$bundle_dir/install.py" "$@" --install
