# services/browser — user-authenticated headless-browser acquisition

Headless production fetch/download uses the host **CEF daemon**
(`cef_client.py` → `AI-CAD --cef-daemon`). Residual Playwright-compatible
surfaces (headed CLI login, embed, cookie export) go through
`services/browser/driver.py`, selected by env:

| `BILLET_PW_BACKEND` | Residual PW driver | Notes |
|---------------------|--------------------|-------|
| `cef` (**default**, unset) | stock `playwright` | Byte-identical to pre-factory; headless stays CEF |
| `patchright` | `patchright` (Apache-2 drop-in) | `pip install patchright` then provision |
| `camoufox` | import-guarded opt-in | Firefox hard-site fallback; not a login/embed drop-in |

One-time setup (already done in the shared mcp-venv; needed again only for a
fresh venv). **Never auto-downloads at import** — provision explicitly:

```
pip install playwright          # or: pip install patchright
# explicit binary fetch (no silent network at import):
python -c "from services.browser.driver import provision_browser; \
           print(provision_browser('playwright', install=True))"
# equivalent classic CLI still works:
playwright install chromium
# patchright:
#   pip install patchright
#   provision_browser('patchright', install=True)
```

`driver.resolve_sync()` / `resolve_async()` lazy-import the selected package so
nothing here crashes an MCP server that hasn't run either step yet — calls just
degrade to `{ok: false, reason: "playwright_not_installed", ...}`. Challenges
(captcha/2FA) are **detected never solved** (`challenge.py`).

Profiles created by `browser_login(site)` live under the app data dir
(`~/.local/share/ShopCraft/browser_profiles/<site>/` or the Windows/legacy
equivalent — see `engine.paths.BROWSER_PROFILES`) and are **local only**:
never synced, never uploaded, never bundled into crash reports or diagnostics.
