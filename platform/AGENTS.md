# platform — cross-OS shims

**Owner:** P3 / N1a scaffold; grows as `host/` dies.

Portable replacements for Windows-only code in `host/` (`win_chrome.cpp`,
`%APPDATA%` paths, native file dialogs). The `app/src/platform/*` code lands
here during the `app/` → `ui/imgui/` migration.

## Write-set rules
- No raw Win32 in new code — use GLFW + portable abstractions.
- `userDataDir()` → `%APPDATA%` / `$XDG_CONFIG_HOME` / `~/Library/Application Support`.
- File dialogs: nfd (Native File Dialog Extended).

## Migration source
- `app/src/platform/file_dialog.*` (P3)
- `host/src/win_chrome.cpp` logic → custom ImGui titlebar (P3/N4b)
