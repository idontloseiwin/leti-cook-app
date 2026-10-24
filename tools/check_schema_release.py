"""Check or regenerate runtime schema identity from the canonical schema manifest."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from application.schema_release import SchemaRelease


def expected_bytes(root=ROOT):
    root = Path(root)
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("schema_manifest_invalid")
            value[key] = item
        return value
    with (root / "schema/manifest.json").open("rb") as source:
        data = source.read(262145)
    if len(data) > 262144:
        raise ValueError("schema_manifest_invalid")
    manifest = json.loads(data.decode("utf-8"), object_pairs_hook=unique)
    if (type(manifest) is not dict or manifest.get("format") != "leti-cook-self-hosted-schema"
            or type(manifest.get("version")) is not int or manifest["version"] != 1):
        raise ValueError("schema_manifest_invalid")
    identity = SchemaRelease(manifest.get("baseline_id"), manifest.get("schema_version"))
    return (json.dumps(identity.document(), indent=2, sort_keys=True) + "\n").encode("utf-8")


def check(root=ROOT, *, write=False):
    root = Path(root)
    expected = expected_bytes(root)
    target = root / "application/schema-release.json"
    if write:
        target.write_bytes(expected)
    elif not target.is_file() or target.read_bytes() != expected:
        raise ValueError("schema_release_metadata_out_of_date")
    return expected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="Regenerate the checked-in runtime metadata.")
    mode.add_argument("--check", action="store_true", help="Check without changing files (default).")
    args = parser.parse_args()
    try:
        check(write=args.write)
    except (OSError, ValueError) as error:
        parser.exit(1, "Schema release metadata check failed: " + type(error).__name__ + ".\n")
    print("Schema release metadata matches the canonical manifest.")


if __name__ == "__main__":
    main()
