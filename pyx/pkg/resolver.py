"""Dependency resolution and lock-file management for PyX."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .manifest import PackageManifest
from .registry import Registry, RegistryError
from .semver import Version, best_matching, matches_constraint


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
    resolved: list[LockedPackage] = []
    selected: dict[str, LockedPackage] = {}
    for name, constraint in manifest.dependencies.items():
        _resolve_one(
            name=name,
            constraint=constraint,
            registry=registry,
            resolved=resolved,
            selected=selected,
            parent=manifest.name,
        )
    return LockFile(packages=resolved)


def _resolve_one(
    name: str,
    constraint: str,
    registry: Registry,
    resolved: list[LockedPackage],
    selected: dict[str, LockedPackage],
    parent: str,
) -> None:
    existing = selected.get(name)
    if existing is not None:
        _ensure_locked_version_matches(existing, constraint, parent)
        return

    pkg_versions = registry.get_package_versions(name)
    if not pkg_versions:
        raise ResolveError(f"dependency '{name}' not found in registry")

    chosen = best_matching(list(pkg_versions.keys()), constraint)
    if chosen is None:
        raise ResolveError(
            f"no version of '{name}' satisfies '{constraint}'; "
            f"available versions: {', '.join(sorted(pkg_versions))}"
        )

    checksum = pkg_versions.get(chosen) or ""
    locked = LockedPackage(name=name, version=chosen, checksum=checksum)
    selected[name] = locked
    try:
        try:
            dep_manifest = registry.load_manifest(name, chosen)
        except RegistryError as exc:
            raise ResolveError(str(exc)) from exc
        for dep_name, dep_constraint in dep_manifest.dependencies.items():
            _resolve_one(
                name=dep_name,
                constraint=dep_constraint,
                registry=registry,
                resolved=resolved,
                selected=selected,
                parent=f"{name}=={chosen}",
            )
    except Exception:
        selected.pop(name, None)
        raise

    resolved.append(locked)


def _ensure_locked_version_matches(pkg: LockedPackage, constraint: str, parent: str) -> None:
    version = Version.parse(pkg.version)
    if not matches_constraint(version, constraint):
        raise ResolveError(
            f"dependency conflict for '{pkg.name}': {parent} requires '{constraint}', "
            f"but locked version is '{pkg.version}'"
        )
