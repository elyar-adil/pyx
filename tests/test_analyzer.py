from pathlib import Path

from pyx.analyzer import Analyzer


def write_tmp(tmp_path: Path, code: str) -> Path:
    p = tmp_path / "sample.py"
    p.write_text(code, encoding="utf-8")
    return p


def test_ok_function(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def add(a: int, b: int) -> int:
    c = a + b
    return c
""",
    )
    errors = Analyzer().analyze_path(src)
    assert errors == []


def test_type_change_rejected(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def f(x: int) -> int:
    y = 1
    y = 's'
    return x
""",
    )
    errors = Analyzer().analyze_path(src)
    assert any("cannot change type" in e.message for e in errors)


def test_reflection_rejected(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def f(x: int) -> int:
    return getattr(x, 'real')
""",
    )
    errors = Analyzer().analyze_path(src)
    assert any("not allowed" in e.message for e in errors)


def test_print_primitives_ok(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def f(i: int, x: float, b: bool, s: str) -> int:
    print(i)
    print(x)
    print(b)
    print(s)
    print(i, x, b, s)
    print()
    return i
""",
    )
    errors = Analyzer().analyze_path(src)
    assert errors == []


def test_print_unsupported_arg_rejected(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def f(xs: list[int]) -> int:
    print(xs)
    return 0
""",
    )
    errors = Analyzer().analyze_path(src)
    assert any("unsupported type" in e.message for e in errors)
