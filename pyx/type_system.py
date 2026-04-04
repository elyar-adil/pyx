from __future__ import annotations

NUMERIC_UNION = "int | float"
NUMERIC_SCALARS = frozenset({"int", "float"})
PRIMITIVE_TYPES = frozenset({"int", "float", "bool", "str", "bytes"})

# ---------------------------------------------------------------------------
# Phase 4: ctypes / C ABI FFI type support
# ---------------------------------------------------------------------------

#: The internal PyX type name for a dynamic-library handle (dlopen result).
CDLL_TYPE = "cdll"

#: ctypes integer-like types and their canonical names.
CTYPES_INT_TYPES: frozenset[str] = frozenset({
    "c_int", "c_uint",
    "c_long", "c_ulong",
    "c_longlong", "c_ulonglong",
    "c_short", "c_ushort",
    "c_byte", "c_ubyte",
    "c_char",
    "c_size_t", "c_ssize_t",
})

#: ctypes floating-point types.
CTYPES_FLOAT_TYPES: frozenset[str] = frozenset({"c_float", "c_double"})

#: ctypes pointer-like types.
CTYPES_PTR_TYPES: frozenset[str] = frozenset({"c_void_p", "c_char_p", "c_wchar_p"})

#: All supported ctypes type names.
CTYPES_ALL_TYPES: frozenset[str] = CTYPES_INT_TYPES | CTYPES_FLOAT_TYPES | CTYPES_PTR_TYPES


def is_cfuncptr_type(type_name: str) -> bool:
    """Return True if *type_name* is an internal cfuncptr(...) type string."""
    return type_name.startswith("cfuncptr(") and type_name.endswith(")")


def parse_cfuncptr_type(type_name: str) -> tuple[str, list[str]] | None:
    """Parse ``cfuncptr(ret,arg1,arg2,...)`` → ``(ret_ctype, [arg_ctypes])``.

    Returns *None* if *type_name* is not a valid cfuncptr type string.
    The first element of the tuple is the return ctypes name (e.g. ``"c_int"``
    or ``"None"``); the rest are the argument ctypes names.
    """
    if not is_cfuncptr_type(type_name):
        return None
    inner = type_name[9:-1]
    if not inner:
        return None
    parts = [p.strip() for p in inner.split(",") if p.strip()]
    if not parts:
        return None
    return parts[0], parts[1:]


def ctypes_to_pyx_type(ctype: str) -> str:
    """Map a ctypes return-type name to the corresponding PyX value type.

    Integer ctypes types map to ``"int"``, float types to ``"float"``,
    ``"None"`` (void) to ``"None"``, and pointer types to ``"Any"`` (not yet
    fully supported).
    """
    if ctype in CTYPES_INT_TYPES:
        return "int"
    if ctype in CTYPES_FLOAT_TYPES:
        return "float"
    if ctype == "None":
        return "None"
    return "Any"  # pointer / unknown → opaque for now


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
    # Phase 4: ctypes FFI types
    if normalized == CDLL_TYPE or is_cfuncptr_type(normalized):
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
