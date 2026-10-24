FROM postgres:17.11-bookworm@sha256:3645570cccdfa447589da9f57dd740faa29b30938e861289a5574b6ca6b03826

# Preserve upstream initialization and volume ownership, then drop privileges
# with Debian's setpriv. Fail the build if the upstream entrypoint changes.
RUN set -eux; \
    test -x /usr/bin/setpriv; \
    command -v getent; \
    entrypoint=/usr/local/bin/docker-entrypoint.sh; \
    test "$(grep -Ec '^[[:space:]]*exec gosu postgres "\$BASH_SOURCE" "\$@"[[:space:]]*$' "$entrypoint")" -eq 1; \
    sed -i 's/^\([[:space:]]*\)exec gosu postgres "\$BASH_SOURCE" "\$@"[[:space:]]*$/\1export HOME="$(getent passwd postgres | cut -d: -f6)"; test -n "$HOME"; exec \/usr\/bin\/setpriv --reuid=postgres --regid=postgres --init-groups -- "$BASH_SOURCE" "$@"/' "$entrypoint"; \
    bash -n "$entrypoint"; \
    if grep -w gosu "$entrypoint"; then exit 1; fi; \
    rm /usr/local/bin/gosu; \
    test ! -e /usr/local/bin/gosu; \
    test ! -L /usr/local/bin/gosu
