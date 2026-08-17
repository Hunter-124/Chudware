# platform — cross-OS shims (ORPHANED — dead remnant)

> **Orphaned (2026-08-16):** this directory was the planned portable-API
> consolidation for OS-specific host code, along the retired ui/imgui
> consolidation path. That consolidation never happened here: nothing in the
> root `CMakeLists.txt` or `host/CMakeLists.txt` references `platform/` or
> builds a `chudware_platform` library, and no host source includes these
> headers. What the host actually uses instead:
> - file dialogs → `host/src/ui/file_dialog.{h,cpp}` (compiled into `Chudware`)
> - borderless window chrome → `host/src/win_chrome.{h,cpp}` (compiled into `Chudware`)
> - paths → `core/util/platform_paths.cpp` (compiled into the host from core/)
> - camera backends → `host/src/platform/camera_{stub,v4l2,mf}.cpp` (a different
>   `platform/` dir, inside `host/src/`, that IS live)
>
> Treat everything below as the original migration design doc, not live API.

**Owner:** F3 / G1 integrator. Portable replacements for OS-specific code in `host/`.

## Public API (panels + shell include these — no `#ifdef _WIN32` outside this tree)

| Header | Purpose |
|--------|---------|
| `dialogs.h` | `saveFileDialog`, `openFileDialog`, `promptImportMeshAsSolid`, `fileDialogsDisabled` |
| `window.h` | Borderless chrome: `installWindowChrome`, `tickWindowChrome`, GLFW init hints |
| `paths.h` | `exeDir`, `appdataDir`, `appdataRoot`, `configPath`, `cacheDir`, `tempDir`, `resourcesDir` |
| `resources.h` | `setWindowIcon` (PNG → GLFW icons) |
| `dpi.h` | `readContentScale`, `applyScaleOverride` (CHUDWARE_SCALE) |

Implementations split by OS: `*_win32.cpp`, `*_linux.cpp`. Build via `platform/CMakeLists.txt` → static lib `chudware_platform`.

## Headless

`CHUDWARE_NO_FILE_DIALOGS=1` → all dialog calls return empty / -1 (CI / `--shot`).

## Migration source

- `host/src/ui/file_dialog.*` → `dialogs_*`
- `host/src/win_chrome.*` → `window_*`
- `host/src/core/app_state.cpp` (`appdataDir`) + `host/src/bootstrap.cpp` (`exeDir`) → `paths_*`
- `host/src/app.cpp` (GLFW scale + X11 hint) → `dpi.*` + `window.h`
