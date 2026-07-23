# In-Panel Browser: CEF Off-Screen Backend

Status: **design + seam landed; CEF backend is next-increment scaffolding (needs the CEF SDK + a real build/verify loop).**

## Why

The vendor sign-in panel embeds a browser so users can log into McMaster,
GrabCAD, TraceParts, SnapEDA, etc. and the [Playwright scraper](../services/browser/)
can then download models/datasheets under that saved session.

The shipping backend (`ScreencastWebView`) streams JPEG frames from a *headless*
Chromium in the worker and forwards synthetic input. That approach cannot render
OS-native UI: **native `<select>` popups don't appear, and the desktop clipboard
is isolated** (no copy/paste, no password-manager autofill). Those are exactly
the interactions logins need.

On Wayland (Fedora default here) you also can't reparent a real browser window
into the app. So the fix is **CEF off-screen rendering (OSR)**: an in-process
Chromium Embedded Framework that paints into a pixel buffer we upload as a GL
texture. CEF composites its own `<select>`/popup layer and bridges the OS
clipboard, so those work — while staying inside the ImGui panel.

## The seam (landed, build-green with CEF OFF)

All UI talks to the browser through one interface; the engine underneath is a
swappable implementation. This is the durable abstraction — CEF is just the
second backend behind it.

```
host/src/ui/widgets/
  web_view_backend.h    IWebViewBackend (open/attach/close/update/draw/url/loginState/…)
                        + makeWebView() factory  ← single backend-selection point
  web_view.{h,cpp}      ScreencastWebView : IWebViewBackend   (default, ships today)
  web_view_backend.cpp  makeWebView(): CEF when CHUDWARE_ENABLE_CEF + CHUDWARE_WEBVIEW=cef, else screencast
  cef/                  CefWebView : IWebViewBackend          (this doc; opt-in)
```

`parts_browser_panel.cpp` holds a `std::unique_ptr<IWebViewBackend>` from
`makeWebView()` and never mentions a concrete backend. Selecting CEF changes one
factory line, not the UI.

Build: `option(CHUDWARE_ENABLE_CEF OFF)` in `host/CMakeLists.txt`. OFF (default)
compiles only the screencast backend — CI and contributors never need the ~1 GB
CEF SDK. ON includes `host/cmake/cef.cmake`.

## Cross-platform strategy — CEF *is* the platform layer

CEF already gives Linux/Windows parity. Keep the OS-specific surface tiny and
isolated in `cef/cef_platform_{linux,windows}.cpp`. It is only three things:

| Concern | Linux | Windows |
| --- | --- | --- |
| `CefMainArgs` | `CefMainArgs(argc, argv)` | `CefMainArgs(GetModuleHandle(nullptr))` |
| Subprocess helper | separate ELF `ai_cad_cef_helper` | separate `.exe` |
| Bundle layout | `libcef.so` + `*.pak`/`locales/` next to the binary | `libcef.dll` + `*.pak`/`locales/` |

Everything else — OSR paint, input translation, cookies, life-span — is
identical CEF C++ compiled for both. **Do not** build a per-OS rendering
abstraction; the valuable abstraction is `IWebViewBackend`, not re-homed OS code.

## Architecture (OSR)

