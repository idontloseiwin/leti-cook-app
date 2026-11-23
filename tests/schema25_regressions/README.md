# Schema regression tests

These checks run the current schema and repository operations in disposable
PostgreSQL 17 and Python containers. They do not use an existing installation.

## Run

Prepare the locked images and dependencies in an isolated test environment:

```bash
bash tools/prepare_schema_ci_images.sh
bash tools/prepare_schema_ci_python.sh "$PWD/.schema-ci-python-deps"
export LIC_SCHEMA_CI_DEPS="$PWD/.schema-ci-python-deps"
export LIC_SCHEMA_CI_MAX_ROWS=15000
bash tools/check_schema_postgres.sh
```

Preparation downloads dependencies. Database execution uses at most two
containers, each limited to 768 MiB, 1.5 CPU and 128 PIDs, without published
ports or provider access. The largest fixture has a 15,000-row budget. Owned
containers are removed after execution, including on failure.

## Contents

`manifest.json` lists SQL groups, Python cases, fixture hashes and row budgets.
Files in `data/` contain synthetic inputs. Keep fixture bytes, hashes and
assertions consistent when changing a case. Missing groups or empty cases fail.

The suite covers collection, schedules, availability, user work, cleanup,
readiness and database permissions. It verifies bootstrap, restart and
dump/restore, and restores initial test state between repository cases.

Focused offline checks:

```bash
python3 -B -m unittest discover -s tests -p 'test_schema25*.py'
python3 -B -m unittest discover -s tests -p 'test_schema_ci*.py'
```

These do not open PostgreSQL or contact YouTube. They do not replace the native
database run or an application installation check.
