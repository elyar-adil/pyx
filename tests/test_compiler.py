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


def test_dunder_main_emits_native_entry_point(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def main() -> int:
    return 0

if __name__ == "__main__":
    main()
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    # PyX 'main' is renamed to 'pyx_main' to avoid clashing with the C entry point
    assert "define i64 @pyx_main()" in ir
    assert "define i32 @main()" in ir
    assert "call i64 @pyx_main()" in ir
    assert "trunc i64 %pyx_ret to i32" in ir


def test_dunder_main_non_int_return(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def run() -> bool:
    return True

if __name__ == "__main__":
    run()
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "define i1 @run()" in ir
    assert "define i32 @main()" in ir
    assert "call i1 @run()" in ir
    assert "ret i32 0" in ir


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
