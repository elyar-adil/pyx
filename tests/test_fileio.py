"""Tests for file I/O operations: open(), read(), write(), readline(), close(),
bytes type lowering, and with-open context manager."""
from pathlib import Path

from pyx.analyzer import Analyzer
from pyx.compiler import CompileError, LLVMCompiler


def write_tmp(tmp_path: Path, code: str) -> Path:
    p = tmp_path / "sample.py"
    p.write_text(code, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Analyzer tests
# ---------------------------------------------------------------------------


def test_open_text_returns_textfile(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def f(name: str) -> int:
    f = open(name, "r")
    f.close()
    return 0
""",
    )
    errors = Analyzer().analyze_path(src)
    assert errors == []


def test_open_binary_returns_binaryfile(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def f(name: str) -> int:
    f = open(name, "rb")
    f.close()
    return 0
""",
    )
    errors = Analyzer().analyze_path(src)
    assert errors == []


def test_open_default_mode_is_text(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def f(name: str) -> int:
    f = open(name)
    content: str = f.read()
    f.close()
    return 0
""",
    )
    errors = Analyzer().analyze_path(src)
    assert errors == []


def test_text_read_returns_str(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def f(name: str) -> str:
    f = open(name, "r")
    content: str = f.read()
    f.close()
    return content
""",
    )
    errors = Analyzer().analyze_path(src)
    assert errors == []


def test_text_readline_returns_str(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def f(name: str) -> str:
    f = open(name, "r")
    line: str = f.readline()
    f.close()
    return line
""",
    )
    errors = Analyzer().analyze_path(src)
    assert errors == []


def test_text_write_returns_int(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def f(name: str) -> int:
    f = open(name, "w")
    n: int = f.write("hello")
    f.close()
    return n
""",
    )
    errors = Analyzer().analyze_path(src)
    assert errors == []


def test_binary_read_returns_bytes(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def f(name: str) -> int:
    f = open(name, "rb")
    data: bytes = f.read()
    f.close()
    return 0
""",
    )
    errors = Analyzer().analyze_path(src)
    assert errors == []


def test_binary_write_returns_int(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def f(name: str) -> int:
    f = open(name, "wb")
    n: int = f.write(b"data")
    f.close()
    return n
""",
    )
    errors = Analyzer().analyze_path(src)
    assert errors == []


def test_with_open_text(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def f(name: str) -> str:
    with open(name, "r") as fp:
        content: str = fp.read()
    return content
""",
    )
    errors = Analyzer().analyze_path(src)
    assert errors == []


def test_with_open_binary(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def f(name: str) -> int:
    with open(name, "wb") as fp:
        fp.write(b"hello")
    return 0
""",
    )
    errors = Analyzer().analyze_path(src)
    assert errors == []


def test_bytes_literal_type_inferred(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def f() -> int:
    data: bytes = b"hello world"
    return len(data)
""",
    )
    errors = Analyzer().analyze_path(src)
    assert errors == []


def test_open_bad_filename_type_rejected(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def f(n: int) -> int:
    fp = open(n, "r")
    fp.close()
    return 0
""",
    )
    errors = Analyzer().analyze_path(src)
    assert any("filename must be str" in e.message for e in errors)


def test_text_write_wrong_type_rejected(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def f(name: str) -> int:
    fp = open(name, "w")
    fp.write(42)
    fp.close()
    return 0
""",
    )
    errors = Analyzer().analyze_path(src)
    # write() on TextFile expects str; 42 is int
    assert any("TextFile" in e.message or "write" in e.message for e in errors)


def test_unknown_file_method_rejected(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def f(name: str) -> int:
    fp = open(name, "r")
    fp.seek(0)
    fp.close()
    return 0
""",
    )
    errors = Analyzer().analyze_path(src)
    assert any("TextFile" in e.message for e in errors)


def test_with_non_open_rejected(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def ctx() -> int:
    return 1

def f() -> int:
    with ctx() as x:
        y = x
    return 0
""",
    )
    errors = Analyzer().analyze_path(src)
    assert any("only supported for open()" in e.message for e in errors)


# ---------------------------------------------------------------------------
# Compiler tests
# ---------------------------------------------------------------------------


def test_compile_open_text_write(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def f(name: str) -> int:
    fp = open(name, "w")
    fp.write("hello")
    fp.close()
    return 0
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "declare ptr @fopen(ptr, ptr)" in ir
    assert "declare i32 @fclose(ptr)" in ir
    assert "declare i64 @fwrite(ptr, i64, i64, ptr)" in ir
    assert "@fopen" in ir
    assert "@fclose" in ir
    assert "@fwrite" in ir


def test_compile_open_text_read(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def f(name: str) -> str:
    fp = open(name, "r")
    content: str = fp.read()
    fp.close()
    return content
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "declare ptr @fopen(ptr, ptr)" in ir
    assert "declare i64 @fread(ptr, i64, i64, ptr)" in ir
    assert "@__pyx_file_read_text" in ir
    assert "@fseek" in ir
    assert "@ftell" in ir


def test_compile_readline(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def f(name: str) -> str:
    fp = open(name, "r")
    line: str = fp.readline()
    fp.close()
    return line
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "@__pyx_file_readline" in ir
    assert "@fgets" in ir
    assert "@strlen" in ir


def test_compile_binary_read(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def f(name: str) -> int:
    fp = open(name, "rb")
    data: bytes = fp.read()
    fp.close()
    return len(data)
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "%pyx.bytes" in ir
    assert "@__pyx_file_read_binary" in ir


def test_compile_binary_write(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def f(name: str) -> int:
    fp = open(name, "wb")
    n: int = fp.write(b"binary data")
    fp.close()
    return n
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "%pyx.bytes" in ir
    assert "@fwrite" in ir
    # bytes literal constant should appear
    assert "@__pyx_bytes_0" in ir


def test_compile_with_open_emits_fclose(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def f(name: str) -> str:
    with open(name, "r") as fp:
        content: str = fp.read()
    return content
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "@fopen" in ir
    assert "@fclose" in ir
    assert "@__pyx_file_read_text" in ir


def test_compile_bytes_literal_len(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def f() -> int:
    data: bytes = b"hello"
    return len(data)
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "%pyx.bytes" in ir
    assert "extractvalue %pyx.bytes" in ir


def test_compile_fopen_mode_in_globals(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def f(name: str) -> int:
    fp = open(name, "wb")
    fp.close()
    return 0
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    # The "wb" mode string should appear as a global constant
    assert 'c"wb\\00"' in ir


def test_compile_open_no_mode_defaults_text(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def f(name: str) -> str:
    fp = open(name)
    line: str = fp.readline()
    fp.close()
    return line
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    assert "@__pyx_file_readline" in ir
    # Default mode "r" should appear as a global
    assert 'c"r\\00"' in ir


def test_compile_bytes_no_file_decls_when_unused(tmp_path: Path) -> None:
    src = write_tmp(
        tmp_path,
        """
def f() -> int:
    data: bytes = b"test"
    return len(data)
""",
    )
    ir = LLVMCompiler.from_path(src).compile_ir()
    # No file operations used, so fopen/fclose should not appear
    assert "fopen" not in ir
    assert "fclose" not in ir
    # But bytes type and literal should be present
    assert "%pyx.bytes" in ir
