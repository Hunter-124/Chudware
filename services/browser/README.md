# services/browser — user-authenticated headless-browser acquisition

One-time setup (already done in the shared mcp-venv; needed again only for a
fresh venv):

```
pip install playwright
playwright install chromium
```

`pip install playwright` pulls the Python package; `playwright install
chromium` downloads the actual browser binary (~150-300 MB) into Playwright's
own cache directory (`~/.cache/ms-playwright` on Linux). Both are one-time per
machine. `services/browser/session.py` lazy-imports `playwright` so nothing
here crashes an MCP server that hasn't run either step yet — calls just
degrade to `{ok: false, reason: "playwright_not_installed", ...}`.

Profiles created by `browser_login(site)` live under the app data dir
(`~/.local/share/ShopCraft/browser_profiles/<site>/` or the Windows/legacy
equivalent — see `engine.paths.BROWSER_PROFILES`) and are **local only**:
never synced, never uploaded, never bundled into crash reports or diagnostics.
