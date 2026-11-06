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

## Profiles and session hand-off

Playwright profiles created by `browser_login(site)` live under the app data dir
(`~/.local/share/Chudware/browser_profiles/<site>/` or the Windows/legacy
equivalent — see `services.paths.BROWSER_PROFILES`). The GUI has its own CEF
store and exports cookies after sign-in: they are staged for the site's
Playwright profile and also imported into the daemon. The staging file contains
secrets and is deleted when Playwright consumes it. These stores are local.

The CEF daemon uses one persistent **global request context** for vendor fetches,
downloads, interaction, and GUI cookie imports. Opening the Parts Library
re-exports matching cookies from the GUI panel's existing store to restore
previously signed-in vendors. The per-site sidecar records login verification
and throttling, not a separate CEF jar. Successful CEF responses identify this
as `session_profile_used: "cef_global"`; Playwright responses identify the
per-site profile. Deleting a site profile removes its Playwright directory,
sidecars, and cached pages, but **does not sign the site out of the shared CEF
daemon or GUI cookie stores**. Deleting the global store would destroy other
vendors' signed-in sessions.

Login imports, profile deletion, detected embedded-browser logout, and a
definitive expired-login probe invalidate that site's page cache. A fetch
already in progress in the same worker cannot repopulate it afterward.
The native GUI has no vendor sign-out event; signing out manually on a vendor
website is not necessarily detected immediately. In that case cached pages can
remain until the next login import, definitive status probe, profile deletion,
or cache expiry.

CEF and Playwright transports report HTTP status, final URL, and `Retry-After`
when available. The failure envelope preserves these fields; unknown values
remain `null`.
If a CEF navigation times out after a successful response and produces a DOM,
the fetch marks it `partial_load: true`; incomplete pages are not cached.
Both backends support sentinel-based same-origin interaction; an unavailable
interaction capability returns `unsupported` instead of switching backends.

Adapter search failures are structured notices with `site`, `op`, `reason`,
`hint`, `url`, `status`, `retry_after_s` and optional `ask_user`; callers
should distinguish missing API credentials from browser login, blocking and
captcha. Successful acquisitions identify `artifact_kind` (`cad`, `document`,
`ecad` or `archive`) and `format` alongside the artifact path and provenance.
CAD content is validated before import; a datasheet is a document, not geometry.
DigiKey and Mouser use their official search APIs when credentials are configured
and otherwise try their public websites; Nexar requires its API credentials.

## Interactive fetch policy

User-initiated requests are paced per site and honour the applicable origin's
published `robots.txt` `Crawl-delay` when longer than the normal interval.
`Disallow` is intentionally not enforced for these interactive requests: this
mechanism is a pacing policy, not an access or terms-of-service determination.
HTTP 403/429/503 trigger cooldowns, including a longer server `Retry-After`.
Idempotent fetch/download retries are bounded to transient failures; challenges
are never retried or solved automatically.
