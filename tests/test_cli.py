from pathlib import Path

from pyx.cli import cmd_build, cmd_check


def write_tmp(tmp_path: Path, code: str) -> Path:
    p = tmp_path / "sample.py"
    p.write_text(code, encoding="utf-8")
    return p


def test_cmd_check_uses_unified_error_format(tmp_path: Path, capsys) -> None:
    src = write_tmp(
        tmp_path,
        """
def f(x) -> int:
    return x
""",
    )
    rc = cmd_check(src)
    out = capsys.readouterr().out
    assert rc == 1
    assert "error[PYX1001]" in out


def test_cmd_build_uses_unified_compiler_error_format(tmp_path: Path, capsys) -> None:
    src = write_tmp(
        tmp_path,
        """
def f(x: int) -> int:
    return x / 2
""",
    )
    rc = cmd_build(src, tmp_path / "dist")
    out = capsys.readouterr().out
    assert rc == 1
    assert "error[PYX2005]" in out
