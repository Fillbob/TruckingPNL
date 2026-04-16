"""Namespace shim mapping `financial_validator_mvp.services` to root `services/`."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_REAL_DIR = _REPO_ROOT / "services"

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Let Python resolve submodules from the real package directory.
__path__ = [str(_REAL_DIR)]
