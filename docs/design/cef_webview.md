# In-Panel Browser: CEF Off-Screen Backend

Status: **implemented native backend, conditional on the CEF build.**
`host/src/ui/widgets/web_view_backend.cpp::makeWebView` selects `CefWebView` when
`CHUDWARE_ENABLE_CEF` is enabled, otherwise `NullWebView`. The host CMake option
defaults ON for native Linux/Windows and is forced OFF for macOS/Web/WASM.
These source facts are not a current Windows interactive-acceptance result.

Original status claim: "shipping and load-bearing on Linux and Windows" for the
in-app browser and headless fetch primitive used by "100+ Parts Library source
adapters". Treat that as historical scope, not a verified current adapter count.
Current native browser ownership is the factory and `cef/` implementation below.

## Why

The vendor sign-in panel embeds a browser so users can log into McMaster,
GrabCAD, TraceParts, SnapEDA, and other sources. The same session is handed to
the headless CEF daemon (`chudware --cef-daemon`) so source adapters can fetch
models and datasheets.

CEF off-screen rendering (OSR) paints into a pixel buffer uploaded as a GL
texture. It composites `<select>` and popup layers and bridges the OS clipboard
while remaining inside the ImGui panel. On Wayland this also avoids trying to
reparent a native browser window into the app.

## The shipping seam

All UI talks to the browser through one interface:

```
host/src/ui/widgets/
  web_view_backend.h    IWebViewBackend (open/attach/close/update/draw/url/loginState/…)
                        + makeWebView() factory  ← single backend-selection point
  web_view_backend.cpp  CefWebView when CHUDWARE_ENABLE_CEF, otherwise NullWebView
  cef/                  CefWebView : IWebViewBackend
```

`ScreencastWebView`, the former worker-side JPEG-streaming backend, was deleted
when CEF landed on 2026-07-07. There is no runtime backend selector.

`CHUDWARE_ENABLE_CEF` defaults ON for native Linux and Windows builds and
requires the SDK under `extern/cef/`. macOS forces the option OFF because the
vendored distribution/build does not implement CEF's Darwin app-bundle and
helper-process layout; it ships the existing `NullWebView`. Web/WASM also forces
CEF OFF because the SDK is native-only.

`parts_browser_panel.cpp` holds a `std::unique_ptr<IWebViewBackend>` from
`makeWebView()` and never mentions a concrete backend. Backend selection is a
compile-time build decision, not an environment-variable toggle.

## Cross-platform strategy

CEF supplies the shared Linux/Windows implementation. Keep their OS-specific
surface tiny and isolated in `cef/cef_platform_{linux,windows}.cpp`:

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

## Session interop with headless fetch

The interactive CefWebView and the headless CEF daemon are separate processes
with separate responsibilities:

- **CefWebView** renders the in-app page, forwards input, and exposes the live
  URL and login state.
- **`services/browser/cef_client.py`** drives `chudware --cef-daemon`, the
  production fetch/download implementation used by source adapters.
- **`exportSession()`** exports the interactive cookie jar and stages it through
  `browser_session_import`, allowing subsequent headless fetches to reuse the
  user's authenticated session.
- **`services/browser/embed.py`** retains login-wizard orchestration and login
  state classification.

The production proof is end to end: sign in through CefWebView, export the
session, then call `browser_fetch(site)` and observe authenticated content rather
than `login_required`. This boundary must remain asynchronous; neither CEF
callbacks nor browser RPC may block the GL thread.
