"""pyx.toml manifest parsing and serialisation."""
from __future__ import annotations

try:
    import tomllib
except ImportError:  # Python < 3.11
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError as _exc:
        raise ImportError(
            "tomllib (Python ≥3.11) or the 'tomli' package is required. "
            "Install it with: pip install tomli"
        ) from _exc
from dataclasses import dataclass, field
from pathlib import Path


class ManifestError(Exception):
    pass


@dataclass
class PackageManifest:
    name: str
    version: str
    description: str = ""
    # dep name -> version constraint string, e.g. ">=1.0.0"
    dependencies: dict[str, str] = field(default_factory=dict)
    # lib alias -> {path: "libc.so.6", ...}
    libraries: dict[str, dict[str, str]] = field(default_factory=dict)


def _parse_manifest_data(data: bytes, source: str) -> PackageManifest:
    try:
        parsed = tomllib.loads(data.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ManifestError(f"manifest must be UTF-8 encoded: {source}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(f"invalid TOML in {source}: {exc}") from exc

    pkg = parsed.get("package", {})
    if not pkg:
        raise ManifestError(f"[package] section missing in {source}")

    name: str = pkg.get("name", "")
    if not name:
        raise ManifestError("package.name is required")

    version: str = pkg.get("version", "")
    if not version:
        raise ManifestError("package.version is required")

    # Validate the version string via the semver module
    from .semver import Version

    try:
        Version.parse(version)
    except ValueError as exc:
        raise ManifestError(f"invalid package.version: {exc}") from exc

    dependencies: dict[str, str] = {}
    for dep_name, constraint in parsed.get("dependencies", {}).items():
        if not isinstance(constraint, str):
            raise ManifestError(
                f"dependency '{dep_name}' value must be a version-constraint string"
            )
        dependencies[dep_name] = constraint

    libraries: dict[str, dict[str, str]] = {}
    for lib_name, lib_info in parsed.get("libraries", {}).items():
        if not isinstance(lib_info, dict):
            raise ManifestError(f"library '{lib_name}' must be an inline table")
        libraries[lib_name] = {k: str(v) for k, v in lib_info.items()}

    return PackageManifest(
        name=name,
        version=version,
        description=pkg.get("description", ""),
        dependencies=dependencies,
        libraries=libraries,
    )


def load_manifest(path: Path) -> PackageManifest:
    """Parse a ``pyx.toml`` file and return a :class:`PackageManifest`."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise ManifestError(f"manifest not found: {path}")
    return _parse_manifest_data(data, str(path))


def save_manifest(manifest: PackageManifest, path: Path) -> None:
    """Write a ``PackageManifest`` as a ``pyx.toml`` file."""
    lines: list[str] = [
        "[package]",
        f'name = "{manifest.name}"',
        f'version = "{manifest.version}"',
    ]
    if manifest.description:
        lines.append(f'description = "{manifest.description}"')

    lines.append("")
    lines.append("[dependencies]")
    for dep_name, constraint in manifest.dependencies.items():
        lines.append(f'{dep_name} = "{constraint}"')

    if manifest.libraries:
        lines.append("")
        lines.append("[libraries]")
        for lib_name, lib_info in manifest.libraries.items():
            pairs = ", ".join(f'{k} = "{v}"' for k, v in lib_info.items())
            lines.append(f"{lib_name} = {{ {pairs} }}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
