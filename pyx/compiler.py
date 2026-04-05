from __future__ import annotations

import ast
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .project import ClassInfo, FunctionSignature, ModuleInfo, ProjectInfo, ProjectLoadError, load_project
from .type_system import (
    BIN_FILE_TYPE,
    CDLL_TYPE,
    CTYPES_ALL_TYPES,
    CTYPES_FLOAT_TYPES,
    CTYPES_INT_TYPES,
    FILE_TYPES,
    NUMERIC_UNION,
    TEXT_FILE_TYPE,
    can_assign_type,
    ctypes_to_pyx_type,
    is_cfuncptr_type,
    is_numeric_type,
    is_supported_type,
    is_union_type,
    merge_numeric_result_type,
    normalize_type_name,
    parse_cfuncptr_type,
    parse_dict_type,
    parse_list_type,
    parse_set_type,
)

TYPE_MAP = {"int": "i64", "float": "double", "bool": "i1"}
UNION_LLVM_TYPE = "{ i1, double }"
STR_LLVM_TYPE = "%pyx.str"
LIST_LLVM_TYPE = "%pyx.list"
BYTES_LLVM_TYPE = "%pyx.bytes"

# ---------------------------------------------------------------------------
# Phase 4: ctypes / C ABI FFI
# ---------------------------------------------------------------------------

#: Maps ctypes type names to their LLVM IR primitive types.
CTYPES_LLVM_MAP: dict[str, str] = {
    "c_int":       "i32",
    "c_uint":      "i32",
    "c_long":      "i64",
    "c_ulong":     "i64",
    "c_longlong":  "i64",
    "c_ulonglong": "i64",
    "c_short":     "i16",
    "c_ushort":    "i16",
    "c_byte":      "i8",
    "c_ubyte":     "i8",
    "c_char":      "i8",
    "c_size_t":    "i64",
    "c_ssize_t":   "i64",
    "c_float":     "float",
    "c_double":    "double",
    "c_void_p":    "ptr",
    "c_char_p":    "ptr",
    "c_wchar_p":   "ptr",
}

_ERR_PROJECT = "PYX2000"
_ERR_MISSING_ANNOTATION = "PYX2001"
_ERR_UNSUPPORTED_TYPE = "PYX2002"
_ERR_MISSING_RETURN = "PYX2003"
_ERR_UNSUPPORTED_STATEMENT = "PYX2004"
_ERR_UNSUPPORTED_EXPRESSION = "PYX2005"
_ERR_TYPE_MISMATCH = "PYX2006"
_ERR_UNKNOWN_VARIABLE = "PYX2007"
_ERR_UNKNOWN_FUNCTION = "PYX2008"
_ERR_CALL_ARG_COUNT = "PYX2009"
_ERR_PRINT_USAGE = "PYX2010"
_ERR_UNKNOWN_FIELD = "PYX2011"

_PRINT_FMT_DEFS: tuple[tuple[str, int, str], ...] = (
    ("@__pyx_fmt_int_sp", 5, 'c"%ld \\00"'),
    ("@__pyx_fmt_int_nl", 5, 'c"%ld\\0a\\00"'),
    ("@__pyx_fmt_flt_sp", 4, 'c"%g \\00"'),
    ("@__pyx_fmt_flt_nl", 4, 'c"%g\\0a\\00"'),
    ("@__pyx_fmt_str_sp", 6, 'c"%.*s \\00"'),
    ("@__pyx_fmt_str_nl", 6, 'c"%.*s\\0a\\00"'),
    ("@__pyx_fmt_nl", 2, 'c"\\0a\\00"'),
    ("@__pyx_str_True", 5, 'c"True\\00"'),
    ("@__pyx_str_False", 6, 'c"False\\00"'),
)

_PRINT_FMT: dict[str, tuple[str, str]] = {
    "int": ("@__pyx_fmt_int_sp", "@__pyx_fmt_int_nl"),
    "float": ("@__pyx_fmt_flt_sp", "@__pyx_fmt_flt_nl"),
    "str": ("@__pyx_fmt_str_sp", "@__pyx_fmt_str_nl"),
    "bool": ("@__pyx_fmt_str_sp", "@__pyx_fmt_str_nl"),
}


def _encode_llvm_string(value: str) -> tuple[str, int]:
    parts: list[str] = []
    for byte in value.encode("utf-8"):
        if 0x20 <= byte <= 0x7E and byte not in (0x22, 0x5C):
            parts.append(chr(byte))
        else:
            parts.append(f"\\{byte:02x}")
    parts.append("\\00")
    return f'c"{"".join(parts)}"', len(value.encode("utf-8")) + 1


def _encode_llvm_bytes(value: bytes) -> tuple[str, int]:
    """Encode a bytes literal as an LLVM byte-array constant (no null terminator)."""
    parts: list[str] = []
    for byte in value:
        if 0x20 <= byte <= 0x7E and byte not in (0x22, 0x5C):
            parts.append(chr(byte))
        else:
            parts.append(f"\\{byte:02x}")
    nbytes = len(value)
    if nbytes == 0:
        # LLVM does not allow zero-length arrays; emit one null byte placeholder.
        parts.append("\\00")
        nbytes = 1
    return f'c"{"".join(parts)}"', nbytes


def _mangle_path(name: str) -> str:
    return name.replace(".", "__")


def _class_type_name(qualified_name: str) -> str:
    return f"%type.{_mangle_path(qualified_name)}"


def llvm_type(py_type: str) -> str:
    normalized = normalize_type_name(py_type)
    if is_union_type(normalized):
        return UNION_LLVM_TYPE
    if normalized in TYPE_MAP:
        return TYPE_MAP[normalized]
    if normalized == "str":
        return STR_LLVM_TYPE
    if normalized == "bytes":
        return BYTES_LLVM_TYPE
    if parse_list_type(normalized) is not None:
        return LIST_LLVM_TYPE
    # Phase 4: cdll handles, function pointers, and opaque Any are all ptr.
    if normalized in {"Any", CDLL_TYPE} or is_cfuncptr_type(normalized):
        return "ptr"
    # File I/O: file handles are opaque FILE* pointers.
    if normalized in FILE_TYPES:
        return "ptr"
    if "." in normalized and "[" not in normalized:
        return _class_type_name(normalized)
    raise CompileError(f"type '{py_type}' cannot be lowered to LLVM IR", code=_ERR_UNSUPPORTED_TYPE)


@dataclass
class CompileError(Exception):
    message: str
    code: str = _ERR_UNSUPPORTED_EXPRESSION
    path: str | Path | None = None
    line: int | None = None
    col: int | None = None

    def __str__(self) -> str:
        if self.line is None:
            return self.message
        return f"line {self.line}:{self.col or 0}: {self.message}"


@dataclass(frozen=True)
class _CallableTarget:
    symbol: str
    arg_types: tuple[str, ...]
    return_type: str


