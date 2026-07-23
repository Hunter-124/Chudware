# T-G3 Questions / Scope Notes

## NON-BLOCKING: File-filter label strings deferred to T-G10

The following user-visible strings appear in file-open/save dialog filter dropdowns and
were intentionally left unchanged in this task:

- `host/src/ui/file_dialog.h:7` — `"Chudware part (*.step+)\0*.step+\0All files (*.*)\0*.*\0"`
- `host/src/ui/feature_tree.cpp:1495,1504` — `kStepPlusFilter` / open filter
- `ui/imgui/src/platform/file_dialog.cpp:12,13` — `"Chudware / CAD files"`, `"Chudware native (.step+)"`
- `ui/imgui/src/ui/feature_tree.cpp:1728,1737` — same filter strings

**Rationale:** These strings name the native file format (`.step+`), not app chrome. They
belong to the same tier as appdata path names and the `.so`/`.exe` names — all owned by
T-G10. Changing them before T-G10 renames the extension/format assets would create an
inconsistency between the filter label and the actual file format identity.

**Recommended answer:** Confirm deferred to T-G10. No action needed here.

---

## NON-BLOCKING: Workspace-dir hint text references "Documents/Chudware" path

- `host/src/ui/settings_modal.cpp:487,490` — help text + placeholder hint showing
  `Documents/Chudware` as the default workspace path

This is the on-disk directory name (infrastructure), not a branding string. Left
unchanged as T-G10 scope (same as appdata paths).

**Recommended answer:** Confirm deferred to T-G10. No action needed here.

---

## NON-BLOCKING: libchudware.so / Chudware.exe references in tooltip

- `host/src/ui/settings_modal.cpp:544` — help text: "stage that DLL next to Chudware.exe"
- `host/src/ui/legacy_chat.cpp:2749` — `dimText`: "Place libchudware.so next to the Chudware executable"

These reference the executable file name, not user-facing branding. T-G10 scope.

---

_sonnet-brio, 2026-07-02_
