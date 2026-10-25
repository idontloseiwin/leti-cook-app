# Research query checks

The query modules and router factory are not connected to the running application.
These checks exercise their contract without changing existing catalogue routes.

Run the offline suite from the repository root with Python 3.12:

```bash
python -B -m unittest discover -s tests -p 'test_research_query*.py'
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
and remove its test credentials. No YouTube calls are made. The fixture bypasses
HOT admission only during this isolated repository test; HTTP integration and
performance at installation scale require separate checks.
