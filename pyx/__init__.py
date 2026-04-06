"""PyX - Python eXtended Static compiler.

This bootstrap module registers ``.pyx`` as a recognised Python-compatible
source extension so that every intra-package import of a ``.pyx`` file works
transparently under the normal CPython import machinery.  It must stay a
plain ``.py`` file so that Python can load it before the hook is active.
"""
from __future__ import annotations

import importlib.machinery
import sys

# ---------------------------------------------------------------------------
# .pyx import hook
# ---------------------------------------------------------------------------
# pyx files are a strict subset of Python – they share identical syntax and
# can be executed by CPython directly.  We just need to tell the import
# system to treat them like .py sources.

if ".pyx" not in importlib.machinery.SOURCE_SUFFIXES:
    # Prepend so .pyx is found before .py when both exist.
    importlib.machinery.SOURCE_SUFFIXES.insert(0, ".pyx")
    # Flush per-directory FileFinder caches so the new suffix is picked up
    # immediately for *this* package's directory and any sub-packages.
    sys.path_importer_cache.clear()

# ---------------------------------------------------------------------------
# Public re-exports (deferred so the hook is active first)
# ---------------------------------------------------------------------------

from .analyzer import AnalysisError, Analyzer  # noqa: E402
from .compiler import CompileError, LLVMCompiler  # noqa: E402
from .diagnostics import Diagnostic  # noqa: E402

__all__ = ["Analyzer", "AnalysisError", "LLVMCompiler", "CompileError", "Diagnostic"]
