"""Generate docs/SQL_CATALOG.md from schema/schema.sql; stdlib only, no database.

The catalogue lists every table, view and function of the fresh-installation
schema with facts read from the schema and the source: signature, result,
language, security, execute grants, the source pins that the schema's own
assert functions keep, and where each function is referenced (other SQL
objects, and Python string literals such as RPC names).

Usage:
  sql_catalog.py --write   rewrite docs/SQL_CATALOG.md
  sql_catalog.py --check   exit 1 if it is out of date or a source pin is stale
"""
from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schema/schema.sql"
OUTPUT = ROOT / "docs/SQL_CATALOG.md"
SCHEMAS = ("public", "operator_private")
DOLLAR = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")
STRING = re.compile(r"'(?:[^']|'')*'")
# Pins name signatures as to_regprocedure accepts them; pg_dump spells types out.
TYPE_ALIASES = {"timestamptz": "timestamp with time zone", "int": "integer", "int4": "integer",
                "int8": "bigint", "bool": "boolean", "varchar": "character varying"}
PIN = re.compile(r"'((?:public|operator_private)\.[a-z0-9_]+)\(([^']*)\)'\s*,\s*'([0-9a-f]{32})'")


@dataclass
class Function:
    schema: str
    name: str
    types: tuple[str, ...]
    returns: str
    language: str
    definer: bool
    volatility: str
    body: str
    grants: set[str] = field(default_factory=set)
    sql_callers: set[str] = field(default_factory=set)
    python_callers: set[str] = field(default_factory=set)
    pinned_by: set[str] = field(default_factory=set)

    @property
    def key(self) -> str:
        return f"{self.schema}.{self.name}({','.join(self.types)})"

    @property
    def label(self) -> str:
        return f"{self.name}({', '.join(self.types)})"


@dataclass
class Relation:
    kind: str
    schema: str
    name: str
    columns: int = 0
    rls: bool = False
    invoker: bool | None = None
    grants: dict[str, str] = field(default_factory=dict)


def statements(text: str) -> list[str]:
    """Split pg_dump output into statements, keeping dollar-quoted bodies byte for byte."""
    result, current, tag = [], [], None
    for line in text.split("\n"):
        if not current and (not line.strip() or line.lstrip().startswith("--")):
            continue
        current.append(line)
        for match in DOLLAR.finditer(line):
            if tag is None:
                tag = match.group(0)
            elif match.group(0) == tag:
                tag = None
        if tag is None and line.rstrip().endswith(";"):
            result.append("\n".join(current))
            current = []
    return result


def split_args(args: str) -> list[str]:
    parts, depth, current = [], 0, ""
    for char in args:
        depth += (char == "(") - (char == ")")
        if char == "," and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += char
    return [p for p in [*parts, current.strip()] if p]


def arg_type(arg: str) -> str:
    """'p_since timestamp with time zone DEFAULT now()' -> 'timestamp with time zone'."""
    name, _, rest = arg.partition(" ")
    if not name.startswith("p_"):
        raise ValueError(f"unexpected parameter: {arg}")
    return re.split(r"\s+DEFAULT\s+", rest, maxsplit=1)[0].strip()


def parse_function(statement: str) -> Function:
    """Header facts from the text without CR; the body exactly as stored in prosrc."""
    header, _, _ = statement.replace("\r", "").partition("\n    AS ")
    match = re.match(r"CREATE FUNCTION (\w+)\.(\w+)\((.*?)\) RETURNS (.*?)\n    LANGUAGE (\w+)(.*?)$",
                     header, re.S | re.M)
    if not match:
        raise ValueError(f"unparsed function header: {header[:120]}")
    schema, name, args, returns, language, flags = match.groups()
    opening = DOLLAR.search(statement, statement.index("\n    AS "))
    closing = statement.rindex(opening.group(0))
    returns = " ".join(returns.split())
    if returns.startswith("TABLE("):
        returns = f"table ({len(split_args(returns[6:-1]))} columns)"
    volatility = "immutable" if "IMMUTABLE" in flags else "stable" if "STABLE" in flags else "volatile"
    return Function(schema, name, tuple(arg_type(a) for a in split_args(args)), returns, language,
                    "SECURITY DEFINER" in flags, volatility, statement[opening.end():closing])