class _ModuleContext:
    def __init__(self, project: ProjectInfo, entry_module: str) -> None:
        self.project = project
        self.entry_module = entry_module
        self.uses_printf = False
        self.uses_malloc = False
        self.uses_realloc = False
        self.uses_memcpy = False
        self.uses_abort = False
        self.uses_utf8_helpers = False
        self.uses_dlopen = False   # Phase 4: ctypes.CDLL
        self.uses_dlsym = False    # Phase 4: fn_t(("sym", lib))
        self.uses_strlen = False   # Phase 4: c_char_p return → bytes
        # File I/O
        self.uses_fopen = False
        self.uses_fclose = False
        self.uses_fwrite = False
        self.uses_file_read_text = False    # emit @__pyx_file_read_text helper
        self.uses_file_read_binary = False  # emit @__pyx_file_read_binary helper
        self.uses_file_readline = False     # emit @__pyx_file_readline helper
        self._str_by_value: dict[str, str] = {}
        self._str_globals: list[tuple[str, str]] = []
        self._bytes_by_value: dict[bytes, str] = {}
        self._bytes_globals: list[tuple[str, bytes]] = []

    def alloc_string(self, value: str) -> str:
        if value not in self._str_by_value:
            name = f"@__pyx_str_{len(self._str_by_value)}"
            self._str_by_value[value] = name
            self._str_globals.append((name, value))
        return self._str_by_value[value]

    def alloc_bytes(self, value: bytes) -> str:
        if value not in self._bytes_by_value:
            name = f"@__pyx_bytes_{len(self._bytes_by_value)}"
            self._bytes_by_value[value] = name
            self._bytes_globals.append((name, value))
        return self._bytes_by_value[value]

    def function_symbol(self, signature: FunctionSignature) -> str:
        if signature.class_name is not None:
            if signature.module_name == self.entry_module:
                return f"@{signature.class_name}__{signature.name}"
            return f"@mod_{_mangle_path(signature.module_name)}__{signature.class_name}__{signature.name}"
        if signature.module_name == self.entry_module:
            return f"@{signature.name}"
        return f"@mod_{_mangle_path(signature.module_name)}__{signature.name}"

    def emit_preamble(self) -> list[str]:
        if self.uses_utf8_helpers:
            self.uses_abort = True
        # File read helpers require malloc, fseek, ftell, fread, fgets, strlen, memcpy.
        if self.uses_file_read_text or self.uses_file_read_binary:
            self.uses_malloc = True
        if self.uses_file_readline:
            self.uses_malloc = True
            self.uses_memcpy = True
        lines = [
            f"{STR_LLVM_TYPE} = type {{ ptr, i64 }}",
            f"{BYTES_LLVM_TYPE} = type {{ ptr, i64 }}",
            f"{LIST_LLVM_TYPE} = type {{ ptr, i64, i64 }}",
        ]
        for module in self.project.modules.values():
            for _, class_info in module.classes.values():
                fields = ", ".join(llvm_type(field_t) for field_t in class_info.field_types)
                lines.append(f"{_class_type_name(class_info.qualified_name)} = type {{ {fields} }}")
        lines.append("")
        if self.uses_printf:
            for gname, nbytes, data in _PRINT_FMT_DEFS:
                lines.append(f"{gname} = private unnamed_addr constant [{nbytes} x i8] {data}")
        for gname, value in self._str_globals:
            literal, nbytes = _encode_llvm_string(value)
            lines.append(f"{gname} = private unnamed_addr constant [{nbytes} x i8] {literal}")
        for gname, value in self._bytes_globals:
            literal, nbytes = _encode_llvm_bytes(value)
            lines.append(f"{gname} = private unnamed_addr constant [{nbytes} x i8] {literal}")
        if self._str_globals or self._bytes_globals:
            lines.append("")
        if self.uses_printf:
            lines.append("declare i32 @printf(ptr, ...)")
        if self.uses_malloc:
            lines.append("declare ptr @malloc(i64)")
        if self.uses_realloc:
            lines.append("declare ptr @realloc(ptr, i64)")
        if self.uses_memcpy:
            lines.append("declare ptr @memcpy(ptr, ptr, i64)")
        if self.uses_abort:
            lines.append("declare void @abort()")
        if self.uses_dlopen:
            lines.append("declare ptr @dlopen(ptr, i32)")
        if self.uses_dlsym:
            lines.append("declare ptr @dlsym(ptr, ptr)")
        if self.uses_fopen:
            lines.append("declare ptr @fopen(ptr, ptr)")
        if self.uses_fclose:
            lines.append("declare i32 @fclose(ptr)")
        if self.uses_fwrite:
            lines.append("declare i64 @fwrite(ptr, i64, i64, ptr)")
        if self.uses_file_read_text or self.uses_file_read_binary:
            lines.append("declare i32 @fseek(ptr, i64, i32)")
            lines.append("declare i64 @ftell(ptr)")
            lines.append("declare i64 @fread(ptr, i64, i64, ptr)")
        if self.uses_file_readline:
            lines.append("declare ptr @fgets(ptr, i32, ptr)")
        if self.uses_file_readline or self.uses_strlen:
            lines.append("declare i64 @strlen(ptr)")
        has_any_decl = (
            self.uses_printf or self.uses_malloc or self.uses_realloc
            or self.uses_memcpy or self.uses_abort or self.uses_dlopen
            or self.uses_dlsym or self.uses_strlen or self.uses_fopen
            or self.uses_fclose or self.uses_fwrite or self.uses_file_read_text
            or self.uses_file_read_binary or self.uses_file_readline
        )
        if has_any_decl:
            lines.append("")
        if self.uses_utf8_helpers:
            lines.extend(self._emit_utf8_helpers())
        if self.uses_file_read_text:
            lines.extend(self._emit_file_read_text_helper())
        if self.uses_file_read_binary:
            lines.extend(self._emit_file_read_binary_helper())
        if self.uses_file_readline:
            lines.extend(self._emit_file_readline_helper())
        return lines

    def _emit_utf8_helpers(self) -> list[str]:
        self.uses_abort = True
        return [
            "define private i64 @__pyx_utf8_char_width(i8 %byte) {",
            "entry:",
            "  %b = zext i8 %byte to i32",
            "  %is_ascii = icmp ult i32 %b, 128",
            "  br i1 %is_ascii, label %ret1, label %check2",
            "check2:",
            "  %is_2 = icmp ult i32 %b, 224",
            "  br i1 %is_2, label %ret2, label %check3",
            "check3:",
            "  %is_3 = icmp ult i32 %b, 240",
            "  br i1 %is_3, label %ret3, label %ret4",
            "ret1:",
            "  ret i64 1",
            "ret2:",
            "  ret i64 2",
            "ret3:",
            "  ret i64 3",
            "ret4:",
            "  ret i64 4",
            "}",
            "",
            "define private i64 @__pyx_utf8_len(ptr %data, i64 %nbytes) {",
            "entry:",
            "  br label %loop",
            "loop:",
            "  %offset = phi i64 [0, %entry], [%next_offset, %step]",
            "  %count = phi i64 [0, %entry], [%next_count, %step]",
            "  %done = icmp eq i64 %offset, %nbytes",
            "  br i1 %done, label %exit, label %step",
            "step:",
            "  %ptr = getelementptr i8, ptr %data, i64 %offset",
            "  %byte = load i8, ptr %ptr",
            "  %width = call i64 @__pyx_utf8_char_width(i8 %byte)",
            "  %next_offset = add i64 %offset, %width",
            "  %next_count = add i64 %count, 1",
            "  br label %loop",
            "exit:",
            "  ret i64 %count",
            "}",
            "",
            f"define private {STR_LLVM_TYPE} @__pyx_utf8_index(ptr %data, i64 %nbytes, i64 %index) {{",
            "entry:",
            "  %negative = icmp slt i64 %index, 0",
            "  br i1 %negative, label %trap, label %loop",
            "loop:",
            "  %offset = phi i64 [0, %entry], [%next_offset, %advance]",
            "  %count = phi i64 [0, %entry], [%next_count, %advance]",
            "  %done = icmp eq i64 %offset, %nbytes",
            "  br i1 %done, label %trap, label %body",
            "body:",
            "  %ptr = getelementptr i8, ptr %data, i64 %offset",
            "  %byte = load i8, ptr %ptr",
            "  %width = call i64 @__pyx_utf8_char_width(i8 %byte)",
            "  %match = icmp eq i64 %count, %index",
            "  br i1 %match, label %ret, label %advance",
            "advance:",
            "  %next_offset = add i64 %offset, %width",
            "  %next_count = add i64 %count, 1",
            "  br label %loop",
            "ret:",
            f"  %str0 = insertvalue {STR_LLVM_TYPE} undef, ptr %ptr, 0",
            f"  %str1 = insertvalue {STR_LLVM_TYPE} %str0, i64 %width, 1",
            f"  ret {STR_LLVM_TYPE} %str1",
            "trap:",
            "  call void @abort()",
            "  unreachable",
            "}",
            "",
        ]


    def _emit_file_read_text_helper(self) -> list[str]:
        """Emit @__pyx_file_read_text(ptr %fp) -> %pyx.str.

        Reads the entire file using fseek/ftell/fread and returns a str value.
        The buffer is heap-allocated and null-terminated.
        """
        return [
            f"define private {STR_LLVM_TYPE} @__pyx_file_read_text(ptr %fp) {{",
            "entry:",
            "  call i32 @fseek(ptr %fp, i64 0, i32 2)",
            "  %size = call i64 @ftell(ptr %fp)",
            "  call i32 @fseek(ptr %fp, i64 0, i32 0)",
            "  %alloc_size = add i64 %size, 1",
            "  %buf = call ptr @malloc(i64 %alloc_size)",
            "  call i64 @fread(ptr %buf, i64 1, i64 %size, ptr %fp)",
            "  %end = getelementptr i8, ptr %buf, i64 %size",
            "  store i8 0, ptr %end",
            f"  %s0 = insertvalue {STR_LLVM_TYPE} undef, ptr %buf, 0",
            f"  %s1 = insertvalue {STR_LLVM_TYPE} %s0, i64 %size, 1",
            f"  ret {STR_LLVM_TYPE} %s1",
            "}",
            "",
        ]

    def _emit_file_read_binary_helper(self) -> list[str]:
        """Emit @__pyx_file_read_binary(ptr %fp) -> %pyx.bytes.

        Reads the entire file and returns a bytes value.
        """
        return [
            f"define private {BYTES_LLVM_TYPE} @__pyx_file_read_binary(ptr %fp) {{",
            "entry:",
            "  call i32 @fseek(ptr %fp, i64 0, i32 2)",
            "  %size = call i64 @ftell(ptr %fp)",
            "  call i32 @fseek(ptr %fp, i64 0, i32 0)",
            "  %buf = call ptr @malloc(i64 %size)",
            "  call i64 @fread(ptr %buf, i64 1, i64 %size, ptr %fp)",
            f"  %b0 = insertvalue {BYTES_LLVM_TYPE} undef, ptr %buf, 0",
            f"  %b1 = insertvalue {BYTES_LLVM_TYPE} %b0, i64 %size, 1",
            f"  ret {BYTES_LLVM_TYPE} %b1",
            "}",
            "",
        ]

    def _emit_file_readline_helper(self) -> list[str]:
        """Emit @__pyx_file_readline(ptr %fp) -> %pyx.str.

        Reads one line (up to 4096 bytes) via fgets and returns a str value.
        Returns an empty str on EOF.
        """
        return [
            f"define private {STR_LLVM_TYPE} @__pyx_file_readline(ptr %fp) {{",
            "entry:",
            "  %buf = call ptr @malloc(i64 4096)",
            "  %result = call ptr @fgets(ptr %buf, i32 4096, ptr %fp)",
            "  %is_eof = icmp eq ptr %result, null",
            "  br i1 %is_eof, label %eof, label %got_line",
            "eof:",
            f"  %se0 = insertvalue {STR_LLVM_TYPE} undef, ptr %buf, 0",
            f"  %se1 = insertvalue {STR_LLVM_TYPE} %se0, i64 0, 1",
            f"  ret {STR_LLVM_TYPE} %se1",
            "got_line:",
            "  %len = call i64 @strlen(ptr %buf)",
            "  %copy_size = add i64 %len, 1",
            "  %heap = call ptr @malloc(i64 %copy_size)",
            "  call ptr @memcpy(ptr %heap, ptr %buf, i64 %copy_size)",
            f"  %sl0 = insertvalue {STR_LLVM_TYPE} undef, ptr %heap, 0",
            f"  %sl1 = insertvalue {STR_LLVM_TYPE} %sl0, i64 %len, 1",
            f"  ret {STR_LLVM_TYPE} %sl1",
            "}",
            "",
        ]


