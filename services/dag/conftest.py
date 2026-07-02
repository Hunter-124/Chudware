"""conftest.py — make services/dag importable as 'dag' when pytest runs from repo root.

Mirrors the implicit sys.path insertion that services/agent/ gets from its __init__.py
being found by pytest's rootdir import logic.  Adding services/ to sys.path here gives
the same effect for the dag package.
"""
import sys
from pathlib import Path

_services_dir = str(Path(__file__).parent.parent)
if _services_dir not in sys.path:
    sys.path.insert(0, _services_dir)