def source_md5(body: str) -> str:
    return hashlib.md5(body.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")).hexdigest()


def python_source_inventory() -> tuple[str, ...]:
    """Explicit actual source roots, valid for both Git and isolated packages."""
    paths = set(ROOT.glob('*.py'))
    for directory in ('application', 'tools'):
        paths.update((ROOT / directory).rglob('*.py'))
    return tuple(sorted(path.relative_to(ROOT).as_posix() for path in paths
                        if path.is_file() and not path.is_symlink()
                        and not set(path.relative_to(ROOT).parts) & {'node_modules', '__pycache__', '.venv', 'venv'}))


def python_strings() -> dict[str, set[str]]:
    """Every string literal in the shipped Python source, with the files that contain it."""
    found: dict[str, set[str]] = {}
    for name in python_source_inventory():
        if name.startswith("tests/"):
            continue
        tree = ast.parse((ROOT / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                found.setdefault(node.value, set()).add(name.removeprefix("application/"))
    return found


def build() -> tuple[list[Relation], list[Function], list[str]]:
    # Some function bodies use CRLF, and a few lines end in CR CR LF. Bodies are
    # kept raw, as PostgreSQL stores them, and hashed the way the pins normalize them.
    relations: dict[str, Relation] = {}
    functions: dict[str, Function] = {}
    others: list[tuple[str, str]] = []
    for raw in statements(SCHEMA.read_bytes().decode("utf-8")):
        statement = raw.replace("\r", "")
        if statement.startswith("CREATE FUNCTION "):
            function = parse_function(raw)
            functions[function.key] = function
        elif match := re.match(r"CREATE TABLE (\w+)\.(\w+) \(\n(.*)\n\);", statement, re.S):
            columns = [line for line in match.group(3).split("\n")
                       if line.startswith("    ") and not line.strip().startswith("CONSTRAINT ")]
            relations[f"{match.group(1)}.{match.group(2)}"] = Relation("table", match.group(1), match.group(2),
                                                                         columns=len(columns))
            others.append((f"table {match.group(2)}", statement))
        elif match := re.match(r"CREATE VIEW (\w+)\.(\w+)(?: WITH \(security_invoker='(\w+)'\))?", statement):
            relations[f"{match.group(1)}.{match.group(2)}"] = Relation(
                "view", match.group(1), match.group(2), invoker=match.group(3) == "true")
            others.append((f"view {match.group(2)}", statement))
        elif match := re.match(r"CREATE TRIGGER (\w+) .*? ON (\w+)\.(\w+)", statement, re.S):
            others.append((f"trigger {match.group(1)} on {match.group(3)}", statement))
        elif match := re.match(r"ALTER TABLE (?:ONLY )?(\w+)\.(\w+) ENABLE ROW LEVEL SECURITY;", statement):
            relations[f"{match.group(1)}.{match.group(2)}"].rls = True
        elif match := re.match(r"GRANT ALL ON FUNCTION (\w+)\.(\w+)\((.*)\) TO (\w+);", statement, re.S):
            key = f"{match.group(1)}.{match.group(2)}({','.join(arg_type(a) for a in split_args(match.group(3)))})"
            functions[key].grants.add(match.group(4))
        elif match := re.fullmatch(r"GRANT ([A-Z, ]+) ON (?:TABLE )?((?:\w+\.\w+)(?:\s*,\s*\w+\.\w+)*) TO (\w+);", statement.strip()):
            for name in re.split(r"\s*,\s*", match.group(2)):
                relations[name].grants[match.group(3)] = match.group(1).replace(' ', '').lower()
        elif match := re.match(r"(ALTER TABLE|CREATE (?:UNIQUE )?INDEX \w+ ON) (?:ONLY )?(\w+)\.(\w+)", statement):
            others.append((f"table {match.group(3)}", statement))
        elif statement.startswith(("DO ", "SELECT ", "INSERT ")):
            others.append(("fresh-install block", statement))

    names = {f.name for f in functions.values()}
    reference = re.compile(r"(?<![\w.$])(?:(?:public|operator_private)\.)?(" + "|".join(sorted(names)) + r")\s*\(")
    by_name: dict[str, list[Function]] = {}
    for function in functions.values():
        by_name.setdefault(function.name, []).append(function)
    sources = [(f"{f.name}", f.body, f) for f in functions.values()] + [(label, body, None) for label, body in others]
    for label, body, owner in sources:
        for called in {m.group(1) for m in reference.finditer(STRING.sub("''", body))}:
            for target in by_name[called]:
                if target is not owner:
                    target.sql_callers.add(label)

    strings = python_strings()
    for function in functions.values():
        function.python_callers = strings.get(function.name, set())

    problems = []
    pin_sources = [(function.name, function.body) for function in functions.values()]
    pin_sources.extend(('db.readiness.intrinsic_capabilities', value) for value, paths in strings.items()
                       if 'db/readiness/intrinsic_capabilities.py' in paths and PIN.search(value))
    for pinning_name, pinning_body in pin_sources:
        for schema_name, types, digest in PIN.findall(pinning_body):
            key = f"{schema_name}({','.join(TYPE_ALIASES.get(t.strip(), t.strip()) for t in types.split(','))})" \
                if types else f"{schema_name}()"
            target = functions.get(key)
            if target is None:
                problems.append(f"{pinning_name} pins {key}, which the schema does not define")
                continue
            target.pinned_by.add(pinning_name)
            if source_md5(target.body) != digest:
                problems.append(f"{pinning_name} pins {key} to {digest}, but its source is {source_md5(target.body)}")
    return sorted(relations.values(), key=lambda r: (r.kind, r.schema, r.name)), \
        sorted(functions.values(), key=lambda f: (f.schema, f.name, f.types)), problems


def cell(values: set[str], limit: int = 4) -> str:
    items = sorted(values)
    if not items:
        return ""
    shown = ", ".join(f"`{item}`" for item in items[:limit])
    return shown + (f" +{len(items) - limit}" if len(items) > limit else "")


def render(relations: list[Relation], functions: list[Function]) -> str:
    tables = [r for r in relations if r.kind == "table"]
    views = [r for r in relations if r.kind == "view"]
    unreferenced = [f for f in functions if not (f.sql_callers or f.python_callers)]
    lines = [
        "# SQL catalogue",
        "",
        "<!-- Generated by tools/sql_catalog.py from schema/schema.sql. Do not edit by hand. -->",
        "",
        "Every table, view and function of the fresh-installation schema",
        "(`schema/schema.sql`), generated by `tools/sql_catalog.py`. After changing the",
        "schema, run `python3 tools/sql_catalog.py --write`; a unit test fails while",
        "this file is out of date or a SQL/Python contract pins a stale function",
        "source. See [Contributing](../CONTRIBUTING.md#changing-the-schema) for schema changes.",
        "",
        f"{len(tables)} tables, {len(views)} views and {len(functions)} functions "
        f"({sum(f.schema == 'public' for f in functions)} in `public`, "
        f"{sum(f.schema == 'operator_private' for f in functions)} in `operator_private`; "
        f"{sum(f.definer for f in functions)} run as `SECURITY DEFINER`). "
        "The backend uses `service_role` for collection and writes, and `lic_reader` "
        "for HTTP reads through admitted views and user-work tables; "
        "`PUBLIC` cannot execute any function.",
        "",
        "Columns in the function tables:",
        "",
        "- **Result**: the return type; `table (N columns)` for `RETURNS TABLE`.",
        "- **Runs as**: `definer` (`SECURITY DEFINER`, with the owner's rights) or `invoker`; "
        "then the volatility.",
        "- **Backend may call**: `yes` when `service_role` has `EXECUTE`.",
        "- **Pinned by**: SQL/Python contracts that check this function's source checksum. Changing its "
        "body means updating that pin.",
        "- **Referenced by SQL**: functions, views, triggers, tables and the fresh-install block whose "
        "text calls it.",
        "- **Named in Python**: files that contain the function name as a string literal, which is "
        "how the backend names an RPC. A name assembled at run time is not found.",
        "",
        "## Tables",
        "",
        "| Table | Columns | Row-level security | `service_role` may | `lic_reader` may |",
        "| --- | --- | --- | --- | --- |",
    ]
    for table in tables:
        lines.append(f"| `{table.schema}.{table.name}` | {table.columns} | {'on' if table.rls else 'off'} | "
                     f"{table.grants.get('service_role', 'nothing').replace(',', ', ')} | "
                     f"{table.grants.get('lic_reader', 'nothing').replace(',', ', ')} |")
    lines += ["", "## Views", "", "| View | Runs as | `service_role` may | `lic_reader` may |", "| --- | --- | --- | --- |"]
    for view in views:
        lines.append(f"| `{view.schema}.{view.name}` | {'invoker' if view.invoker else 'owner'} | "
                     f"{view.grants.get('service_role', 'nothing').replace(',', ', ')} | "
                     f"{view.grants.get('lic_reader', 'nothing').replace(',', ', ')} |")
    for schema in SCHEMAS:
        lines += ["", f"## Functions in `{schema}`", "",
                  "| Function | Result | Language | Runs as | Backend may call | Pinned by | "
                  "Referenced by SQL | Named in Python |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- |"]
        for f in (f for f in functions if f.schema == schema):
            lines.append(f"| `{f.label}` | `{f.returns}` | {f.language} | "
                         f"{'definer' if f.definer else 'invoker'}, {f.volatility} | "
                         f"{'yes' if 'service_role' in f.grants else 'no'} | {cell(f.pinned_by, 2)} | "
                         f"{cell(f.sql_callers)} | {cell(f.python_callers, 3)} |")
    lines += ["", "## Functions without a reference", "",
              "No SQL object calls these and no Python string names them. Some are entry points kept on "
              "purpose; check before removing one.", ""]
    lines += [f"- `{f.schema}.{f.label}`" for f in unreferenced] or ["None."]
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    relations, functions, problems = build()
    for problem in problems:
        print(problem, file=sys.stderr)
    text = render(relations, functions)
    if args.write:
        OUTPUT.write_bytes(text.encode("utf-8"))
        return 1 if problems else 0
    current = OUTPUT.read_bytes().decode("utf-8") if OUTPUT.exists() else ""
    if current != text:
        print(f"{OUTPUT.relative_to(ROOT)} is out of date; run python3 tools/sql_catalog.py --write", file=sys.stderr)
        return 1
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
