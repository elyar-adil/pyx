"""Phase 5: Package manager tests.

Covers:
  - Semantic versioning (parsing, comparison, constraints, best_matching)
  - Manifest load/save (pyx.toml)
  - Registry publish/list/fetch
  - Dependency resolver and LockFile
  - Installer (install_package, publish_package, install_from_manifest)
  - CLI commands (cmd_pkg_install, cmd_pkg_publish)
  - project.py pyx_packages/ search path
  - End-to-end: publish a libc-wrapper package and install it
"""
from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from pyx.pkg.semver import Version, best_matching, matches_constraint
from pyx.pkg.manifest import ManifestError, PackageManifest, load_manifest, save_manifest
from pyx.pkg.registry import Registry, RegistryError
from pyx.pkg.resolver import LockFile, LockedPackage, ResolveError, resolve_dependencies
from pyx.pkg.installer import InstallError, install_package, publish_package, install_from_manifest
from pyx.cli import cmd_pkg_install, cmd_pkg_publish


# ===========================================================================
# Semantic versioning
# ===========================================================================


class TestVersionParsing:
    def test_parse_valid(self):
        v = Version.parse("1.2.3")
        assert v.major == 1 and v.minor == 2 and v.patch == 3

    def test_str_roundtrip(self):
        assert str(Version.parse("0.10.1")) == "0.10.1"

    def test_parse_invalid_raises(self):
        with pytest.raises(ValueError):
            Version.parse("1.2")
        with pytest.raises(ValueError):
            Version.parse("1.2.3.4")
        with pytest.raises(ValueError):
            Version.parse("v1.2.3")

    def test_comparison(self):
        assert Version.parse("1.0.0") < Version.parse("2.0.0")
        assert Version.parse("1.2.0") > Version.parse("1.1.9")
        assert Version.parse("1.0.0") == Version.parse("1.0.0")


class TestVersionConstraints:
    def test_wildcard(self):
        v = Version.parse("1.2.3")
        assert matches_constraint(v, "*")
        assert matches_constraint(v, "")

    def test_exact(self):
        v = Version.parse("1.2.3")
        assert matches_constraint(v, "1.2.3")
        assert matches_constraint(v, "==1.2.3")
        assert not matches_constraint(v, "1.2.4")

    def test_not_equal(self):
        v = Version.parse("1.2.3")
        assert matches_constraint(v, "!=1.2.4")
        assert not matches_constraint(v, "!=1.2.3")

    def test_gte(self):
        assert matches_constraint(Version.parse("1.2.3"), ">=1.2.3")
        assert matches_constraint(Version.parse("1.2.4"), ">=1.2.3")
        assert not matches_constraint(Version.parse("1.2.2"), ">=1.2.3")

    def test_lt(self):
        assert matches_constraint(Version.parse("1.2.2"), "<1.2.3")
        assert not matches_constraint(Version.parse("1.2.3"), "<1.2.3")

    def test_caret(self):
        # ^1.2.0 means >=1.2.0, <2.0.0
        assert matches_constraint(Version.parse("1.9.9"), "^1.2.0")
        assert not matches_constraint(Version.parse("2.0.0"), "^1.2.0")
        assert not matches_constraint(Version.parse("1.1.9"), "^1.2.0")

    def test_tilde(self):
        # ~1.2.0 means >=1.2.0, <1.3.0
        assert matches_constraint(Version.parse("1.2.5"), "~1.2.0")
        assert not matches_constraint(Version.parse("1.3.0"), "~1.2.0")

    def test_compound_constraint(self):
        assert matches_constraint(Version.parse("1.5.0"), ">=1.0.0,<2.0.0")
        assert not matches_constraint(Version.parse("2.0.0"), ">=1.0.0,<2.0.0")

    def test_invalid_operator_raises(self):
        with pytest.raises(ValueError):
            matches_constraint(Version.parse("1.0.0"), "??1.0.0")


