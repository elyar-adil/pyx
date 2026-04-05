"""Local file-based package registry for PyX."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tarfile
from pathlib import Path

from .manifest import ManifestError, PackageManifest, _parse_manifest_data

DEFAULT_REGISTRY_DIR: Path = Path.home() / ".pyx" / "registry"


def get_registry_dir() -> Path:
    """Return the active registry directory (overridable via ``PYX_REGISTRY``)."""
    env = os.environ.get("PYX_REGISTRY")
    if env:
        return Path(env)
    return DEFAULT_REGISTRY_DIR


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


class RegistryError(Exception):
    pass


class Registry:
    """A local directory-backed package registry.

    Layout::

        <path>/
          index.json          # {"pkg": {"1.0.0": "sha256:...", ...}, ...}
          packages/
            pkg-1.0.0.tar.gz
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._packages_dir = path / "packages"
        self._index_path = path / "index.json"

    # ------------------------------------------------------------------
    # Index helpers
    # ------------------------------------------------------------------

    def _load_index(self) -> dict[str, dict[str, str]]:
        if not self._index_path.exists():
            return {}
        with open(self._index_path, encoding="utf-8") as fh:
            return json.load(fh)  # type: ignore[no-any-return]

    def _save_index(self, index: dict[str, dict[str, str]]) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        self._index_path.write_text(json.dumps(index, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_package_versions(self, name: str) -> dict[str, str]:
        """Return ``{version: checksum}`` for *name* (empty dict if unknown)."""
        return dict(self._load_index().get(name, {}))

    def list_versions(self, name: str) -> list[str]:
        """Return all known versions for *name* (may be empty)."""
        return list(self.get_package_versions(name).keys())

    def get_checksum(self, name: str, version: str) -> str | None:
        """Return the stored checksum for *name==version*, or ``None``."""
        return self.get_package_versions(name).get(version)

    def fetch_archive(self, name: str, version: str) -> Path:
        """Return the local path to the ``.tar.gz`` archive for *name==version*.

        Raises :class:`RegistryError` if the archive is not present.
        """
        archive_path = self._packages_dir / f"{name}-{version}.tar.gz"
        if not archive_path.exists():
            raise RegistryError(
                f"package '{name}=={version}' not found in registry at {self.path}"
            )
        return archive_path

    def load_manifest(self, name: str, version: str) -> PackageManifest:
        """Load ``pyx.toml`` metadata from the archive for *name==version*."""
        archive_path = self.fetch_archive(name, version)
        try:
            with tarfile.open(archive_path, "r:gz") as tf:
                try:
                    member = tf.getmember("pyx.toml")
                except KeyError:
                    return PackageManifest(name=name, version=version)
                extracted = tf.extractfile(member)
                if extracted is None:
                    return PackageManifest(name=name, version=version)
                return _parse_manifest_data(
                    extracted.read(),
                    f"{archive_path}!pyx.toml",
                )
        except (tarfile.TarError, ManifestError) as exc:
            raise RegistryError(
                f"package '{name}=={version}' metadata could not be read: {exc}"
            ) from exc

    def publish(self, archive_path: Path, name: str, version: str) -> str:
        """Copy *archive_path* into the registry and record its checksum.

        Returns the ``sha256:<hex>`` checksum string.
        """
        self._packages_dir.mkdir(parents=True, exist_ok=True)

        checksum = _sha256(archive_path)

        dest = self._packages_dir / f"{name}-{version}.tar.gz"
        shutil.copy2(archive_path, dest)

        index = self._load_index()
        index.setdefault(name, {})[version] = checksum
        self._save_index(index)

        return checksum
