from __future__ import annotations

import ast
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .project import ClassInfo, FunctionSignature, ModuleInfo, ProjectInfo, ProjectLoadError, load_project
from .type_system import (
    NUMERIC_UNION,
    can_assign_type,
    is_numeric_type,
    is_supported_type,
    is_union_type,
    merge_numeric_result_type,
    normalize_type_name,
    parse_dict_type,
    parse_list_type,
)

TYPE_MAP = {"int": "i64", "float": "double", "bool": "i1"}
UNION_LLVM_TYPE = "{ i1, double }"
STR_LLVM_TYPE = "%pyx.str"
LIST_LLVM_TYPE = "%pyx.list"

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
    if parse_list_type(normalized) is not None:
        return LIST_LLVM_TYPE
    if "." in normalized and "[" not in normalized:
        return _class_type_name(normalized)
    raise KeyError(py_type)


@dataclass
class CompileError(Exception):
    message: str
    code: str = _ERR_UNSUPPORTED_EXPRESSION
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
        self._str_by_value: dict[str, str] = {}
        self._str_globals: list[tuple[str, str]] = []

    def alloc_string(self, value: str) -> str:
        if value not in self._str_by_value:
            name = f"@__pyx_str_{len(self._str_by_value)}"
            self._str_by_value[value] = name
            self._str_globals.append((name, value))
        return self._str_by_value[value]

    def function_symbol(self, signature: FunctionSignature) -> str:
        if signature.class_name is not None:
            if signature.module_name == self.entry_module:
                return f"@{signature.class_name}__{signature.name}"
            return f"@mod_{_mangle_path(signature.module_name)}__{signature.class_name}__{signature.name}"
        if signature.module_name == self.entry_module:
            return f"@{signature.name}"
        return f"@mod_{_mangle_path(signature.module_name)}__{signature.name}"

    def emit_preamble(self) -> list[str]:
        lines = [
            f"{STR_LLVM_TYPE} = type {{ ptr, i64 }}",
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
        if self._str_globals:
            lines.append("")
        if self.uses_printf:
            lines.append("declare i32 @printf(ptr, ...)")
        if self.uses_malloc:
            lines.append("declare ptr @malloc(i64)")
        if self.uses_realloc:
            lines.append("declare ptr @realloc(ptr, i64)")
        if self.uses_memcpy:
            lines.append("declare ptr @memcpy(ptr, ptr, i64)")
        if self.uses_printf or self.uses_malloc or self.uses_realloc or self.uses_memcpy:
            lines.append("")
        return lines


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
                _, length = self._extract_str_parts(value)
                return length, "int"
            if parse_list_type(value_t) is not None:
                _, length, _ = self._extract_list_parts(value)
                return length, "int"
            raise CompileError(f"len() does not support '{value_t}' in LLVM mode", code=_ERR_UNSUPPORTED_EXPRESSION, line=node.lineno, col=node.col_offset)

        if self._is_list_append_call(node):
            raise CompileError("list.append() is a statement in LLVM mode", code=_ERR_UNSUPPORTED_EXPRESSION, line=node.lineno, col=node.col_offset)

        if isinstance(node.func, ast.Name) and node.func.id in self.module.classes:
            return self._compile_constructor(self.module.classes[node.func.id][1], node.args, node)

        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id in self.module.imported_modules:
            imported_module = self.module.imported_modules[node.func.value.id]
            target_module = self.project.lookup_module(imported_module.module_name)
            assert target_module is not None
            if node.func.attr in target_module.classes:
                return self._compile_constructor(target_module.classes[node.func.attr][1], node.args, node)

        if isinstance(node.func, ast.Attribute):
            owner_val, owner_t = self._compile_expr(node.func.value)
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
        item_t = parse_list_type(container_t)
        if item_t is None:
            raise CompileError(f"subscript is only supported for list[T] in LLVM mode, got {container_t}", code=_ERR_UNSUPPORTED_EXPRESSION, line=node.lineno, col=node.col_offset)
        index, index_t = self._compile_expr(node.slice)
        self._require_assignable(index_t, "int", node, "list index must be int")
        data_ptr, _, _ = self._extract_list_parts(container)
        elem_ptr = self._new_reg()
        elem_val = self._new_reg()
        self.body_lines.append(f"  {elem_ptr} = getelementptr {llvm_type(item_t)}, ptr {data_ptr}, i64 {index}")
        self.body_lines.append(f"  {elem_val} = load {llvm_type(item_t)}, ptr {elem_ptr}")
        return elem_val, item_t

    def _compile_list_literal(self, node: ast.List) -> tuple[str, str]:
        if not node.elts:
            return self._build_list_value("null", "0", "0"), "list[Any]"
        item_t = self._infer_list_item_type(node)
        if item_t not in {"int", "float", "bool", "str"}:
            raise CompileError(f"list element type '{item_t}' is not supported in LLVM mode", code=_ERR_UNSUPPORTED_TYPE, line=node.lineno, col=node.col_offset)
        self.ctx.uses_malloc = True
        count = len(node.elts)
        alloc_reg = self._new_reg()
        self.body_lines.append(f"  {alloc_reg} = call ptr @malloc(i64 {count * self._element_size(item_t)})")
        for index, elt in enumerate(node.elts):
            value, got_t = self._compile_expr(elt)
            coerced, _ = self._coerce_value(value, got_t, item_t, elt, "list literal element type mismatch")
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
        if ty == "bytes" or parse_dict_type(ty) is not None:
            raise CompileError(f"type '{ty}' is planned but not lowered in LLVM mode yet", code=_ERR_UNSUPPORTED_TYPE, line=node.lineno, col=node.col_offset)

    def _require_assignable(self, got: str, expected: str, node: ast.AST, msg: str) -> None:
        if not can_assign_type(got, expected):
            raise CompileError(f"{msg}: expected {expected}, got {got}", code=_ERR_TYPE_MISMATCH, line=node.lineno, col=node.col_offset)

    def _resolve_callable(self, func: ast.AST) -> _CallableTarget | None:
        if isinstance(func, ast.Name):
            if func.id in self.module.functions:
                signature = self.module.functions[func.id][1]
                return _CallableTarget(self.ctx.function_symbol(signature), signature.arg_types, signature.return_type)
            imported = self.module.imported_symbols.get(func.id)
            if imported is not None:
                target_module = self.project.lookup_module(imported.module_name)
                assert target_module is not None
                if imported.symbol_name in target_module.functions:
                    signature = target_module.functions[imported.symbol_name][1]
                    return _CallableTarget(self.ctx.function_symbol(signature), signature.arg_types, signature.return_type)
            return None
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            imported_module = self.module.imported_modules.get(func.value.id)
            if imported_module is not None:
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

    def _infer_list_item_type(self, node: ast.List) -> str:
        _, item_t = self._compile_expr(node.elts[0])
        for elt in node.elts[1:]:
            _, other_t = self._compile_expr(elt)
            item_t = self._merge_item_type(item_t, other_t)
        return item_t

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
            raise CompileError(str(exc), code=_ERR_PROJECT) from exc
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
            raise CompileError("no top-level functions found to compile", code=_ERR_UNSUPPORTED_STATEMENT)

        ctx = _ModuleContext(self.project, self.project.entry_path.stem)
        chunks = ["; ModuleID = 'pyx'", 'source_filename = "pyx"', ""]
        compiled_functions = [_FunctionCompiler(module, fn_node, signature, self.project, ctx).compile() for module, fn_node, signature in functions]
        chunks.extend(ctx.emit_preamble())
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
