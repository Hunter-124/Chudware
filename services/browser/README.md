# services/browser — user-authenticated headless-browser acquisition

Headless production fetch/download uses the backend selected by
`services/browser/driver.py`. Linux and Windows default to the host **CEF
daemon** (`cef_client.py` → `chudware --cef-daemon`); macOS defaults to
`pw_client.py` because it ships CEF off. The same env also selects residual
Playwright-compatible surfaces (headed CLI login, embed, cookie export):

| `CHUDWARE_PW_BACKEND` | Headless fetch/download | Residual PW driver |
|---------------------|-------------------------|--------------------|
| `cef` | CEF daemon | stock `playwright` |
| `playwright` | stock Playwright | stock `playwright` |
| `patchright` | Patchright | `patchright` (Apache-2 drop-in) |
| `camoufox` | CEF daemon | import-guarded opt-in |

Unset defaults to `playwright` on macOS and `cef` on Linux/Windows. See the
[canonical macOS Parts Library platform note](https://chudware.com/docs/troubleshooting/#macos-parts-library-vendor-access);
release notes should link there rather than duplicate its wording.

## Backend boundaries and setup

The host's native CEF panel and headless daemon are implemented under
`host/src/ui/widgets/cef/`. Headless calls use `driver.headless_client()`;
the residual Python embed worker in `embed.py` still uses Playwright/CDP.
These are separate surfaces, not interchangeable browser transports.
The CEF client currently uses POSIX local sockets; selecting `cef` does not
provide Windows named-pipe transport support.

For a fresh runtime venv, install the selected residual driver explicitly.
The CEF daemon needs a CEF-enabled Chudware host binary, not a Playwright
binary download. Residual login/embed/cookie export (and headless calls
when selecting `playwright` or `patchright`) need their Python driver and
browser binary. **Never auto-downloads at import** — provision explicitly:

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
nothing here crashes an MCP server that has not run either step. Headless calls
route through `driver.headless_client()`. Challenges (captcha/2FA) are **detected,
never solved** (`challenge.py`).

Profiles created by `browser_login(site)` live under the app data dir
(`~/.local/share/Chudware/browser_profiles/<site>/` or the Windows/legacy
equivalent — see `services.paths.BROWSER_PROFILES`) and are **local only**:
never synced, never uploaded, never bundled into crash reports or diagnostics.
