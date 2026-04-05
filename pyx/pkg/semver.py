"""Semantic versioning utilities for PyX package manager."""
from __future__ import annotations

import re
from dataclasses import dataclass

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_CONSTRAINT_TOKEN_RE = re.compile(r"^([><=!^~]*)(.+)$")


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, s: str) -> Version:
        m = _VERSION_RE.match(s.strip())
        if not m:
            raise ValueError(f"invalid version string: {s!r}")
        return cls(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


def _matches_single(version: Version, constraint: str) -> bool:
    constraint = constraint.strip()
    if not constraint or constraint == "*":
        return True
    m = _CONSTRAINT_TOKEN_RE.match(constraint)
    if not m:
        raise ValueError(f"invalid constraint: {constraint!r}")
    op, ver_str = m.group(1), m.group(2)
    ver = Version.parse(ver_str)
    if op in ("", "==", "="):
        return version == ver
    if op == "!=":
        return version != ver
    if op == ">=":
        return version >= ver
    if op == ">":
        return version > ver
    if op == "<=":
        return version <= ver
    if op == "<":
        return version < ver
    if op == "^":
        # Compatible release: same major, >= minor.patch
        return version.major == ver.major and version >= ver
    if op == "~":
        # Approximately equal: same major.minor, >= patch
        return version.major == ver.major and version.minor == ver.minor and version >= ver
    raise ValueError(f"unknown constraint operator: {op!r}")


def matches_constraint(version: Version, constraint: str) -> bool:
    """Return True if *version* satisfies *constraint*.

    Constraint may be a comma-separated list of individual constraints,
    all of which must be satisfied (logical AND).
    """
    for part in constraint.split(","):
        if not _matches_single(version, part.strip()):
            return False
    return True


def best_matching(versions: list[str], constraint: str) -> str | None:
    """Return the highest version string that satisfies *constraint*, or None."""
    parsed: list[Version] = []
    for v in versions:
        try:
            parsed.append(Version.parse(v))
        except ValueError:
            continue
    matching = [v for v in parsed if matches_constraint(v, constraint)]
    if not matching:
        return None
    return str(max(matching))
