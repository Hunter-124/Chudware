# platform — cross-OS shims

**Owner:** F3 / G1 integrator. Portable replacements for OS-specific code in `host/`.

## Public API (panels + shell include these — no `#ifdef _WIN32` outside this tree)

| Header | Purpose |
|--------|---------|
| `dialogs.h` | `saveFileDialog`, `openFileDialog`, `promptImportMeshAsSolid`, `fileDialogsDisabled` |
| `window.h` | Borderless chrome: `installWindowChrome`, `tickWindowChrome`, GLFW init hints |
| `paths.h` | `exeDir`, `appdataDir`, `appdataRoot`, `configPath`, `cacheDir`, `tempDir`, `resourcesDir` |
| `resources.h` | `setWindowIcon` (PNG → GLFW icons) |
| `dpi.h` | `readContentScale`, `applyScaleOverride` (AI_CAD_SCALE) |

Implementations split by OS: `*_win32.cpp`, `*_linux.cpp`. Build via `platform/CMakeLists.txt` → static lib `aicad_platform`.

## AI-CAD2 wrappers

`ui/imgui/src/platform/file_dialog.*` — CAD-specific `openCadFile` / `saveCadFile` over `dialogs.h`.

## Headless

`AI_CAD_NO_FILE_DIALOGS=1` → all dialog calls return empty / -1 (CI / `--shot`).

## Migration source

- `host/src/ui/file_dialog.*` → `dialogs_*`
- `host/src/win_chrome.*` → `window_*`
- `host/src/core/app_state.cpp` (`appdataDir`) + `host/src/bootstrap.cpp` (`exeDir`) → `paths_*`
- `host/src/app.cpp` (GLFW scale + X11 hint) → `dpi.*` + `window.h`