class TestBestMatching:
    def test_returns_highest_matching(self):
        versions = ["0.9.0", "1.0.0", "1.1.0", "2.0.0"]
        assert best_matching(versions, "^1.0.0") == "1.1.0"

    def test_returns_none_when_no_match(self):
        assert best_matching(["0.9.0"], ">=1.0.0") is None

    def test_empty_list(self):
        assert best_matching([], "*") is None

    def test_skips_unparseable(self):
        assert best_matching(["bad", "1.0.0"], "*") == "1.0.0"


# ===========================================================================
# Manifest
# ===========================================================================


def _write_toml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "pyx.toml"
    p.write_text(content, encoding="utf-8")
    return p


class TestManifest:
    def test_minimal_load(self, tmp_path):
        p = _write_toml(tmp_path, '[package]\nname = "mylib"\nversion = "0.1.0"\n')
        m = load_manifest(p)
        assert m.name == "mylib"
        assert m.version == "0.1.0"
        assert m.description == ""
        assert m.dependencies == {}
        assert m.libraries == {}

    def test_load_with_deps_and_libs(self, tmp_path):
        content = (
            '[package]\nname = "app"\nversion = "1.0.0"\ndescription = "an app"\n\n'
            '[dependencies]\nlibc-wrap = ">=0.1.0"\n\n'
            '[libraries]\nlibc = { path = "libc.so.6" }\n'
        )
        p = _write_toml(tmp_path, content)
        m = load_manifest(p)
        assert m.dependencies == {"libc-wrap": ">=0.1.0"}
        assert m.libraries == {"libc": {"path": "libc.so.6"}}

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(ManifestError, match="not found"):
            load_manifest(tmp_path / "missing.toml")

    def test_missing_package_section_raises(self, tmp_path):
        p = _write_toml(tmp_path, '[other]\nkey = "val"\n')
        with pytest.raises(ManifestError, match="\\[package\\]"):
            load_manifest(p)

    def test_missing_name_raises(self, tmp_path):
        p = _write_toml(tmp_path, '[package]\nversion = "1.0.0"\n')
        with pytest.raises(ManifestError, match="name"):
            load_manifest(p)

    def test_missing_version_raises(self, tmp_path):
        p = _write_toml(tmp_path, '[package]\nname = "x"\n')
        with pytest.raises(ManifestError, match="version"):
            load_manifest(p)

    def test_invalid_version_raises(self, tmp_path):
        p = _write_toml(tmp_path, '[package]\nname = "x"\nversion = "bad"\n')
        with pytest.raises(ManifestError, match="invalid"):
            load_manifest(p)

    def test_invalid_toml_raises(self, tmp_path):
        p = tmp_path / "pyx.toml"
        p.write_text("not [ valid toml !!!!", encoding="utf-8")
        with pytest.raises(ManifestError, match="invalid TOML"):
            load_manifest(p)

    def test_save_roundtrip(self, tmp_path):
        manifest = PackageManifest(
            name="mylib",
            version="0.2.0",
            description="A test package",
            dependencies={"dep": "^1.0.0"},
            libraries={"libc": {"path": "libc.so.6"}},
        )
        dest = tmp_path / "pyx.toml"
        save_manifest(manifest, dest)
        loaded = load_manifest(dest)
        assert loaded.name == manifest.name
        assert loaded.version == manifest.version
        assert loaded.description == manifest.description
        assert loaded.dependencies == manifest.dependencies
        assert loaded.libraries == manifest.libraries


# ===========================================================================
# Registry
# ===========================================================================


def _make_archive(tmp_path: Path, name: str, version: str, files: dict[str, str]) -> Path:
    """Create a .tar.gz archive containing *files* (filename -> content)."""
    archive_path = tmp_path / f"{name}-{version}.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tf:
        for fname, content in files.items():
            fpath = tmp_path / fname
            fpath.write_text(content, encoding="utf-8")
            tf.add(fpath, arcname=fname)
    return archive_path


