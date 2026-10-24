#!/usr/bin/env bash
# Sourced/executed by the official postgres image ONLY for a new empty volume.
# This file is not an upgrade, private-dump importer, or restore command.
# The supplied schema is the generated self-hosted artifact, never private data.
set -euo pipefail

if [[ "${POSTGRES_USER:-}" != postgres || "${POSTGRES_DB:-}" != leti-cook ]]; then
    printf '%s\n' 'self_hosted_bootstrap_requires_dedicated_database' >&2
    exit 1
fi
if [[ ! "${AUTHENTICATOR_PASSWORD:-}" =~ ^[A-Za-z0-9_-]{32,}$ ]]; then
    printf '%s\n' 'self_hosted_authenticator_password_requires_32_url_safe_characters' >&2
    exit 1
fi
if [[ ! -s /opt/leti-cook/schema.sql || -e "${PGDATA:?}/.self-hosted-schema-ready" ]]; then
    printf '%s\n' 'self_hosted_bootstrap_requires_fresh_generated_schema' >&2
    exit 1
fi
if [[ ! "${SELF_HOSTED_SCHEMA_SHA256:-}" =~ ^[a-f0-9]{64}$ ]]; then
    printf '%s\n' 'self_hosted_schema_digest_required' >&2
    exit 1
fi
IFS= read -r schema_header < /opt/leti-cook/schema.sql
if [[ "$schema_header" != '-- leti-cook self-hosted clean schema; format=1; application-data=none' ]]; then
    printf '%s\n' 'self_hosted_generated_schema_header_required' >&2
    exit 1
fi
if ! printf '%s  %s\n' "$SELF_HOSTED_SCHEMA_SHA256" /opt/leti-cook/schema.sql | sha256sum --check --status; then
    printf '%s\n' 'self_hosted_generated_schema_digest_mismatch' >&2
    exit 1
fi

# psql's quoted variable is a SQL literal, not shell/SQL interpolation. Only the
# internal authenticator selects a reviewed runtime role; no browser connects.
psql --no-psqlrc --set=ON_ERROR_STOP=1 --username=postgres --dbname=leti-cook <<'SQL'
\getenv authenticator_password AUTHENTICATOR_PASSWORD
begin;
create role anon nologin;
create role authenticated nologin;
create role service_role nologin bypassrls;
create role lic_reader nologin noinherit nobypassrls;
create role authenticator login noinherit password :'authenticator_password';
-- Default bound for the non-owner runtime login. The direct driver applies
-- reviewed transaction-local statement and lock timeouts for each operation;
-- stored functions retain their own configuration.
alter role authenticator set statement_timeout = '60s';
grant service_role, lic_reader to authenticator;
-- Only the backend's non-owner login may connect. No role but the owner may create
-- temporary objects, so none can shadow a table inside a SECURITY DEFINER
-- function (those that use temporary tables run as postgres).
revoke connect, temporary on database "leti-cook" from public;
grant connect on database "leti-cook" to authenticator;
create schema extensions;
create extension pgcrypto with schema extensions;
commit;
SQL

# Separate psql session: generated schema owns its reviewed transaction/settings.
# ON_ERROR_STOP leaves a failed volume unready, without retrying or dropping it.
psql --no-psqlrc --set=ON_ERROR_STOP=1 --username=postgres --dbname=leti-cook \
    --file=/opt/leti-cook/schema.sql

umask 077
printf '%s\n' 'fresh-schema-applied' > "${PGDATA}/.self-hosted-schema-ready"
