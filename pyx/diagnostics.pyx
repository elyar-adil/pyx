from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(eq=True, frozen=True)
class Diagnostic:
    code: str
    message: str
    path: str | Path | None = None
    line: int | None = None
    col: int | None = None


def format_diagnostic(source: str | Path, diagnostic: Diagnostic) -> str:
    path = str(diagnostic.path or source)
    if diagnostic.line is None:
        return f"{path}: error[{diagnostic.code}]: {diagnostic.message}"
    return (
        f"{path}:{diagnostic.line}:{diagnostic.col or 0}: "
        f"error[{diagnostic.code}]: {diagnostic.message}"
    )
