"""Root conftest.py — ensure repo root is on sys.path for all test modules."""
import os
import sys

# Add repo root so that top-level packages (tools/, services/, mcp_server/) are importable
# regardless of where pytest is invoked from.
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
