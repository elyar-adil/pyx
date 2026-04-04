from pathlib import Path

from pyx.compiler import LLVMCompiler


def write_tmp(tmp_path: Path, code: str) -> Path:
    p = tmp_path / "sample.py"
    p.write_text(code, encoding="utf-8")
    return p


def test_compile_recursive_function_to_llvm_ir(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def fib(n: int) -> int:
    if n <= 1:
        return n
    return fib(n - 1) + fib(n - 2)
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "define i64 @fib(i64 %n)" in ir
    assert "icmp sle i64" in ir
    assert "call i64 @fib(i64" in ir


def test_compile_multiple_functions(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def add(a: int, b: int) -> int:
    return a + b

def main(x: int) -> int:
    return add(x, 2)
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "define i64 @add(i64 %a, i64 %b)" in ir
    assert "define i64 @main(i64 %x)" in ir


def test_reassign_and_while_lowering(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def sum_to(n: int) -> int:
    i = 0
    acc = 0
    while i < n:
        acc = acc + i
        i = i + 1
    return acc
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "while_cond" in ir
    assert "store i64" in ir
    assert "load i64" in ir


def test_float_and_bool_lowering(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def gt(a: float, b: float) -> bool:
    return a > b
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "define i1 @gt(double %a, double %b)" in ir
    assert "fcmp ogt double" in ir


def test_print_int(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def f(n: int) -> int:
    print(n)
    return n
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "declare i32 @printf(ptr, ...)" in ir
    assert "@__pyx_fmt_int_nl" in ir
    assert "call i32 (ptr, ...) @printf" in ir


def test_print_str_literal(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def f(n: int) -> int:
    print("hello")
    return n
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "declare i32 @printf(ptr, ...)" in ir
    assert "@__pyx_str_0" in ir
    assert 'c"hello\\00"' in ir
    assert "call i32 (ptr, ...) @printf" in ir


def test_print_bool(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def f(b: bool) -> bool:
    print(b)
    return b
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "select i1" in ir
    assert "@__pyx_str_True" in ir
    assert "@__pyx_str_False" in ir


def test_print_no_args(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def f(n: int) -> int:
    print()
    return n
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "@__pyx_fmt_nl" in ir


def test_print_multiple_args(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def f(a: int, b: int) -> int:
    print(a, b)
    return a
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    # First arg uses space-suffix format, last uses newline-suffix format.
    assert "@__pyx_fmt_int_sp" in ir
    assert "@__pyx_fmt_int_nl" in ir


def test_no_print_globals_when_print_unused(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def add(a: int, b: int) -> int:
    return a + b
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "printf" not in ir
    assert "__pyx_fmt" not in ir


def test_compile_if_elif_else_branch_merge(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def classify(n: int) -> int:
    if n < 0:
        value = 1
    elif n == 0:
        value = 2
    else:
        value = 3
    return value
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "endif" in ir
    assert "store i64 1, ptr %value.slot" in ir
    assert "store i64 2, ptr %value.slot" in ir
    assert "store i64 3, ptr %value.slot" in ir
    assert "load i64, ptr %value.slot" in ir


def test_compile_union_numeric_lowering(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def add_one(x: int | float) -> int | float:
    return x + 1
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "define { i1, double } @add_one({ i1, double } %x)" in ir
    assert "extractvalue { i1, double }" in ir
    assert "insertvalue { i1, double }" in ir
    assert "phi i1" in ir


def test_compile_mixed_numeric_arithmetic_to_float(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def widen(x: int) -> float:
    return x + 1.5
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "sitofp i64" in ir
    assert "fadd double" in ir