class _FunctionCompiler:
    def __init__(
        self,
        module: ModuleInfo,
        node: ast.FunctionDef,
        signature: FunctionSignature,
        project: ProjectInfo,
        ctx: _ModuleContext,
    ) -> None:
        self.module = module
        self.node = node
        self.signature = signature
        self.project = project
        self.ctx = ctx
        self.entry_lines: list[str] = []
        self.body_lines: list[str] = []
        self.reg_idx = 0
        self.label_idx = 0
        self.slot_types: dict[str, str] = {}

    def compile(self) -> list[str]:
        try:
            arg_sig: list[str] = []
            for arg_name, arg_t in zip(self.signature.arg_names, self.signature.arg_types, strict=True):
                self._ensure_supported_type(arg_t, self.node)
                arg_sig.append(f"{llvm_type(arg_t)} %{arg_name}")
                self._ensure_slot(arg_name, arg_t)
                self.body_lines.append(f"  store {llvm_type(arg_t)} %{arg_name}, ptr %{arg_name}.slot")

            ret_t = self.signature.return_type
            self._ensure_supported_type(ret_t, self.node)
            header = [f"define {llvm_type(ret_t)} {self.ctx.function_symbol(self.signature)}({', '.join(arg_sig)}) {{", "entry:"]
            terminated = self._compile_statements(self.node.body, ret_t)
            if not terminated:
                raise CompileError(
                    f"Function '{self.node.name}' must explicitly return on all paths for LLVM lowering",
                    code=_ERR_MISSING_RETURN,
                    line=self.node.lineno,
                    col=self.node.col_offset,
                )
            return header + self.entry_lines + self.body_lines + ["}"]
        except CompileError as exc:
            if exc.path is None:
                exc.path = self.module.path
            raise

    def _compile_statements(self, body: list[ast.stmt], expected_ret: str) -> bool:
        for stmt in body:
            if isinstance(stmt, ast.Return):
                if stmt.value is None:
                    raise CompileError("void return is not supported", code=_ERR_UNSUPPORTED_STATEMENT, line=stmt.lineno, col=stmt.col_offset)
                value, ty = self._compile_expr(stmt.value)
                coerced, _ = self._coerce_value(value, ty, expected_ret, stmt, "return type mismatch")
                self.body_lines.append(f"  ret {llvm_type(expected_ret)} {coerced}")
                return True

            if isinstance(stmt, ast.Assign):
                if len(stmt.targets) != 1:
                    raise CompileError("only single-target assignment is supported", code=_ERR_UNSUPPORTED_STATEMENT, line=stmt.lineno, col=stmt.col_offset)
                value, ty = self._compile_expr(stmt.value)
                self._assign_target(stmt.targets[0], value, ty, stmt)
                continue

            if isinstance(stmt, ast.AnnAssign):
                if not isinstance(stmt.target, ast.Name):
                    raise CompileError("only simple annotated assignment is supported", code=_ERR_UNSUPPORTED_STATEMENT, line=stmt.lineno, col=stmt.col_offset)
                annotated = self._render_annotation(stmt.annotation)
                self._ensure_supported_type(annotated, stmt)
                self._ensure_slot(stmt.target.id, annotated)
                if stmt.value is not None:
                    value, ty = self._compile_expr(stmt.value)
                    coerced, _ = self._coerce_value(value, ty, annotated, stmt, f"assignment type mismatch for '{stmt.target.id}'")
                    self.body_lines.append(f"  store {llvm_type(annotated)} {coerced}, ptr %{stmt.target.id}.slot")
                continue

            if isinstance(stmt, ast.AnnAssign):
                if not isinstance(stmt.target, ast.Name):
                    raise CompileError(
                        "only simple name annotation is supported",
                        code=_ERR_UNSUPPORTED_STATEMENT,
                        line=stmt.lineno,
                        col=stmt.col_offset,
                    )
                name = stmt.target.id
                ann_ty = self._annotation_to_type(stmt.annotation, stmt, f"annotated variable '{name}'")
                if stmt.value is not None:
                    value, value_ty = self._compile_expr(stmt.value)
                    if name not in self.slot_types:
                        self._ensure_slot(name, ann_ty)
                    coerced, _ = self._coerce_value(value, value_ty, self.slot_types[name], stmt, f"annotated assignment type mismatch for '{name}'")
                    self.body_lines.append(f"  store {llvm_type(self.slot_types[name])} {coerced}, ptr %{name}.slot")
                else:
                    self._ensure_slot(name, ann_ty)
                continue

            if isinstance(stmt, ast.If):
                cond, cond_t = self._compile_expr(stmt.test)
                self._require_assignable(cond_t, "bool", stmt, "if condition must be bool")
                then_label = self._new_label("then")
                else_label = self._new_label("else")
                end_label = self._new_label("endif")
                self.body_lines.append(f"  br i1 {cond}, label %{then_label}, label %{else_label}")
                self.body_lines.append(f"{then_label}:")
                then_terminated = self._compile_statements(stmt.body, expected_ret)
                if not then_terminated:
                    self.body_lines.append(f"  br label %{end_label}")
                self.body_lines.append(f"{else_label}:")
                else_terminated = self._compile_statements(stmt.orelse, expected_ret)
                if not else_terminated:
                    self.body_lines.append(f"  br label %{end_label}")
                if then_terminated and else_terminated:
                    return True
                self.body_lines.append(f"{end_label}:")
                continue

            if isinstance(stmt, ast.While):
                cond_label = self._new_label("while_cond")
                body_label = self._new_label("while_body")
                end_label = self._new_label("while_end")
                self.body_lines.append(f"  br label %{cond_label}")
                self.body_lines.append(f"{cond_label}:")
                cond, cond_t = self._compile_expr(stmt.test)
                self._require_assignable(cond_t, "bool", stmt, "while condition must be bool")
                self.body_lines.append(f"  br i1 {cond}, label %{body_label}, label %{end_label}")
                self.body_lines.append(f"{body_label}:")
                body_terminated = self._compile_statements(stmt.body, expected_ret)
                if not body_terminated:
                    self.body_lines.append(f"  br label %{cond_label}")
                self.body_lines.append(f"{end_label}:")
                continue

            if isinstance(stmt, ast.With):
                body_terminated = self._compile_with_stmt(stmt, expected_ret)
                if body_terminated:
                    return True
                continue

            if isinstance(stmt, ast.Expr):
                if isinstance(stmt.value, ast.Call) and isinstance(stmt.value.func, ast.Name) and stmt.value.func.id == "print":
                    self._compile_print(stmt.value)
                    continue
                if isinstance(stmt.value, ast.Call) and self._is_list_append_call(stmt.value):
                    self._compile_list_append(stmt.value)
                    continue
                if isinstance(stmt.value, ast.Call):
                    self._compile_expr(stmt.value)
                    continue
                raise CompileError("only calls are supported as expression statements", code=_ERR_UNSUPPORTED_STATEMENT, line=stmt.lineno, col=stmt.col_offset)

            raise CompileError(
                f"unsupported statement {stmt.__class__.__name__} in LLVM mode",
                code=_ERR_UNSUPPORTED_STATEMENT,
                line=stmt.lineno,
                col=stmt.col_offset,
            )
        return False

    def _assign_target(self, target: ast.expr, value: str, ty: str, node: ast.AST) -> None:
        if isinstance(target, ast.Name):
            if target.id not in self.slot_types:
                self._ensure_slot(target.id, ty)
            coerced, _ = self._coerce_value(value, ty, self.slot_types[target.id], node, f"assignment type mismatch for '{target.id}'")
            self.body_lines.append(f"  store {llvm_type(self.slot_types[target.id])} {coerced}, ptr %{target.id}.slot")
            return

        if isinstance(target, ast.Attribute):
            if not isinstance(target.value, ast.Name):
                raise CompileError("field assignment requires a named object", code=_ERR_UNSUPPORTED_STATEMENT, line=node.lineno, col=node.col_offset)
            owner_name = target.value.id
            if owner_name not in self.slot_types:
                raise CompileError(f"unknown variable '{owner_name}'", code=_ERR_UNKNOWN_VARIABLE, line=target.lineno, col=target.col_offset)
            owner_t = self.slot_types[owner_name]
            class_info = self.project.lookup_class(owner_t)
            if class_info is None:
                raise CompileError(f"type '{owner_t}' has no fields", code=_ERR_UNKNOWN_FIELD, line=target.lineno, col=target.col_offset)
            field_index = self._class_field_index(class_info, target.attr, target)
            field_t = class_info.field_types[field_index]
            coerced, _ = self._coerce_value(value, ty, field_t, node, f"field assignment type mismatch for '{target.attr}'")
            current = self._new_reg()
            updated = self._new_reg()
            self.body_lines.append(f"  {current} = load {llvm_type(owner_t)}, ptr %{owner_name}.slot")
            self.body_lines.append(f"  {updated} = insertvalue {llvm_type(owner_t)} {current}, {llvm_type(field_t)} {coerced}, {field_index}")
            self.body_lines.append(f"  store {llvm_type(owner_t)} {updated}, ptr %{owner_name}.slot")
            return

        if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name):
            list_name = target.value.id
            list_t = self.slot_types.get(list_name)
            item_t = None if list_t is None else parse_list_type(list_t)
            if item_t is not None:
                list_val = self._new_reg()
                self.body_lines.append(f"  {list_val} = load {LIST_LLVM_TYPE}, ptr %{list_name}.slot")
                data_ptr, length, _ = self._extract_list_parts(list_val)
                index, index_t = self._compile_expr(target.slice)
                self._require_assignable(index_t, "int", target, "list assignment index must be int")
                self._emit_bounds_check(index, length, "list_index")
                coerced, _ = self._coerce_value(value, ty, item_t, node, "list assignment type mismatch")
                elem_ptr = self._new_reg()
                self.body_lines.append(f"  {elem_ptr} = getelementptr {llvm_type(item_t)}, ptr {data_ptr}, i64 {index}")
                self.body_lines.append(f"  store {llvm_type(item_t)} {coerced}, ptr {elem_ptr}")
                return

        raise CompileError("unsupported assignment target", code=_ERR_UNSUPPORTED_STATEMENT, line=node.lineno, col=node.col_offset)

    def _compile_print(self, node: ast.Call) -> None:
        self.ctx.uses_printf = True
        if not node.args:
            reg = self._new_reg()
            self.body_lines.append(f"  {reg} = call i32 (ptr, ...) @printf(ptr @__pyx_fmt_nl)")
            return

        for i, arg_node in enumerate(node.args):
            is_last = i == len(node.args) - 1
            val, ty = self._compile_expr(arg_node)
            if ty not in _PRINT_FMT:
                raise CompileError(f"print() argument has unsupported type '{ty}'", code=_ERR_PRINT_USAGE, line=getattr(arg_node, "lineno", None), col=getattr(arg_node, "col_offset", None))
            fmt_sp, fmt_nl = _PRINT_FMT[ty]
            fmt = fmt_nl if is_last else fmt_sp
            reg = self._new_reg()

            if ty == "bool":
                str_reg = self._new_reg()
                len_reg = self._new_reg()
                self.body_lines.append(f"  {str_reg} = select i1 {val}, ptr @__pyx_str_True, ptr @__pyx_str_False")
                self.body_lines.append(f"  {len_reg} = select i1 {val}, i32 4, i32 5")
                self.body_lines.append(f"  {reg} = call i32 (ptr, ...) @printf(ptr {fmt}, i32 {len_reg}, ptr {str_reg})")
            elif ty == "int":
                self.body_lines.append(f"  {reg} = call i32 (ptr, ...) @printf(ptr {fmt}, i64 {val})")
            elif ty == "float":
                self.body_lines.append(f"  {reg} = call i32 (ptr, ...) @printf(ptr {fmt}, double {val})")
            else:
                data_ptr, length = self._extract_str_parts(val)
                len_i32 = self._new_reg()
                self.body_lines.append(f"  {len_i32} = trunc i64 {length} to i32")
                self.body_lines.append(f"  {reg} = call i32 (ptr, ...) @printf(ptr {fmt}, i32 {len_i32}, ptr {data_ptr})")

    def _compile_expr(self, node: ast.AST) -> tuple[str, str]:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool):
                return ("1" if node.value else "0", "bool")
            if isinstance(node.value, int):
                return (str(node.value), "int")
            if isinstance(node.value, float):
                return (str(node.value), "float")
            if isinstance(node.value, str):
                return self._compile_string_literal(node.value), "str"
            if isinstance(node.value, bytes):
                gname = self.ctx.alloc_bytes(node.value)
                return self._build_bytes_value(gname, str(len(node.value))), "bytes"

        if isinstance(node, ast.Name):
            if node.id not in self.slot_types:
                raise CompileError(f"unknown variable '{node.id}'", code=_ERR_UNKNOWN_VARIABLE, line=node.lineno, col=node.col_offset)
            ty = self.slot_types[node.id]
            reg = self._new_reg()
            self.body_lines.append(f"  {reg} = load {llvm_type(ty)}, ptr %{node.id}.slot")
            return reg, ty

        if isinstance(node, ast.UnaryOp):
            operand, ty = self._compile_expr(node.operand)
            if isinstance(node.op, ast.Not):
                self._require_assignable(ty, "bool", node, "unary 'not' requires bool operand")
                reg = self._new_reg()
                self.body_lines.append(f"  {reg} = xor i1 {operand}, 1")
                return reg, "bool"
            if isinstance(node.op, ast.USub):
                if ty == "int":
                    reg = self._new_reg()
                    self.body_lines.append(f"  {reg} = sub i64 0, {operand}")
                    return reg, "int"
                if ty == "float":
                    reg = self._new_reg()
                    self.body_lines.append(f"  {reg} = fneg double {operand}")
                    return reg, "float"
            raise CompileError(
                f"unsupported unary operator {node.op.__class__.__name__}",
                code=_ERR_UNSUPPORTED_EXPRESSION,
                line=getattr(node, "lineno", None),
                col=getattr(node, "col_offset", None),
            )

        if isinstance(node, ast.List):
            return self._compile_list_literal(node)

        if isinstance(node, ast.Set):
            raise CompileError(
                "type 'set[T]' is planned but not lowered in LLVM mode yet",
                code=_ERR_UNSUPPORTED_TYPE,
                line=node.lineno,
                col=node.col_offset,
            )

        if isinstance(node, ast.Dict):
            raise CompileError(
                "type 'dict[K,V]' is planned but not lowered in LLVM mode yet",
                code=_ERR_UNSUPPORTED_TYPE,
                line=node.lineno,
                col=node.col_offset,
            )

        if isinstance(node, ast.Attribute):
            owner, owner_t = self._compile_expr(node.value)
            class_info = self.project.lookup_class(owner_t)
            if class_info is None:
                raise CompileError(f"type '{owner_t}' has no attribute '{node.attr}'", code=_ERR_UNKNOWN_FIELD, line=node.lineno, col=node.col_offset)
            field_index = self._class_field_index(class_info, node.attr, node)
            field_t = class_info.field_types[field_index]
            reg = self._new_reg()
            self.body_lines.append(f"  {reg} = extractvalue {llvm_type(owner_t)} {owner}, {field_index}")
            return reg, field_t

        if isinstance(node, ast.Subscript):
            return self._compile_subscript(node)

        if isinstance(node, ast.BinOp):
            left, left_t = self._compile_expr(node.left)
            right, right_t = self._compile_expr(node.right)
            if isinstance(node.op, ast.Add) and left_t == right_t == "str":
                return self._compile_string_concat(left, right), "str"
            if not is_numeric_type(left_t) or not is_numeric_type(right_t):
                raise CompileError("arithmetic only supports int/float, int | float, and str concatenation", code=_ERR_TYPE_MISMATCH, line=node.lineno, col=node.col_offset)
            if isinstance(node.op, ast.Add):
                return self._compile_numeric_binop(left, left_t, right, right_t, node, "add", "fadd")
            if isinstance(node.op, ast.Sub):
                return self._compile_numeric_binop(left, left_t, right, right_t, node, "sub", "fsub")
            if isinstance(node.op, ast.Mult):
                return self._compile_numeric_binop(left, left_t, right, right_t, node, "mul", "fmul")
            raise CompileError("only +, -, * are supported in LLVM mode", code=_ERR_UNSUPPORTED_EXPRESSION, line=node.lineno, col=node.col_offset)

        if isinstance(node, ast.Compare) and len(node.ops) == 1 and len(node.comparators) == 1:
            left, left_t = self._compile_expr(node.left)
            right, right_t = self._compile_expr(node.comparators[0])
            op = node.ops[0]

            if left_t == "bool" and right_t == "bool" and isinstance(op, (ast.Eq, ast.NotEq)):
                pred = "eq" if isinstance(op, ast.Eq) else "ne"
                reg = self._new_reg()
                self.body_lines.append(f"  {reg} = icmp {pred} i1 {left}, {right}")
                return reg, "bool"
            if left_t == right_t == "str" and isinstance(op, (ast.Eq, ast.NotEq)):
                raise CompileError(
                    "string comparison is planned but not lowered in LLVM mode yet",
                    code=_ERR_UNSUPPORTED_TYPE,
                    line=node.lineno,
                    col=node.col_offset,
                )
            if is_numeric_type(left_t) and is_numeric_type(right_t):
                return self._compile_numeric_compare(left, left_t, right, right_t, node, op), "bool"
            raise CompileError("comparison type not supported", code=_ERR_TYPE_MISMATCH, line=node.lineno, col=node.col_offset)

        if isinstance(node, ast.Call):
            return self._compile_call(node)

        raise CompileError(f"unsupported expression {node.__class__.__name__} in LLVM mode", code=_ERR_UNSUPPORTED_EXPRESSION, line=getattr(node, "lineno", None), col=getattr(node, "col_offset", None))

    def _compile_call(self, node: ast.Call) -> tuple[str, str]:
        if isinstance(node.func, ast.Name) and node.func.id == "print":
            raise CompileError("'print()' is a statement; its return value cannot be used in an expression", code=_ERR_PRINT_USAGE, line=getattr(node, "lineno", None), col=getattr(node, "col_offset", None))

        if isinstance(node.func, ast.Name) and node.func.id == "len":
            if len(node.args) != 1:
                raise CompileError("len() expects exactly one argument", code=_ERR_CALL_ARG_COUNT, line=node.lineno, col=node.col_offset)
            value, value_t = self._compile_expr(node.args[0])
            if value_t == "str":
                self.ctx.uses_utf8_helpers = True
                data_ptr, byte_length = self._extract_str_parts(value)
                reg = self._new_reg()
                self.body_lines.append(f"  {reg} = call i64 @__pyx_utf8_len(ptr {data_ptr}, i64 {byte_length})")
                return reg, "int"
            if value_t == "bytes":
                _, byte_length = self._extract_bytes_parts(value)
                return byte_length, "int"
            if parse_list_type(value_t) is not None:
                _, length, _ = self._extract_list_parts(value)
                return length, "int"
            raise CompileError(f"len() does not support '{value_t}' in LLVM mode", code=_ERR_UNSUPPORTED_EXPRESSION, line=node.lineno, col=node.col_offset)

        if isinstance(node.func, ast.Name) and node.func.id == "open":
            return self._compile_open(node)

        if self._is_list_append_call(node):
            raise CompileError("list.append() is a statement in LLVM mode", code=_ERR_UNSUPPORTED_EXPRESSION, line=node.lineno, col=node.col_offset)

        # ------------------------------------------------------------------
        # Phase 4: ctypes FFI patterns — must be checked before the generic
        # imported-symbol constructor path (which asserts on project modules).
        # ------------------------------------------------------------------
        if self._is_ctypes_call(node.func, "CDLL"):
            return self._compile_cdll(node)
        if self._is_ctypes_call(node.func, "CFUNCTYPE"):
            return self._compile_cfunctype(node)
        if self._is_ctypes_call(node.func, "string_at"):
            return self._compile_string_at(node)
        if self._is_cfuncptr_binding(node):
            return self._compile_cfuncptr_binding(node)
        if self._is_cfuncptr_call(node):
            return self._compile_cfuncptr_call(node)
        # ------------------------------------------------------------------

        if isinstance(node.func, ast.Name) and node.func.id in self.module.classes:
            return self._compile_constructor(self.module.classes[node.func.id][1], node.args, node)

        if isinstance(node.func, ast.Name):
            imported = self.module.imported_symbols.get(node.func.id)
            if imported is not None and imported.module_name != "ctypes":
                target_module = self.project.lookup_module(imported.module_name)
                assert target_module is not None
                if imported.symbol_name in target_module.classes:
                    return self._compile_constructor(target_module.classes[imported.symbol_name][1], node.args, node)

        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id in self.module.imported_modules:
            imported_module = self.module.imported_modules[node.func.value.id]
            if imported_module.module_name == "ctypes":
                raise CompileError(
                    f"Unsupported ctypes call pattern '{ast.unparse(node.func)}'",
                    code=_ERR_UNSUPPORTED_EXPRESSION,
                    line=node.lineno,
                    col=node.col_offset,
                )
            target_module = self.project.lookup_module(imported_module.module_name)
            assert target_module is not None
            if node.func.attr in target_module.classes:
                return self._compile_constructor(target_module.classes[node.func.attr][1], node.args, node)

        if isinstance(node.func, ast.Attribute):
            owner_val, owner_t = self._compile_expr(node.func.value)

            # File method dispatch
            if owner_t in FILE_TYPES:
                return self._compile_file_method(owner_val, owner_t, node.func.attr, node)

            class_info = self.project.lookup_class(owner_t)
            if class_info is not None and node.func.attr in class_info.methods:
                signature = class_info.methods[node.func.attr]
                compiled_args = [f"{llvm_type(owner_t)} {owner_val}"]
                if len(node.args) != len(signature.arg_types) - 1:
                    raise CompileError(f"call arg count mismatch for '{node.func.attr}'", code=_ERR_CALL_ARG_COUNT, line=node.lineno, col=node.col_offset)
                for arg_node, expected_t in zip(node.args, signature.arg_types[1:], strict=True):
                    val, got_t = self._compile_expr(arg_node)
                    coerced, _ = self._coerce_value(val, got_t, expected_t, arg_node, f"call arg type mismatch for '{node.func.attr}'")
                    compiled_args.append(f"{llvm_type(expected_t)} {coerced}")
                reg = self._new_reg()
                self.body_lines.append(f"  {reg} = call {llvm_type(signature.return_type)} {self.ctx.function_symbol(signature)}({', '.join(compiled_args)})")
                return reg, signature.return_type

        target = self._resolve_callable(node.func)
        if target is None:
            raise CompileError(f"call target '{ast.unparse(node.func)}' is not a known function", code=_ERR_UNKNOWN_FUNCTION, line=node.lineno, col=node.col_offset)
        if len(target.arg_types) != len(node.args):
            raise CompileError(f"call arg count mismatch for '{ast.unparse(node.func)}'", code=_ERR_CALL_ARG_COUNT, line=node.lineno, col=node.col_offset)
        compiled_args: list[str] = []
        for arg_node, expected_t in zip(node.args, target.arg_types, strict=True):
            val, got_t = self._compile_expr(arg_node)
            coerced, _ = self._coerce_value(val, got_t, expected_t, arg_node, f"call arg type mismatch for '{ast.unparse(node.func)}'")
            compiled_args.append(f"{llvm_type(expected_t)} {coerced}")
        reg = self._new_reg()
        self.body_lines.append(f"  {reg} = call {llvm_type(target.return_type)} {target.symbol}({', '.join(compiled_args)})")
        return reg, target.return_type

    def _compile_constructor(self, class_info: ClassInfo, args: list[ast.expr], node: ast.AST) -> tuple[str, str]:
        if len(args) != len(class_info.field_types):
            raise CompileError(f"constructor '{class_info.name}' expects {len(class_info.field_types)} arguments, got {len(args)}", code=_ERR_CALL_ARG_COUNT, line=node.lineno, col=node.col_offset)
        struct_value = "undef"
        struct_ty = llvm_type(class_info.qualified_name)
        for index, (arg_node, expected_t) in enumerate(zip(args, class_info.field_types, strict=True)):
            val, got_t = self._compile_expr(arg_node)
            coerced, _ = self._coerce_value(val, got_t, expected_t, arg_node, f"constructor type mismatch for field {class_info.field_names[index]}")
            next_reg = self._new_reg()
            self.body_lines.append(f"  {next_reg} = insertvalue {struct_ty} {struct_value}, {llvm_type(expected_t)} {coerced}, {index}")
            struct_value = next_reg
        return struct_value, class_info.qualified_name

    def _compile_subscript(self, node: ast.Subscript) -> tuple[str, str]:
        container, container_t = self._compile_expr(node.value)
        index, index_t = self._compile_expr(node.slice)
        self._require_assignable(index_t, "int", node, "index must be int")

        item_t = parse_list_type(container_t)
        if item_t is not None:
            data_ptr, length, _ = self._extract_list_parts(container)
            self._emit_bounds_check(index, length, "list_index")
            elem_ptr = self._new_reg()
            elem_val = self._new_reg()
            self.body_lines.append(f"  {elem_ptr} = getelementptr {llvm_type(item_t)}, ptr {data_ptr}, i64 {index}")
            self.body_lines.append(f"  {elem_val} = load {llvm_type(item_t)}, ptr {elem_ptr}")
            return elem_val, item_t

        if container_t == "str":
            self.ctx.uses_utf8_helpers = True
            data_ptr, byte_length = self._extract_str_parts(container)
            reg = self._new_reg()
            self.body_lines.append(f"  {reg} = call {STR_LLVM_TYPE} @__pyx_utf8_index(ptr {data_ptr}, i64 {byte_length}, i64 {index})")
            return reg, "str"

        raise CompileError(
            f"subscript is not supported for '{container_t}' in LLVM mode",
            code=_ERR_UNSUPPORTED_EXPRESSION,
            line=node.lineno,
            col=node.col_offset,
        )

    def _compile_list_literal(self, node: ast.List) -> tuple[str, str]:
        if not node.elts:
            return self._build_list_value("null", "0", "0"), "list[Any]"
        # Single pass: compile all elements first, then determine item type.
        compiled: list[tuple[str, str]] = [self._compile_expr(elt) for elt in node.elts]
        item_t = compiled[0][1]
        for _, t in compiled[1:]:
            item_t = self._merge_item_type(item_t, t)
        if item_t not in {"int", "float", "bool", "str"}:
            raise CompileError(f"list element type '{item_t}' is not supported in LLVM mode", code=_ERR_UNSUPPORTED_TYPE, line=node.lineno, col=node.col_offset)
        self.ctx.uses_malloc = True
        count = len(node.elts)
        alloc_reg = self._new_reg()
        self.body_lines.append(f"  {alloc_reg} = call ptr @malloc(i64 {count * self._element_size(item_t)})")
        for index, (value, got_t) in enumerate(compiled):
            coerced, _ = self._coerce_value(value, got_t, item_t, node.elts[index], "list literal element type mismatch")
            elem_ptr = self._new_reg()
            self.body_lines.append(f"  {elem_ptr} = getelementptr {llvm_type(item_t)}, ptr {alloc_reg}, i64 {index}")
            self.body_lines.append(f"  store {llvm_type(item_t)} {coerced}, ptr {elem_ptr}")
        return self._build_list_value(alloc_reg, str(count), str(count)), f"list[{item_t}]"

    def _compile_list_append(self, node: ast.Call) -> None:
        if not isinstance(node.func, ast.Attribute) or not isinstance(node.func.value, ast.Name):
            raise CompileError("list.append() requires a named list variable", code=_ERR_UNSUPPORTED_STATEMENT, line=node.lineno, col=node.col_offset)
        list_name = node.func.value.id
        if list_name not in self.slot_types:
            raise CompileError(f"unknown variable '{list_name}'", code=_ERR_UNKNOWN_VARIABLE, line=node.lineno, col=node.col_offset)
        list_t = self.slot_types[list_name]
        item_t = parse_list_type(list_t)
        if item_t is None:
            raise CompileError(f"'{list_name}' is not a list", code=_ERR_TYPE_MISMATCH, line=node.lineno, col=node.col_offset)
        if item_t not in {"int", "float", "bool", "str"}:
            raise CompileError(f"list element type '{item_t}' is not supported in LLVM mode", code=_ERR_UNSUPPORTED_TYPE, line=node.lineno, col=node.col_offset)
        if len(node.args) != 1:
            raise CompileError("list.append() expects one argument", code=_ERR_CALL_ARG_COUNT, line=node.lineno, col=node.col_offset)

        self.ctx.uses_realloc = True
        list_val = self._new_reg()
        self.body_lines.append(f"  {list_val} = load {LIST_LLVM_TYPE}, ptr %{list_name}.slot")
        data_ptr, length, capacity = self._extract_list_parts(list_val)
        arg_val, arg_t = self._compile_expr(node.args[0])
        coerced, _ = self._coerce_value(arg_val, arg_t, item_t, node.args[0], "list.append() type mismatch")

        full_reg = self._new_reg()
        grow_label = self._new_label("list_grow")
        keep_label = self._new_label("list_keep")
        merge_label = self._new_label("list_merge")
        self.body_lines.append(f"  {full_reg} = icmp eq i64 {length}, {capacity}")
        self.body_lines.append(f"  br i1 {full_reg}, label %{grow_label}, label %{keep_label}")

        self.body_lines.append(f"{grow_label}:")
        zero_cap = self._new_reg()
        doubled_cap = self._new_reg()
        new_cap = self._new_reg()
        byte_count = self._new_reg()
        grown_ptr = self._new_reg()
        self.body_lines.append(f"  {zero_cap} = icmp eq i64 {capacity}, 0")
        self.body_lines.append(f"  {doubled_cap} = mul i64 {capacity}, 2")
        self.body_lines.append(f"  {new_cap} = select i1 {zero_cap}, i64 4, i64 {doubled_cap}")
        self.body_lines.append(f"  {byte_count} = mul i64 {new_cap}, {self._element_size(item_t)}")
        self.body_lines.append(f"  {grown_ptr} = call ptr @realloc(ptr {data_ptr}, i64 {byte_count})")
        self.body_lines.append(f"  br label %{merge_label}")

        self.body_lines.append(f"{keep_label}:")
        self.body_lines.append(f"  br label %{merge_label}")

        self.body_lines.append(f"{merge_label}:")
        data_phi = self._new_reg()
        cap_phi = self._new_reg()
        elem_ptr = self._new_reg()
        new_len = self._new_reg()
        self.body_lines.append(f"  {data_phi} = phi ptr [{grown_ptr}, %{grow_label}], [{data_ptr}, %{keep_label}]")
        self.body_lines.append(f"  {cap_phi} = phi i64 [{new_cap}, %{grow_label}], [{capacity}, %{keep_label}]")
        self.body_lines.append(f"  {elem_ptr} = getelementptr {llvm_type(item_t)}, ptr {data_phi}, i64 {length}")
        self.body_lines.append(f"  store {llvm_type(item_t)} {coerced}, ptr {elem_ptr}")
        self.body_lines.append(f"  {new_len} = add i64 {length}, 1")
        new_list = self._build_list_value(data_phi, new_len, cap_phi)
        self.body_lines.append(f"  store {LIST_LLVM_TYPE} {new_list}, ptr %{list_name}.slot")

    # ------------------------------------------------------------------
    # File I/O helpers
    # ------------------------------------------------------------------

    def _compile_with_stmt(self, stmt: ast.With, expected_ret: str) -> bool:
        """Compile ``with open(...) as f:`` — returns True if body terminated."""
        if len(stmt.items) != 1:
            raise CompileError(
                "only single-item with statements are supported",
                code=_ERR_UNSUPPORTED_STATEMENT,
                line=stmt.lineno,
                col=stmt.col_offset,
            )
        item = stmt.items[0]
        if not (isinstance(item.context_expr, ast.Call)
                and isinstance(item.context_expr.func, ast.Name)
                and item.context_expr.func.id == "open"):
            raise CompileError(
                "with statement is only supported for open()",
                code=_ERR_UNSUPPORTED_STATEMENT,
                line=stmt.lineno,
                col=stmt.col_offset,
            )
        fp_val, file_t = self._compile_expr(item.context_expr)
        var_name: str | None = None
        if item.optional_vars is not None and isinstance(item.optional_vars, ast.Name):
            var_name = item.optional_vars.id
            self._ensure_slot(var_name, file_t)
            self.body_lines.append(f"  store ptr {fp_val}, ptr %{var_name}.slot")
        body_terminated = self._compile_statements(stmt.body, expected_ret)
        if not body_terminated:
            # Emit implicit close on normal exit.
            self.ctx.uses_fclose = True
            if var_name is not None:
                fp_reg = self._new_reg()
                self.body_lines.append(f"  {fp_reg} = load ptr, ptr %{var_name}.slot")
                self.body_lines.append(f"  call i32 @fclose(ptr {fp_reg})")
            else:
                self.body_lines.append(f"  call i32 @fclose(ptr {fp_val})")
        return body_terminated

    def _compile_open(self, node: ast.Call) -> tuple[str, str]:
        """Compile ``open(filename, mode)`` → ``call ptr @fopen(...)``."""
        self.ctx.uses_fopen = True
        if not node.args:
            raise CompileError(
                "open() requires at least a filename argument",
                code=_ERR_CALL_ARG_COUNT,
                line=node.lineno,
                col=node.col_offset,
            )
        fname_val, fname_t = self._compile_expr(node.args[0])
        if fname_t != "str":
            raise CompileError(
                f"open() filename must be str, got '{fname_t}'",
                code=_ERR_TYPE_MISMATCH,
                line=node.lineno,
                col=node.col_offset,
            )
        fname_ptr, _ = self._extract_str_parts(fname_val)
        mode = "r"
        if len(node.args) >= 2:
            if not isinstance(node.args[1], ast.Constant) or not isinstance(node.args[1].value, str):
                raise CompileError(
                    "open() mode must be a string literal",
                    code=_ERR_UNSUPPORTED_EXPRESSION,
                    line=node.lineno,
                    col=node.col_offset,
                )
            mode = node.args[1].value
        mode_gname = self.ctx.alloc_string(mode)
        reg = self._new_reg()
        self.body_lines.append(f"  {reg} = call ptr @fopen(ptr {fname_ptr}, ptr {mode_gname})")
        file_t = BIN_FILE_TYPE if "b" in mode else TEXT_FILE_TYPE
        return reg, file_t

    def _compile_file_method(
        self,
        fp: str,
        file_t: str,
        attr: str,
        node: ast.Call,
    ) -> tuple[str, str]:
        """Dispatch a method call on a TextFile or BinaryFile value."""
        if attr == "close":
            self.ctx.uses_fclose = True
            self.body_lines.append(f"  call i32 @fclose(ptr {fp})")
            return "0", "None"

        if attr == "read":
            if file_t == TEXT_FILE_TYPE:
                self.ctx.uses_file_read_text = True
                reg = self._new_reg()
                self.body_lines.append(
                    f"  {reg} = call {STR_LLVM_TYPE} @__pyx_file_read_text(ptr {fp})"
                )
                return reg, "str"
            else:
                self.ctx.uses_file_read_binary = True
                reg = self._new_reg()
                self.body_lines.append(
                    f"  {reg} = call {BYTES_LLVM_TYPE} @__pyx_file_read_binary(ptr {fp})"
                )
                return reg, "bytes"

        if attr == "readline":
            if file_t != TEXT_FILE_TYPE:
                raise CompileError(
                    "readline() is only supported on text-mode files",
                    code=_ERR_UNSUPPORTED_EXPRESSION,
                    line=node.lineno,
                    col=node.col_offset,
                )
            self.ctx.uses_file_readline = True
            reg = self._new_reg()
            self.body_lines.append(
                f"  {reg} = call {STR_LLVM_TYPE} @__pyx_file_readline(ptr {fp})"
            )
            return reg, "str"

        if attr == "write":
            if len(node.args) != 1:
                raise CompileError(
                    "write() expects exactly one argument",
                    code=_ERR_CALL_ARG_COUNT,
                    line=node.lineno,
                    col=node.col_offset,
                )
            self.ctx.uses_fwrite = True
            arg_val, arg_t = self._compile_expr(node.args[0])
            if file_t == TEXT_FILE_TYPE:
                if arg_t != "str":
                    raise CompileError(
                        f"TextFile.write() expects str, got '{arg_t}'",
                        code=_ERR_TYPE_MISMATCH,
                        line=node.lineno,
                        col=node.col_offset,
                    )
                data_ptr, data_len = self._extract_str_parts(arg_val)
            else:
                if arg_t != "bytes":
                    raise CompileError(
                        f"BinaryFile.write() expects bytes, got '{arg_t}'",
                        code=_ERR_TYPE_MISMATCH,
                        line=node.lineno,
                        col=node.col_offset,
                    )
                data_ptr, data_len = self._extract_bytes_parts(arg_val)
            reg = self._new_reg()
            self.body_lines.append(
                f"  {reg} = call i64 @fwrite(ptr {data_ptr}, i64 1, i64 {data_len}, ptr {fp})"
            )
            return reg, "int"

        raise CompileError(
            f"unknown file method '{attr}'",
            code=_ERR_UNKNOWN_FUNCTION,
            line=node.lineno,
            col=node.col_offset,
        )

    # ------------------------------------------------------------------
    # Phase 4: ctypes / C ABI FFI helpers
    # ------------------------------------------------------------------

    def _is_ctypes_call(self, func: ast.AST, attr: str) -> bool:
        """Return True if *func* refers to ``ctypes.<attr>`` or an imported alias."""
        if isinstance(func, ast.Attribute) and func.attr == attr and isinstance(func.value, ast.Name):
            mod = self.module.imported_modules.get(func.value.id)
            if mod is not None and mod.module_name == "ctypes":
                return True
        if isinstance(func, ast.Name):
            sym = self.module.imported_symbols.get(func.id)
            if sym is not None and sym.module_name == "ctypes" and sym.symbol_name == attr:
                return True
        return False

    def _resolve_ctypes_type_name(self, node: ast.AST) -> str | None:
        """Extract a ctypes type name from an AST expression node.

        Handles ``ctypes.c_int``, ``c_int`` (after ``from ctypes import``),
        ``ctypes.POINTER(T)`` (→ ``"c_void_p"``), and ``None`` (void return).
        """
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.attr in CTYPES_ALL_TYPES:
                mod = self.module.imported_modules.get(node.value.id)
                if mod is not None and mod.module_name == "ctypes":
                    return node.attr
        if isinstance(node, ast.Name):
            sym = self.module.imported_symbols.get(node.id)
            if sym is not None and sym.module_name == "ctypes" and sym.symbol_name in CTYPES_ALL_TYPES:
                return sym.symbol_name
        if isinstance(node, ast.Constant) and node.value is None:
            return "None"
        # POINTER(T) composite type — all pointers are opaque ptr in LLVM IR.
        if isinstance(node, ast.Call) and self._is_ctypes_call(node.func, "POINTER"):
            return "c_void_p"
        return None

    def _is_cfuncptr_binding(self, node: ast.Call) -> bool:
        """Return True for the ``fn_type_var(("sym_name", lib_var))`` pattern."""
        if not isinstance(node.func, ast.Name):
            return False
        func_t = self.slot_types.get(node.func.id)
        if func_t is None or not is_cfuncptr_type(func_t):
            return False
        if len(node.args) != 1 or not isinstance(node.args[0], ast.Tuple):
            return False
        tup = node.args[0]
        return (len(tup.elts) == 2
                and isinstance(tup.elts[0], ast.Constant)
                and isinstance(tup.elts[0].value, str))

    def _is_cfuncptr_call(self, node: ast.Call) -> bool:
        """Return True when calling a cfuncptr variable (non-binding form)."""
        if not isinstance(node.func, ast.Name):
            return False
        func_t = self.slot_types.get(node.func.id)
        return func_t is not None and is_cfuncptr_type(func_t) and not self._is_cfuncptr_binding(node)

    def _compile_cdll(self, node: ast.Call) -> tuple[str, str]:
        """Compile ``ctypes.CDLL(name)`` → ``call ptr @dlopen(ptr name, i32 1)``."""
        self.ctx.uses_dlopen = True
        if not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
            raise CompileError(
                "CDLL() requires a string-literal library name",
                code=_ERR_UNSUPPORTED_EXPRESSION,
                line=node.lineno,
                col=node.col_offset,
            )
        lib_name = node.args[0].value
        gname = self.ctx.alloc_string(lib_name)
        reg = self._new_reg()
        self.body_lines.append(f"  {reg} = call ptr @dlopen(ptr {gname}, i32 1)")
        return reg, CDLL_TYPE

    def _compile_string_at(self, node: ast.Call) -> tuple[str, str]:
        """Compile ``ctypes.string_at(ptr, size)`` → ``%pyx.bytes`` struct.

        Copies *size* bytes from the raw C pointer into a heap-allocated buffer
        and wraps the result as a PyX ``bytes`` value.
        """
        if len(node.args) != 2:
            raise CompileError(
                "string_at() expects exactly 2 arguments (ptr, size)",
                code=_ERR_CALL_ARG_COUNT,
                line=node.lineno,
                col=node.col_offset,
            )
        self.ctx.uses_malloc = True
        self.ctx.uses_memcpy = True
        ptr_val, _ = self._compile_expr(node.args[0])
        size_val, size_t = self._compile_expr(node.args[1])
        # size must be i64; extend from i32 if needed
        if size_t == "int":
            size_i64 = size_val
        else:
            size_i64 = size_val
        buf_reg = self._new_reg()
        self.body_lines.append(f"  {buf_reg} = call ptr @malloc(i64 {size_i64})")
        self.body_lines.append(f"  call ptr @memcpy(ptr {buf_reg}, ptr {ptr_val}, i64 {size_i64})")
        return self._build_bytes_value(buf_reg, size_i64), "bytes"

    def _compile_cfunctype(self, node: ast.Call) -> tuple[str, str]:
        """Compile ``ctypes.CFUNCTYPE(restype, *argtypes)`` → null ptr.

        CFUNCTYPE is a compile-time type descriptor; no runtime code is
        generated.  The type string is re-derived from the AST arguments so
        the compiler can use it for subsequent dlsym / indirect-call lowering.
        """
        ret_ctype = self._resolve_ctypes_type_name(node.args[0]) if node.args else "c_int"
        arg_ctypes = [self._resolve_ctypes_type_name(a) for a in node.args[1:]]
        ret = ret_ctype or "c_int"
        valid_args = [c for c in arg_ctypes if c is not None]
        inner = ",".join([ret] + valid_args)
        fn_type = f"cfuncptr({inner})"
        # No runtime code — the value is a null ptr placeholder.
        return "null", fn_type

    def _compile_cfuncptr_binding(self, node: ast.Call) -> tuple[str, str]:
        """Compile ``fn_type_var(("sym", lib))`` → ``call ptr @dlsym(ptr lib, ptr sym)``."""
        self.ctx.uses_dlsym = True
        func_name = node.func.id  # type: ignore[union-attr]
        func_t = self.slot_types[func_name]
        tup = node.args[0]  # ast.Tuple
        sym_name: str = tup.elts[0].value  # type: ignore[union-attr]
        lib_node = tup.elts[1]

        # Load the library handle
        lib_val, lib_t = self._compile_expr(lib_node)
        if lib_t != CDLL_TYPE:
            raise CompileError(
                f"dlsym binding expects a CDLL handle, got '{lib_t}'",
                code=_ERR_TYPE_MISMATCH,
                line=node.lineno,
                col=node.col_offset,
            )

        gname = self.ctx.alloc_string(sym_name)
        reg = self._new_reg()
        self.body_lines.append(f"  {reg} = call ptr @dlsym(ptr {lib_val}, ptr {gname})")
        return reg, func_t

    def _compile_cfuncptr_call(self, node: ast.Call) -> tuple[str, str]:
        """Compile an indirect call through a cfuncptr variable."""
        func_name = node.func.id  # type: ignore[union-attr]
        func_t = self.slot_types[func_name]
        parsed = parse_cfuncptr_type(func_t)
        if parsed is None:
            raise CompileError(
                f"Cannot call '{func_name}': not a function pointer type",
                code=_ERR_TYPE_MISMATCH,
                line=node.lineno,
                col=node.col_offset,
            )
        ret_ctype, arg_ctypes = parsed

        if len(node.args) != len(arg_ctypes):
            raise CompileError(
                f"Function pointer call expects {len(arg_ctypes)} argument(s), got {len(node.args)}",
                code=_ERR_CALL_ARG_COUNT,
                line=node.lineno,
                col=node.col_offset,
            )

        # Load the function pointer from its slot.
        fn_ptr = self._new_reg()
        self.body_lines.append(f"  {fn_ptr} = load ptr, ptr %{func_name}.slot")

        # Compile and coerce each argument to the expected ctypes LLVM type.
        compiled_args: list[str] = []
        for arg_node, expected_ctype in zip(node.args, arg_ctypes):
            arg_val, arg_t = self._compile_expr(arg_node)
            coerced = self._coerce_to_ctype(arg_val, arg_t, expected_ctype, arg_node)
            ctype_llvm = CTYPES_LLVM_MAP.get(expected_ctype, "ptr")
            compiled_args.append(f"{ctype_llvm} {coerced}")

        # Build LLVM function type signature for the indirect call.
        arg_llvm_types = [CTYPES_LLVM_MAP.get(c, "ptr") for c in arg_ctypes]
        arg_types_str = ", ".join(arg_llvm_types)

        if ret_ctype == "None":
            # void return
            self.body_lines.append(f"  call void ({arg_types_str}) {fn_ptr}({', '.join(compiled_args)})")
            return "0", "None"

        ret_llvm_t = CTYPES_LLVM_MAP.get(ret_ctype, "ptr")
        result_reg = self._new_reg()
        self.body_lines.append(
            f"  {result_reg} = call {ret_llvm_t} ({arg_types_str}) {fn_ptr}({', '.join(compiled_args)})"
        )
        pyx_type = ctypes_to_pyx_type(ret_ctype)
        coerced_result = self._coerce_from_ctype(result_reg, ret_ctype, ret_llvm_t, node)
        return coerced_result, pyx_type

    def _coerce_to_ctype(self, value: str, pyx_t: str, ctype: str, node: ast.AST) -> str:
        """Coerce a PyX LLVM value to the LLVM type expected by a ctypes argument."""
        target_llvm = CTYPES_LLVM_MAP.get(ctype)
        if target_llvm is None:
            raise CompileError(
                f"ctypes type '{ctype}' is not supported in LLVM lowering",
                code=_ERR_UNSUPPORTED_TYPE,
                line=getattr(node, "lineno", None),
                col=getattr(node, "col_offset", None),
            )
        if pyx_t == "int" and ctype in CTYPES_INT_TYPES:
            if target_llvm == "i64":
                return value  # i64 → i64, no coercion
            # Truncate i64 to the narrower integer type
            reg = self._new_reg()
            self.body_lines.append(f"  {reg} = trunc i64 {value} to {target_llvm}")
            return reg
        if pyx_t == "float" and ctype in CTYPES_FLOAT_TYPES:
            if target_llvm == "double":
                return value
            reg = self._new_reg()
            self.body_lines.append(f"  {reg} = fptrunc double {value} to float")
            return reg
        # str → c_char_p: extract the data pointer from the %pyx.str struct.
        # PyX str data is always null-terminated (literals + concat both add \0).
        if pyx_t == "str" and ctype == "c_char_p":
            reg = self._new_reg()
            self.body_lines.append(f"  {reg} = extractvalue {STR_LLVM_TYPE} {value}, 0")
            return reg
        # bytes → c_char_p: extract the data pointer from the %pyx.bytes struct.
        # The caller is responsible for null-termination when required by the C API.
        if pyx_t == "bytes" and ctype == "c_char_p":
            reg = self._new_reg()
            self.body_lines.append(f"  {reg} = extractvalue {BYTES_LLVM_TYPE} {value}, 0")
            return reg
        # Any (opaque ptr) → pointer-typed ctypes: pass through as-is.
        if pyx_t == "Any" and target_llvm == "ptr":
            return value
        raise CompileError(
            f"Cannot coerce PyX type '{pyx_t}' to ctypes '{ctype}'",
            code=_ERR_TYPE_MISMATCH,
            line=getattr(node, "lineno", None),
            col=getattr(node, "col_offset", None),
        )

    def _coerce_from_ctype(self, value: str, ctype: str, llvm_t: str, node: ast.AST) -> str:
        """Extend / promote a ctypes LLVM result back to the corresponding PyX type."""
        if ctype in CTYPES_INT_TYPES and llvm_t != "i64":
            reg = self._new_reg()
            self.body_lines.append(f"  {reg} = sext {llvm_t} {value} to i64")
            return reg
        if ctype in CTYPES_FLOAT_TYPES and llvm_t == "float":
            reg = self._new_reg()
            self.body_lines.append(f"  {reg} = fpext float {value} to double")
            return reg
        # c_char_p → bytes: use strlen to discover length, then wrap in pyx.bytes struct.
        if ctype == "c_char_p":
            self.ctx.uses_strlen = True
            self.ctx.uses_malloc = True
            self.ctx.uses_memcpy = True
            length_reg = self._new_reg()
            buf_reg = self._new_reg()
            self.body_lines.append(f"  {length_reg} = call i64 @strlen(ptr {value})")
            # Copy into a heap buffer owned by the bytes value (no null terminator needed).
            self.body_lines.append(f"  {buf_reg} = call ptr @malloc(i64 {length_reg})")
            self.body_lines.append(f"  call ptr @memcpy(ptr {buf_reg}, ptr {value}, i64 {length_reg})")
            return self._build_bytes_value(buf_reg, length_reg)
        return value  # already the right LLVM type (e.g. i64 / double / ptr)

    # ------------------------------------------------------------------

    def _compile_string_literal(self, value: str) -> str:
        gname = self.ctx.alloc_string(value)
        return self._build_str_value(gname, str(len(value.encode('utf-8'))))

    def _compile_string_concat(self, left: str, right: str) -> str:
        self.ctx.uses_malloc = True
        self.ctx.uses_memcpy = True
        left_ptr, left_len = self._extract_str_parts(left)
        right_ptr, right_len = self._extract_str_parts(right)
        new_len = self._new_reg()
        alloc_size = self._new_reg()
        buf = self._new_reg()
        copy1 = self._new_reg()
        right_dst = self._new_reg()
        copy2 = self._new_reg()
        zero_dst = self._new_reg()
        self.body_lines.append(f"  {new_len} = add i64 {left_len}, {right_len}")
        self.body_lines.append(f"  {alloc_size} = add i64 {new_len}, 1")
        self.body_lines.append(f"  {buf} = call ptr @malloc(i64 {alloc_size})")
        self.body_lines.append(f"  {copy1} = call ptr @memcpy(ptr {buf}, ptr {left_ptr}, i64 {left_len})")
        self.body_lines.append(f"  {right_dst} = getelementptr i8, ptr {buf}, i64 {left_len}")
        self.body_lines.append(f"  {copy2} = call ptr @memcpy(ptr {right_dst}, ptr {right_ptr}, i64 {right_len})")
        self.body_lines.append(f"  {zero_dst} = getelementptr i8, ptr {buf}, i64 {new_len}")
        self.body_lines.append(f"  store i8 0, ptr {zero_dst}")
        return self._build_str_value(buf, new_len)

    def _compile_numeric_binop(
        self,
        left: str,
        left_t: str,
        right: str,
        right_t: str,
        node: ast.AST,
        int_op: str,
        float_op: str,
    ) -> tuple[str, str]:
        result_t = merge_numeric_result_type(left_t, right_t)
        if result_t is None:
            raise CompileError("binary operands must be numeric", code=_ERR_TYPE_MISMATCH, line=node.lineno, col=node.col_offset)
        if result_t == "int":
            reg = self._new_reg()
            self.body_lines.append(f"  {reg} = {int_op} i64 {left}, {right}")
            return reg, "int"
        if result_t == "float":
            left_f = self._coerce_scalar_to_float(left, left_t, node)
            right_f = self._coerce_scalar_to_float(right, right_t, node)
            reg = self._new_reg()
            self.body_lines.append(f"  {reg} = {float_op} double {left_f}, {right_f}")
            return reg, "float"
        return self._compile_union_numeric_binop(left, left_t, right, right_t, node, int_op, float_op), NUMERIC_UNION

    def _compile_union_numeric_binop(self, left: str, left_t: str, right: str, right_t: str, node: ast.AST, int_op: str, float_op: str) -> str:
        left_u = self._ensure_union_value(left, left_t, node, "numeric union coercion failed")
        right_u = self._ensure_union_value(right, right_t, node, "numeric union coercion failed")
        left_tag, left_payload = self._extract_union_parts(left_u)
        right_tag, right_payload = self._extract_union_parts(right_u)
        left_is_int = self._new_reg()
        right_is_int = self._new_reg()
        both_int = self._new_reg()
        self.body_lines.append(f"  {left_is_int} = icmp eq i1 {left_tag}, 0")
        self.body_lines.append(f"  {right_is_int} = icmp eq i1 {right_tag}, 0")
        self.body_lines.append(f"  {both_int} = and i1 {left_is_int}, {right_is_int}")
        int_label = self._new_label("union_int")
        float_label = self._new_label("union_float")
        merge_label = self._new_label("union_merge")
        self.body_lines.append(f"  br i1 {both_int}, label %{int_label}, label %{float_label}")

        self.body_lines.append(f"{int_label}:")
        left_i = self._new_reg()
        right_i = self._new_reg()
        int_result = self._new_reg()
        int_payload = self._new_reg()
        self.body_lines.append(f"  {left_i} = fptosi double {left_payload} to i64")
        self.body_lines.append(f"  {right_i} = fptosi double {right_payload} to i64")
        self.body_lines.append(f"  {int_result} = {int_op} i64 {left_i}, {right_i}")
        self.body_lines.append(f"  {int_payload} = sitofp i64 {int_result} to double")
        self.body_lines.append(f"  br label %{merge_label}")

        self.body_lines.append(f"{float_label}:")
        float_result = self._new_reg()
        self.body_lines.append(f"  {float_result} = {float_op} double {left_payload}, {right_payload}")
        self.body_lines.append(f"  br label %{merge_label}")

        self.body_lines.append(f"{merge_label}:")
        tag_phi = self._new_reg()
        payload_phi = self._new_reg()
        self.body_lines.append(f"  {tag_phi} = phi i1 [0, %{int_label}], [1, %{float_label}]")
        self.body_lines.append(f"  {payload_phi} = phi double [{int_payload}, %{int_label}], [{float_result}, %{float_label}]")
        return self._build_union_value(tag_phi, payload_phi)

    def _compile_numeric_compare(self, left: str, left_t: str, right: str, right_t: str, node: ast.AST, op: ast.cmpop) -> str:
        if left_t == "int" and right_t == "int":
            pred_map = {ast.Lt: "slt", ast.LtE: "sle", ast.Gt: "sgt", ast.GtE: "sge", ast.Eq: "eq", ast.NotEq: "ne"}
            pred = pred_map.get(type(op))
            if pred is None:
                raise CompileError("comparison operator not supported", code=_ERR_UNSUPPORTED_EXPRESSION, line=node.lineno, col=node.col_offset)
            reg = self._new_reg()
            self.body_lines.append(f"  {reg} = icmp {pred} i64 {left}, {right}")
            return reg

        if not is_union_type(left_t) and not is_union_type(right_t):
            pred_map = {ast.Lt: "olt", ast.LtE: "ole", ast.Gt: "ogt", ast.GtE: "oge", ast.Eq: "oeq", ast.NotEq: "one"}
            pred = pred_map.get(type(op))
            if pred is None:
                raise CompileError("comparison operator not supported", code=_ERR_UNSUPPORTED_EXPRESSION, line=node.lineno, col=node.col_offset)
            left_f = self._coerce_scalar_to_float(left, left_t, node)
            right_f = self._coerce_scalar_to_float(right, right_t, node)
            reg = self._new_reg()
            self.body_lines.append(f"  {reg} = fcmp {pred} double {left_f}, {right_f}")
            return reg

        left_u = self._ensure_union_value(left, left_t, node, "comparison operands must be numeric")
        right_u = self._ensure_union_value(right, right_t, node, "comparison operands must be numeric")
        left_tag, left_payload = self._extract_union_parts(left_u)
        right_tag, right_payload = self._extract_union_parts(right_u)
        left_is_int = self._new_reg()
        right_is_int = self._new_reg()
        both_int = self._new_reg()
        self.body_lines.append(f"  {left_is_int} = icmp eq i1 {left_tag}, 0")
        self.body_lines.append(f"  {right_is_int} = icmp eq i1 {right_tag}, 0")
        self.body_lines.append(f"  {both_int} = and i1 {left_is_int}, {right_is_int}")
        int_label = self._new_label("cmp_int")
        float_label = self._new_label("cmp_float")
        merge_label = self._new_label("cmp_merge")
        self.body_lines.append(f"  br i1 {both_int}, label %{int_label}, label %{float_label}")

        int_pred_map = {ast.Lt: "slt", ast.LtE: "sle", ast.Gt: "sgt", ast.GtE: "sge", ast.Eq: "eq", ast.NotEq: "ne"}
        float_pred_map = {ast.Lt: "olt", ast.LtE: "ole", ast.Gt: "ogt", ast.GtE: "oge", ast.Eq: "oeq", ast.NotEq: "one"}
        int_pred = int_pred_map.get(type(op))
        float_pred = float_pred_map.get(type(op))
        if int_pred is None or float_pred is None:
            raise CompileError("comparison operator not supported", code=_ERR_UNSUPPORTED_EXPRESSION, line=node.lineno, col=node.col_offset)

        self.body_lines.append(f"{int_label}:")
        left_i = self._new_reg()
        right_i = self._new_reg()
        int_result = self._new_reg()
        self.body_lines.append(f"  {left_i} = fptosi double {left_payload} to i64")
        self.body_lines.append(f"  {right_i} = fptosi double {right_payload} to i64")
        self.body_lines.append(f"  {int_result} = icmp {int_pred} i64 {left_i}, {right_i}")
        self.body_lines.append(f"  br label %{merge_label}")

        self.body_lines.append(f"{float_label}:")
        float_result = self._new_reg()
        self.body_lines.append(f"  {float_result} = fcmp {float_pred} double {left_payload}, {right_payload}")
        self.body_lines.append(f"  br label %{merge_label}")

        self.body_lines.append(f"{merge_label}:")
        result = self._new_reg()
        self.body_lines.append(f"  {result} = phi i1 [{int_result}, %{int_label}], [{float_result}, %{float_label}]")
        return result

    def _coerce_value(self, value: str, got_t: str, expected_t: str, node: ast.AST, message: str) -> tuple[str, str]:
        if not can_assign_type(got_t, expected_t):
            raise CompileError(message + f": expected {expected_t}, got {got_t}", code=_ERR_TYPE_MISMATCH, line=node.lineno, col=node.col_offset)
        if got_t == expected_t:
            return value, expected_t
        if expected_t == "float" and got_t == "int":
            return self._coerce_scalar_to_float(value, got_t, node), "float"
        if is_union_type(expected_t):
            return self._ensure_union_value(value, got_t, node, message), expected_t
        return value, expected_t

    def _coerce_scalar_to_float(self, value: str, ty: str, node: ast.AST) -> str:
        if ty == "float":
            return value
        if ty == "int":
            reg = self._new_reg()
            self.body_lines.append(f"  {reg} = sitofp i64 {value} to double")
            return reg
        raise CompileError(f"cannot coerce {ty} to float", code=_ERR_TYPE_MISMATCH, line=node.lineno, col=node.col_offset)

    def _ensure_union_value(self, value: str, ty: str, node: ast.AST, message: str) -> str:
        if is_union_type(ty):
            return value
        if ty == "int":
            return self._build_union_value("0", self._coerce_scalar_to_float(value, ty, node))
        if ty == "float":
            return self._build_union_value("1", value)
        raise CompileError(message + f": expected {NUMERIC_UNION}, got {ty}", code=_ERR_TYPE_MISMATCH, line=node.lineno, col=node.col_offset)

    def _extract_union_parts(self, value: str) -> tuple[str, str]:
        tag = self._new_reg()
        payload = self._new_reg()
        self.body_lines.append(f"  {tag} = extractvalue {UNION_LLVM_TYPE} {value}, 0")
        self.body_lines.append(f"  {payload} = extractvalue {UNION_LLVM_TYPE} {value}, 1")
        return tag, payload

    def _build_union_value(self, tag: str, payload: str) -> str:
        union0 = self._new_reg()
        union1 = self._new_reg()
        self.body_lines.append(f"  {union0} = insertvalue {UNION_LLVM_TYPE} undef, i1 {tag}, 0")
        self.body_lines.append(f"  {union1} = insertvalue {UNION_LLVM_TYPE} {union0}, double {payload}, 1")
        return union1

    def _build_str_value(self, data_ptr: str, length: str) -> str:
        str0 = self._new_reg()
        str1 = self._new_reg()
        self.body_lines.append(f"  {str0} = insertvalue {STR_LLVM_TYPE} undef, ptr {data_ptr}, 0")
        self.body_lines.append(f"  {str1} = insertvalue {STR_LLVM_TYPE} {str0}, i64 {length}, 1")
        return str1

    def _extract_str_parts(self, value: str) -> tuple[str, str]:
        data_ptr = self._new_reg()
        length = self._new_reg()
        self.body_lines.append(f"  {data_ptr} = extractvalue {STR_LLVM_TYPE} {value}, 0")
        self.body_lines.append(f"  {length} = extractvalue {STR_LLVM_TYPE} {value}, 1")
        return data_ptr, length

    def _build_bytes_value(self, data_ptr: str, length: str) -> str:
        b0 = self._new_reg()
        b1 = self._new_reg()
        self.body_lines.append(f"  {b0} = insertvalue {BYTES_LLVM_TYPE} undef, ptr {data_ptr}, 0")
        self.body_lines.append(f"  {b1} = insertvalue {BYTES_LLVM_TYPE} {b0}, i64 {length}, 1")
        return b1

    def _extract_bytes_parts(self, value: str) -> tuple[str, str]:
        data_ptr = self._new_reg()
        length = self._new_reg()
        self.body_lines.append(f"  {data_ptr} = extractvalue {BYTES_LLVM_TYPE} {value}, 0")
        self.body_lines.append(f"  {length} = extractvalue {BYTES_LLVM_TYPE} {value}, 1")
        return data_ptr, length

    def _build_list_value(self, data_ptr: str, length: str, capacity: str) -> str:
        list0 = self._new_reg()
        list1 = self._new_reg()
        list2 = self._new_reg()
        self.body_lines.append(f"  {list0} = insertvalue {LIST_LLVM_TYPE} undef, ptr {data_ptr}, 0")
        self.body_lines.append(f"  {list1} = insertvalue {LIST_LLVM_TYPE} {list0}, i64 {length}, 1")
        self.body_lines.append(f"  {list2} = insertvalue {LIST_LLVM_TYPE} {list1}, i64 {capacity}, 2")
        return list2

    def _extract_list_parts(self, value: str) -> tuple[str, str, str]:
        data_ptr = self._new_reg()
        length = self._new_reg()
        capacity = self._new_reg()
        self.body_lines.append(f"  {data_ptr} = extractvalue {LIST_LLVM_TYPE} {value}, 0")
        self.body_lines.append(f"  {length} = extractvalue {LIST_LLVM_TYPE} {value}, 1")
        self.body_lines.append(f"  {capacity} = extractvalue {LIST_LLVM_TYPE} {value}, 2")
        return data_ptr, length, capacity

    def _render_annotation(self, node: ast.AST) -> str:
        rendered = ast.unparse(node)
        compact = rendered.replace(" ", "")
        if compact in {"int|float", "float|int"}:
            return NUMERIC_UNION
        if isinstance(node, ast.Name) and node.id in self.module.classes:
            return self.module.classes[node.id][1].qualified_name
        return normalize_type_name(rendered)

    def _ensure_slot(self, name: str, ty: str) -> None:
        if name in self.slot_types:
            return
        self.slot_types[name] = ty
        self.entry_lines.append(f"  %{name}.slot = alloca {llvm_type(ty)}")

    def _ensure_supported_type(self, ty: str, node: ast.AST) -> None:
        if not is_supported_type(ty, self.project.known_type_names()):
            raise CompileError(f"type '{ty}' is not supported", code=_ERR_UNSUPPORTED_TYPE, line=node.lineno, col=node.col_offset)
        if parse_dict_type(ty) is not None or parse_set_type(ty) is not None:
            raise CompileError(f"type '{ty}' is planned but not lowered in LLVM mode yet", code=_ERR_UNSUPPORTED_TYPE, line=node.lineno, col=node.col_offset)

    def _emit_bounds_check(self, index: str, length: str, prefix: str) -> None:
        self.ctx.uses_abort = True
        negative = self._new_reg()
        too_large = self._new_reg()
        out_of_bounds = self._new_reg()
        trap_label = self._new_label(f"{prefix}_trap")
        ok_label = self._new_label(f"{prefix}_ok")
        self.body_lines.append(f"  {negative} = icmp slt i64 {index}, 0")
        self.body_lines.append(f"  {too_large} = icmp sge i64 {index}, {length}")
        self.body_lines.append(f"  {out_of_bounds} = or i1 {negative}, {too_large}")
        self.body_lines.append(f"  br i1 {out_of_bounds}, label %{trap_label}, label %{ok_label}")
        self.body_lines.append(f"{trap_label}:")
        self.body_lines.append("  call void @abort()")
        self.body_lines.append("  unreachable")
        self.body_lines.append(f"{ok_label}:")

    def _require_assignable(self, got: str, expected: str, node: ast.AST, msg: str) -> None:
        if not can_assign_type(got, expected):
            raise CompileError(f"{msg}: expected {expected}, got {got}", code=_ERR_TYPE_MISMATCH, line=node.lineno, col=node.col_offset)

    def _resolve_callable(self, func: ast.AST) -> _CallableTarget | None:
        if isinstance(func, ast.Name):
            if func.id in self.module.functions:
                signature = self.module.functions[func.id][1]
                return _CallableTarget(self.ctx.function_symbol(signature), signature.arg_types, signature.return_type)
            imported = self.module.imported_symbols.get(func.id)
            if imported is not None and imported.module_name != "ctypes":
                target_module = self.project.lookup_module(imported.module_name)
                assert target_module is not None
                if imported.symbol_name in target_module.functions:
                    signature = target_module.functions[imported.symbol_name][1]
                    return _CallableTarget(self.ctx.function_symbol(signature), signature.arg_types, signature.return_type)
            return None
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            imported_module = self.module.imported_modules.get(func.value.id)
            if imported_module is not None and imported_module.module_name != "ctypes":
                target_module = self.project.lookup_module(imported_module.module_name)
                assert target_module is not None
                if func.attr in target_module.functions:
                    signature = target_module.functions[func.attr][1]
                    return _CallableTarget(self.ctx.function_symbol(signature), signature.arg_types, signature.return_type)
        return None

    def _class_field_index(self, class_info: ClassInfo, field_name: str, node: ast.AST) -> int:
        try:
            return class_info.field_names.index(field_name)
        except ValueError as exc:
            raise CompileError(f"class '{class_info.name}' has no field '{field_name}'", code=_ERR_UNKNOWN_FIELD, line=node.lineno, col=node.col_offset) from exc

    def _merge_item_type(self, left: str, right: str) -> str:
        if left == right:
            return left
        merged = merge_numeric_result_type(left, right)
        if merged is not None:
            return merged
        return "Any"

    def _element_size(self, item_t: str) -> int:
        if item_t in {"int", "float"}:
            return 8
        if item_t == "bool":
            return 1
        if item_t == "str":
            return 16
        raise CompileError(f"list element type '{item_t}' size is unknown", code=_ERR_UNSUPPORTED_TYPE)

    def _is_list_append_call(self, node: ast.Call) -> bool:
        return isinstance(node.func, ast.Attribute) and node.func.attr == "append"

    def _new_reg(self) -> str:
        self.reg_idx += 1
        return f"%r{self.reg_idx}"

    def _new_label(self, prefix: str) -> str:
        self.label_idx += 1
        return f"{prefix}{self.label_idx}"


