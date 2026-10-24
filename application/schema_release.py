"""Generated release schema identity; no database or provider dependencies."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re

FORMAT = "leti-cook-schema-release"
MAX_METADATA_BYTES = 4096


@dataclass(frozen=True)
class SchemaRelease:
    baseline_id: str
    schema_version: int

    def __post_init__(self):
        if (type(self.baseline_id) is not str
                or re.fullmatch(r"[a-z][a-z0-9-]{1,79}", self.baseline_id) is None
                or type(self.schema_version) is not int or not 1 <= self.schema_version <= 1000000):
            raise ValueError("schema_release_metadata_invalid")

    def document(self):
        return {"format": FORMAT, "format_version": 1,
                "baseline_id": self.baseline_id, "schema_version": self.schema_version}


def parse_schema_release(data: bytes) -> SchemaRelease:
    if type(data) is not bytes or not 1 <= len(data) <= MAX_METADATA_BYTES:
        raise ValueError("schema_release_metadata_invalid")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=unique)
        if (type(value) is not dict or set(value) != {"format", "format_version", "baseline_id", "schema_version"}
                or value["format"] != FORMAT or type(value["format_version"]) is not int
                or value["format_version"] != 1):
            raise ValueError()
        return SchemaRelease(value["baseline_id"], value["schema_version"])
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ValueError("schema_release_metadata_invalid") from None


def load_schema_release(path=None) -> SchemaRelease:
    target = Path(path) if path is not None else Path(__file__).with_name("schema-release.json")
    with target.open("rb") as stream:
        return parse_schema_release(stream.read(MAX_METADATA_BYTES + 1))
