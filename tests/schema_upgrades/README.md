# Schema upgrade tests

Run only in an isolated test environment, after preparing the pinned image:

```bash
bash tools/prepare_schema_ci_images.sh
LIC_SCHEMA_UPGRADES_MAX_ROWS=1000 bash tools/check_schema_upgrades.sh
```

The suite creates two independent disposable PostgreSQL 17 clusters, one at a
time. Each has no network or published ports and is limited to 768 MiB and
1.5 CPU. Test data stay below 1,000 rows. The runner verifies container ownership
before mutation or removal and cleans up its containers and volumes.

The checks compare the actual baseline plus upgrades with the current fresh
schema. Separate synthetic fixtures exercise rollback, interrupted results,
repeated application, locking and schema drift. They are test inputs, not
application migrations.

The comparator checks database objects, permissions and configuration seeds.
Unsupported object types fail rather than being silently ignored. When extending
the schema, add comparator coverage and regression cases for any new object type.

Offline orchestration checks are in `tests/test_schema_upgrades.py`; they use
fake Docker and do not replace this native database run.
