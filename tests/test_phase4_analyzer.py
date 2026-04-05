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


# ---------------------------------------------------------------------------
# POINTER(T) type support
# ---------------------------------------------------------------------------


def test_pointer_type_in_cfunctype_no_error(tmp_path: Path) -> None:
    """POINTER(c_int) in CFUNCTYPE signature should be accepted."""
    src = write_tmp(
        tmp_path,
        """
import ctypes

def call_with_ptr(n: int) -> int:
    lib = ctypes.CDLL("libc.so.6")
    fn_t = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(ctypes.c_int))
    c_fn = fn_t(("some_fn", lib))
    return n
""",
    )
    errors = Analyzer().analyze_path(src)
    assert errors == []


def test_pointer_return_type_in_cfunctype(tmp_path: Path) -> None:
    """POINTER(T) as return type in CFUNCTYPE should be accepted."""
    src = write_tmp(
        tmp_path,
        """
import ctypes

def get_ptr() -> int:
    lib = ctypes.CDLL("libc.so.6")
    fn_t = ctypes.CFUNCTYPE(ctypes.POINTER(ctypes.c_char), ctypes.c_int)
    c_fn = fn_t(("alloc_buf", lib))
    return 0
""",
    )
    errors = Analyzer().analyze_path(src)
    assert errors == []


# ---------------------------------------------------------------------------
# str / bytes → c_char_p argument coercion
# ---------------------------------------------------------------------------


def test_str_arg_to_c_char_p_no_error(tmp_path: Path) -> None:
    """Passing a str to a c_char_p parameter should be accepted."""
    src = write_tmp(
        tmp_path,
        """
import ctypes

def call_puts(s: str) -> int:
    lib = ctypes.CDLL("libc.so.6")
    puts_t = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p)
    c_puts = puts_t(("puts", lib))
    return c_puts(s)
""",
    )
    errors = Analyzer().analyze_path(src)
    assert errors == []


def test_bytes_arg_to_c_char_p_no_error(tmp_path: Path) -> None:
    """Passing bytes to a c_char_p parameter should be accepted."""
    src = write_tmp(
        tmp_path,
        """
import ctypes

def call_puts_bytes(s: bytes) -> int:
    lib = ctypes.CDLL("libc.so.6")
    puts_t = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p)
    c_puts = puts_t(("puts", lib))
    return c_puts(s)
""",
    )
    errors = Analyzer().analyze_path(src)
    assert errors == []


def test_int_arg_to_c_char_p_is_error(tmp_path: Path) -> None:
    """Passing int to a c_char_p parameter should report a type error."""
    src = write_tmp(
        tmp_path,
        """
import ctypes

def bad_call(n: int) -> int:
    lib = ctypes.CDLL("libc.so.6")
    puts_t = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p)
    c_puts = puts_t(("puts", lib))
    return c_puts(n)
""",
    )
    errors = Analyzer().analyze_path(src)
    assert any("c_char_p" in e.message for e in errors)


def test_str_arg_to_c_int_is_error(tmp_path: Path) -> None:
    """Passing str to a c_int parameter should report a type error."""
    src = write_tmp(
        tmp_path,
        """
import ctypes

def bad_call(s: str) -> int:
    lib = ctypes.CDLL("libc.so.6")
    abs_t = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int)
    c_abs = abs_t(("abs", lib))
    return c_abs(s)
""",
    )
    errors = Analyzer().analyze_path(src)
    assert any("c_int" in e.message for e in errors)


# ---------------------------------------------------------------------------
# c_char_p return → bytes
# ---------------------------------------------------------------------------


def test_c_char_p_return_inferred_as_bytes(tmp_path: Path) -> None:
    """A function returning c_char_p should yield type bytes at the call site."""
    src = write_tmp(
        tmp_path,
        """
import ctypes

def get_env(name: str) -> bytes:
    lib = ctypes.CDLL("libc.so.6")
    getenv_t = ctypes.CFUNCTYPE(ctypes.c_char_p, ctypes.c_char_p)
    c_getenv = getenv_t(("getenv", lib))
    result = c_getenv(name)
    return result
""",
    )
    errors = Analyzer().analyze_path(src)
    assert errors == []


# ---------------------------------------------------------------------------
# ctypes.string_at(ptr, size) → bytes
# ---------------------------------------------------------------------------


def test_string_at_returns_bytes(tmp_path: Path) -> None:
    """ctypes.string_at(ptr, size) should return bytes with no errors."""
    src = write_tmp(
        tmp_path,
        """
import ctypes

def read_buf(n: int) -> bytes:
    lib = ctypes.CDLL("libc.so.6")
    fn_t = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_int)
    c_fn = fn_t(("some_fn", lib))
    ptr = c_fn(n)
    data = ctypes.string_at(ptr, n)
    return data
""",
    )
    errors = Analyzer().analyze_path(src)
    assert errors == []


def test_string_at_wrong_size_type(tmp_path: Path) -> None:
    """string_at() with a non-int size should report a type error."""
    src = write_tmp(
        tmp_path,
        """
import ctypes

def bad(s: str, n: int) -> bytes:
    lib = ctypes.CDLL("libc.so.6")
    fn_t = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_int)
    c_fn = fn_t(("some_fn", lib))
    ptr = c_fn(n)
    data = ctypes.string_at(ptr, s)
    return data
""",
    )
    errors = Analyzer().analyze_path(src)
    assert any("size" in e.message for e in errors)
