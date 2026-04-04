from __future__ import annotations

NUMERIC_UNION = "int | float"
NUMERIC_SCALARS = frozenset({"int", "float"})
PRIMITIVE_TYPES = frozenset({"int", "float", "bool", "str", "bytes"})


def normalize_type_name(type_name: str) -> str:
    compact = type_name.replace(" ", "")
    if compact in {"int|float", "float|int"}:
        return NUMERIC_UNION
    if compact.startswith("list[") and compact.endswith("]"):
        inner = compact[5:-1]
        return f"list[{normalize_type_name(inner)}]"
    if compact.startswith("set[") and compact.endswith("]"):
        inner = compact[4:-1]
        return f"set[{normalize_type_name(inner)}]"
    if compact.startswith("dict[") and compact.endswith("]"):
        inner = compact[5:-1]
        parts = _split_generic_args(inner)
        if len(parts) == 2:
            return f"dict[{normalize_type_name(parts[0])},{normalize_type_name(parts[1])}]"
    return compact


def _split_generic_args(source: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    start = 0
    for index, ch in enumerate(source):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(source[start:index])
            start = index + 1
    parts.append(source[start:])
    return [part.strip() for part in parts if part.strip()]


def parse_list_type(type_name: str) -> str | None:
    normalized = normalize_type_name(type_name)
    if normalized.startswith("list[") and normalized.endswith("]"):
        return normalized[5:-1]
    return None


def parse_set_type(type_name: str) -> str | None:
    normalized = normalize_type_name(type_name)
    if normalized.startswith("set[") and normalized.endswith("]"):
        return normalized[4:-1]
    return None


def parse_dict_type(type_name: str) -> tuple[str, str] | None:
    normalized = normalize_type_name(type_name)
    if not normalized.startswith("dict[") or not normalized.endswith("]"):
        return None
    parts = _split_generic_args(normalized[5:-1])
    if len(parts) != 2:
        return None
    return parts[0], parts[1]


def is_supported_type(type_name: str, known_types: set[str] | frozenset[str] | None = None) -> bool:
    normalized = normalize_type_name(type_name)
    if normalized in PRIMITIVE_TYPES or normalized == NUMERIC_UNION:
        return True

    list_item = parse_list_type(normalized)
    if list_item is not None:
        return is_supported_type(list_item, known_types)

    set_item = parse_set_type(normalized)
    if set_item is not None:
        return is_supported_type(set_item, known_types)

    dict_types = parse_dict_type(normalized)
    if dict_types is not None:
        key_t, value_t = dict_types
        return is_supported_type(key_t, known_types) and is_supported_type(value_t, known_types)

    known = set() if known_types is None else set(known_types)
    return normalized in known


def is_numeric_type(type_name: str) -> bool:
    normalized = normalize_type_name(type_name)
    return normalized in NUMERIC_SCALARS or normalized == NUMERIC_UNION


def is_union_type(type_name: str) -> bool:
    return normalize_type_name(type_name) == NUMERIC_UNION


def can_assign_type(got: str, expected: str) -> bool:
    got = normalize_type_name(got)
    expected = normalize_type_name(expected)
    if got == "Any" or expected == "Any":
        return True
    if got == expected:
        return True
    return expected == NUMERIC_UNION and got in NUMERIC_SCALARS


def merge_numeric_result_type(left: str, right: str) -> str | None:
    left = normalize_type_name(left)
    right = normalize_type_name(right)
    if left == right and left in NUMERIC_SCALARS:
        return left
    if {left, right} == NUMERIC_SCALARS:
        return "float"
    if is_numeric_type(left) and is_numeric_type(right):
        return NUMERIC_UNION
    return None
