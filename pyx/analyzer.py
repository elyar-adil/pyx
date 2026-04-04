from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from .type_system import can_assign_type, is_numeric_type, merge_numeric_result_type

_ERR_PARAM_ANNOTATION = "PYX1001"
_ERR_RETURN_ANNOTATION = "PYX1002"
_ERR_VARIABLE_TYPE_CHANGE = "PYX1003"
_ERR_ANNOTATED_ASSIGN = "PYX1004"
_ERR_REFLECTION = "PYX1005"
_ERR_PRINT_TYPE = "PYX1006"
_ERR_RETURN_MISMATCH = "PYX1007"
_ERR_UNKNOWN_FUNCTION = "PYX1009"
_ERR_CALL_ARG_COUNT = "PYX1010"
_ERR_CALL_ARG_TYPE = "PYX1011"


@dataclass(eq=True, frozen=True)
class AnalysisError:
    code: str
    message: str
    line: int
    col: int


class Analyzer(ast.NodeVisitor):
    """Minimal static-subset analyzer for PyX."""

    _PRINTABLE_TYPES: frozenset[str] = frozenset({"int", "float", "bool", "str"})

    def __init__(self) -> None:
        self.errors: list[AnalysisError] = []
        self.var_types: dict[str, str] = {}
        self.function_returns: list[str] = []
        self.function_signatures: dict[str, tuple[list[str], str]] = {}

    def analyze_path(self, file_path: str | Path) -> list[AnalysisError]:
        path = Path(file_path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        self.errors = []
        self.var_types = {}
        self.function_returns = []
        self.function_signatures = self._collect_function_signatures(tree)
        self.visit(tree)
        return self.errors

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        for arg in node.args.args:
            if arg.annotation is None:
                self._error(arg, _ERR_PARAM_ANNOTATION, f"Function parameter '{arg.arg}' requires type annotation")
        if node.returns is None:
            self._error(node, _ERR_RETURN_ANNOTATION, f"Function '{node.name}' requires return annotation")
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
                if name in self.var_types and not can_assign_type(inferred, self.var_types[name]):
                    self._error(
                        node,
                        _ERR_VARIABLE_TYPE_CHANGE,
                        f"Variable '{name}' cannot change type from {self.var_types[name]} to {inferred}",
                    )
                elif name not in self.var_types:
                    self.var_types[name] = inferred
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name):
            annotated = self._annotation_to_str(node.annotation)
            if node.value is not None:
                inferred = self._infer_expr_type(node.value)
                if not can_assign_type(inferred, annotated):
                    self._error(
                        node,
                        _ERR_ANNOTATED_ASSIGN,
                        f"Annotated variable '{node.target.id}' expects {annotated}, got {inferred}",
                    )
            self.var_types[node.target.id] = annotated
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name):
            name = node.func.id
            if name in {"getattr", "setattr", "delattr"}:
                self._error(node, _ERR_REFLECTION, f"'{name}' is not allowed in statically compilable subset")
            elif name == "print":
                for i, arg in enumerate(node.args):
                    arg_type = self._infer_expr_type(arg)
                    if arg_type not in self._PRINTABLE_TYPES and arg_type != "Any":
                        self._error(
                            arg,
                            _ERR_PRINT_TYPE,
                            f"print() argument {i + 1} has unsupported type '{arg_type}';"
                            f" expected one of: {', '.join(sorted(self._PRINTABLE_TYPES))}",
                        )
            elif name in self.function_signatures:
                expected_args, _ = self.function_signatures[name]
                if len(node.args) != len(expected_args):
                    self._error(
                        node,
                        _ERR_CALL_ARG_COUNT,
                        f"Call to '{name}' expects {len(expected_args)} arguments, got {len(node.args)}",
                    )
                for index, (arg_node, expected_t) in enumerate(zip(node.args, expected_args), start=1):
                    got_t = self._infer_expr_type(arg_node)
                    if not can_assign_type(got_t, expected_t):
                        self._error(
                            arg_node,
                            _ERR_CALL_ARG_TYPE,
                            f"Call to '{name}' argument {index} expects {expected_t}, got {got_t}",
                        )
            else:
                self._error(node, _ERR_UNKNOWN_FUNCTION, f"Call target '{name}' is not a known function")
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:
        if self.function_returns:
            expected = self.function_returns[-1]
            got = self._infer_expr_type(node.value) if node.value else "None"
            if expected != "Any" and got != "Any" and not can_assign_type(got, expected):
                self._error(node, _ERR_RETURN_MISMATCH, f"Return type mismatch: expected {expected}, got {got}")
        self.generic_visit(node)

    def _annotation_to_str(self, node: ast.AST) -> str:
        rendered = ast.unparse(node)
        compact = rendered.replace(" ", "")
        if compact in {"int|float", "float|int"}:
            return "int | float"
        return rendered

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
            merged = merge_numeric_result_type(left, right)
            if merged is not None:
                return merged
            return "Any"
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and len(node.comparators) == 1:
            left = self._infer_expr_type(node.left)
            right = self._infer_expr_type(node.comparators[0])
            if left == right:
                return "bool"
            if is_numeric_type(left) and is_numeric_type(right):
                return "bool"
            return "Any"
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "print":
                return "None"
            if isinstance(node.func, ast.Name) and node.func.id in self.function_signatures:
                return self.function_signatures[node.func.id][1]
            return "Any"
        return "Any"

    def _collect_function_signatures(self, tree: ast.Module) -> dict[str, tuple[list[str], str]]:
        signatures: dict[str, tuple[list[str], str]] = {}
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            arg_types: list[str] = []
            for arg in node.args.args:
                if arg.annotation is None:
                    arg_types.append("Any")
                else:
                    arg_types.append(self._annotation_to_str(arg.annotation))
            ret_t = "Any" if node.returns is None else self._annotation_to_str(node.returns)
            signatures[node.name] = (arg_types, ret_t)
        return signatures

    def _error(self, node: ast.AST, code: str, message: str) -> None:
        self.errors.append(AnalysisError(code=code, message=message, line=node.lineno, col=node.col_offset))
