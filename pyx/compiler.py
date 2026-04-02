from __future__ import annotations

import ast
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


TYPE_MAP = {"int": "i64", "float": "double", "bool": "i1", "str": "ptr"}

# Format string globals emitted when print() is used.
# Each entry: (global_name, byte_count, llvm_c_string)
_PRINT_FMT_DEFS: tuple[tuple[str, int, str], ...] = (
    ("@__pyx_fmt_int_sp",  5, 'c"%ld \\00"'),
    ("@__pyx_fmt_int_nl",  5, 'c"%ld\\0a\\00"'),
    ("@__pyx_fmt_flt_sp",  4, 'c"%g \\00"'),
    ("@__pyx_fmt_flt_nl",  4, 'c"%g\\0a\\00"'),
    ("@__pyx_fmt_str_sp",  4, 'c"%s \\00"'),
    ("@__pyx_fmt_str_nl",  4, 'c"%s\\0a\\00"'),
    ("@__pyx_fmt_nl",      2, 'c"\\0a\\00"'),
    ("@__pyx_str_True",    5, 'c"True\\00"'),
    ("@__pyx_str_False",   6, 'c"False\\00"'),
)

# fmt lookup: pyx_type -> (space_variant_name, newline_variant_name)
_PRINT_FMT: dict[str, tuple[str, str]] = {
    "int":   ("@__pyx_fmt_int_sp", "@__pyx_fmt_int_nl"),
    "float": ("@__pyx_fmt_flt_sp", "@__pyx_fmt_flt_nl"),
    "str":   ("@__pyx_fmt_str_sp", "@__pyx_fmt_str_nl"),
    "bool":  ("@__pyx_fmt_str_sp", "@__pyx_fmt_str_nl"),
}


def _encode_llvm_string(value: str) -> tuple[str, int]:
    """Encode a Python string as an LLVM ``c"..."`` literal.

    Returns ``(llvm_literal, byte_count)`` where *byte_count* includes the
    null terminator.  Only UTF-8 is supported; characters outside the printable
    ASCII range (0x20–0x7E, excluding ``"`` and ``\\``) are hex-escaped.
    """
    parts: list[str] = []
    for byte in value.encode("utf-8"):
        if 0x20 <= byte <= 0x7E and byte not in (0x22, 0x5C):  # not " or \
            parts.append(chr(byte))
        else:
            parts.append(f"\\{byte:02x}")
    parts.append("\\00")
    return f'c"{"".join(parts)}"', len(value.encode("utf-8")) + 1


