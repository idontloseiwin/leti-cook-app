"""Compare a separate source build with the shipped frontend, without editing it."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SHIPPED = ROOT / "application/frontend/dist-self-hosted"


def differences(shipped: Path, built: Path) -> list[str]:
    for directory in (shipped, built):
        if not directory.is_dir():
            raise ValueError(f"Frontend directory is missing: {directory}")
    if shipped.resolve() == built.resolve():
        raise ValueError("Build into a separate directory; comparing a build to itself is invalid")
    expected = {p.relative_to(shipped): p for p in shipped.rglob("*") if p.is_file()}
    actual = {p.relative_to(built): p for p in built.rglob("*") if p.is_file()}
    if not expected or not actual:
        raise ValueError("Frontend directories must not be empty")
    result = [f"Missing from source build: {p.as_posix()}" for p in sorted(expected.keys() - actual.keys())]
    result += [f"Not shipped: {p.as_posix()}" for p in sorted(actual.keys() - expected.keys())]
    result += [f"Different bytes: {p.as_posix()}" for p in sorted(expected.keys() & actual.keys())
               if expected[p].read_bytes() != actual[p].read_bytes()]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--built-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        errors = differences(SHIPPED, args.built_dir)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if errors:
        print("\n".join(errors), file=sys.stderr)
        print("Rebuild dist-self-hosted, update packaging/files.json if paths changed, and commit both source and output.", file=sys.stderr)
        return 1
    print("Shipped frontend matches the separate source build byte for byte.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
