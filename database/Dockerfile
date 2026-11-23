FROM postgres:17.11-bookworm@sha256:3645570cccdfa447589da9f57dd740faa29b30938e861289a5574b6ca6b03826

# Apply Debian security fixes while the pinned base awaits its next rebuild.
# Keep the fixed-version floor even if the repository returns a newer update.
RUN apt-get update && \
    apt-get install --yes --no-install-recommends --only-upgrade libssl3 openssl apt libapt-pkg6.0 && \
    dpkg --compare-versions "$(dpkg-query -W -f='${Version}' libssl3)" ge '3.0.22-1~deb12u2' && \
    dpkg --compare-versions "$(dpkg-query -W -f='${Version}' openssl)" ge '3.0.22-1~deb12u2' && \
    dpkg --compare-versions "$(dpkg-query -W -f='${Version}' apt)" ge '2.6.2' && \
    dpkg --compare-versions "$(dpkg-query -W -f='${Version}' libapt-pkg6.0)" ge '2.6.2' && \
    rm -rf /var/lib/apt/lists/*

# Connection rules live in the image, not in the data volume (compose.yaml
# selects this file), so an update and its rollback change them together. Over
# the network only the application's login may connect, and only to its
# database; the superuser signs in from inside this container. The file stays
# root-owned: the database server can read it but not change it.
RUN set -eu; \
    install -d -m 0755 /usr/local/share/leti-cook; \
    printf '%s\n' \
        'local  all        all                          trust' \
        'host   all        all            127.0.0.1/32  scram-sha-256' \
        'host   all        all            ::1/128       scram-sha-256' \
        'host   leti-cook  authenticator  all           scram-sha-256' \
        'host   all        all            all           reject' \
        > /usr/local/share/leti-cook/pg_hba.conf; \
    chmod 0644 /usr/local/share/leti-cook/pg_hba.conf

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
