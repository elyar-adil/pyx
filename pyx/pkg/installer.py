"""Package install / publish helpers for PyX."""
from __future__ import annotations

import hashlib
import shutil
import tarfile
import tempfile
from pathlib import Path

from .manifest import PackageManifest
from .registry import Registry, RegistryError
from .resolver import LockFile, ResolveError, resolve_dependencies
from .semver import best_matching

# Where packages are extracted after installation (per-project vendor directory).
PKG_DIR_NAME = "pyx_packages"


class InstallError(Exception):
    pass


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _safe_extractall(tf: tarfile.TarFile, dest: Path) -> None:
    """Extract *tf* to *dest* in a forward-compatible way."""
    try:
        tf.extractall(dest, filter="data")  # Python 3.12+
    except TypeError:
        tf.extractall(dest)  # noqa: S202 – best effort for Python 3.11


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------


def install_package(
    name: str,
    registry: Registry,
    install_dir: Path,
    constraint: str = "*",
) -> Path:
    """Download and extract the best-matching version of *name* from *registry*.

    Files are extracted into ``install_dir/<name>/``.
    Returns the extraction directory.
    """
    versions = registry.list_versions(name)
    if not versions:
        raise InstallError(f"package '{name}' not found in registry")

    chosen = best_matching(versions, constraint)
    if chosen is None:
        raise InstallError(
            f"no version of '{name}' satisfies '{constraint}'; "
            f"available: {', '.join(sorted(versions))}"
        )

    try:
        archive_path = registry.fetch_archive(name, chosen)
    except RegistryError as exc:
        raise InstallError(str(exc)) from exc

    # Verify checksum
    actual = _sha256(archive_path)
    expected = registry.get_checksum(name, chosen)
    if expected and actual != expected:
        raise InstallError(
            f"checksum mismatch for '{name}=={chosen}': "
            f"expected {expected}, got {actual}"
        )

    dest = install_dir / name
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)

    with tarfile.open(archive_path, "r:gz") as tf:
        _safe_extractall(tf, dest)

    return dest


def install_from_manifest(
    manifest: PackageManifest,
    registry: Registry,
    project_dir: Path,
) -> LockFile:
    """Resolve and install all dependencies declared in *manifest*.

    Packages are extracted into ``<project_dir>/pyx_packages/<name>/``.
    The resulting lock file is written to ``<project_dir>/pyx.lock``.
    """
    try:
        lock = resolve_dependencies(manifest, registry)
    except ResolveError as exc:
        raise InstallError(str(exc)) from exc

    install_dir = project_dir / PKG_DIR_NAME

    for pkg in lock.packages:
        try:
            install_package(pkg.name, registry, install_dir, pkg.version)
        except InstallError as exc:
            raise InstallError(f"failed to install '{pkg.name}': {exc}") from exc

    lock.save(project_dir / "pyx.lock")
    return lock


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------

_EXCLUDE_NAMES = frozenset({
    ".git",
    "__pycache__",
    "dist",
    PKG_DIR_NAME,
    "pyx.lock",
    ".pytest_cache",
    ".mypy_cache",
})


def publish_package(
    source_dir: Path,
    manifest: PackageManifest,
    registry: Registry,
) -> str:
    """Bundle *source_dir* as a ``.tar.gz`` and publish it to *registry*.

    Returns the ``sha256:<hex>`` checksum of the published archive.
    """
    archive_name = f"{manifest.name}-{manifest.version}.tar.gz"

    with tempfile.TemporaryDirectory() as tmp:
        archive_path = Path(tmp) / archive_name
        with tarfile.open(archive_path, "w:gz") as tf:
            for item in sorted(source_dir.iterdir()):
                if item.name in _EXCLUDE_NAMES or item.name.endswith(".pyc"):
                    continue
                tf.add(item, arcname=item.name, recursive=True)

        checksum = registry.publish(archive_path, manifest.name, manifest.version)

    return checksum
