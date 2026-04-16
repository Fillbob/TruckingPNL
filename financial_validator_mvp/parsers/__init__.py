"""Bridge package to expose top-level `parsers` as `financial_validator_mvp.parsers`."""

from __future__ import annotations

import importlib
import pkgutil
import sys

_REAL_PACKAGE_NAME = "parsers"
_REAL_PACKAGE = importlib.import_module(_REAL_PACKAGE_NAME)

# Mirror package search path for submodule loading.
__path__ = list(getattr(_REAL_PACKAGE, "__path__", []))

# Eagerly alias discovered submodules.
for _module_info in pkgutil.iter_modules(__path__):
    _submodule_name = _module_info.name
    _full_real_name = f"{_REAL_PACKAGE_NAME}.{_submodule_name}"
    try:
        _module = importlib.import_module(_full_real_name)
    except Exception:
        continue
    sys.modules[f"{__name__}.{_submodule_name}"] = _module

# Re-export names from the real package.
for _name in getattr(_REAL_PACKAGE, "__all__", []):
    globals()[_name] = getattr(_REAL_PACKAGE, _name)
