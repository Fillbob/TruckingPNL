"""Compatibility package shim for Streamlit Cloud.

The repository currently uses a flat layout (models/, parsers/, services/)
while imports reference `financial_validator_mvp.*`.

This module only ensures repository root is importable and applies a pandas
Styler compatibility shim used by Streamlit table rendering.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PACKAGE_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _PACKAGE_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Pandas/Streamlit compatibility:
# Some runtimes expose Styler.map but not Styler.applymap.
try:
    import pandas as _pd

    _styler_cls = _pd.io.formats.style.Styler
    if not hasattr(_styler_cls, "applymap") and hasattr(_styler_cls, "map"):
        def _applymap_compat(self, func, subset=None, **kwargs):
            return self.map(func, subset=subset, **kwargs)

        _styler_cls.applymap = _applymap_compat  # type: ignore[attr-defined]
except Exception:
    # Never fail import if pandas internals differ.
    pass
