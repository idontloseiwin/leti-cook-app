"""Checked inventory of current SQL, repository cases and synthetic fixture data."""
from pathlib import Path
import hashlib
import importlib
import json
import re

ROOT = Path(__file__).resolve().parents[2]
DATA = Path("tests/schema25_regressions/data")
CASE_MODULES = {"cases_retention", "cases_jobs", "cases_user_work", "cases_policy", "cases_readiness"}


def require(value, code):
    if not value:
        raise AssertionError(code)


def _read_checked(root, directory, name, entry, prefix):
    path = root / directory / name
    require(path.resolve().is_relative_to(root) and not path.is_symlink(), prefix + "_escape")
    require(path.is_file(), prefix + "_missing")
    raw = path.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == entry["sha256"], prefix + "_digest")
    return raw


def load(root=ROOT):
    root = Path(root).resolve()
    data = json.loads((root / "tests/schema25_regressions/manifest.json").read_text(encoding="utf-8"))
    require(data["format"] == 2 and data["schema_version"] == 25, "manifest_version")
    require(len(data["sql"]) == data["expected_sql_count"] == 10, "sql_manifest_count")
    require(len(data["python"]) == data["expected_python_count"] == 17, "python_manifest_count")
    require(set(data["sql"]) == {p.name for p in (root / "tests/sql").glob("*.sql")}, "sql_manifest_coverage")
    for name, entry in data["sql"].items():
        require(re.fullmatch(r"[0-9]{2}_[a-z_]+\.sql", name), "sql_path")
        _read_checked(root, "tests/sql", name, entry, "sql")
        require(entry["row_budget"] == (15000 if name == "40_user_work_limits.sql" else 1000), "sql_row_bound")

    consumers = {name: set() for name in data["fixtures"]}
    for name, entry in data["python"].items():
        require(re.fullmatch(r"[a-z][a-z_]+", name), "case_name")
        # inventory_window: 3000 core rows plus one read-model row and one queue
        # marker per inventory video (schema 35).
        require(entry["row_budget"] == (6500 if name == "inventory_window" else 1000), "python_row_bound")
        require(entry["owners"] and entry["assertions"] and isinstance(entry["fixtures"], list), "case_contract")
        require(len(entry["fixtures"]) == len(set(entry["fixtures"])), "case_fixture_duplicate")
        require(entry["callable"] in {module + "." + name for module in CASE_MODULES}, "case_binding")
        for fixture in entry["fixtures"]:
            require(fixture in consumers, "case_fixture_missing")
            consumers[fixture].add(name)

    for name, entry in data["fixtures"].items():
        require(re.fullmatch(r"[a-z][a-z_]+\.sql", name), "fixture_path")
        require(entry["kind"] in ("seed", "expression"), "fixture_kind")
        require(entry["cases"] and len(entry["cases"]) == len(set(entry["cases"]))
                and set(entry["cases"]) == consumers[name], "fixture_consumers")
        _read_checked(root, DATA, name, entry, "fixture")
    require(set(data["fixtures"]) == {p.name for p in (root / DATA).iterdir() if p.is_file()},
            "fixture_manifest_coverage")
    return data


def load_fixture(name, *, kind, root=ROOT):
    """Read only a named, pinned current fixture of the requested kind."""
    root = Path(root).resolve()
    data = load(root)
    require(name in data["fixtures"], "fixture_name")
    entry = data["fixtures"][name]
    require(entry["kind"] == kind, "fixture_kind")
    return _read_checked(root, DATA, name, entry, "fixture").decode("utf-8")


def validate_dispatch(data, cases):
    """Names, callables and their current owners must all agree before connecting."""
    require(set(cases) == set(data["python"]), "exact_dispatch")
    require(all(callable(case) for case in cases.values()), "case_binding")
    require(len({id(case) for case in cases.values()}) == len(cases), "case_binding")
    for name, entry in data["python"].items():
        module_name, function_name = entry["callable"].split(".")
        owner = importlib.import_module("schema25_regressions." + module_name)
        require(cases[name] is getattr(owner, function_name), "case_binding")
