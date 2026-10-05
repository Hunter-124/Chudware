"""services/browser — user-authenticated headless-browser acquisition.

Persistent, LOCAL-ONLY per-site login profiles + rate-limited fetch/download
primitives. Source adapters (services.browser.adapters) call
:func:`browser_login`, :func:`browser_fetch`, and :func:`browser_download` from
this package — never Playwright directly.

See ``contracts/license_debt.md`` for the acquisition policy. Profiles remain
local-only; captcha/2FA and per-source terms require human action.
"""
from __future__ import annotations

from .session import browser_download, browser_fetch, browser_login

__all__ = ["browser_download", "browser_fetch", "browser_login"]
