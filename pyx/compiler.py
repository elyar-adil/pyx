from __future__ import annotations

import ast
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .type_system import (
    NUMERIC_UNION,
    can_assign_type,
    is_numeric_type,
    is_supported_type,
    is_union_type,
    merge_numeric_result_type,
)

TYPE_MAP = {"int": "i64", "float": "double", "bool": "i1", "str": "ptr"}
UNION_LLVM_TYPE = "{ i1, double }"

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

# Format string globals emitted when print() is used.
# Each entry: (global_name, byte_count, llvm_c_string)
_PRINT_FMT_DEFS: tuple[tuple[str, int, str], ...] = (
    ("@__pyx_fmt_int_sp", 5, 'c"%ld \\00"'),
    ("@__pyx_fmt_int_nl", 5, 'c"%ld\\0a\\00"'),
    ("@__pyx_fmt_flt_sp", 4, 'c"%g \\00"'),
    ("@__pyx_fmt_flt_nl", 4, 'c"%g\\0a\\00"'),
    ("@__pyx_fmt_str_sp", 4, 'c"%s \\00"'),
    ("@__pyx_fmt_str_nl", 4, 'c"%s\\0a\\00"'),
    ("@__pyx_fmt_nl", 2, 'c"\\0a\\00"'),
    ("@__pyx_str_True", 5, 'c"True\\00"'),
    ("@__pyx_str_False", 6, 'c"False\\00"'),
)

# fmt lookup: pyx_type -> (space_variant_name, newline_variant_name)
_PRINT_FMT: dict[str, tuple[str, str]] = {
    "int": ("@__pyx_fmt_int_sp", "@__pyx_fmt_int_nl"),
    "float": ("@__pyx_fmt_flt_sp", "@__pyx_fmt_flt_nl"),
    "str": ("@__pyx_fmt_str_sp", "@__pyx_fmt_str_nl"),
    "bool": ("@__pyx_fmt_str_sp", "@__pyx_fmt_str_nl"),
}


def llvm_type(py_type: str) -> str:
    if is_union_type(py_type):
        return UNION_LLVM_TYPE
    return TYPE_MAP[py_type]


def _encode_llvm_string(value: str) -> tuple[str, int]:
    """Encode a Python string as an LLVM ``c"..."`` literal."""
    parts: list[str] = []
    for byte in value.encode("utf-8"):
        if 0x20 <= byte <= 0x7E and byte not in (0x22, 0x5C):
            parts.append(chr(byte))
        else:
            parts.append(f"\\{byte:02x}")
    parts.append("\\00")
    return f'c"{"".join(parts)}"', len(value.encode("utf-8")) + 1


class _ModuleContext:
    """Accumulates module-level globals required during function compilation."""

    def __init__(self) -> None:
        self.uses_printf: bool = False
        self._str_by_value: dict[str, str] = {}
        self._str_globals: list[tuple[str, str]] = []
        self._str_counter: int = 0

    def alloc_string(self, value: str) -> str:
        if value not in self._str_by_value:
            name = f"@__pyx_str_{self._str_counter}"
            self._str_counter += 1
            self._str_by_value[value] = name
            self._str_globals.append((name, value))
        return self._str_by_value[value]

    def emit_preamble(self) -> list[str]:
        if not self.uses_printf:
            return []
        lines: list[str] = []
        for gname, nbytes, data in _PRINT_FMT_DEFS:
            lines.append(f"{gname} = private unnamed_addr constant [{nbytes} x i8] {data}")
        for gname, value in self._str_globals:
            literal, nbytes = _encode_llvm_string(value)
            lines.append(f"{gname} = private unnamed_addr constant [{nbytes} x i8] {literal}")
        lines.append("declare i32 @printf(ptr, ...)")
        lines.append("")
        return lines


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


