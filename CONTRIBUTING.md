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

Database checks require disposable PostgreSQL 17 containers. Use the preparation
and execution instructions in the [schema regression guide](tests/schema25_regressions/README.md)
and [upgrade test guide](tests/schema_upgrades/README.md). Never run fixtures
against a user's database. Live collection checks need a separate disposable
installation and your own API key.

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

Run the documentation check locally with `python3 -B tools/check_docs.py`.
The checks use no live installation or YouTube requests. Delivery checks use an
ephemeral signing key and a local registry; publication remains a separate tag
workflow.

## Changing the web app

Edit `application/frontend/src/`, then rebuild:

```bash
npm --prefix application/frontend run build:self-hosted
```

Keep the source and `dist-self-hosted/` output together. Do not edit compiled
assets directly. Update asset paths in `packaging/files.json` when names change.

## Changing the schema

`schema/schema.sql` defines a fresh database. The baseline and numbered files in
`schema/steps/` define upgrades. Keep both paths consistent. Do not change an
already distributed migration; add a new numbered step instead.

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

## Dependencies and documentation

Update dependency declarations and lockfiles together. Preserve upstream license
texts and keep both third-party notice copies consistent. The project license
is [AGPL-3.0-or-later](LICENSE).

Documentation is in English. Describe current behavior and necessary user steps.
Keep release preparation, internal decisions, review reports and development
journals outside the public documentation. `docs/SQL_CATALOG.md` is generated;
do not edit it by hand.
