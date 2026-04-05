from pathlib import Path

from pyx.compiler import CompileError, LLVMCompiler


def write_tmp(tmp_path: Path, code: str) -> Path:
    p = tmp_path / "sample.py"
    p.write_text(code, encoding="utf-8")
    return p


def write_project(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, code in files.items():
        path = tmp_path / name
        path.write_text(code, encoding="utf-8")
    return tmp_path / "main.py"


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


def test_not_equal_comparison(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def neq(a: int, b: int) -> bool:
    return a != b
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "icmp ne i64" in ir


def test_unary_not(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def flip(b: bool) -> bool:
    return not b
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "xor i1" in ir


def test_unary_negate_int(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def neg(n: int) -> int:
    return -n
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "sub i64 0," in ir


def test_ann_assign_in_compiler(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def f(n: int) -> int:
    x: int = n + 1
    return x
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "store i64" in ir
    assert "%x.slot" in ir


def test_list_literal_no_double_compile(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def counter() -> int:
    return 1

def make_list() -> int:
    xs = [counter(), counter()]
    return xs[0]
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert ir.count("call i64 @counter") == 2


def test_str_index_lowering(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def first_char(s: str) -> str:
    return s[0]
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "getelementptr i8" in ir
    assert f"insertvalue {{% pyx.str}}" in ir or "insertvalue %pyx.str" in ir


def test_from_import_constructor(tmp_path: Path) -> None:
    (tmp_path / "models.py").write_text(
        """
class Point:
    x: int
    y: int
""",
        encoding="utf-8",
    )
    src = write_tmp(
        tmp_path,
        """
from models import Point

def make() -> int:
    p = Point(1, 2)
    return p.x
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "insertvalue" in ir
    assert "extractvalue" in ir


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


def test_compile_imported_class_method_lowering(tmp_path: Path) -> None:
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
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "%type.geom__Point = type { i64, i64 }" in ir
    assert "define i64 @mod_geom__Point__total(%type.geom__Point %self)" in ir
    assert "call i64 @mod_geom__Point__total" in ir


def test_compile_stdlib_import_reports_module_not_found(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
import logging
from urllib.parse import urlparse

def run() -> int:
    return 0
""",
    )
    try:
        LLVMCompiler.from_path(src).compile_ir()
    except CompileError as exc:
        assert exc.code == "PYX2000"
        assert "cannot resolve imported module 'logging'" in exc.message
    else:
        raise AssertionError("expected CompileError")


def test_compile_list_append_and_index_lowering(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def run(n: int) -> int:
    xs = [n, 2]
    xs.append(3)
    return xs[1]
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "%pyx.list = type { ptr, i64, i64 }" in ir
    assert "call ptr @realloc" in ir
    assert "getelementptr i64" in ir
    assert "extractvalue %pyx.list" in ir


def test_compile_string_concat_lowering(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def run() -> str:
    a = "he"
    b = "llo"
    return a + b
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "%pyx.str = type { ptr, i64 }" in ir
    assert "declare ptr @malloc(i64)" in ir
    assert "declare ptr @memcpy(ptr, ptr, i64)" in ir
    assert "insertvalue %pyx.str" in ir


def test_compile_utf8_len_uses_helper(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def run() -> int:
    return len("你")
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "define private i64 @__pyx_utf8_len(ptr %data, i64 %nbytes)" in ir
    assert "call i64 @__pyx_utf8_len(ptr" in ir


def test_compile_utf8_index_uses_helper(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def run(s: str) -> str:
    return s[0]
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "define private %pyx.str @__pyx_utf8_index(ptr %data, i64 %nbytes, i64 %index)" in ir
    assert "declare void @abort()" in ir
    assert "call %pyx.str @__pyx_utf8_index" in ir


def test_compile_list_item_assignment_lowering(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def run() -> int:
    xs = [1, 2]
    xs[0] = 3
    return xs[0]
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "declare void @abort()" in ir
    assert "getelementptr i64" in ir
    assert "store i64 3" in ir


def test_set_type_reports_planned_not_lowered(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def run(xs: set[int]) -> int:
    return 0
""",
    )
    try:
        LLVMCompiler.from_path(src).compile_ir()
    except CompileError as exc:
        assert exc.code == "PYX2002"
        assert "planned but not lowered" in exc.message
    else:
        raise AssertionError("expected CompileError")


def test_string_compare_reports_planned_not_lowered(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def run(a: str, b: str) -> bool:
    return a == b
""",
    )
    try:
        LLVMCompiler.from_path(src).compile_ir()
    except CompileError as exc:
        assert exc.code == "PYX2002"
        assert "string comparison" in exc.message
    else:
        raise AssertionError("expected CompileError")


def test_compile_dict_literal_and_subscript_lowering(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def run() -> int:
    d: dict[str, int] = {"a": 1}
    return d["a"]
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "%pyx.dict = type { ptr, i64, i64 }" in ir
    assert "define private %pyx.dict @__pyx_dict_new__dict_str__int()" in ir
    assert "define private %pyx.dict @__pyx_dict_set__dict_str__int" in ir
    assert "define private i1 @__pyx_dict_try_get__dict_str__int" in ir
    assert "call %pyx.dict @__pyx_dict_set__dict_str__int" in ir
    assert "call i1 @__pyx_dict_try_get__dict_str__int" in ir


def test_compile_dict_contains_get_and_len_lowering(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def run() -> int:
    d: dict[str, int] = {}
    if "a" in d:
        return d.get("a", 0)
    d["a"] = 2
    return len(d)
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "define private i1 @__pyx_dict_contains__dict_str__int" in ir
    assert "call i1 @__pyx_dict_contains__dict_str__int" in ir
    assert "call i1 @__pyx_dict_try_get__dict_str__int" in ir
    assert "extractvalue %pyx.dict" in ir


def test_compile_empty_dict_reassignment_uses_existing_type_context(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def run() -> int:
    d: dict[str, int] = {"a": 1}
    d = {}
    return len(d)
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "call %pyx.dict @__pyx_dict_new__dict_str__int()" in ir
    assert "extractvalue %pyx.dict" in ir


def test_compile_dict_with_hashable_class_key(tmp_path: Path) -> None:
    src = write_project(
        tmp_path,
        {
            "models.py": """
class Point:
    x: int
    y: int
""",
            "main.py": """
import models

def run(n: int) -> int:
    p = models.Point(n, 1)
    d: dict[models.Point, int] = {p: 3}
    return d[p]
""",
        },
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "define private i64 @__pyx_hash__models__Point" in ir
    assert "define private i1 @__pyx_eq__models__Point" in ir
    assert "define private i1 @__pyx_dict_try_get__dict_models__Point__int" in ir


def test_compile_unhashable_dict_key_reports_error(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def run() -> int:
    d: dict[list[int], int] = {}
    return 0
""",
    )
    try:
        LLVMCompiler.from_path(src).compile_ir()
    except CompileError as exc:
        assert exc.code == "PYX2002"
        assert "not hashable" in exc.message
    else:
        raise AssertionError("expected CompileError")
