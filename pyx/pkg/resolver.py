"""Dependency resolution and lock-file management for PyX."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .manifest import PackageManifest
from .registry import Registry, RegistryError
from .semver import best_matching


class ResolveError(Exception):
    pass


@dataclass
class LockedPackage:
    name: str
    version: str
    checksum: str


@dataclass
class LockFile:
    """In-memory representation of ``pyx.lock``."""

    packages: list[LockedPackage] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> LockFile:
        """Load a ``pyx.lock`` JSON file, returning an empty :class:`LockFile` if absent."""
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        packages = [
            LockedPackage(
                name=p["name"],
                version=p["version"],
                checksum=p["checksum"],
            )
            for p in data.get("packages", [])
        ]
        return cls(packages=packages)

    def save(self, path: Path) -> None:
        """Persist the lock file as JSON."""
        data = {
            "packages": [
                {"name": p.name, "version": p.version, "checksum": p.checksum}
                for p in self.packages
            ]
        }
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def find(self, name: str) -> LockedPackage | None:
        """Return the locked entry for *name*, or ``None``."""
        for pkg in self.packages:
            if pkg.name == name:
                return pkg
        return None


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve_dependencies(manifest: PackageManifest, registry: Registry) -> LockFile:
    """Resolve all direct (and transitive) dependencies of *manifest*.

    Returns a fully-populated :class:`LockFile` ready to be saved.
    Raises :class:`ResolveError` when a dependency cannot be satisfied.
    """
    packages: list[LockedPackage] = []
    seen: set[str] = set()
    _resolve_recursive(manifest.dependencies, registry, packages, seen)
    return LockFile(packages=packages)


def _resolve_recursive(
    dependencies: dict[str, str],
    registry: Registry,
    resolved: list[LockedPackage],
    seen: set[str],
) -> None:
    for name, constraint in dependencies.items():
        if name in seen:
            continue
        seen.add(name)

        versions = registry.list_versions(name)
        if not versions:
            raise ResolveError(
                f"dependency '{name}' not found in registry"
            )

        chosen = best_matching(versions, constraint)
        if chosen is None:
            raise ResolveError(
                f"no version of '{name}' satisfies '{constraint}'; "
                f"available versions: {', '.join(sorted(versions))}"
            )

        checksum = registry.get_checksum(name, chosen) or ""
        resolved.append(LockedPackage(name=name, version=chosen, checksum=checksum))
