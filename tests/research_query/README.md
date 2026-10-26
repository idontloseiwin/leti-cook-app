# Research query checks

The owner-only query preview is mounted separately from existing catalogue routes
and is disabled unless the API process has `LIC_RESEARCH_QUERY_PREVIEW=1`.
These checks cover its contract, owner access, HOT admission and source lifetime.

Run the offline suite from the repository root with Python 3.12:

```bash
python -B -m unittest discover -s tests -p 'test_research_query*.py'
node tests/test_research_query_ui.mjs
```

`tests/research_query_pg.py` additionally runs the SQL on PostgreSQL 17. It is
intentionally excluded from automatic unittest discovery. Use a newly created,
disposable cluster, listening only on loopback, with an empty database named
`leti-cook` and a test-only `postgres` password. Never supply an existing
installation or its credentials. The test creates roles and applies the baseline
and numbered migrations; it needs a superuser only for that fixture setup.
Application reads then use the normal restricted `lic_reader` role through the
PostgreSQL gateway.

Set these variables in the test process:

- `LIC_RESEARCH_QUERY_DISPOSABLE=1`: explicit acknowledgement of the disposable cluster.
- `LIC_QUERY_TEST_PORT`: that cluster's loopback port.
- `LIC_QUERY_TEST_PASSWORD`: its temporary test password.
- `LIC_DEPLOYMENT_PROFILE=self-hosted` and `LIC_DATABASE_TRANSPORT=postgres`.

Then run `python -B tests/research_query_pg.py`, stop the disposable cluster,
and remove its test credentials. CI does the same in a disposable container
through `tools/check_research_query_postgres.sh`. No YouTube calls are made. The fixture bypasses
HOT readiness only inside this isolated fixture. The native suite also compares
HTTP pages with direct executor results for all three types. Offline integration
tests separately exercise HOT denial before and after execution. Performance at
installation scale requires separate measurements.

For manual browser verification, use a fresh disposable cluster with the same
variables and run `python -B tests/research_query_preview.py --port PORT --state DIR`.
DIR must be an empty task-owned directory. This serves the built interface and
adds synthetic rows for pagination; sidebar organization and HOT readiness are
fixtures. Write a `stop` file in DIR to stop the web server, then stop the cluster
and remove test credentials. Do not point this entrypoint at an installation.

## Synthetic performance measurements

`tests/research_query_benchmark.py` is a manual experiment, excluded from test
discovery. It requires the same disposable-cluster variables and an empty
PostgreSQL 17 database. It creates deterministic data with skewed channel sizes,
three formats, unknown and expired values, hidden channels and conflicting type
evidence. Constraints and read security remain enabled.

Choose `--size pilot`, `S` or `M`, a new `--output DIR`, and a bounded `--seconds`
value. S contains 100 channels, 10,000 videos and 100,000 observations; M contains
1,000 channels, 250,000 videos and 1,000,000 observations. Agree the total time
and disk budget before running S/M; the cluster owner must enforce that budget
and stop the cluster afterwards. No live database or YouTube calls are used.

The output separates successful reads from timeouts, pool wait from transaction
work, and loopback HTTP from direct service calls. Server plans use EXPLAIN
ANALYZE under `lic_reader`. Measurements use the current executor without
replacing its SQL; comparisons with an earlier run require its preserved inputs.
Synthetic write probes
are small counter updates and expired-snapshot deletions, not collector acceptance.
The first call follows loading and ANALYZE; it is not a cold operating-system
cache measurement. Small samples do not establish tail latency or production SLOs.
