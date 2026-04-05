from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

# Modules that are part of Python's standard library or runtime and should not
# be resolved as local .py files. The compiler handles them via built-in rules.
BUILTIN_MODULES: frozenset[str] = frozenset({"ctypes"})


@dataclass(frozen=True)
class FunctionSignature:
    module_name: str
    name: str
    arg_names: tuple[str, ...]
    arg_types: tuple[str, ...]
    return_type: str
    class_name: str | None = None

    @property
    def qualified_name(self) -> str:
        if self.class_name is None:
            return f"{self.module_name}.{self.name}"
        return f"{self.module_name}.{self.class_name}.{self.name}"

    @property
    def is_method(self) -> bool:
        return self.class_name is not None


@dataclass(frozen=True)
class ClassInfo:
    module_name: str
    name: str
    field_names: tuple[str, ...]
    field_types: tuple[str, ...]
    methods: dict[str, FunctionSignature]
    is_dataclass: bool = False

    @property
    def qualified_name(self) -> str:
        return f"{self.module_name}.{self.name}"

    @property
    def fields(self) -> dict[str, str]:
        return dict(zip(self.field_names, self.field_types, strict=True))


@dataclass(frozen=True)
class ImportedModule:
    local_name: str
    module_name: str


@dataclass(frozen=True)
class ImportedSymbol:
    local_name: str
    module_name: str
    symbol_name: str


@dataclass
class ModuleInfo:
    name: str
    path: Path
    tree: ast.Module
    functions: dict[str, tuple[ast.FunctionDef, FunctionSignature]] = field(default_factory=dict)
    classes: dict[str, tuple[ast.ClassDef, ClassInfo]] = field(default_factory=dict)
    imported_modules: dict[str, ImportedModule] = field(default_factory=dict)
    imported_symbols: dict[str, ImportedSymbol] = field(default_factory=dict)


@dataclass
class ProjectInfo:
    root_dir: Path
    entry_path: Path
    modules: dict[str, ModuleInfo]

    def lookup_module(self, name: str) -> ModuleInfo | None:
        return self.modules.get(name)

    def lookup_class(self, qualified_name: str) -> ClassInfo | None:
        for module in self.modules.values():
            for _, info in module.classes.values():
                if info.qualified_name == qualified_name:
                    return info
        return None

    def known_type_names(self) -> set[str]:
        names: set[str] = set()
        for module in self.modules.values():
            for _, info in module.classes.values():
                names.add(info.qualified_name)
        return names


class ProjectLoadError(Exception):
    pass


def load_project(entry_path: str | Path) -> ProjectInfo:
    entry = Path(entry_path).resolve()
    root_dir = entry.parent
    modules: dict[str, ModuleInfo] = {}
    _load_module(entry, entry.stem, root_dir, modules)
    return ProjectInfo(root_dir=root_dir, entry_path=entry, modules=modules)


def _load_module(path: Path, module_name: str, root_dir: Path, modules: dict[str, ModuleInfo]) -> None:
    if module_name in modules:
        return
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    module = ModuleInfo(name=module_name, path=path, tree=tree)
    modules[module_name] = module

    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported = alias.name
                local = alias.asname or imported
                if imported in BUILTIN_MODULES:
                    module.imported_modules[local] = ImportedModule(local_name=local, module_name=imported)
                    continue
                imported_path = _resolve_module_path(imported, root_dir)
                _load_module(imported_path, imported, root_dir, modules)
                module.imported_modules[local] = ImportedModule(local_name=local, module_name=imported)
        elif isinstance(node, ast.ImportFrom):
            if node.level != 0 or node.module is None:
                raise ProjectLoadError(f"relative imports are not supported in {path}")
            if node.module in BUILTIN_MODULES:
                for alias in node.names:
                    local = alias.asname or alias.name
                    module.imported_symbols[local] = ImportedSymbol(
                        local_name=local,
                        module_name=node.module,
                        symbol_name=alias.name,
                    )
                continue
            imported_path = _resolve_module_path(node.module, root_dir)
            _load_module(imported_path, node.module, root_dir, modules)
            for alias in node.names:
                local = alias.asname or alias.name
                module.imported_symbols[local] = ImportedSymbol(
                    local_name=local,
                    module_name=node.module,
                    symbol_name=alias.name,
                )

    known_types = _known_types_from_loaded(modules)
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            known_types.add(f"{module_name}.{node.name}")
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            signature = _collect_function_signature(node, module_name, None, known_types)
            module.functions[node.name] = (node, signature)
        elif isinstance(node, ast.ClassDef):
            class_info = _collect_class_info(node, module_name, known_types)
            module.classes[node.name] = (node, class_info)
            known_types.add(class_info.qualified_name)


