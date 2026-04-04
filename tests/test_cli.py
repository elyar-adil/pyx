from pathlib import Path

from pyx.cli import cmd_build, cmd_check


def write_tmp(tmp_path: Path, code: str) -> Path:
    p = tmp_path / "sample.py"
    p.write_text(code, encoding="utf-8")
    return p


def write_project(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, code in files.items():
        path = tmp_path / name
        path.write_text(code, encoding="utf-8")
    return tmp_path / "main.py"


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


def test_cmd_build_supports_multi_module_phase3_project(tmp_path: Path, capsys) -> None:
    src = write_project(
        tmp_path,
        {
            "geom.py": """
class Point:
    x: int
    y: int

    def total(self) -> int:
        return self.x + self.y
""",
            "main.py": """
import geom

def run(n: int) -> int:
    p = geom.Point(n, 2)
    return p.total()
""",
        },
    )
    out_dir = tmp_path / "dist"
    rc = cmd_build(src, out_dir)
    out = capsys.readouterr().out
    ir = (out_dir / "main.ll").read_text(encoding="utf-8")
    assert rc == 0
    assert "Build artifacts written" in out
    assert "@mod_geom__Point__total" in ir
