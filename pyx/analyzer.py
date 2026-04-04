from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from .project import ClassInfo, FunctionSignature, ModuleInfo, ProjectInfo, ProjectLoadError, load_project
from .type_system import (
    can_assign_type,
    is_numeric_type,
    is_supported_type,
    merge_numeric_result_type,
    normalize_type_name,
    parse_dict_type,
    parse_list_type,
    parse_set_type,
)

_ERR_PARAM_ANNOTATION = "PYX1001"
_ERR_RETURN_ANNOTATION = "PYX1002"
_ERR_VARIABLE_TYPE_CHANGE = "PYX1003"
_ERR_ANNOTATED_ASSIGN = "PYX1004"
_ERR_REFLECTION = "PYX1005"
_ERR_PRINT_TYPE = "PYX1006"
_ERR_RETURN_MISMATCH = "PYX1007"
_ERR_UNKNOWN_SYMBOL = "PYX1009"
_ERR_CALL_ARG_COUNT = "PYX1010"
_ERR_CALL_ARG_TYPE = "PYX1011"
_ERR_IMPORT = "PYX1012"
_ERR_UNKNOWN_FIELD = "PYX1013"
_ERR_UNSUPPORTED = "PYX1014"
_ERR_UNKNOWN_TYPE = "PYX1015"


@dataclass(eq=True, frozen=True)
class AnalysisError:
    code: str
    message: str
    line: int | None
    col: int | None


@dataclass
class _FunctionContext:
    module: ModuleInfo
    signature: FunctionSignature
    locals: dict[str, str]


@dataclass(frozen=True)
class _CallableTarget:
    arg_types: tuple[str, ...]
    return_type: str
    display_name: str


