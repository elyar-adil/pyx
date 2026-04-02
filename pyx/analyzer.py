from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


@dataclass
class AnalysisError:
    line: int
    col: int
    message: str


class Analyzer(ast.NodeVisitor):
    """Minimal static-subset analyzer for PyX."""

    def __init__(self) -> None:
        self.errors: list[AnalysisError] = []
        self.var_types: dict[str, str] = {}
        self.function_returns: list[str] = []

    def analyze_path(self, file_path: str | Path) -> list[AnalysisError]:
        path = Path(file_path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        self.visit(tree)
        return self.errors

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        for arg in node.args.args:
            if arg.annotation is None:
                self._error(arg, f"Function parameter '{arg.arg}' requires type annotation")
        if node.returns is None:
            self._error(node, f"Function '{node.name}' requires return annotation")
            expected_return = "Any"
        else:
            expected_return = self._annotation_to_str(node.returns)

        self.function_returns.append(expected_return)
        previous = self.var_types.copy()
        for arg in node.args.args:
            if arg.annotation is not None:
                self.var_types[arg.arg] = self._annotation_to_str(arg.annotation)

        for stmt in node.body:
            self.visit(stmt)

        self.var_types = previous
        self.function_returns.pop()

    def visit_Assign(self, node: ast.Assign) -> None:
        inferred = self._infer_expr_type(node.value)
        for target in node.targets:
            if isinstance(target, ast.Name):
                name = target.id
                if name in self.var_types and self.var_types[name] != inferred:
                    self._error(
                        node,
                        f"Variable '{name}' cannot change type from {self.var_types[name]} to {inferred}",
                    )
                else:
                    self.var_types[name] = inferred
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name):
            annotated = self._annotation_to_str(node.annotation)
            if node.value is not None:
                inferred = self._infer_expr_type(node.value)
                if annotated != inferred and inferred != "Any":
                    self._error(
                        node,
                        f"Annotated variable '{node.target.id}' expects {annotated}, got {inferred}",
                    )
            self.var_types[node.target.id] = annotated
        self.generic_visit(node)

    _PRINTABLE_TYPES: frozenset[str] = frozenset({"int", "float", "bool", "str"})

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name):
            name = node.func.id
            if name in {"getattr", "setattr", "delattr"}:
                self._error(node, f"'{name}' is not allowed in statically compilable subset")
            elif name == "print":
                for i, arg in enumerate(node.args):
                    arg_type = self._infer_expr_type(arg)
                    if arg_type not in self._PRINTABLE_TYPES and arg_type != "Any":
                        self._error(
                            arg,
                            f"print() argument {i + 1} has unsupported type '{arg_type}';"
                            f" expected one of: {', '.join(sorted(self._PRINTABLE_TYPES))}",
                        )
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:
        if self.function_returns:
            expected = self.function_returns[-1]
            got = self._infer_expr_type(node.value) if node.value else "None"
            if expected != "Any" and got != "Any" and expected != got:
                self._error(node, f"Return type mismatch: expected {expected}, got {got}")
        self.generic_visit(node)

    def _annotation_to_str(self, node: ast.AST) -> str:
        return ast.unparse(node)

    def _infer_expr_type(self, node: ast.AST | None) -> str:
        if node is None:
            return "None"
        if isinstance(node, ast.Constant):
            value = node.value
            if isinstance(value, bool):
                return "bool"
            if isinstance(value, int):
                return "int"
            if isinstance(value, float):
                return "float"
            if isinstance(value, str):
                return "str"
            if value is None:
                return "None"
            return "Any"
        if isinstance(node, ast.Name):
            return self.var_types.get(node.id, "Any")
        if isinstance(node, ast.List):
            if not node.elts:
                return "list[Any]"
            subtype = self._infer_expr_type(node.elts[0])
            return f"list[{subtype}]"
        if isinstance(node, ast.Set):
            if not node.elts:
                return "set[Any]"
            subtype = self._infer_expr_type(node.elts[0])
            return f"set[{subtype}]"
        if isinstance(node, ast.Dict):
            if not node.keys:
                return "dict[Any,Any]"
            key_t = self._infer_expr_type(node.keys[0])
            val_t = self._infer_expr_type(node.values[0])
            return f"dict[{key_t},{val_t}]"
        if isinstance(node, ast.BinOp):
            left = self._infer_expr_type(node.left)
            right = self._infer_expr_type(node.right)
            if left == right:
                return left
            if {left, right} == {"int", "float"}:
                return "float"
            return "Any"
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "print":
                return "None"
            return "Any"
        return "Any"

    def _error(self, node: ast.AST, message: str) -> None:
        self.errors.append(AnalysisError(node.lineno, node.col_offset, message))
