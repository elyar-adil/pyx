"""Phase 4 compiler tests: ctypes C ABI FFI LLVM IR lowering."""
from pathlib import Path

import pytest

from pyx.compiler import CompileError, LLVMCompiler


def write_tmp(tmp_path: Path, code: str) -> Path:
    p = tmp_path / "sample.py"
    p.write_text(code, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


def test_cdll_emits_dlopen(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
import ctypes

def f(n: int) -> int:
    lib = ctypes.CDLL("libc.so.6")
    return n
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "declare ptr @dlopen(ptr, i32)" in ir
    assert "call ptr @dlopen(ptr" in ir
    assert "libc.so.6" in ir


def test_cfunctype_stores_null_ptr(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
import ctypes

def f(n: int) -> int:
    abs_t = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int)
    return n
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    # CFUNCTYPE generates no runtime code — a null is stored to the slot.
    assert "store ptr null, ptr %abs_t.slot" in ir
    # No dlopen / dlsym needed just for the type descriptor.
    assert "@dlopen" not in ir
    assert "@dlsym" not in ir


def test_cfuncptr_binding_emits_dlsym(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
import ctypes

def f(n: int) -> int:
    lib = ctypes.CDLL("libc.so.6")
    abs_t = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int)
    c_abs = abs_t(("abs", lib))
    return n
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "declare ptr @dlsym(ptr, ptr)" in ir
    assert "call ptr @dlsym(ptr" in ir
    assert "abs" in ir


def test_cfuncptr_call_emits_indirect_call(tmp_path: Path) -> None:
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
    ir = LLVMCompiler.from_path(src).compile_ir()
    # Indirect call through function pointer: call i32 (i32) %rN(i32 ...)
    assert "call i32 (i32)" in ir
    # The int arg must be truncated i64 → i32
    assert "trunc i64" in ir
    # The i32 result must be sign-extended back to i64
    assert "sext i32" in ir


def test_full_abs_ffi(tmp_path: Path) -> None:
    """End-to-end test: same .py works under python and pyx build."""
    src = write_tmp(
        tmp_path,
        """
import ctypes

def call_abs(n: int) -> int:
    lib = ctypes.CDLL("libc.so.6")
    abs_t = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int)
    c_abs = abs_t(("abs", lib))
    return c_abs(n)
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "define i64 @call_abs(i64 %n)" in ir
    assert "@dlopen" in ir
    assert "@dlsym" in ir
    assert "call i32 (i32)" in ir


def test_double_ffi_sqrt(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
import ctypes

def call_sqrt(x: float) -> float:
    lib = ctypes.CDLL("libm.so.6")
    sqrt_t = ctypes.CFUNCTYPE(ctypes.c_double, ctypes.c_double)
    c_sqrt = sqrt_t(("sqrt", lib))
    return c_sqrt(x)
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "define double @call_sqrt(double %x)" in ir
    # double → double: no trunc/sext needed
    assert "call double (double)" in ir
    assert "trunc" not in ir
    assert "sext" not in ir


def test_from_ctypes_import_style(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
from ctypes import CDLL, CFUNCTYPE, c_int

def call_abs(n: int) -> int:
    lib = CDLL("libc.so.6")
    abs_t = CFUNCTYPE(c_int, c_int)
    c_abs = abs_t(("abs", lib))
    return c_abs(n)
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "@dlopen" in ir
    assert "@dlsym" in ir
    assert "call i32 (i32)" in ir


def test_long_arg_no_trunc(tmp_path: Path) -> None:
    """c_long is i64 — no truncation for int (i64) args."""
    src = write_tmp(
        tmp_path,
        """
import ctypes

def call_labs(n: int) -> int:
    lib = ctypes.CDLL("libc.so.6")
    labs_t = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_long)
    c_labs = labs_t(("labs", lib))
    return c_labs(n)
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "call i64 (i64)" in ir
    assert "trunc" not in ir
    assert "sext" not in ir


def test_float_arg_truncation(tmp_path: Path) -> None:
    """c_float is 'float' — PyX float (double) must be fptrunc'd."""
    src = write_tmp(
        tmp_path,
        """
import ctypes

def call_sinf(x: float) -> float:
    lib = ctypes.CDLL("libm.so.6")
    sinf_t = ctypes.CFUNCTYPE(ctypes.c_float, ctypes.c_float)
    c_sinf = sinf_t(("sinf", lib))
    return c_sinf(x)
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "call float (float)" in ir
    assert "fptrunc double" in ir
    assert "fpext float" in ir


def test_dlopen_rtld_lazy_flag(tmp_path: Path) -> None:
    """dlopen is called with flag 1 (RTLD_LAZY)."""
    src = write_tmp(
        tmp_path,
        """
import ctypes

def f(n: int) -> int:
    lib = ctypes.CDLL("libc.so.6")
    return n
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "i32 1" in ir  # RTLD_LAZY flag


def test_cfunctype_no_args(tmp_path: Path) -> None:
    """A function taking no arguments."""
    src = write_tmp(
        tmp_path,
        """
import ctypes

def call_getpid() -> int:
    lib = ctypes.CDLL("libc.so.6")
    getpid_t = ctypes.CFUNCTYPE(ctypes.c_int)
    c_getpid = getpid_t(("getpid", lib))
    return c_getpid()
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    # Indirect call with no arguments, returning i32
    assert "call i32 ()" in ir
    assert "sext i32" in ir


# ---------------------------------------------------------------------------
# Error-path tests
# ---------------------------------------------------------------------------


def test_cdll_non_literal_name_error(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
import ctypes

def f(name: str) -> int:
    lib = ctypes.CDLL(name)
    return 0
""",
    )
    with pytest.raises(CompileError, match="string-literal"):
        LLVMCompiler.from_path(src).compile_ir()
