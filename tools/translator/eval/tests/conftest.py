#!/usr/bin/env python3
"""Make the repo root importable so `tools.translator.*` resolves regardless of the
cwd pytest is invoked from. Mirrors the sys.path bootstrap in
tools/translator/eval/harness.py (one directory shallower there since harness.py
lives at tools/translator/eval/, this file at tools/translator/eval/tests/)."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
