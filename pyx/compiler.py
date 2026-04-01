from __future__ import annotations

import ast
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


TYPE_MAP = {"int": "i64", "float": "double", "bool": "i1"}


def _is_dunder_main_test(test: ast.AST) -> bool:
    """Return True if *test* is the ``__name__ == "__main__"`` compile-time constant."""
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "__main__"
    )


@dataclass
class CompileError(Exception):
    message: str
    line: int | None = None
    col: int | None = None

    def __str__(self) -> str:
        if self.line is None:
            return self.message
        return f"line {self.line}:{self.col or 0}: {self.message}"


class _BaseCompiler:
    """Shared LLVM IR compilation logic for function bodies and module-level code."""

    def __init__(
        self,
        signatures: dict[str, tuple[list[str], str]],
        name_remap: dict[str, str] | None = None,
    ) -> None:
        self.signatures = signatures
        self.name_remap: dict[str, str] = name_remap or {}
        self.entry_lines: list[str] = []
        self.body_lines: list[str] = []
        self.reg_idx = 0
        self.label_idx = 0
        self.slot_types: dict[str, str] = {}

    def _compile_statements(self, body: list[ast.stmt], expected_ret: str | None) -> bool:
        for stmt in body:
            if isinstance(stmt, ast.Return):
                if expected_ret is None:
                    raise CompileError("return is not allowed at module level", stmt.lineno, stmt.col_offset)
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

            if isinstance(stmt, ast.Expr):
                # Expression statement: compile and discard the result.
                self._compile_expr(stmt.value)
                continue

            if isinstance(stmt, ast.If):
                # __name__ == "__main__" is always True at compile time: inline the body.
                if _is_dunder_main_test(stmt.test):
                    terminated = self._compile_statements(stmt.body, expected_ret)
                    if terminated:
                        return True
                    continue

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

            raise CompileError(
                f"unsupported statement {stmt.__class__.__name__} in LLVM mode",
                stmt.lineno,
                stmt.col_offset,
            )

        return False

    def _compile_expr(self, node: ast.AST) -> tuple[str, str]:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool):
                return ("1" if node.value else "0", "bool")
            if isinstance(node.value, int):
                return (str(node.value), "int")
            if isinstance(node.value, float):
                return (str(node.value), "float")

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

            callee_ir = self.name_remap.get(callee, callee)
            reg = self._new_reg()
            self.body_lines.append(f"  {reg} = call {TYPE_MAP[ret_t]} @{callee_ir}({', '.join(compiled_args)})")
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


class _FunctionCompiler(_BaseCompiler):
    def __init__(
        self,
        node: ast.FunctionDef,
        signatures: dict[str, tuple[list[str], str]],
        name_remap: dict[str, str] | None = None,
    ) -> None:
        super().__init__(signatures, name_remap)
        self.node = node

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

        fn_name = self.name_remap.get(self.node.name, self.node.name)
        header = [f"define {TYPE_MAP[ret_t]} @{fn_name}({', '.join(arg_sig)}) {{", "entry:"]
        terminated = self._compile_statements(self.node.body, ret_t)
        if not terminated:
            raise CompileError(
                f"Function '{self.node.name}' must explicitly return on all paths for LLVM lowering",
                self.node.lineno,
                self.node.col_offset,
            )

        footer = ["}"]
        return header + self.entry_lines + self.body_lines + footer


class _EntryPointCompiler(_BaseCompiler):
    """Compiles module-level statements into a C ``define i32 @main()`` entry point."""

    def compile(self, stmts: list[ast.stmt]) -> list[str]:
        self._compile_statements(stmts, expected_ret=None)
        return (
            ["define i32 @main() {", "entry:"]
            + self.entry_lines
            + self.body_lines
            + ["  ret i32 0", "}"]
        )


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

        module_stmts = [n for n in self.tree.body if not isinstance(n, ast.FunctionDef)]

        # If we will emit a C 'main' entry point and a PyX function is also named
        # 'main', rename the PyX one to 'pyx_main' to avoid an LLVM symbol clash.
        name_remap: dict[str, str] = {}
        if module_stmts and any(fn.name == "main" for fn in functions):
            name_remap = {"main": "pyx_main"}

        signatures: dict[str, tuple[list[str], str]] = {}
        for fn in functions:
            arg_types = [self._annotation_to_type(a.annotation, a, f"parameter '{a.arg}'") for a in fn.args.args]
            ret_t = self._annotation_to_type(fn.returns, fn, "return type")
            signatures[fn.name] = (arg_types, ret_t)

        chunks = ["; ModuleID = 'pyx'", "source_filename = \"pyx\"", ""]
        for fn in functions:
            chunks.extend(_FunctionCompiler(fn, signatures, name_remap).compile())
            chunks.append("")

        if module_stmts:
            chunks.extend(_EntryPointCompiler(signatures, name_remap).compile(module_stmts))
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
