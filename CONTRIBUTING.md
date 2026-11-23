# Contributing

Report reproducible bugs, suggest research workflows or send a focused change.
Explain the user-visible result and the checks you ran. Keep secrets, personal
datasets and private logs out of issues and commits. Use
[Security](SECURITY.md#reporting-a-vulnerability) for sensitive reports.

## Development setup

The backend is in `application/`, the web app in `application/frontend/`, and
offline checks in `tests/`. Use Python 3.12 and the Node version in
`application/frontend/.nvmrc`.

```bash
python3 -m pip install --require-hashes --no-deps -r application/requirements.lock
npm --prefix application/frontend ci
```

Windows Python tests also need `tzdata`. The backend requires
`LIC_DEPLOYMENT_PROFILE=self-hosted`; the installer sets it. See
[installation from source](docs/INSTALLATION.md#from-source-development).

## Running the checks

Run the checks relevant to your change. For example:

```bash
python3 -B -m unittest discover -s tests -p 'test_honest_status.py'
node tests/test_explorer_v3_routes.mjs
node --test tests/test_chromium_bridge.mjs tests/test_chromium_content.mjs
```

The complete Python suite is `python3 -B -m unittest discover -s tests`.
Ordinary Python and Node checks use fixtures rather than a live installation or
YouTube. Some filesystem cases require an isolated Linux environment with root
permissions; do not bypass permission checks to run them elsewhere.

Database checks require disposable PostgreSQL 17 containers. Image preparation
uses the network to apply Debian security updates to OpenSSL and apt and enforce
fixed minimum versions. The actual database fixtures still run with networking disabled.
Use the preparation and execution instructions in the [schema regression guide](tests/schema25_regressions/README.md)
and the upgrade test guide, `tests/schema_upgrades/README.md` in the source
repository. Never run fixtures against a user's database. Live collection
checks need a separate disposable installation and your own API key.

## Pull request checks

`CI result` is the required merge check. The same routing runs on pull requests
and pushes to `main`. Changes limited to the explicit documentation allowlist in
`tools/ci_policy.py` receive local link/anchor checks and source-package validation.
They do not start Docker, PostgreSQL tests or frontend builds.

Every other change, including mixed code/documentation changes, generated SQL
documentation and CI changes, runs the full application, schema, dependency,
image and signed-delivery checks. Incomplete change detection selects full CI;
missing, failed or unexpectedly skipped checks cannot pass the final result.
Weekly dependency and image audits run independently of file changes.
The dependency audit installs `pip-audit` only from the hash-checked
`tools/requirements-audit.lock` and audits that lock too. It fails on any known
vulnerability in the Python locks, on a high or critical one in the web app's
shipped packages, and on a critical one anywhere in the web app's lock,
including the build tools that produce the shipped web app.

Run the documentation check locally with `python3 -B tools/check_docs.py`.
The native database runner includes `tests/update_copy_lifecycle_pg.py`: copy
admission after a completed playlist absence and before the first successful
page of a retained retry, with damaged-proof and expiry counterexamples.
The checks use no live installation or YouTube requests. Delivery checks use an
ephemeral signing key and a local registry; publication remains a separate tag
workflow.

## Changing the web app

Edit `application/frontend/src/`, then rebuild:

```bash
npm --prefix application/frontend run build:self-hosted
```

Commit the source and the `dist-self-hosted/` output together. Do not edit
compiled assets directly. A release package contains only the built
`dist-self-hosted/` app; its source stays in this repository. Update the asset
paths in `packaging/files.json` when built file names change; source files have
no entries there.

## Changing research operations

Library, the research workshop and Home share one stored-data query backend in
`application/research_query/`. Each whole-population operation is one module in
`application/research_query/operation_kinds/`; its package docstring lists what
a new one provides. An operation runs only if its policy's installation
switches are on (`application/web/research.py`). A new policy needs its own
entry there. Run the PostgreSQL query suites when changing an operation.

## Checking the YouTube API registry

The runtime checks the integrity of method and field definitions and support states. Run the offline checks with `python -m unittest discover -s tests -p test_provider_registry.py`.

## Changing the schema

`schema/schema.sql` defines a fresh database. The baseline and numbered files in
`schema/steps/` define upgrades. The baseline, `schema/baseline/schema.sql`, is
the schema of version 51 and never changes; the first step is `0052`. Keep both
paths consistent. Do not change an already distributed migration; add a new
numbered step instead.

Update schema checksums, manifest entries, configuration seeds and affected
function pins together. Generate and check the schema identity and SQL catalogue:

```bash
python3 tools/check_schema_release.py --write
python3 tools/check_schema_release.py --check
python3 tools/sql_catalog.py --write
```

Validate fresh installation and upgrades in the disposable tests above. Keep
database queries in `application/db/`, use parameterized SQL and bounded
transactions, and keep YouTube requests outside database transactions.

### Home at database scale

The optional Home check generates 250,200 synthetic videos across 900 channels
in a disposable PostgreSQL 17 container. Run these commands from the repository
root on Linux with Docker. Choose a dependency directory that does not yet exist.

```bash
export LIC_SCHEMA_CI_DEPS="$PWD/.schema-ci-deps"
bash tools/prepare_schema_ci_python.sh "$LIC_SCHEMA_CI_DEPS"
source tools/schema_ci_postgres_image.sh
schema_ci_postgres_build "$PWD"
bash tools/check_research_query_postgres.sh --large-home
```

With the dependencies and database image prepared, repeat only the last command.
The standard database suite runs its smaller Home fixtures.

## Dependencies and documentation

The MCP endpoint is `application/research_mcp.py` with its routes and the owner's
key actions in `application/web/agents.py`; `tests/test_agent_mcp_http.py` covers
them through the real auth, setup and research middleware. The stdio bridge for
Claude Desktop lives in `integrations/mcp/` and uses the standard library only.
Run its tests with no installed packages:
`python -I -B -m unittest discover -s integrations/mcp/tests -v`.
`tests/research_mcp_pg.py` is an explicit disposable-PG17 test entry point;
it requires the same newly created cluster as `tests/research_query_pg.py` and
drives a served application with the official MCP SDK as a reference client.
The native CI runner includes it. Dependency preparation installs that SDK into
a separate `mcp` subdirectory from `tests/mcp_reference/requirements.lock`; it is
never shipped. Database execution remains offline and uses backend packages first.

Keep MCP thin: expose the backend's registries, validation and query executor;
do not add another filter or analytics implementation. Changes to authorization
must cover setup admission, exact routes, revocation during reads and the
trusted-local-mode limitation. New executable operations need a validated help
example in `application/research_query/discovery.py`.

To refresh the reference client lock, use pip-tools 7.5.2 with Python 3.12:
`pip-compile --generate-hashes --no-header --no-annotate --no-emit-index-url --no-emit-trusted-host -c application/requirements.lock -o tests/mcp_reference/requirements.lock tests/mcp_reference/requirements.txt`.
Retain the Windows marker on `pywin32` and audit the lock separately from the backend.

To refresh the audit tool lock, use uv with Python 3.12 and accept only
releases at least two weeks old (set `--exclude-newer` to that date):
`uv pip compile tools/requirements-audit.txt --python-version 3.12 --python-platform linux --generate-hashes --no-annotate --constraint application/requirements.lock --exclude-newer 2026-10-25T00:00:00Z -o tools/requirements-audit.lock`.

Update dependency declarations and lockfiles together. Preserve upstream license
texts. Root notices cover the source package; application notices cover only
components shipped in the backend container. The project license
is [AGPL-3.0-or-later](LICENSE).

Documentation is in English. Describe current behavior and necessary user steps.
Keep release preparation, internal decisions, review reports and development
journals outside the public documentation. `docs/SQL_CATALOG.md` is generated;
do not edit it by hand.
