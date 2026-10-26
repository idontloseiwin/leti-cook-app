"""Run a native module's cases on one disposable PostgreSQL fixture, in order.

The fixture is restored after every case; a failed case cannot affect the
next one. Prints one JSON line per case and a summary, and exits non-zero
when any case fails. Needs the same suite-owned DSNs as the schema25 suite.
"""
import json
import os
import time

from schema25_regressions.fixtures import Fixture, admission, dsns
from schema25_regressions.manifest import require


def run_cases(name, cases, *, row_cap=1000):
    require(os.getenv("LIC_SCHEMA_CI_MAX_ROWS") == str(row_cap), "explicit_row_budget")
    report = dict(module=name, cases=[])
    started = time.monotonic()
    with admission() as gate:
        f = Fixture(*dsns(), gate)
        try:
            for case in cases:
                before = f.checks
                item = dict(name=case.__name__, status="failed")
                try:
                    case(f)
                    item.update(status="passed", rows=f.observe())
                except Exception as error:
                    item.update(error_type=type(error).__name__,
                                check_code=str(error) if isinstance(error, AssertionError) else None,
                                sqlstate=getattr(error, "sqlstate", None))
                item["checks"] = f.checks - before
                report["cases"].append(item)
                print(json.dumps(item, default=str), flush=True)
                f.reset()
                require(time.monotonic() < f.deadline, "suite_deadline")
        finally:
            f.close()
    failed = [item["name"] for item in report["cases"] if item["status"] != "passed"]
    print(json.dumps(dict(module=name, status="failed" if failed else "passed", cases=len(report["cases"]),
                          failed=failed, seconds=round(time.monotonic() - started, 3))), flush=True)
    return 1 if failed else 0
