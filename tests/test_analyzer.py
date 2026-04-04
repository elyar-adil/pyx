from pathlib import Path

from pyx.analyzer import Analyzer


def write_tmp(tmp_path: Path, code: str) -> Path:
    p = tmp_path / "sample.py"
    p.write_text(code, encoding="utf-8")
    return p


def write_project(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, code in files.items():
        path = tmp_path / name
        path.write_text(code, encoding="utf-8")
    return tmp_path / "main.py"


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


def test_union_numeric_type_supported(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def widen(x: int) -> int | float:
    y: int | float = x
    y = 1.5
    return y + 1
""",
    )
    errors = Analyzer().analyze_path(src)
    assert errors == []


def test_call_argument_type_checked(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def accept(x: int | float) -> int | float:
    return x

def main(flag: bool) -> int | float:
    return accept(flag)
""",
    )
    errors = Analyzer().analyze_path(src)
    assert any(e.code == "PYX1011" for e in errors)


def test_imported_class_and_list_ops_supported(tmp_path: Path) -> None:
    src = write_project(
        tmp_path,
        {
            "models.py": """
class Point:
    x: int
    y: int

    def total(self) -> int:
        return self.x + self.y
""",
            "main.py": """
import models

def run(n: int) -> int:
    p = models.Point(n, 2)
    xs = [p.total(), n]
    xs.append(5)
    return len(xs)
""",
        },
    )
    errors = Analyzer().analyze_path(src)
    assert errors == []


def test_non_call_expression_statement_rejected(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def f(x: int) -> int:
    x + 1
    return x
""",
    )
    errors = Analyzer().analyze_path(src)
    assert any(e.code == "PYX1014" for e in errors)


def test_unknown_field_assignment_rejected(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
class Point:
    x: int

def run(n: int) -> int:
    p = Point(n)
    p.y = 2
    return p.x
""",
    )
    errors = Analyzer().analyze_path(src)
    assert any(e.code == "PYX1013" for e in errors)
