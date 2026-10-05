# Browser integration tests (opt-in)

These tests drive the **real** per-source adapters (GrabCAD, TraceParts, McMaster,
Amazon, JLCPCB, PCBWay) against the **live** sites, under **your own** logged-in
browser profile. They are **off by default**: CI never runs them, and no credentials
are ever committed.

## What always runs (even in CI)

Two cases in `test_integration.py` run unconditionally and need no network:

- `test_expected_adapters_are_registered` — every source resolves to a callable adapter.
- `test_adapter_search_executes_real_code_offline` — a real adapter's `search()` runs
  end-to-end with `browser_fetch` faked to a captured page.

So a skipped live case always means "profile/flag not provisioned", never "code missing".

## How to run the live cases

Live cases are gated behind an env flag **and** a saved profile per source.

1. **Provision the selected backend** — see `services/browser/README.md`.
   Headless calls default to the CEF host daemon on Linux/Windows and Playwright
   on macOS. Ordinary headed login still needs the residual Playwright-compatible
   driver; for stock Playwright, use `pip install playwright` and provision Chromium
   explicitly with `playwright install chromium`.
2. **Log into each source** you want to test — this opens a real headed window and
   saves a LOCAL-ONLY profile (cookies never leave your machine, never committed):

   ```python
   from services.browser import session
   session.browser_login("grabcad")    # sign in, then close the window
   session.browser_login("mcmaster")   # ...repeat per source you want to cover
   ```

   If the site shows a **captcha or 2FA**, complete it yourself in that window — the
   tool detects it and prints a reminder, but never solves or bypasses it.

3. **Run the gated suite** with the flag set:

   ```bash
   CHUDWARE_BROWSER_INTEGRATION_TESTS=1 \
     python -m pytest \
     services/browser/integration/ -v
   ```

   A source with no saved profile skips with a clear message; sources you logged into
   run their real `search("bracket")` and assert the adapter produced the contract's
   list-of-dict envelope (parsed results, a login-required ask if the session expired,
   or a captcha/2FA abort — all real-code outcomes).

## Safety posture (do not weaken)

- **No evasion, ever.** Captcha/2FA hands control back to you (headed window) or aborts
  the fetch (`ok:false, "captcha_or_2fa_detected"`). Never solve, retry-around, or bypass.
- **Local-only profiles.** Saved sessions live under the app data dir; never synced,
  uploaded, logged, or committed. Do not add profiles or credentials to git.
- **Terms of service.** Each source's ToS posture is tracked in
  `contracts/license_debt.md`; these tests are for local developer verification, not a
  redistribution or SaaS mechanism.
