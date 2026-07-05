"""services/browser — user-authenticated headless-browser acquisition (T-X1/T-X2).

Persistent, LOCAL-ONLY per-site login profiles + rate-limited fetch/download
primitives. Source adapters (services.browser.adapters, T-X2+) call only
:func:`browser_login`, :func:`browser_fetch`, and :func:`browser_download` from
this package — never Playwright directly.

See ``work/tasks/T-X1.md``/``T-X2.md`` and ``contracts/license_debt.md`` for the
acquisition policy this package implements (Hunter directive 2026-07-04).
"""
from __future__ import annotations

from .session import browser_download, browser_fetch, browser_login

__all__ = ["browser_download", "browser_fetch", "browser_login"]
