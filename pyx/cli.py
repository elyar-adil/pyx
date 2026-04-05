from __future__ import annotations

import argparse
import json
from pathlib import Path

from .analyzer import Analyzer
from .compiler import CompileError, LLVMCompiler, emit_native_object
from .diagnostics import Diagnostic, format_diagnostic


def cmd_check(source: Path) -> int:
    analyzer = Analyzer()
    errors = analyzer.analyze_path(source)
    if errors:
        for err in errors:
            print(format_diagnostic(source, Diagnostic(err.code, err.message, err.path, err.line, err.col)))
        return 1
    print(f"{source}: OK")
    return 0


def cmd_build(source: Path, out_dir: Path) -> int:
    rc = cmd_check(source)
    if rc != 0:
        return rc

    try:
        compiler = LLVMCompiler.from_path(source)
        llvm_ir = compiler.compile_ir()
    except CompileError as exc:
        print(format_diagnostic(source, Diagnostic(exc.code, exc.message, exc.path, exc.line, exc.col)))
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)
    ll_path = out_dir / f"{source.stem}.ll"
    ll_path.write_text(llvm_ir, encoding="utf-8")

    object_path = out_dir / f"{source.stem}.o"
    object_emitted = False
    try:
        object_emitted = emit_native_object(ll_path, object_path)
    except Exception as exc:  # pragma: no cover - external toolchain failure
        print(f"warning: failed to emit native object: {exc}")

    report = {
        "source": str(source),
        "status": "llvm-ir-generated",
        "llvm_ir": str(ll_path),
        "native_object": str(object_path) if object_emitted else None,
    }
    (out_dir / "build_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Build artifacts written to {out_dir}")
    return 0


# ---------------------------------------------------------------------------
# Package manager commands
# ---------------------------------------------------------------------------


def _make_registry(registry_path: Path | None) -> "Registry":  # noqa: F821
    from .pkg.registry import Registry, get_registry_dir

    return Registry(registry_path if registry_path is not None else get_registry_dir())


def cmd_pkg_install(name: str, registry_path: Path | None, project_dir: Path) -> int:
    """Install *name* (and its dependencies) into ``<project_dir>/pyx_packages/``."""
    from .pkg.installer import InstallError, install_requirement

    registry = _make_registry(registry_path)

    try:
        lock = install_requirement(name, registry, project_dir)
        print(f"Installed {name} with {len(lock.packages)} package(s)")
        return 0
    except InstallError as exc:
        print(f"error: {exc}")
        return 1


def cmd_pkg_publish(source_dir: Path, registry_path: Path | None) -> int:
    """Package the project in *source_dir* and publish it to the registry."""
    from .pkg.installer import InstallError, publish_package
    from .pkg.manifest import ManifestError, load_manifest
    from .pkg.registry import RegistryError

    manifest_path = source_dir / "pyx.toml"
    try:
        manifest = load_manifest(manifest_path)
    except ManifestError as exc:
        print(f"error: {exc}")
        return 1

    registry = _make_registry(registry_path)
    try:
        checksum = publish_package(source_dir, manifest, registry)
        print(f"Published {manifest.name}=={manifest.version}  checksum: {checksum}")
        return 0
    except (InstallError, RegistryError) as exc:
        print(f"error: failed to publish: {exc}")
        return 1


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(prog="pyx")
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="Run static subset checks")
    p_check.add_argument("source", type=Path)

    p_build = sub.add_parser("build", help="Compile typed subset to LLVM IR and native object")
    p_build.add_argument("source", type=Path)
    p_build.add_argument("-o", "--out-dir", type=Path, default=Path("dist"))

    p_pkg = sub.add_parser("pkg", help="Package manager")
    pkg_sub = p_pkg.add_subparsers(dest="pkg_command", required=True)

    p_install = pkg_sub.add_parser("install", help="Install a package")
    p_install.add_argument("name", help="Package name")
    p_install.add_argument(
        "--registry",
        type=Path,
        default=None,
        metavar="DIR",
        help="Override registry directory (default: $PYX_REGISTRY or ~/.pyx/registry)",
    )
    p_install.add_argument(
        "--project-dir",
        type=Path,
        default=Path("."),
        metavar="DIR",
        help="Project root directory (default: current directory)",
    )

    p_publish = pkg_sub.add_parser("publish", help="Publish the current package to the registry")
    p_publish.add_argument(
        "--source-dir",
        type=Path,
        default=Path("."),
        metavar="DIR",
        help="Package source directory containing pyx.toml (default: current directory)",
    )
    p_publish.add_argument(
        "--registry",
        type=Path,
        default=None,
        metavar="DIR",
        help="Override registry directory (default: $PYX_REGISTRY or ~/.pyx/registry)",
    )

    args = parser.parse_args()

    if args.command == "check":
        return cmd_check(args.source)
    if args.command == "build":
        return cmd_build(args.source, args.out_dir)
    if args.command == "pkg":
        if args.pkg_command == "install":
            return cmd_pkg_install(args.name, args.registry, args.project_dir)
        if args.pkg_command == "publish":
            return cmd_pkg_publish(args.source_dir, args.registry)
    return 1  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
