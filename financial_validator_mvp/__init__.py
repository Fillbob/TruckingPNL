"""Compatibility package shim for Streamlit Cloud.

The repository currently has a flat layout (parsers/, services/, models/),
while the app imports use `financial_validator_mvp.*`.
This module aliases those flat packages to the expected package namespace.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

_PACKAGE_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _PACKAGE_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

for _name in ("models", "parsers", "services", "tests"):
    try:
        _module = importlib.import_module(_name)
    except Exception:
        continue
    sys.modules[f"{__name__}.{_name}"] = _module