class _FunctionCompiler:
    def __init__(
        self,
        node: ast.FunctionDef,
        signatures: dict[str, tuple[list[str], str]],
        ctx: _ModuleContext,
    ) -> None:
        self.node = node
        self.signatures = signatures
        self.ctx = ctx
        self.entry_lines: list[str] = []
        self.body_lines: list[str] = []
        self.reg_idx = 0
        self.label_idx = 0
        self.slot_types: dict[str, str] = {}

    def compile(self) -> list[str]:
        arg_sig: list[str] = []
        for arg in self.node.args.args:
            ty = self._annotation_to_type(arg.annotation, arg, f"parameter '{arg.arg}'")
            arg_sig.append(f"{llvm_type(ty)} %{arg.arg}")
            self._ensure_slot(arg.arg, ty)
            self.body_lines.append(f"  store {llvm_type(ty)} %{arg.arg}, ptr %{arg.arg}.slot")

        ret_t = self._annotation_to_type(self.node.returns, self.node, "return type")

        header = [f"define {llvm_type(ret_t)} @{self.node.name}({', '.join(arg_sig)}) {{", "entry:"]
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
                if len(stmt.targets) != 1 or not isinstance(stmt.targets[0], ast.Name):
                    raise CompileError(
                        "only simple name assignment is supported",
                        code=_ERR_UNSUPPORTED_STATEMENT,
                        line=stmt.lineno,
                        col=stmt.col_offset,
                    )
                name = stmt.targets[0].id
                value, ty = self._compile_expr(stmt.value)
                if name not in self.slot_types:
                    self._ensure_slot(name, ty)
                coerced, _ = self._coerce_value(value, ty, self.slot_types[name], stmt, f"assignment type mismatch for '{name}'")
                self.body_lines.append(f"  store {llvm_type(self.slot_types[name])} {coerced}, ptr %{name}.slot")
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
                raise CompileError(
                    "only print() calls are supported as expression statements",
                    code=_ERR_UNSUPPORTED_STATEMENT,
                    line=stmt.lineno,
                    col=stmt.col_offset,
                )

            raise CompileError(
                f"unsupported statement {stmt.__class__.__name__} in LLVM mode",
                code=_ERR_UNSUPPORTED_STATEMENT,
                line=stmt.lineno,
                col=stmt.col_offset,
            )

        return False

    def _compile_print(self, node: ast.Call) -> None:
        self.ctx.uses_printf = True
        args = node.args
        if not args:
            reg = self._new_reg()
            self.body_lines.append(f"  {reg} = call i32 (ptr, ...) @printf(ptr @__pyx_fmt_nl)")
            return

        for i, arg_node in enumerate(args):
            is_last = i == len(args) - 1
            val, ty = self._compile_expr(arg_node)
            if ty not in _PRINT_FMT:
                raise CompileError(
                    f"print() argument has unsupported type '{ty}'",
                    code=_ERR_PRINT_USAGE,
                    line=getattr(arg_node, "lineno", None),
                    col=getattr(arg_node, "col_offset", None),
                )
            fmt_sp, fmt_nl = _PRINT_FMT[ty]
            fmt = fmt_nl if is_last else fmt_sp
            reg = self._new_reg()

            if ty == "bool":
                str_reg = self._new_reg()
                self.body_lines.append(f"  {str_reg} = select i1 {val}, ptr @__pyx_str_True, ptr @__pyx_str_False")
                self.body_lines.append(f"  {reg} = call i32 (ptr, ...) @printf(ptr {fmt}, ptr {str_reg})")
            elif ty == "int":
                self.body_lines.append(f"  {reg} = call i32 (ptr, ...) @printf(ptr {fmt}, i64 {val})")
            elif ty == "float":
                self.body_lines.append(f"  {reg} = call i32 (ptr, ...) @printf(ptr {fmt}, double {val})")
            else:
                self.body_lines.append(f"  {reg} = call i32 (ptr, ...) @printf(ptr {fmt}, ptr {val})")

    def _compile_expr(self, node: ast.AST) -> tuple[str, str]:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool):
                return ("1" if node.value else "0", "bool")
            if isinstance(node.value, int):
                return (str(node.value), "int")
            if isinstance(node.value, float):
                return (str(node.value), "float")
            if isinstance(node.value, str):
                gname = self.ctx.alloc_string(node.value)
                self.ctx.uses_printf = True
                return (gname, "str")

        if isinstance(node, ast.Name):
            if node.id not in self.slot_types:
                raise CompileError(
                    f"unknown variable '{node.id}'",
                    code=_ERR_UNKNOWN_VARIABLE,
                    line=node.lineno,
                    col=node.col_offset,
                )
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

        if isinstance(node, ast.BinOp):
            left, left_t = self._compile_expr(node.left)
            right, right_t = self._compile_expr(node.right)
            if not is_numeric_type(left_t) or not is_numeric_type(right_t):
                raise CompileError(
                    "arithmetic only supports int/float and int | float",
                    code=_ERR_TYPE_MISMATCH,
                    line=node.lineno,
                    col=node.col_offset,
                )
            if isinstance(node.op, ast.Add):
                return self._compile_numeric_binop(left, left_t, right, right_t, node, "add", "fadd")
            if isinstance(node.op, ast.Sub):
                return self._compile_numeric_binop(left, left_t, right, right_t, node, "sub", "fsub")
            if isinstance(node.op, ast.Mult):
                return self._compile_numeric_binop(left, left_t, right, right_t, node, "mul", "fmul")
            raise CompileError(
                "only +, -, * are supported in LLVM mode",
                code=_ERR_UNSUPPORTED_EXPRESSION,
                line=node.lineno,
                col=node.col_offset,
            )

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

            raise CompileError(
                "comparison type not supported",
                code=_ERR_TYPE_MISMATCH,
                line=node.lineno,
                col=node.col_offset,
            )

        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            callee = node.func.id
            if callee == "print":
                raise CompileError(
                    "'print()' is a statement; its return value cannot be used in an expression",
                    code=_ERR_PRINT_USAGE,
                    line=getattr(node, "lineno", None),
                    col=getattr(node, "col_offset", None),
                )
            if callee not in self.signatures:
                raise CompileError(
                    f"call target '{callee}' is not a known function",
                    code=_ERR_UNKNOWN_FUNCTION,
                    line=node.lineno,
                    col=node.col_offset,
                )
            arg_types, ret_t = self.signatures[callee]
            if len(arg_types) != len(node.args):
                raise CompileError(
                    f"call arg count mismatch for '{callee}'",
                    code=_ERR_CALL_ARG_COUNT,
                    line=node.lineno,
                    col=node.col_offset,
                )

            compiled_args: list[str] = []
            for arg_node, expected_t in zip(node.args, arg_types):
                val, got_t = self._compile_expr(arg_node)
                coerced, _ = self._coerce_value(val, got_t, expected_t, arg_node, f"call arg type mismatch for '{callee}'")
                compiled_args.append(f"{llvm_type(expected_t)} {coerced}")

            reg = self._new_reg()
            self.body_lines.append(f"  {reg} = call {llvm_type(ret_t)} @{callee}({', '.join(compiled_args)})")
            return reg, ret_t

        raise CompileError(
            f"unsupported expression {node.__class__.__name__} in LLVM mode",
            code=_ERR_UNSUPPORTED_EXPRESSION,
            line=getattr(node, "lineno", None),
            col=getattr(node, "col_offset", None),
        )

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
            raise CompileError(
                "binary operands must be numeric",
                code=_ERR_TYPE_MISMATCH,
                line=node.lineno,
                col=node.col_offset,
            )

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

    def _compile_union_numeric_binop(
        self,
        left: str,
        left_t: str,
        right: str,
        right_t: str,
        node: ast.AST,
        int_op: str,
        float_op: str,
    ) -> str:
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

    def _compile_numeric_compare(
        self,
        left: str,
        left_t: str,
        right: str,
        right_t: str,
        node: ast.AST,
        op: ast.cmpop,
    ) -> str:
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

    def _coerce_value(
        self,
        value: str,
        got_t: str,
        expected_t: str,
        node: ast.AST,
        message: str,
    ) -> tuple[str, str]:
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
        raise CompileError(
            f"cannot coerce {ty} to float",
            code=_ERR_TYPE_MISMATCH,
            line=node.lineno,
            col=node.col_offset,
        )

    def _ensure_union_value(self, value: str, ty: str, node: ast.AST, message: str) -> str:
        if is_union_type(ty):
            return value
        if ty == "int":
            payload = self._coerce_scalar_to_float(value, ty, node)
            return self._build_union_value("0", payload)
        if ty == "float":
            return self._build_union_value("1", value)
        raise CompileError(
            message + f": expected {NUMERIC_UNION}, got {ty}",
            code=_ERR_TYPE_MISMATCH,
            line=node.lineno,
            col=node.col_offset,
        )

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

    def _annotation_to_type(self, annotation: ast.AST | None, node: ast.AST, label: str) -> str:
        if annotation is None:
            raise CompileError(f"{label} must be annotated", code=_ERR_MISSING_ANNOTATION, line=node.lineno, col=node.col_offset)
        rendered = ast.unparse(annotation)
        compact = rendered.replace(" ", "")
        if compact in {"int|float", "float|int"}:
            return NUMERIC_UNION
        if not is_supported_type(rendered):
            raise CompileError(
                f"{label} must be one of int, float, bool, str, int | float",
                code=_ERR_UNSUPPORTED_TYPE,
                line=node.lineno,
                col=node.col_offset,
            )
        return rendered

    def _ensure_slot(self, name: str, ty: str) -> None:
        if name in self.slot_types:
            return
        self.slot_types[name] = ty
        self.entry_lines.append(f"  %{name}.slot = alloca {llvm_type(ty)}")

    def _require_assignable(self, got: str, expected: str, node: ast.AST, msg: str) -> None:
        if not can_assign_type(got, expected):
            raise CompileError(f"{msg}: expected {expected}, got {got}", code=_ERR_TYPE_MISMATCH, line=node.lineno, col=node.col_offset)

    def _new_reg(self) -> str:
        self.reg_idx += 1
        return f"%r{self.reg_idx}"

    def _new_label(self, prefix: str) -> str:
        self.label_idx += 1
        return f"{prefix}{self.label_idx}"