class TestRegistry:
    def test_publish_and_list(self, tmp_path):
        reg = Registry(tmp_path / "reg")
        archive = _make_archive(tmp_path, "mypkg", "1.0.0", {"mypkg.py": "x: int = 1\n"})
        reg.publish(archive, "mypkg", "1.0.0")
        assert reg.list_versions("mypkg") == ["1.0.0"]

    def test_publish_updates_checksum(self, tmp_path):
        reg = Registry(tmp_path / "reg")
        archive = _make_archive(tmp_path, "pkg", "0.1.0", {"pkg.py": ""})
        checksum = reg.publish(archive, "pkg", "0.1.0")
        assert checksum.startswith("sha256:")
        assert reg.get_checksum("pkg", "0.1.0") == checksum

    def test_list_unknown_package(self, tmp_path):
        reg = Registry(tmp_path / "reg")
        assert reg.list_versions("unknown") == []

    def test_fetch_archive_returns_path(self, tmp_path):
        reg = Registry(tmp_path / "reg")
        archive = _make_archive(tmp_path, "pkg", "1.0.0", {"pkg.py": ""})
        reg.publish(archive, "pkg", "1.0.0")
        fetched = reg.fetch_archive("pkg", "1.0.0")
        assert fetched.exists()

    def test_fetch_missing_raises(self, tmp_path):
        reg = Registry(tmp_path / "reg")
        with pytest.raises(RegistryError, match="not found"):
            reg.fetch_archive("ghost", "1.0.0")

    def test_publish_multiple_versions(self, tmp_path):
        reg = Registry(tmp_path / "reg")
        for ver in ("0.9.0", "1.0.0", "1.1.0"):
            archive = _make_archive(tmp_path, "pkg", ver, {"pkg.py": f"V = '{ver}'\n"})
            reg.publish(archive, "pkg", ver)
        versions = reg.list_versions("pkg")
        assert set(versions) == {"0.9.0", "1.0.0", "1.1.0"}


# ===========================================================================
# Resolver & LockFile
# ===========================================================================


class TestLockFile:
    def test_save_and_load(self, tmp_path):
        lock = LockFile(packages=[
            LockedPackage(name="dep", version="1.0.0", checksum="sha256:abc"),
        ])
        path = tmp_path / "pyx.lock"
        lock.save(path)
        loaded = LockFile.load(path)
        assert len(loaded.packages) == 1
        assert loaded.packages[0].name == "dep"
        assert loaded.packages[0].version == "1.0.0"

    def test_load_missing_returns_empty(self, tmp_path):
        lock = LockFile.load(tmp_path / "missing.lock")
        assert lock.packages == []

    def test_find(self, tmp_path):
        lock = LockFile(packages=[LockedPackage("a", "1.0.0", "sha256:x")])
        assert lock.find("a") is not None
        assert lock.find("b") is None


