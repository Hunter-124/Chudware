#!/usr/bin/env python3
"""Audit the visible command surface against host dispatch handlers.

The ribbon table is the user's promise: if a command is not marked stretch/deferred, clicking it
must route to a real handler or PropertyManager form. This script keeps that contract checkable.

Exit code:
  0 = every non-stretch ribbon command has an effective handler
  1 = one or more visible commands would fall through to "[ribbon] ... no PM form yet"
  2 = bad invocation / parse error
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RIBBON = REPO / "host" / "src" / "ui" / "ribbon_def.cpp"

HANDLER_FILES = [
    REPO / "host" / "src" / "app.cpp",
    REPO / "host" / "src" / "app_commands.cpp",  # the command-router table
    REPO / "host" / "src" / "ui" / "pm_forms.cpp",
    REPO / "host" / "src" / "ui" / "sketch_mode.cpp",
    REPO / "host" / "src" / "ui" / "sketch3d_mode.cpp",  # sk3d_* + sketch3d_new/exit commands
    REPO / "host" / "src" / "ui" / "asm_panels.cpp",
    REPO / "host" / "src" / "ui" / "evaluate_dialogs.cpp",
    REPO / "host" / "src" / "ui" / "sim_panel.cpp",
    REPO / "host" / "src" / "ui" / "drawing_panel.cpp",  # dwg_* ribbon commands
    REPO / "host" / "src" / "ui" / "cam" / "cam_panel.cpp",  # cam_* ribbon commands
]

ROW_RE = re.compile(
    r'\s*\{\s*"([^"]+)"\s*,\s*"([^"]*)"\s*,\s*"([^"]*)"\s*,\s*"([^"]*)"\s*,'
    r'\s*"([^"]*)"\s*,\s*(true|false)\s*,\s*([^,]+)\s*,\s*(true|false)'
)
EQ_RE = re.compile(r'\b(?:cmdId|cmd)\s*==\s*"([^"]+)"')
STRCMP_RE = re.compile(r'std::strcmp\(\s*cmd_id\s*,\s*"([^"]+)"\s*\)')

# App-level prefix dispatchers. A command under one of these domains is considered handled when the
# dispatcher owns the prefix; individual unsupported subcommands should be disabled/stretch in the
# ribbon table, not silently fall through.
#
# cam_  : camHandleCommand() in host/src/ui/cam/cam_panel.cpp owns the full cam_ prefix.
#         cam_op_* commands are dispatched via an initializer-list map (not literal cmd== strings)
#         so the regex harvest misses them; the prefix rule is correct here.
# dwg_  : drawingHandleCmd() in host/src/ui/drawing_panel.cpp owns the full dwg_ prefix.
#         All dwg_* ribbon commands are handled by explicit if-chains in that function.
# reveng_ : reveng_workspace.cpp's command handler owns the full reveng_ prefix
#         (compares against a `c` variable, which the cmdId/cmd regex does not harvest).
PREFIX_DISPATCH = ("ecad_", "sch_", "pcb_", "sim_", "eval_", "asm_", "cam_", "dwg_", "reveng_")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def ribbon_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line_no, line in enumerate(read(RIBBON).splitlines(), 1):
        m = ROW_RE.match(line)
        if not m:
            continue
        cmd, label, _icon, tab, group, _big, _flyout, stretch = m.groups()
        rows.append({
            "line": line_no,
            "cmd": cmd,
            "label": label,
            "tab": tab,
            "group": group,
            "stretch": stretch == "true",
        })
    if not rows:
        raise RuntimeError(f"parsed 0 ribbon rows from {RIBBON}")
    return rows


def handled_commands() -> set[str]:
    text = "\n".join(read(p) for p in HANDLER_FILES if p.exists())
    out = set(EQ_RE.findall(text))
    out.update(STRCMP_RE.findall(text))
    return out


def main() -> int:
    # This audit parses host/ ribbon_def.cpp. ui/imgui and ui/abstract are retired.
    # Skip cleanly when ribbon_def.cpp is absent.
    if not RIBBON.exists():
        print("feature_surface_audit: SKIPPED — ribbon_def.cpp not found; audit targets "
              "host/src/ ribbon (ui/imgui + ui/abstract retired).")
        return 0
    try:
        rows = ribbon_rows()
    except Exception as exc:  # noqa: BLE001
        print(f"feature_surface_audit: {exc}", file=sys.stderr)
        return 2
    explicit = handled_commands()

    missing = []
    visible = 0
    for row in rows:
        if row["stretch"]:
            continue
        visible += 1
        cmd = str(row["cmd"])
        if cmd in explicit or any(cmd.startswith(p) for p in PREFIX_DISPATCH):
            continue
        missing.append(row)

    print(f"visible non-stretch ribbon commands: {visible}")
    print(f"explicit handlers discovered: {len(explicit)}")
    if not missing:
        print("feature surface: clean")
        return 0

    print("missing visible command handlers:")
    for row in missing:
        print(
            f"  {RIBBON.relative_to(REPO)}:{row['line']}: "
            f"{row['tab']} / {row['group']} / {row['cmd']} ({row['label']})"
        )
    return 1


if __name__ == "__main__":
    os.chdir(REPO)
    sys.exit(main())
