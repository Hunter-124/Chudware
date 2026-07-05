"""services/browser — user-authenticated headless-browser acquisition (T-X1).

Persistent, LOCAL-ONLY per-site login profiles + a rate-limited fetch primitive.
Source adapters (T-X2+) call only :func:`browser_login` and :func:`browser_fetch`
from this package — never Playwright directly.

See ``work/tasks/T-X1.md`` and ``contracts/license_debt.md`` for the acquisition
policy this package implements (Hunter directive 2026-07-04).
"""
from __future__ import annotations

from .session import browser_fetch, browser_login

__all__ = ["browser_fetch", "browser_login"]