class TestResolver:
    def _setup_registry_with_dep(self, tmp_path):
        reg = Registry(tmp_path / "reg")
        archive = _make_archive(tmp_path, "mylib", "1.0.0", {"mylib.py": "X: int = 1\n"})
        reg.publish(archive, "mylib", "1.0.0")
        return reg

    def test_resolve_single_dep(self, tmp_path):
        reg = self._setup_registry_with_dep(tmp_path)
        manifest = PackageManifest(name="app", version="0.1.0", dependencies={"mylib": ">=1.0.0"})
        lock = resolve_dependencies(manifest, reg)
        assert len(lock.packages) == 1
        assert lock.packages[0].name == "mylib"
        assert lock.packages[0].version == "1.0.0"

    def test_resolve_no_deps(self, tmp_path):
        reg = Registry(tmp_path / "reg")
        manifest = PackageManifest(name="app", version="0.1.0")
        lock = resolve_dependencies(manifest, reg)
        assert lock.packages == []

    def test_resolve_missing_dep_raises(self, tmp_path):
        reg = Registry(tmp_path / "reg")
        manifest = PackageManifest(name="app", version="0.1.0", dependencies={"missing": "*"})
        with pytest.raises(ResolveError, match="not found"):
            resolve_dependencies(manifest, reg)

    def test_resolve_unsatisfiable_constraint_raises(self, tmp_path):
        reg = self._setup_registry_with_dep(tmp_path)
        manifest = PackageManifest(name="app", version="0.1.0", dependencies={"mylib": ">=2.0.0"})
        with pytest.raises(ResolveError, match="satisfies"):
            resolve_dependencies(manifest, reg)

    def test_resolve_picks_highest_version(self, tmp_path):
        reg = Registry(tmp_path / "reg")
        for ver in ("1.0.0", "1.1.0", "1.2.0"):
            archive = _make_archive(tmp_path, "lib", ver, {"lib.py": ""})
            reg.publish(archive, "lib", ver)
        manifest = PackageManifest(name="app", version="0.1.0", dependencies={"lib": "^1.0.0"})
        lock = resolve_dependencies(manifest, reg)
        assert lock.packages[0].version == "1.2.0"


# ===========================================================================
# Installer
# ===========================================================================


class TestInstaller:
    def _setup_pkg(self, tmp_path, name="mypkg", version="1.0.0"):
        reg = Registry(tmp_path / "reg")
        archive = _make_archive(
            tmp_path, name, version,
            {f"{name}.py": f"# {name} v{version}\nVERSION: str = '{version}'\n"}
        )
        reg.publish(archive, name, version)
        return reg

    def test_install_package(self, tmp_path):
        reg = self._setup_pkg(tmp_path)
        install_dir = tmp_path / "pkgs"
        dest = install_package("mypkg", reg, install_dir)
        assert dest.exists()
        assert (dest / "mypkg.py").exists()

    def test_install_unknown_raises(self, tmp_path):
        reg = Registry(tmp_path / "reg")
        with pytest.raises(InstallError, match="not found"):
            install_package("ghost", reg, tmp_path / "pkgs")

    def test_install_replaces_existing(self, tmp_path):
        reg = self._setup_pkg(tmp_path)
        install_dir = tmp_path / "pkgs"
        install_package("mypkg", reg, install_dir)
        # Publish a new version and reinstall
        archive2 = _make_archive(tmp_path, "mypkg", "2.0.0", {"mypkg.py": "VERSION: str = '2.0.0'\n"})
        reg.publish(archive2, "mypkg", "2.0.0")
        install_package("mypkg", reg, install_dir, "2.0.0")
        content = (install_dir / "mypkg" / "mypkg.py").read_text()
        assert "2.0.0" in content

    def test_publish_package(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "pyx.toml").write_text('[package]\nname = "mypkg"\nversion = "1.0.0"\n', encoding="utf-8")
        (src / "mypkg.py").write_text("X: int = 1\n", encoding="utf-8")
        manifest = load_manifest(src / "pyx.toml")
        reg = Registry(tmp_path / "reg")
        checksum = publish_package(src, manifest, reg)
        assert checksum.startswith("sha256:")
        assert reg.list_versions("mypkg") == ["1.0.0"]

    def test_publish_excludes_pycache(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "pyx.toml").write_text('[package]\nname = "p"\nversion = "0.1.0"\n', encoding="utf-8")
        (src / "p.py").write_text("X: int = 0\n", encoding="utf-8")
        cache = src / "__pycache__"
        cache.mkdir()
        (cache / "p.cpython-311.pyc").write_bytes(b"\x00\x01")
        manifest = load_manifest(src / "pyx.toml")
        reg = Registry(tmp_path / "reg")
        checksum = publish_package(src, manifest, reg)
        # Verify __pycache__ was not included
        archive = reg.fetch_archive("p", "0.1.0")
        with tarfile.open(archive, "r:gz") as tf:
            names = tf.getnames()
        assert not any("__pycache__" in n for n in names)

    def test_install_from_manifest(self, tmp_path):
        # Set up registry with one dep
        reg = Registry(tmp_path / "reg")
        dep_archive = _make_archive(tmp_path, "dep", "1.0.0", {"dep.py": "X: int = 42\n"})
        reg.publish(dep_archive, "dep", "1.0.0")
        # Create project with that dependency
        proj = tmp_path / "project"
        proj.mkdir()
        (proj / "pyx.toml").write_text(
            '[package]\nname = "myapp"\nversion = "0.1.0"\n\n[dependencies]\ndep = ">=1.0.0"\n',
            encoding="utf-8",
        )
        manifest = load_manifest(proj / "pyx.toml")
        lock = install_from_manifest(manifest, reg, proj)
        assert len(lock.packages) == 1
        assert lock.packages[0].name == "dep"
        assert (proj / "pyx_packages" / "dep" / "dep.py").exists()
        assert (proj / "pyx.lock").exists()