- **`CefApp`** (browser + render process): registers the subprocess helper,
  sets `windowless_rendering_enabled`, forces the shared cookie/OSCrypt settings
  (see interop). `CefExecuteProcess()` must run at the very top of `main()` (and
  the helper's `main()`) — it early-returns in the browser process and blocks in
  helper processes. This is a sensitive edit to app entry; it's mandatory.
- **`CefClient` + `CefRenderHandler`** (`CefWebView`'s guts):
  - `GetViewRect` → current panel size in DIP.
  - `OnPaint(type, dirtyRects, buffer, w, h)` → fires on a **CEF thread**, hands
    **BGRA** pixels. Copy into a double-buffer under a mutex; DO NOT touch GL
    here. `type == PET_VIEW` is the page, `PET_POPUP` is the `<select>`/dropdown
    layer — composite the popup over the view at the rect from `OnPopupSize`.
  - The GL upload + draw happens in `CefWebView::draw()` on the **GL thread**
    only (swizzle BGRA→RGBA or use `GL_BGRA`), mirroring `web_view.cpp`'s
    existing texture discipline (MEMORY `gl-thread-rpc-freeze`).
- **Message pump**: use CEF's external pump via `OnScheduleMessagePump` +
  `CefDoMessageLoopWork()` scheduled from the GLFW loop. Do **not** call
  `CefDoMessageLoopWork()` blindly every frame — it starves/lags OSR.
- **Input**: translate ImGui events to `CefMouseEvent`/`CefKeyEvent`
  (`SendMouseMoveEvent`/`SendMouseClickEvent`/`SendMouseWheelEvent`/
  `SendKeyEvent`) with correct `EVENTFLAG_*` modifiers. Coordinates are the same
  page-pixel mapping `collectInput()` already computes.

## Cookie interop — the make-or-break risk

Sign-in only matters if the **Playwright scraper** (`services/browser/session.py`)
can reuse the session CEF created. Same profile dir is necessary but **not
sufficient**:

- **Linux**: Chromium encrypts cookie values with `OSCrypt` using a key from the
  Secret Service (gnome-keyring/kwallet), or a fixed key under
  `--password-store=basic`. If CEF uses the keyring and Playwright doesn't (or
  vice-versa), cookies written by one are **undecryptable** by the other even in
  the same dir.
- **Windows**: same trap via DPAPI (per-user key) — generally fine if both run
  as the same user, but pin it.

Plan, in order:
1. **Force both engines to the same cookie crypto.** Launch CEF and Playwright
   with `--password-store=basic` (Playwright: add to
   `stealth.launch_args`; CEF: `CefSettings`/command-line). Removes the keyring
   variable entirely.
2. **Version-match** CEF's Chromium to Playwright's bundled Chromium (currently
   **149** — see `stealth.chromium_version_*`) so the cookie DB schema matches.
3. **Fallback**: if shared-dir still misbehaves, use CEF's cookie API to export
   and import into the Playwright profile after sign-in.
4. **Shared profile dir**: CEF cache path = `profiles.profile_dir(site)` (the
   same per-vendor dir), serialized by the existing `profile_lock` so the two
   engines never open it concurrently.

**Verify first, build second.** The earliest possible check once CEF renders:
sign in via CEF → run an existing `browser_fetch(site)` → confirm it comes back
authenticated (no `login_required`). That one test validates the whole feature's
reason to exist; do it before finishing the OSR/input polish.

## C++/Python boundary — reuse the verified detection

Keep the browser state split clean (avoid the RPC-sync shape that caused past GL
freezes):

- **C++ (CefWebView)**: renders, forwards input, writes cookies to the profile
  dir, and reports `current_url` + page `login_state`.
- **Python (`services/browser/embed.py`)**: keeps wizard orchestration —
  `classify_login_state`, `login_verified_ts` stamping, walk-all site list,
  auto-advance. These are already implemented and tested; don't reimplement them
  in C++.
- CEF reports URL/HTML to the wizard over the existing `login_wizard_*` RPC
  surface so `classify_login_state` runs unchanged. (For a fully C++-side flow
  later, `classify_login_state`'s auth-URL + login-wall logic can be mirrored,
  but the first increment reuses Python.)

## Phasing

1. ✅ **Seam**: `IWebViewBackend`, `ScreencastWebView` refactor, factory,
   `CHUDWARE_ENABLE_CEF` option — build-green with CEF OFF. *(done)*
2. **CEF hello-world**: SDK via `cmake/cef.cmake`, subprocess helper,
   `CefExecuteProcess` in `main()`, OSR paints example.com into the panel.
   *(needs SDK + build loop)*
3. **Cookie interop check**: sign in on one site, confirm `browser_fetch` is
   authenticated. Gate everything else on this.
4. **Input + popups + clipboard**: full mouse/key, `<select>` popup compositing,
   OS clipboard.
5. **Cross-platform**: Windows helper/bundle parity; verify OSR + cookies there.
6. **Flip default** once proven; keep screencast as fallback.

Until phase 6, `ScreencastWebView` remains the default and Hunter can sign in
today via the headed `browser_login` fallback (native dropdowns/clipboard work
there already) to unblock download testing.
