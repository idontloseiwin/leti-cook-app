"""Product version shared by the application and trusted host release tools.

Importing this module reads no configuration and opens no database or network.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import total_ordering
from pathlib import Path
import re

MAX_VERSION_BYTES = 256
_VERSION = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
)


@total_ordering
@dataclass(frozen=True, eq=False)
class Version:
    """SemVer precedence; build metadata is retained but does not affect order."""

    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()
    build: tuple[str, ...] = ()

    def __str__(self):
        value = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            value += "-" + ".".join(self.prerelease)
        if self.build:
            value += "+" + ".".join(self.build)
        return value

    def _precedence(self):
        identifiers = tuple((0, int(item)) if item.isdigit() else (1, item)
                            for item in self.prerelease)
        return self.major, self.minor, self.patch, not self.prerelease, identifiers

    def __eq__(self, other):
        if not isinstance(other, Version):
            return NotImplemented
        return self._precedence() == other._precedence()

    def __lt__(self, other):
        if not isinstance(other, Version):
            return NotImplemented
        return self._precedence() < other._precedence()

    def __hash__(self):
        return hash(self._precedence())


def parse_version(value: str) -> Version:
    """Accept canonical bounded SemVer, without a tag prefix or whitespace."""
    if type(value) is not str or not 1 <= len(value) < MAX_VERSION_BYTES:
        raise ValueError("invalid_release_version")
    match = _VERSION.fullmatch(value)
    if match is None:
        raise ValueError("invalid_release_version")
    major, minor, patch, prerelease, build = match.groups()
    identifiers = tuple(prerelease.split(".")) if prerelease else ()
    if any(item.isdigit() and len(item) > 1 and item.startswith("0") for item in identifiers):
        raise ValueError("invalid_release_version")
    return Version(int(major), int(minor), int(patch), identifiers,
                   tuple(build.split(".")) if build else ())


def parse_version_file(data: bytes) -> str:
    """Read the canonical VERSION representation: ASCII and one final LF."""
    if type(data) is not bytes or not 2 <= len(data) <= MAX_VERSION_BYTES or not data.endswith(b"\n"):
        raise ValueError("invalid_release_version")
    try:
        value = data[:-1].decode("ascii")
    except UnicodeDecodeError:
        raise ValueError("invalid_release_version") from None
    return str(parse_version(value))


def read_version(path: Path | None = None) -> str:
    """Read the adjacent VERSION unless a trusted caller supplies a file path."""
    target = Path(path) if path is not None else Path(__file__).with_name("VERSION")
    with target.open("rb") as source:
        data = source.read(MAX_VERSION_BYTES + 1)
    return parse_version_file(data)