# ===========================================================================
# CLI commands
# ===========================================================================


class TestCLIPkg:
    def test_cmd_pkg_install_success(self, tmp_path, capsys):
        reg = Registry(tmp_path / "reg")
        archive = _make_archive(tmp_path, "mypkg", "1.0.0", {"mypkg.py": "V: int = 1\n"})
        reg.publish(archive, "mypkg", "1.0.0")
        rc = cmd_pkg_install("mypkg", tmp_path / "reg", tmp_path / "project")
        out = capsys.readouterr().out
        assert rc == 0
        assert "Installed mypkg" in out

    def test_cmd_pkg_install_missing_package(self, tmp_path, capsys):
        reg = Registry(tmp_path / "reg")
        rc = cmd_pkg_install("ghost", tmp_path / "reg", tmp_path / "project")
        out = capsys.readouterr().out
        assert rc == 1
        assert "error:" in out

    def test_cmd_pkg_publish_success(self, tmp_path, capsys):
        src = tmp_path / "src"
        src.mkdir()
        (src / "pyx.toml").write_text('[package]\nname = "mypkg"\nversion = "0.1.0"\n', encoding="utf-8")
        (src / "mypkg.py").write_text("V: int = 1\n", encoding="utf-8")
        rc = cmd_pkg_publish(src, tmp_path / "reg")
        out = capsys.readouterr().out
        assert rc == 0
        assert "Published mypkg==0.1.0" in out

    def test_cmd_pkg_publish_missing_manifest(self, tmp_path, capsys):
        rc = cmd_pkg_publish(tmp_path / "empty", tmp_path / "reg")
        out = capsys.readouterr().out
        assert rc == 1
        assert "error:" in out


# ===========================================================================
# project.py pyx_packages/ search path
# ===========================================================================


class TestProjectPkgSearchPath:
    def test_import_from_pyx_packages(self, tmp_path):
        """Modules installed in pyx_packages/ should be resolvable by the project loader."""
        from pyx.project import load_project, ProjectLoadError

        # Install a package into pyx_packages/
        pkg_dir = tmp_path / "pyx_packages" / "mylib"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "mylib.py").write_text("VALUE: int = 99\n", encoding="utf-8")

        # Main module importing it
        main = tmp_path / "main.py"
        main.write_text("import mylib\n\ndef get() -> int:\n    return mylib.VALUE\n", encoding="utf-8")

        project = load_project(main)
        assert "mylib" in project.modules

    def test_direct_pyx_packages_flat_module(self, tmp_path):
        """A flat .py file directly in pyx_packages/ should also be found."""
        from pyx.project import load_project

        (tmp_path / "pyx_packages").mkdir()
        (tmp_path / "pyx_packages" / "flatmod.py").write_text("X: int = 1\n", encoding="utf-8")

        main = tmp_path / "main.py"
        main.write_text("import flatmod\n\ndef run() -> int:\n    return flatmod.X\n", encoding="utf-8")

        project = load_project(main)
        assert "flatmod" in project.modules