class _ModuleContext:
    """Accumulates module-level globals required during function compilation.

    A single instance is shared across all ``_FunctionCompiler`` objects for
    one compilation unit.  After all functions are compiled, call
    :meth:`emit_preamble` to obtain the LLVM IR lines that must appear before
    any function definitions.
    """

    def __init__(self) -> None:
        self.uses_printf: bool = False
        # Ordered mapping value -> global_name for deduplication.
        self._str_by_value: dict[str, str] = {}
        self._str_globals: list[tuple[str, str]] = []  # (name, value)
        self._str_counter: int = 0

    def alloc_string(self, value: str) -> str:
        """Intern *value* and return its LLVM global name (``@__pyx_str_N``)."""
        if value not in self._str_by_value:
            name = f"@__pyx_str_{self._str_counter}"
            self._str_counter += 1
            self._str_by_value[value] = name
            self._str_globals.append((name, value))
        return self._str_by_value[value]

    def emit_preamble(self) -> list[str]:
        """Return LLVM IR lines for all globals/declarations collected so far."""
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
        arg_types: list[str] = []
        arg_sig: list[str] = []
        for arg in self.node.args.args:
            ty = self._annotation_to_type(arg.annotation, arg, f"parameter '{arg.arg}'")
            arg_types.append(ty)
            llvm_t = TYPE_MAP[ty]
            arg_name = f"%{arg.arg}"
            arg_sig.append(f"{llvm_t} {arg_name}")
            self._ensure_slot(arg.arg, ty)
            self.body_lines.append(f"  store {llvm_t} {arg_name}, ptr %{arg.arg}.slot")

        ret_t = self._annotation_to_type(self.node.returns, self.node, "return type")

        header = [f"define {TYPE_MAP[ret_t]} @{self.node.name}({', '.join(arg_sig)}) {{", "entry:"]
        terminated = self._compile_statements(self.node.body, ret_t)
        if not terminated:
            raise CompileError(
                f"Function '{self.node.name}' must explicitly return on all paths for LLVM lowering",
                self.node.lineno,
                self.node.col_offset,
            )

        footer = ["}"]
        return header + self.entry_lines + self.body_lines + footer

    def _compile_statements(self, body: list[ast.stmt], expected_ret: str) -> bool:
        for stmt in body:
            if isinstance(stmt, ast.Return):
                if stmt.value is None:
                    raise CompileError("void return is not supported", stmt.lineno, stmt.col_offset)
                value, ty = self._compile_expr(stmt.value)
                self._require_type(ty, expected_ret, stmt, "return type mismatch")
                self.body_lines.append(f"  ret {TYPE_MAP[ty]} {value}")
                return True

            if isinstance(stmt, ast.Assign):
                if len(stmt.targets) != 1 or not isinstance(stmt.targets[0], ast.Name):
                    raise CompileError("only simple name assignment is supported", stmt.lineno, stmt.col_offset)
                name = stmt.targets[0].id
                value, ty = self._compile_expr(stmt.value)
                if name not in self.slot_types:
                    self._ensure_slot(name, ty)
                self._require_type(ty, self.slot_types[name], stmt, f"assignment type mismatch for '{name}'")
                self.body_lines.append(f"  store {TYPE_MAP[ty]} {value}, ptr %{name}.slot")
                continue

            if isinstance(stmt, ast.If):
                cond, cond_t = self._compile_expr(stmt.test)
                self._require_type(cond_t, "bool", stmt, "if condition must be bool")
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
                self._require_type(cond_t, "bool", stmt, "while condition must be bool")
                self.body_lines.append(f"  br i1 {cond}, label %{body_label}, label %{end_label}")
                self.body_lines.append(f"{body_label}:")
                body_terminated = self._compile_statements(stmt.body, expected_ret)
                if not body_terminated:
                    self.body_lines.append(f"  br label %{cond_label}")
                self.body_lines.append(f"{end_label}:")
                continue

            if isinstance(stmt, ast.Expr):
                if isinstance(stmt.value, ast.Call) and isinstance(stmt.value.func, ast.Name):
                    if stmt.value.func.id == "print":
                        self._compile_print(stmt.value)
                        continue
                raise CompileError(
                    "only print() calls are supported as expression statements",
                    stmt.lineno,
                    stmt.col_offset,
                )

            raise CompileError(
                f"unsupported statement {stmt.__class__.__name__} in LLVM mode",
                stmt.lineno,
                stmt.col_offset,
            )

        return False

    def _compile_print(self, node: ast.Call) -> None:
        """Lower a ``print(...)`` call to one or more ``printf`` invocations."""
        self.ctx.uses_printf = True
        args = node.args
        n = len(args)

        if n == 0:
            reg = self._new_reg()
            self.body_lines.append(f"  {reg} = call i32 (ptr, ...) @printf(ptr @__pyx_fmt_nl)")
            return

        for i, arg_node in enumerate(args):
            is_last = i == n - 1
            val, ty = self._compile_expr(arg_node)
            if ty not in _PRINT_FMT:
                raise CompileError(
                    f"print() argument has unsupported type '{ty}'",
                    getattr(arg_node, "lineno", None),
                    getattr(arg_node, "col_offset", None),
                )
            fmt_sp, fmt_nl = _PRINT_FMT[ty]
            fmt = fmt_nl if is_last else fmt_sp
            reg = self._new_reg()

            if ty == "bool":
                # Materialise "True" / "False" at runtime via select.
                str_reg = self._new_reg()
                self.body_lines.append(
                    f"  {str_reg} = select i1 {val}, ptr @__pyx_str_True, ptr @__pyx_str_False"
                )
                self.body_lines.append(
                    f"  {reg} = call i32 (ptr, ...) @printf(ptr {fmt}, ptr {str_reg})"
                )
            elif ty == "int":
                self.body_lines.append(
                    f"  {reg} = call i32 (ptr, ...) @printf(ptr {fmt}, i64 {val})"
                )
            elif ty == "float":
                self.body_lines.append(
                    f"  {reg} = call i32 (ptr, ...) @printf(ptr {fmt}, double {val})"
                )
            else:  # str
                self.body_lines.append(
                    f"  {reg} = call i32 (ptr, ...) @printf(ptr {fmt}, ptr {val})"
                )

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
                self.ctx.uses_printf = True  # str literals only appear via print paths
                return (gname, "str")

        if isinstance(node, ast.Name):
            if node.id not in self.slot_types:
                raise CompileError(f"unknown variable '{node.id}'", node.lineno, node.col_offset)
            ty = self.slot_types[node.id]
            reg = self._new_reg()
            self.body_lines.append(f"  {reg} = load {TYPE_MAP[ty]}, ptr %{node.id}.slot")
            return reg, ty

        if isinstance(node, ast.BinOp):
            left, l_t = self._compile_expr(node.left)
            right, r_t = self._compile_expr(node.right)
            self._require_type(l_t, r_t, node, "binary operands must have same type")
            reg = self._new_reg()
            if isinstance(node.op, ast.Add):
                op = "add" if l_t == "int" else "fadd"
            elif isinstance(node.op, ast.Sub):
                op = "sub" if l_t == "int" else "fsub"
            elif isinstance(node.op, ast.Mult):
                op = "mul" if l_t == "int" else "fmul"
            else:
                raise CompileError("only +, -, * are supported in LLVM mode", node.lineno, node.col_offset)

            if l_t not in {"int", "float"}:
                raise CompileError("arithmetic only supports int/float", node.lineno, node.col_offset)
            self.body_lines.append(f"  {reg} = {op} {TYPE_MAP[l_t]} {left}, {right}")
            return reg, l_t

        if isinstance(node, ast.Compare) and len(node.ops) == 1 and len(node.comparators) == 1:
            left, l_t = self._compile_expr(node.left)
            right, r_t = self._compile_expr(node.comparators[0])
            self._require_type(l_t, r_t, node, "comparison operands must have same type")
            op = node.ops[0]
            reg = self._new_reg()

            if l_t == "int":
                pred_map = {ast.Lt: "slt", ast.LtE: "sle", ast.Gt: "sgt", ast.GtE: "sge", ast.Eq: "eq"}
                pred = pred_map.get(type(op))
                if pred is None:
                    raise CompileError("comparison operator not supported", node.lineno, node.col_offset)
                self.body_lines.append(f"  {reg} = icmp {pred} i64 {left}, {right}")
                return reg, "bool"

            if l_t == "float":
                pred_map = {ast.Lt: "olt", ast.LtE: "ole", ast.Gt: "ogt", ast.GtE: "oge", ast.Eq: "oeq"}
                pred = pred_map.get(type(op))
                if pred is None:
                    raise CompileError("comparison operator not supported", node.lineno, node.col_offset)
                self.body_lines.append(f"  {reg} = fcmp {pred} double {left}, {right}")
                return reg, "bool"

            if l_t == "bool" and isinstance(op, ast.Eq):
                self.body_lines.append(f"  {reg} = icmp eq i1 {left}, {right}")
                return reg, "bool"

            raise CompileError("comparison type not supported", node.lineno, node.col_offset)

        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            callee = node.func.id
            if callee == "print":
                raise CompileError(
                    "'print()' is a statement; its return value cannot be used in an expression",
                    getattr(node, "lineno", None),
                    getattr(node, "col_offset", None),
                )
            if callee not in self.signatures:
                raise CompileError(f"call target '{callee}' is not a known function", node.lineno, node.col_offset)
            arg_types, ret_t = self.signatures[callee]
            if len(arg_types) != len(node.args):
                raise CompileError(f"call arg count mismatch for '{callee}'", node.lineno, node.col_offset)

            compiled_args: list[str] = []
            for arg_node, expected_t in zip(node.args, arg_types):
                val, got_t = self._compile_expr(arg_node)
                self._require_type(got_t, expected_t, node, f"call arg type mismatch for '{callee}'")
                compiled_args.append(f"{TYPE_MAP[expected_t]} {val}")

            reg = self._new_reg()
            self.body_lines.append(f"  {reg} = call {TYPE_MAP[ret_t]} @{callee}({', '.join(compiled_args)})")
            return reg, ret_t

        raise CompileError(
            f"unsupported expression {node.__class__.__name__} in LLVM mode",
            getattr(node, "lineno", None),
            getattr(node, "col_offset", None),
        )

    def _annotation_to_type(self, annotation: ast.AST | None, node: ast.AST, label: str) -> str:
        if annotation is None:
            raise CompileError(f"{label} must be annotated", node.lineno, node.col_offset)
        ty = ast.unparse(annotation)
        if ty not in TYPE_MAP:
            raise CompileError(f"{label} must be one of {', '.join(TYPE_MAP)}", node.lineno, node.col_offset)
        return ty

    def _ensure_slot(self, name: str, ty: str) -> None:
        if name in self.slot_types:
            return
        self.slot_types[name] = ty
        self.entry_lines.append(f"  %{name}.slot = alloca {TYPE_MAP[ty]}")

    def _require_type(self, got: str, expected: str, node: ast.AST, msg: str) -> None:
        if got != expected:
            raise CompileError(f"{msg}: expected {expected}, got {got}", node.lineno, node.col_offset)

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
            raise CompileError("no top-level functions found to compile")

        signatures: dict[str, tuple[list[str], str]] = {}
        for fn in functions:
            arg_types = [self._annotation_to_type(a.annotation, a, f"parameter '{a.arg}'") for a in fn.args.args]
            ret_t = self._annotation_to_type(fn.returns, fn, "return type")
            signatures[fn.name] = (arg_types, ret_t)

        ctx = _ModuleContext()
        function_chunks: list[list[str]] = [
            _FunctionCompiler(fn, signatures, ctx).compile() for fn in functions
        ]

        chunks = ["; ModuleID = 'pyx'", "source_filename = \"pyx\"", ""]
        chunks.extend(ctx.emit_preamble())
        for fn_lines in function_chunks:
            chunks.extend(fn_lines)
            chunks.append("")
        return "\n".join(chunks).strip() + "\n"

    def _annotation_to_type(self, annotation: ast.AST | None, node: ast.AST, label: str) -> str:
        if annotation is None:
            raise CompileError(f"{label} must be annotated", node.lineno, node.col_offset)
        ty = ast.unparse(annotation)
        if ty not in TYPE_MAP:
            raise CompileError(f"{label} must be one of {', '.join(TYPE_MAP)}", node.lineno, node.col_offset)
        return ty


def emit_native_object(ll_path: Path, output_path: Path) -> bool:
    clang = shutil.which("clang")
    if clang is None:
        return False
    subprocess.run([clang, "-c", str(ll_path), "-O2", "-o", str(output_path)], check=True)
    return True