class LLVMCompiler:
    def __init__(self, project: ProjectInfo) -> None:
        self.project = project

    @classmethod
    def from_path(cls, source: str | Path) -> "LLVMCompiler":
        try:
            project = load_project(source)
        except ProjectLoadError as exc:
            raise CompileError(str(exc), code=_ERR_PROJECT, path=Path(source)) from exc
        return cls(project)

    def compile_ir(self) -> str:
        functions: list[tuple[ModuleInfo, ast.FunctionDef, FunctionSignature]] = []
        for module in self.project.modules.values():
            for _, (fn_node, signature) in module.functions.items():
                functions.append((module, fn_node, signature))
            for _, (class_node, class_info) in module.classes.items():
                for stmt in class_node.body:
                    if isinstance(stmt, ast.FunctionDef):
                        functions.append((module, stmt, class_info.methods[stmt.name]))
        if not functions:
            raise CompileError("no top-level functions found to compile", code=_ERR_UNSUPPORTED_STATEMENT, path=self.project.entry_path)

        ctx = _ModuleContext(self.project, self.project.entry_path.stem)
        chunks = ["; ModuleID = 'pyx'", 'source_filename = "pyx"', ""]
        compiled_functions: list[list[str]] = []
        for module, fn_node, signature in functions:
            compiled_functions.append(_FunctionCompiler(module, fn_node, signature, self.project, ctx).compile())
        try:
            chunks.extend(ctx.emit_preamble())
        except CompileError as exc:
            if exc.path is None:
                exc.path = self.project.entry_path
            raise
        for fn_lines in compiled_functions:
            chunks.extend(fn_lines)
            chunks.append("")
        return "\n".join(chunks).strip() + "\n"


def emit_native_object(ll_path: Path, output_path: Path) -> bool:
    clang = shutil.which("clang")
    if clang is None:
        return False
    subprocess.run([clang, "-c", str(ll_path), "-O2", "-o", str(output_path)], check=True)
    return True