# ===========================================================================
# End-to-end: publish libc-wrapper, install in another project
# ===========================================================================


LIBC_WRAPPER_TOML = """\
[package]
name = "libc-wrap"
version = "0.1.0"
description = "Minimal libc wrapper for PyX"

[libraries]
libc = { path = "libc.so.6" }
"""

LIBC_WRAPPER_PY = """\
import ctypes
from ctypes import CDLL, CFUNCTYPE, c_int

libc = CDLL("libc.so.6")
abs_t = CFUNCTYPE(c_int, c_int)
c_abs = abs_t(("abs", libc))
"""

CONSUMER_TOML = """\
[package]
name = "consumer"
version = "0.1.0"

[dependencies]
libc-wrap = ">=0.1.0"
"""

CONSUMER_PY = """\
import libc_wrap

def compute(x: int) -> int:
    return x
"""


class TestEndToEnd:
    def test_publish_and_install_libc_wrapper(self, tmp_path):
        """Full cycle: publish libc-wrap, install it, verify pyx_packages/ layout."""
        # 1. Create the libc-wrap package source
        pkg_src = tmp_path / "libc-wrap-src"
        pkg_src.mkdir()
        (pkg_src / "pyx.toml").write_text(LIBC_WRAPPER_TOML, encoding="utf-8")
        (pkg_src / "libc_wrap.py").write_text(LIBC_WRAPPER_PY, encoding="utf-8")

        reg = Registry(tmp_path / "registry")

        # 2. Publish the package
        manifest = load_manifest(pkg_src / "pyx.toml")
        checksum = publish_package(pkg_src, manifest, reg)
        assert checksum.startswith("sha256:")
        assert "libc-wrap" in reg.list_versions.__func__(reg, "libc-wrap") or \
               "0.1.0" in reg.list_versions("libc-wrap")

        # 3. Create the consumer project
        consumer_dir = tmp_path / "consumer"
        consumer_dir.mkdir()
        (consumer_dir / "pyx.toml").write_text(CONSUMER_TOML, encoding="utf-8")
        (consumer_dir / "main.py").write_text(CONSUMER_PY, encoding="utf-8")

        # 4. Install dependencies
        consumer_manifest = load_manifest(consumer_dir / "pyx.toml")
        lock = install_from_manifest(consumer_manifest, reg, consumer_dir)

        # 5. Verify
        assert len(lock.packages) == 1
        assert lock.packages[0].name == "libc-wrap"
        assert (consumer_dir / "pyx_packages" / "libc-wrap").exists()
        assert (consumer_dir / "pyx.lock").exists()

        lock_data = json.loads((consumer_dir / "pyx.lock").read_text())
        assert lock_data["packages"][0]["name"] == "libc-wrap"

    def test_pyx_packages_importable_after_install(self, tmp_path):
        """After install, the package module is resolvable by the project loader."""
        from pyx.project import load_project

        # Set up registry + publish
        pkg_src = tmp_path / "pkg"
        pkg_src.mkdir()
        (pkg_src / "pyx.toml").write_text('[package]\nname = "utils"\nversion = "1.0.0"\n', encoding="utf-8")
        (pkg_src / "utils.py").write_text("def helper(x: int) -> int:\n    return x\n", encoding="utf-8")

        reg = Registry(tmp_path / "reg")
        manifest = load_manifest(pkg_src / "pyx.toml")
        publish_package(pkg_src, manifest, reg)

        # Install into consumer project
        consumer = tmp_path / "consumer"
        consumer.mkdir()
        install_package("utils", reg, consumer / "pyx_packages")

        # Write a main.py that imports the installed package
        (consumer / "main.py").write_text(
            "import utils\n\ndef run(n: int) -> int:\n    return utils.helper(n)\n",
            encoding="utf-8",
        )

        project = load_project(consumer / "main.py")
        assert "utils" in project.modules
