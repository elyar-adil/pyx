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
            print(format_diagnostic(source, Diagnostic(err.code, err.message, err.line, err.col)))
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
        print(format_diagnostic(source, Diagnostic(exc.code, exc.message, exc.line, exc.col)))
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


def main() -> int:
    parser = argparse.ArgumentParser(prog="pyx")
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="Run static subset checks")
    p_check.add_argument("source", type=Path)

    p_build = sub.add_parser("build", help="Compile typed subset to LLVM IR and native object")
    p_build.add_argument("source", type=Path)
    p_build.add_argument("-o", "--out-dir", type=Path, default=Path("dist"))

    args = parser.parse_args()
    if args.command == "check":
        return cmd_check(args.source)
    return cmd_build(args.source, args.out_dir)


if __name__ == "__main__":
    raise SystemExit(main())