class Analyzer:
    """Static analyzer for the PyX Phase 3 subset."""

    _PRINTABLE_TYPES: frozenset[str] = frozenset({"int", "float", "bool", "str"})

    def __init__(self) -> None:
        self.errors: list[AnalysisError] = []
        self.project: ProjectInfo | None = None

    def analyze_path(self, file_path: str | Path) -> list[AnalysisError]:
        self.errors = []
        try:
            self.project = load_project(file_path)
        except ProjectLoadError as exc:
            self.errors.append(AnalysisError(code=_ERR_IMPORT, message=str(exc), line=None, col=None))
            return self.errors

        assert self.project is not None
        for module in self.project.modules.values():
            self._analyze_module(module)
        return self.errors

    def _analyze_module(self, module: ModuleInfo) -> None:
        for _, (fn_node, signature) in module.functions.items():
            self._analyze_function(module, fn_node, signature)

        for _, (class_node, class_info) in module.classes.items():
            self._analyze_class(module, class_node, class_info)

    def _analyze_class(self, module: ModuleInfo, node: ast.ClassDef, class_info: ClassInfo) -> None:
        if class_info.is_dataclass and not class_info.field_names:
            self._error(node, _ERR_UNSUPPORTED, f"Dataclass '{class_info.name}' must declare annotated fields")

        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign):
                if not isinstance(stmt.target, ast.Name):
                    self._error(stmt, _ERR_UNSUPPORTED, "Only simple annotated class fields are supported")
                    continue
                field_t = class_info.fields[stmt.target.id]
                if not is_supported_type(field_t, self.project.known_type_names()):
                    self._error(stmt, _ERR_UNKNOWN_TYPE, f"Unknown field type '{field_t}' in class '{class_info.name}'")
                if stmt.value is not None:
                    inferred = self._infer_expr_type(stmt.value, _FunctionContext(module, FunctionSignature(module.name, "__class_init__", (), (), "None"), {}))
                    if not can_assign_type(inferred, field_t):
                        self._error(
                            stmt,
                            _ERR_ANNOTATED_ASSIGN,
                            f"Annotated field '{stmt.target.id}' expects {field_t}, got {inferred}",
                        )
            elif isinstance(stmt, ast.FunctionDef):
                signature = class_info.methods[stmt.name]
                self._analyze_function(module, stmt, signature)
            else:
                self._error(stmt, _ERR_UNSUPPORTED, f"Unsupported class statement '{stmt.__class__.__name__}'")

    def _analyze_function(self, module: ModuleInfo, node: ast.FunctionDef, signature: FunctionSignature) -> None:
        local_types: dict[str, str] = {}
        known_types = self.project.known_type_names()

        for index, arg in enumerate(node.args.args):
            expected = signature.arg_types[index]
            if signature.class_name is not None and index == 0 and arg.arg == "self" and arg.annotation is None:
                local_types[arg.arg] = expected
                continue
            if arg.annotation is None:
                self._error(arg, _ERR_PARAM_ANNOTATION, f"Function parameter '{arg.arg}' requires type annotation")
                local_types[arg.arg] = "Any"
            else:
                rendered = signature.arg_types[index]
                if not is_supported_type(rendered, known_types):
                    self._error(arg, _ERR_UNKNOWN_TYPE, f"Unknown parameter type '{rendered}'")
                local_types[arg.arg] = rendered

        if node.returns is None:
            self._error(node, _ERR_RETURN_ANNOTATION, f"Function '{node.name}' requires return annotation")
        elif not is_supported_type(signature.return_type, known_types):
            self._error(node, _ERR_UNKNOWN_TYPE, f"Unknown return type '{signature.return_type}'")

        ctx = _FunctionContext(module=module, signature=signature, locals=local_types)
        self._check_block(node.body, ctx)

    def _check_block(self, body: list[ast.stmt], ctx: _FunctionContext) -> None:
        for stmt in body:
            self._check_stmt(stmt, ctx)

    def _check_stmt(self, stmt: ast.stmt, ctx: _FunctionContext) -> None:
        if isinstance(stmt, ast.Return):
            got = self._infer_expr_type(stmt.value, ctx) if stmt.value is not None else "None"
            expected = ctx.signature.return_type
            if expected != "Any" and got != "Any" and not can_assign_type(got, expected):
                self._error(stmt, _ERR_RETURN_MISMATCH, f"Return type mismatch: expected {expected}, got {got}")
            return

        if isinstance(stmt, ast.Assign):
            inferred = self._infer_expr_type(stmt.value, ctx)
            for target in stmt.targets:
                self._assign_target(target, inferred, ctx, stmt)
            return

        if isinstance(stmt, ast.AnnAssign):
            if not isinstance(stmt.target, ast.Name):
                self._error(stmt, _ERR_UNSUPPORTED, "Only simple annotated assignments are supported")
                return
            annotated = self._render_annotation(stmt.annotation, ctx.module)
            if not is_supported_type(annotated, self.project.known_type_names()):
                self._error(stmt, _ERR_UNKNOWN_TYPE, f"Unknown annotation '{annotated}'")
            if stmt.value is not None:
                inferred = self._infer_expr_type(stmt.value, ctx)
                if not can_assign_type(inferred, annotated):
                    self._error(
                        stmt,
                        _ERR_ANNOTATED_ASSIGN,
                        f"Annotated variable '{stmt.target.id}' expects {annotated}, got {inferred}",
                    )
            ctx.locals[stmt.target.id] = annotated
            return

        if isinstance(stmt, ast.Expr):
            self._infer_expr_type(stmt.value, ctx)
            return

        if isinstance(stmt, ast.If):
            test_t = self._infer_expr_type(stmt.test, ctx)
            if not can_assign_type(test_t, "bool"):
                self._error(stmt.test, _ERR_CALL_ARG_TYPE, f"if condition expects bool, got {test_t}")
            before = ctx.locals.copy()
            then_ctx = _FunctionContext(ctx.module, ctx.signature, before.copy())
            else_ctx = _FunctionContext(ctx.module, ctx.signature, before.copy())
            self._check_block(stmt.body, then_ctx)
            self._check_block(stmt.orelse, else_ctx)
            ctx.locals = self._merge_branch_locals(before, then_ctx.locals, else_ctx.locals)
            return

        if isinstance(stmt, ast.While):
            test_t = self._infer_expr_type(stmt.test, ctx)
            if not can_assign_type(test_t, "bool"):
                self._error(stmt.test, _ERR_CALL_ARG_TYPE, f"while condition expects bool, got {test_t}")
            loop_ctx = _FunctionContext(ctx.module, ctx.signature, ctx.locals.copy())
            self._check_block(stmt.body, loop_ctx)
            return

        self._error(stmt, _ERR_UNSUPPORTED, f"Unsupported statement '{stmt.__class__.__name__}'")

    def _merge_branch_locals(
        self,
        before: dict[str, str],
        then_locals: dict[str, str],
        else_locals: dict[str, str],
    ) -> dict[str, str]:
        merged = before.copy()
        for name in set(then_locals) | set(else_locals):
            if name in before:
                if name in then_locals and can_assign_type(then_locals[name], before[name]):
                    merged[name] = before[name]
                elif name in else_locals and can_assign_type(else_locals[name], before[name]):
                    merged[name] = before[name]
                continue
            then_t = then_locals.get(name)
            else_t = else_locals.get(name)
            if then_t is not None and else_t is not None and then_t == else_t:
                merged[name] = then_t
        return merged

    def _assign_target(self, target: ast.expr, inferred: str, ctx: _FunctionContext, node: ast.AST) -> None:
        if isinstance(target, ast.Name):
            name = target.id
            current = ctx.locals.get(name)
            if current is not None and not can_assign_type(inferred, current):
                self._error(
                    node,
                    _ERR_VARIABLE_TYPE_CHANGE,
                    f"Variable '{name}' cannot change type from {current} to {inferred}",
                )
            elif current is None:
                ctx.locals[name] = inferred
            return

        if isinstance(target, ast.Attribute):
            owner_t = self._infer_expr_type(target.value, ctx)
            class_info = self.project.lookup_class(normalize_type_name(owner_t))
            if class_info is None:
                self._error(target, _ERR_UNKNOWN_FIELD, f"Type '{owner_t}' has no field '{target.attr}'")
                return
            field_t = class_info.fields.get(target.attr)
            if field_t is None:
                self._error(target, _ERR_UNKNOWN_FIELD, f"Class '{class_info.name}' has no field '{target.attr}'")
                return
            if not can_assign_type(inferred, field_t):
                self._error(target, _ERR_CALL_ARG_TYPE, f"Field '{target.attr}' expects {field_t}, got {inferred}")
            return

        if isinstance(target, ast.Subscript):
            container_t = self._infer_expr_type(target.value, ctx)
            list_item = parse_list_type(container_t)
            if list_item is not None:
                index_t = self._infer_expr_type(target.slice, ctx)
                if index_t != "int":
                    self._error(target.slice, _ERR_CALL_ARG_TYPE, f"List index expects int, got {index_t}")
                if not can_assign_type(inferred, list_item):
                    self._error(target, _ERR_CALL_ARG_TYPE, f"List item expects {list_item}, got {inferred}")
                return
            dict_types = parse_dict_type(container_t)
            if dict_types is not None:
                key_t, value_t = dict_types
                got_key = self._infer_expr_type(target.slice, ctx)
                if not can_assign_type(got_key, key_t):
                    self._error(target.slice, _ERR_CALL_ARG_TYPE, f"Dict key expects {key_t}, got {got_key}")
                if not can_assign_type(inferred, value_t):
                    self._error(target, _ERR_CALL_ARG_TYPE, f"Dict value expects {value_t}, got {inferred}")
                return

        self._error(target, _ERR_UNSUPPORTED, "Unsupported assignment target")

    def _infer_expr_type(self, node: ast.AST | None, ctx: _FunctionContext) -> str:
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
            if isinstance(value, bytes):
                return "bytes"
            if value is None:
                return "None"
            return "Any"

        if isinstance(node, ast.Name):
            if node.id in ctx.locals:
                return ctx.locals[node.id]
            if node.id in ctx.module.functions:
                return f"fn:{ctx.module.name}.{node.id}"
            if node.id in ctx.module.classes:
                _, class_info = ctx.module.classes[node.id]
                return class_info.qualified_name
            imported = ctx.module.imported_symbols.get(node.id)
            if imported is not None:
                target_module = self.project.lookup_module(imported.module_name)
                assert target_module is not None
                if imported.symbol_name in target_module.classes:
                    return target_module.classes[imported.symbol_name][1].qualified_name
                if imported.symbol_name in target_module.functions:
                    return f"fn:{imported.module_name}.{imported.symbol_name}"
            if node.id in {"True", "False"}:
                return "bool"
            self._error(node, _ERR_UNKNOWN_SYMBOL, f"Unknown symbol '{node.id}'")
            return "Any"

        if isinstance(node, ast.List):
            if not node.elts:
                return "list[Any]"
            item_t = self._infer_expr_type(node.elts[0], ctx)
            for elt in node.elts[1:]:
                item_t = self._merge_collection_item_type(item_t, self._infer_expr_type(elt, ctx))
            return f"list[{item_t}]"

        if isinstance(node, ast.Set):
            if not node.elts:
                return "set[Any]"
            item_t = self._infer_expr_type(node.elts[0], ctx)
            for elt in node.elts[1:]:
                item_t = self._merge_collection_item_type(item_t, self._infer_expr_type(elt, ctx))
            return f"set[{item_t}]"

        if isinstance(node, ast.Dict):
            if not node.keys:
                return "dict[Any,Any]"
            key_t = self._infer_expr_type(node.keys[0], ctx)
            value_t = self._infer_expr_type(node.values[0], ctx)
            for key_node, value_node in zip(node.keys[1:], node.values[1:], strict=True):
                key_t = self._merge_collection_item_type(key_t, self._infer_expr_type(key_node, ctx))
                value_t = self._merge_collection_item_type(value_t, self._infer_expr_type(value_node, ctx))
            return f"dict[{key_t},{value_t}]"

        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name):
                imported_module = ctx.module.imported_modules.get(node.value.id)
                if imported_module is not None:
                    target_module = self.project.lookup_module(imported_module.module_name)
                    assert target_module is not None
                    if node.attr in target_module.classes:
                        return target_module.classes[node.attr][1].qualified_name
                    if node.attr in target_module.functions:
                        return f"fn:{target_module.name}.{node.attr}"
            owner_t = self._infer_expr_type(node.value, ctx)
            class_info = self.project.lookup_class(owner_t)
            if class_info is None:
                self._error(node, _ERR_UNKNOWN_FIELD, f"Type '{owner_t}' has no attribute '{node.attr}'")
                return "Any"
            if node.attr in class_info.fields:
                return class_info.fields[node.attr]
            if node.attr in class_info.methods:
                method = class_info.methods[node.attr]
                return f"fn:{method.qualified_name}"
            self._error(node, _ERR_UNKNOWN_FIELD, f"Class '{class_info.name}' has no attribute '{node.attr}'")
            return "Any"

        if isinstance(node, ast.Subscript):
            container_t = self._infer_expr_type(node.value, ctx)
            list_item = parse_list_type(container_t)
            if list_item is not None:
                index_t = self._infer_expr_type(node.slice, ctx)
                if index_t != "int":
                    self._error(node.slice, _ERR_CALL_ARG_TYPE, f"List index expects int, got {index_t}")
                return list_item
            dict_types = parse_dict_type(container_t)
            if dict_types is not None:
                key_t, value_t = dict_types
                index_t = self._infer_expr_type(node.slice, ctx)
                if not can_assign_type(index_t, key_t):
                    self._error(node.slice, _ERR_CALL_ARG_TYPE, f"Dict key expects {key_t}, got {index_t}")
                return value_t
            if container_t == "str":
                index_t = self._infer_expr_type(node.slice, ctx)
                if index_t != "int":
                    self._error(node.slice, _ERR_CALL_ARG_TYPE, f"String index expects int, got {index_t}")
                return "str"
            self._error(node, _ERR_UNSUPPORTED, f"Subscript is not supported for '{container_t}'")
            return "Any"

        if isinstance(node, ast.BinOp):
            left = self._infer_expr_type(node.left, ctx)
            right = self._infer_expr_type(node.right, ctx)
            if isinstance(node.op, ast.Add) and left == right == "str":
                return "str"
            merged = merge_numeric_result_type(left, right)
            if merged is not None:
                return merged
            return "Any"

        if isinstance(node, ast.Compare) and len(node.ops) == 1 and len(node.comparators) == 1:
            left = self._infer_expr_type(node.left, ctx)
            right = self._infer_expr_type(node.comparators[0], ctx)
            if left == right or (is_numeric_type(left) and is_numeric_type(right)):
                return "bool"
            return "Any"

        if isinstance(node, ast.Call):
            return self._infer_call_type(node, ctx)

        self._error(node, _ERR_UNSUPPORTED, f"Unsupported expression '{node.__class__.__name__}'")
        return "Any"

    def _infer_call_type(self, node: ast.Call, ctx: _FunctionContext) -> str:
        if isinstance(node.func, ast.Name):
            name = node.func.id
            if name in {"getattr", "setattr", "delattr"}:
                self._error(node, _ERR_REFLECTION, f"'{name}' is not allowed in statically compilable subset")
                return "Any"
            if name == "print":
                for index, arg in enumerate(node.args, start=1):
                    arg_t = self._infer_expr_type(arg, ctx)
                    if arg_t not in self._PRINTABLE_TYPES and arg_t != "Any":
                        self._error(
                            arg,
                            _ERR_PRINT_TYPE,
                            f"print() argument {index} has unsupported type '{arg_t}';"
                            f" expected one of: {', '.join(sorted(self._PRINTABLE_TYPES))}",
                        )
                return "None"
            if name == "len":
                if len(node.args) != 1:
                    self._error(node, _ERR_CALL_ARG_COUNT, f"Call to 'len' expects 1 argument, got {len(node.args)}")
                    return "int"
                arg_t = self._infer_expr_type(node.args[0], ctx)
                if arg_t == "str" or arg_t == "bytes" or parse_list_type(arg_t) is not None or parse_dict_type(arg_t) is not None:
                    return "int"
                self._error(node.args[0], _ERR_CALL_ARG_TYPE, f"len() does not support '{arg_t}'")
                return "int"

        target = self._resolve_callable(node.func, ctx)
        if target is None:
            return "Any"
        if len(node.args) != len(target.arg_types):
            self._error(
                node,
                _ERR_CALL_ARG_COUNT,
                f"Call to '{target.display_name}' expects {len(target.arg_types)} arguments, got {len(node.args)}",
            )
        for index, (arg_node, expected_t) in enumerate(zip(node.args, target.arg_types), start=1):
            got_t = self._infer_expr_type(arg_node, ctx)
            if not can_assign_type(got_t, expected_t):
                self._error(
                    arg_node,
                    _ERR_CALL_ARG_TYPE,
                    f"Call to '{target.display_name}' argument {index} expects {expected_t}, got {got_t}",
                )
        return target.return_type

    def _resolve_callable(self, func: ast.AST, ctx: _FunctionContext) -> _CallableTarget | None:
        if isinstance(func, ast.Name):
            if func.id in ctx.module.functions:
                signature = ctx.module.functions[func.id][1]
                return _CallableTarget(signature.arg_types, signature.return_type, func.id)
            if func.id in ctx.module.classes:
                class_info = ctx.module.classes[func.id][1]
                return _CallableTarget(class_info.field_types, class_info.qualified_name, func.id)
            imported = ctx.module.imported_symbols.get(func.id)
            if imported is not None:
                target_module = self.project.lookup_module(imported.module_name)
                assert target_module is not None
                if imported.symbol_name in target_module.functions:
                    signature = target_module.functions[imported.symbol_name][1]
                    return _CallableTarget(signature.arg_types, signature.return_type, func.id)
                if imported.symbol_name in target_module.classes:
                    class_info = target_module.classes[imported.symbol_name][1]
                    return _CallableTarget(class_info.field_types, class_info.qualified_name, func.id)
            self._error(func, _ERR_UNKNOWN_SYMBOL, f"Call target '{func.id}' is not a known function")
            return None

        if isinstance(func, ast.Attribute):
            if isinstance(func.value, ast.Name):
                imported_module = ctx.module.imported_modules.get(func.value.id)
                if imported_module is not None:
                    target_module = self.project.lookup_module(imported_module.module_name)
                    assert target_module is not None
                    if func.attr in target_module.functions:
                        signature = target_module.functions[func.attr][1]
                        return _CallableTarget(signature.arg_types, signature.return_type, f"{func.value.id}.{func.attr}")
                    if func.attr in target_module.classes:
                        class_info = target_module.classes[func.attr][1]
                        return _CallableTarget(class_info.field_types, class_info.qualified_name, f"{func.value.id}.{func.attr}")

            owner_t = self._infer_expr_type(func.value, ctx)
            list_item = parse_list_type(owner_t)
            if list_item is not None and func.attr == "append":
                return _CallableTarget((list_item,), "None", f"{owner_t}.append")

            class_info = self.project.lookup_class(owner_t)
            if class_info is not None and func.attr in class_info.methods:
                signature = class_info.methods[func.attr]
                return _CallableTarget(signature.arg_types[1:], signature.return_type, f"{class_info.name}.{func.attr}")

        self._error(func, _ERR_UNKNOWN_SYMBOL, f"Unsupported call target '{ast.unparse(func)}'")
        return None

    def _render_annotation(self, node: ast.AST, module: ModuleInfo) -> str:
        rendered = ast.unparse(node)
        compact = rendered.replace(" ", "")
        if compact in {"int|float", "float|int"}:
            return "int | float"
        if isinstance(node, ast.Name):
            if node.id in module.classes:
                return module.classes[node.id][1].qualified_name
            imported = module.imported_symbols.get(node.id)
            if imported is not None:
                target_module = self.project.lookup_module(imported.module_name)
                assert target_module is not None
                if imported.symbol_name in target_module.classes:
                    return target_module.classes[imported.symbol_name][1].qualified_name
        return normalize_type_name(rendered)

    def _merge_collection_item_type(self, left: str, right: str) -> str:
        if left == right:
            return left
        merged = merge_numeric_result_type(left, right)
        if merged is not None:
            return merged
        if can_assign_type(left, right):
            return right
        if can_assign_type(right, left):
            return left
        return "Any"

    def _error(self, node: ast.AST, code: str, message: str) -> None:
        self.errors.append(
            AnalysisError(
                code=code,
                message=message,
                line=getattr(node, "lineno", None),
                col=getattr(node, "col_offset", None),
            )
        )