class LLVMCompiler:
    def __init__(self, tree: ast.Module) -> None:
        self.tree = tree

    @classmethod
    def from_path(cls, source: str | Path) -> "LLVMCompiler":
        path = Path(source)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        return cls(tree)

    def compile_ir(self) -> str:
        functions = [n for n in self.tree.body if isinstance(n, ast.FunctionDef)]
        if not functions:
            raise CompileError("no top-level functions found to compile", code=_ERR_UNSUPPORTED_STATEMENT)

        signatures: dict[str, tuple[list[str], str]] = {}
        for fn in functions:
            arg_types = [self._annotation_to_type(a.annotation, a, f"parameter '{a.arg}'") for a in fn.args.args]
            ret_t = self._annotation_to_type(fn.returns, fn, "return type")
            signatures[fn.name] = (arg_types, ret_t)

        ctx = _ModuleContext()
        function_chunks: list[list[str]] = [_FunctionCompiler(fn, signatures, ctx).compile() for fn in functions]

        chunks = ["; ModuleID = 'pyx'", 'source_filename = "pyx"', ""]
        chunks.extend(ctx.emit_preamble())
        for fn_lines in function_chunks:
            chunks.extend(fn_lines)
            chunks.append("")
        return "\n".join(chunks).strip() + "\n"

    def _annotation_to_type(self, annotation: ast.AST | None, node: ast.AST, label: str) -> str:
        if annotation is None:
            raise CompileError(f"{label} must be annotated", code=_ERR_MISSING_ANNOTATION, line=node.lineno, col=node.col_offset)
        rendered = ast.unparse(annotation)
        compact = rendered.replace(" ", "")
        if compact in {"int|float", "float|int"}:
            return NUMERIC_UNION
        if not is_supported_type(rendered):
            raise CompileError(
                f"{label} must be one of int, float, bool, str, int | float",
                code=_ERR_UNSUPPORTED_TYPE,
                line=node.lineno,
                col=node.col_offset,
            )
        return rendered


def emit_native_object(ll_path: Path, output_path: Path) -> bool:
    clang = shutil.which("clang")
    if clang is None:
        return False
    subprocess.run([clang, "-c", str(ll_path), "-O2", "-o", str(output_path)], check=True)
    return True
