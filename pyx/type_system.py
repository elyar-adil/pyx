from __future__ import annotations

NUMERIC_UNION = "int | float"
NUMERIC_SCALARS = frozenset({"int", "float"})
PRIMITIVE_TYPES = frozenset({"int", "float", "bool", "str"})
SUPPORTED_TYPES = frozenset(set(PRIMITIVE_TYPES) | {NUMERIC_UNION})


def normalize_type_name(type_name: str) -> str:
    compact = type_name.replace(" ", "")
    if compact in {"int|float", "float|int"}:
        return NUMERIC_UNION
    return type_name


def is_supported_type(type_name: str) -> bool:
    return normalize_type_name(type_name) in SUPPORTED_TYPES


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