def _known_types_from_loaded(modules: dict[str, ModuleInfo]) -> set[str]:
    known: set[str] = set()
    for loaded in modules.values():
        for _, info in loaded.classes.values():
            known.add(info.qualified_name)
    return known


def _resolve_module_path(module_name: str, root_dir: Path) -> Path:
    parts = module_name.split(".")
    # Search order: project root, then pyx_packages/<name>/<name>.py, then pyx_packages/<name>.py
    candidates: list[Path] = [
        root_dir.joinpath(*parts).with_suffix(".py"),
        root_dir / "pyx_packages" / parts[0] / module_name.replace(".", "/"),
        (root_dir / "pyx_packages").joinpath(*parts).with_suffix(".py"),
    ]
    # For single-segment module names also look for pyx_packages/<name>/<name>.py
    if len(parts) == 1:
        candidates.insert(
            1,
            root_dir / "pyx_packages" / parts[0] / f"{parts[0]}.py",
        )
    for candidate in candidates:
        if candidate.suffix != ".py":
            candidate = candidate.with_suffix(".py")
        if candidate.exists():
            return candidate
    raise ProjectLoadError(f"cannot resolve imported module '{module_name}' from {root_dir}")


def _collect_class_info(node: ast.ClassDef, module_name: str, known_types: set[str]) -> ClassInfo:
    field_names: list[str] = []
    field_types: list[str] = []
    methods: dict[str, FunctionSignature] = {}
    qualified_name = f"{module_name}.{node.name}"
    augmented_known = set(known_types)
    augmented_known.add(qualified_name)

    is_dataclass = any(_decorator_name(decorator) == "dataclass" for decorator in node.decorator_list)
    for stmt in node.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            field_names.append(stmt.target.id)
            field_types.append(_render_annotation(stmt.annotation, module_name, augmented_known))
        elif isinstance(stmt, ast.FunctionDef):
            methods[stmt.name] = _collect_function_signature(stmt, module_name, node.name, augmented_known)
    return ClassInfo(
        module_name=module_name,
        name=node.name,
        field_names=tuple(field_names),
        field_types=tuple(field_types),
        methods=methods,
        is_dataclass=is_dataclass,
    )


def _collect_function_signature(
    node: ast.FunctionDef,
    module_name: str,
    class_name: str | None,
    known_types: set[str],
) -> FunctionSignature:
    arg_names: list[str] = []
    arg_types: list[str] = []
    for index, arg in enumerate(node.args.args):
        arg_names.append(arg.arg)
        if class_name is not None and index == 0 and arg.arg == "self" and arg.annotation is None:
            arg_types.append(f"{module_name}.{class_name}")
            continue
        if arg.annotation is None:
            arg_types.append("Any")
            continue
        arg_types.append(_render_annotation(arg.annotation, module_name, known_types))

    return_type = "Any" if node.returns is None else _render_annotation(node.returns, module_name, known_types)
    return FunctionSignature(
        module_name=module_name,
        name=node.name,
        arg_names=tuple(arg_names),
        arg_types=tuple(arg_types),
        return_type=return_type,
        class_name=class_name,
    )


def _render_annotation(node: ast.AST, module_name: str, known_types: set[str]) -> str:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left = _render_annotation(node.left, module_name, known_types)
        right = _render_annotation(node.right, module_name, known_types)
        compact = {left.replace(" ", ""), right.replace(" ", "")}
        if compact == {"int", "float"}:
            return "int | float"
    if isinstance(node, ast.Name):
        if node.id in {"int", "float", "bool", "str", "bytes"}:
            return node.id
        qualified = f"{module_name}.{node.id}"
        if qualified in known_types:
            return qualified
        for candidate in known_types:
            if candidate.endswith(f".{node.id}"):
                return candidate
        return node.id
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        candidate = f"{node.value.id}.{node.attr}"
        for known in known_types:
            if known == candidate or known.endswith(f".{candidate}"):
                return known
        return candidate
    if isinstance(node, ast.Subscript):
        if isinstance(node.value, ast.Name) and node.value.id in {"list", "set"}:
            inner = _render_annotation(node.slice, module_name, known_types)
            return f"{node.value.id}[{inner}]"
        if isinstance(node.value, ast.Name) and node.value.id == "dict":
            if isinstance(node.slice, ast.Tuple) and len(node.slice.elts) == 2:
                key_t = _render_annotation(node.slice.elts[0], module_name, known_types)
                val_t = _render_annotation(node.slice.elts[1], module_name, known_types)
                return f"dict[{key_t},{val_t}]"
    return ast.unparse(node)


def _decorator_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ast.unparse(node)
