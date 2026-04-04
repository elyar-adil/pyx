"""Phase 4 analyzer tests: ctypes C ABI FFI pattern recognition."""
from pathlib import Path

from pyx.analyzer import Analyzer


def write_tmp(tmp_path: Path, code: str) -> Path:
    p = tmp_path / "sample.py"
    p.write_text(code, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


def test_import_ctypes_no_error(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
import ctypes

def f(n: int) -> int:
    return n
""",
    )
    errors = Analyzer().analyze_path(src)
    assert errors == []


def test_cdll_inferred_as_cdll_type(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
import ctypes

def f(n: int) -> int:
    lib = ctypes.CDLL("libc.so.6")
    return n
""",
    )
    errors = Analyzer().analyze_path(src)
    assert errors == []


def test_cfunctype_no_error(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
import ctypes

def f(n: int) -> int:
    abs_t = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int)
    return n
""",
    )
    errors = Analyzer().analyze_path(src)
    assert errors == []


def test_cfuncptr_binding_no_error(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
import ctypes

def call_abs(n: int) -> int:
    lib = ctypes.CDLL("libc.so.6")
    abs_t = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int)
    c_abs = abs_t(("abs", lib))
    return n
""",
    )
    errors = Analyzer().analyze_path(src)
    assert errors == []


def test_cfuncptr_call_returns_int(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
import ctypes

def call_abs(n: int) -> int:
    lib = ctypes.CDLL("libc.so.6")
    abs_t = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int)
    c_abs = abs_t(("abs", lib))
    result = c_abs(n)
    return result
""",
    )
    errors = Analyzer().analyze_path(src)
    assert errors == []


def test_cfunctype_double_return(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
import ctypes

def call_sqrt(x: float) -> float:
    lib = ctypes.CDLL("libm.so.6")
    sqrt_t = ctypes.CFUNCTYPE(ctypes.c_double, ctypes.c_double)
    c_sqrt = sqrt_t(("sqrt", lib))
    result = c_sqrt(x)
    return result
""",
    )
    errors = Analyzer().analyze_path(src)
    assert errors == []


def test_from_ctypes_import_style(tmp_path: Path) -> None:
    """from ctypes import CDLL, CFUNCTYPE, c_int must also work."""
    src = write_tmp(
        tmp_path,
        """
from ctypes import CDLL, CFUNCTYPE, c_int

def call_abs(n: int) -> int:
    lib = CDLL("libc.so.6")
    abs_t = CFUNCTYPE(c_int, c_int)
    c_abs = abs_t(("abs", lib))
    result = c_abs(n)
    return result
""",
    )
    errors = Analyzer().analyze_path(src)
    assert errors == []


def test_cfunctype_void_return(tmp_path: Path) -> None:
    """CFUNCTYPE(None, ...) means void return."""
    src = write_tmp(
        tmp_path,
        """
import ctypes

def run(n: int) -> int:
    lib = ctypes.CDLL("libc.so.6")
    exit_t = ctypes.CFUNCTYPE(None, ctypes.c_int)
    c_exit = exit_t(("exit", lib))
    return n
""",
    )
    errors = Analyzer().analyze_path(src)
    assert errors == []


def test_cdll_multiple_functions(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
import ctypes

def demo(n: int, x: float) -> int:
    lib = ctypes.CDLL("libc.so.6")
    abs_t = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int)
    c_abs = abs_t(("abs", lib))
    mlib = ctypes.CDLL("libm.so.6")
    sqrt_t = ctypes.CFUNCTYPE(ctypes.c_double, ctypes.c_double)
    c_sqrt = sqrt_t(("sqrt", mlib))
    r = c_abs(n)
    return r
""",
    )
    errors = Analyzer().analyze_path(src)
    assert errors == []


# ---------------------------------------------------------------------------
# Error-path tests
# ---------------------------------------------------------------------------


def test_cdll_wrong_arg_type(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
import ctypes

def f(n: int) -> int:
    lib = ctypes.CDLL(n)
    return n
""",
    )
    errors = Analyzer().analyze_path(src)
    assert any("CDLL() expects a str" in e.message for e in errors)


def test_cfunctype_no_args_error(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
import ctypes

def f(n: int) -> int:
    t = ctypes.CFUNCTYPE()
    return n
""",
    )
    errors = Analyzer().analyze_path(src)
    assert any("CFUNCTYPE() requires" in e.message for e in errors)


def test_cfunctype_bad_arg_type_error(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
import ctypes

def f(n: int) -> int:
    t = ctypes.CFUNCTYPE(ctypes.c_int, n)
    return n
""",
    )
    errors = Analyzer().analyze_path(src)
    assert any("must be a ctypes type" in e.message for e in errors)


def test_cfuncptr_binding_wrong_lib_type(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
import ctypes

def f(n: int) -> int:
    abs_t = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int)
    c_abs = abs_t(("abs", n))
    return n
""",
    )
    errors = Analyzer().analyze_path(src)
    assert any("CDLL handle" in e.message for e in errors)
