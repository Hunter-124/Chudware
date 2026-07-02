#!/usr/bin/env python3
"""
ui_inventory.py — AI-CAD UI element inventory for the human walkthrough kit.

Enumerates every ribbon tab/button (ribbon_def.cpp), command-palette command
(command_registry.cpp), PM form + field (panel_spec.cpp), settings tab/option
(settings_modal.cpp kSettingsEntries), and known viewport scenarios, then
emits a Markdown checklist with columns:

  id | area | location | launch | keep/fix/kill | notes

Supports --diff <previous-round.md> to mark NEW / CHANGED / REMOVED rows.

Usage:
  python3 scripts/ui_inventory.py [--repo /path/to/CAD] [--out OUTFILE]
      [--diff PREV_ROUND_MD]

Outputs to stdout or --out file.  Used by the round-1 kit generator.
"""

import argparse
import re
import sys
import os
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Repo paths
# ---------------------------------------------------------------------------

def repo_root(explicit: Optional[str]) -> Path:
    if explicit:
        return Path(explicit).resolve()
    # Climb from this script's location (scripts/ → repo root)
    here = Path(__file__).resolve().parent
    return here.parent


# ---------------------------------------------------------------------------
# 1. Ribbon commands  (ribbon_def.cpp)
# ---------------------------------------------------------------------------

def parse_ribbon_def(repo: Path) -> list[dict]:
    """
    Parse host/src/ui/ribbon_def.cpp and return list of ribbon command dicts.
    Each dict: {id, label, tab, group, big, sub_of, stretch}
    """
    src = repo / "host" / "src" / "ui" / "ribbon_def.cpp"
    if not src.exists():
        print(f"[WARN] ribbon_def.cpp not found at {src}", file=sys.stderr)
        return []

    text = src.read_text(encoding="utf-8")
    # Match rows inside kCmds[]:
    # { "cmd_id", "Label", "icon", "Tab", "Group", big, sub_of, stretch }
    row_re = re.compile(
        r'\{\s*"([^"]+)"\s*,\s*"([^"]+)"\s*,\s*"([^"]+)"\s*,\s*'
        r'"([^"]+)"\s*,\s*"([^"]+)"\s*,\s*(true|false)\s*,\s*'
        r'(nullptr|"[^"]*")\s*,\s*(true|false)\s*\}'
    )
    cmds = []
    for m in row_re.finditer(text):
        sub_raw = m.group(7)
        sub_of = None if sub_raw == "nullptr" else sub_raw.strip('"')
        cmds.append({
            "id": m.group(1),
            "label": m.group(2),
            "icon": m.group(3),
            "tab": m.group(4),
            "group": m.group(5),
            "big": m.group(6) == "true",
            "sub_of": sub_of,
            "stretch": m.group(8) == "true",
        })
    return cmds


# ---------------------------------------------------------------------------
# 2. Command-palette / workspace commands  (command_registry.cpp)
# ---------------------------------------------------------------------------

def parse_command_registry(repo: Path) -> list[dict]:
    """
    Parse ui/abstract/command_registry.cpp, return all Command table entries.
    Each dict: {id, label, workspace, group, kind, parent, keywords}
    """
    src = repo / "ui" / "abstract" / "command_registry.cpp"
    if not src.exists():
        print(f"[WARN] command_registry.cpp not found at {src}", file=sys.stderr)
        return []

    text = src.read_text(encoding="utf-8")

    # Map C++ enum shorthands to labels
    workspace_map = {
        "W::Model": "Model", "W::Sketch": "Sketch",
        "W::SheetMetal": "Sheet Metal", "W::Weldment": "Weldment",
        "W::Surface": "Surface", "W::Assemble": "Assemble",
        "W::Drafting": "Drafting", "W::Simulate": "Simulate",
        "W::Schematic": "Schematic", "W::Board": "Board",
    }
    kind_map = {
        "K::Top": "Top", "K::Flyout": "Flyout", "K::Variant": "Variant",
    }
    group_map = {
        "G::Sketch": "Sketch", "G::Create": "Create", "G::Modify": "Modify",
        "G::Pattern": "Pattern", "G::Construct": "Construct",
        "G::Inspect": "Inspect", "G::Output": "Output",
        "G::Appearance": "Appearance", "G::Draw": "Draw",
        "G::Dimension": "Dimension", "G::Relations": "Relations",
        "G::Finish": "Finish", "G::Study": "Study", "G::Setup": "Setup",
        "G::Run": "Run", "G::Insert": "Insert", "G::Position": "Position",
        "G::Display": "Display", "G::Views": "Views",
        "G::Annotate": "Annotate", "G::Tables": "Tables",
        "G::Sheet": "Sheet", "G::New": "New", "G::Verify": "Verify",
        "G::Place": "Place", "G::Auto": "Auto", "G::Analyze": "Analyze",
        "G::AI": "AI",
    }

    # Each table row spans one source line (they're single lines in the file):
    # { "id", "label", "icon", W::X, G::X, K::X, nullptr/"parent", "kw" },
    row_re = re.compile(
        r'\{\s*"([^"]+)"\s*,\s*"([^"]+)"\s*,\s*"[^"]+"\s*,\s*'
        r'(W::\w+)\s*,\s*(G::\w+)\s*,\s*(K::\w+)\s*,\s*'
        r'(nullptr|"[^"]*")\s*,\s*(nullptr|"[^"]*")\s*\}'
    )

    cmds = []
    for m in row_re.finditer(text):
        parent_raw = m.group(6)
        kw_raw = m.group(7)
        cmds.append({
            "id": m.group(1),
            "label": m.group(2),
            "workspace": workspace_map.get(m.group(3), m.group(3)),
            "group": group_map.get(m.group(4), m.group(4)),
            "kind": kind_map.get(m.group(5), m.group(5)),
            "parent": None if parent_raw == "nullptr" else parent_raw.strip('"'),
            "keywords": None if kw_raw == "nullptr" else kw_raw.strip('"'),
        })
    return cmds


# ---------------------------------------------------------------------------
# 3. PM forms + fields  (panel_spec.cpp)
# ---------------------------------------------------------------------------

def parse_panel_spec(repo: Path) -> list[dict]:
    """
    Parse ui/abstract/panel_spec.cpp, return all PanelSpec entries.
    Each dict: {feature_type, title, field_count, fields: [{key,label,group,kind}]}
    """
    src = repo / "ui" / "abstract" / "panel_spec.cpp"
    if not src.exists():
        print(f"[WARN] panel_spec.cpp not found at {src}", file=sys.stderr)
        return []

    text = src.read_text(encoding="utf-8")

    # Match top-level PanelSpec entries:  { "featureType", "Title", "icon", { ... }}
    spec_re = re.compile(
        r'\{\s*"([^"]+)"\s*,\s*"([^"]+)"\s*,\s*"[^"]+"\s*,\s*\{([^}]*(?:\{[^}]*\}[^}]*)*)\}\s*\}',
        re.DOTALL
    )

    # Individual field constructor calls — match helper name + first two string args (key, label)
    field_re = re.compile(
        r'(Dim|DimIf|DimU|Angle|AngleIf|Well|WellIf|Combo|ComboL|ComboLIf|Check|CheckIf|Int|Text|DatumRef|FeatWell|FeatWell1)'
        r'\s*\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*,\s*"([^"]+)"'
    )

    forms = []
    for m in spec_re.finditer(text):
        feature_type = m.group(1)
        title = m.group(2)
        body = m.group(3)
        fields = []
        for fm in field_re.finditer(body):
            fields.append({
                "key": fm.group(2),
                "label": fm.group(3),
                "group": fm.group(4),
                "kind": fm.group(1),
            })
        forms.append({
            "feature_type": feature_type,
            "title": title,
            "field_count": len(fields),
            "fields": fields,
        })
    return forms


# ---------------------------------------------------------------------------
# 4. Settings tabs + options  (settings_modal.cpp)
# ---------------------------------------------------------------------------

def parse_settings(repo: Path) -> list[dict]:
    """
    Parse host/src/ui/settings_modal.cpp kSettingsEntries[], return list of
    {label, tab_name, tab_idx}.
    """
    src = repo / "host" / "src" / "ui" / "settings_modal.cpp"
    if not src.exists():
        print(f"[WARN] settings_modal.cpp not found at {src}", file=sys.stderr)
        return []

    text = src.read_text(encoding="utf-8")
    # Match kSettingsEntries rows:  { "Label", "TabName", idx },
    row_re = re.compile(
        r'\{\s*"([^"]+)"\s*,\s*"([^"]+)"\s*,\s*(\d+)\s*\}'
    )
    entries = []
    in_block = False
    for line in text.splitlines():
        if "kSettingsEntries[]" in line:
            in_block = True
        if in_block:
            m = row_re.search(line)
            if m:
                entries.append({
                    "label": m.group(1),
                    "tab_name": m.group(2),
                    "tab_idx": int(m.group(3)),
                })
            if "kSettingsEntryCount" in line:
                break
    return entries


# ---------------------------------------------------------------------------
# 5. Known scenarios (for launch column)
# ---------------------------------------------------------------------------

KNOWN_SCENARIOS = {
    "m1":         "AI_CAD_SHOT=m1  — box+fillet+shell, PM active after fillet edit",
    "sketch":     "AI_CAD_SHOT=sketch — enter sketch, draw, add dim, exit to solid",
    "asm":        "AI_CAD_SHOT=asm — assembly with mate",
    "fea":        "AI_CAD_SHOT=fea — static FEA study",
    "cam":        "AI_CAD_SHOT=cam — CAM job fast-path",
    "flow":       "AI_CAD_SHOT=flow — external CFD flow study",
    "evalmass":   "AI_CAD_SHOT=evalmass — mass properties dialog",
    "activity":   "AI_CAD_SHOT=activity — agent timeline / activity panel",
    "multidoc":   "AI_CAD_SHOT=multidoc — multi-document mid-edit",
    "drawing":    "AI_CAD_SHOT=drawing — engineering drawing sheet with views",
    "appearance": "AI_CAD_SHOT=appearance — appearance/material scenario",
}

# Ribbon tabs → best scenario for launch
TAB_SCENARIO = {
    "Features":    "m1",
    "Sketch":      "sketch",
    "Evaluate":    "m1",
    "Simulation":  "fea",
    "Assembly":    "asm",
    "Sheet Metal": "m1",      # no dedicated SM scenario yet
    "Weldments":   "m1",
    "PCB":         None,
    "Surfaces":    "m1",
    "View Layout": "drawing",
    "Annotation":  "drawing",
    "Appearance":  "appearance",
    "Manufacture": "cam",
}


# ---------------------------------------------------------------------------
# 6. Load previous round for diff
# ---------------------------------------------------------------------------

def load_prev_ids(path: str) -> dict[str, str]:
    """
    Parse a previous round's checklist markdown, return
    {element_id: raw_row_line}.
    """
    ids: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                # look for table rows that start with | id_token |
                m = re.match(r'\|\s*`([^`]+)`\s*\|', line)
                if m:
                    ids[m.group(1)] = line.rstrip()
    except FileNotFoundError:
        print(f"[WARN] --diff file not found: {path}", file=sys.stderr)
    return ids


# ---------------------------------------------------------------------------
# 7. Emit checklist sections
# ---------------------------------------------------------------------------

def section_header(title: str, description: str = "") -> list[str]:
    lines = ["", f"## {title}", ""]
    if description:
        lines += [description, ""]
    lines += [
        "| id | area | location | launch | keep/fix/kill | notes |",
        "|----|------|----------|--------|---------------|-------|",
    ]
    return lines


def make_row(elem_id: str, area: str, location: str, launch: str,
             diff_tag: str = "") -> str:
    tag = f" **{diff_tag}**" if diff_tag else ""
    return f"| `{elem_id}`{tag} | {area} | {location} | {launch} | `[ ]` keep / `[ ]` fix / `[ ]` kill | |"


# ---------------------------------------------------------------------------
# 8. T-G2 NEEDS-LIVE seeded rows
# ---------------------------------------------------------------------------

NEEDS_LIVE_ROWS = [
    {
        "id": "doc_tab_plus_x",
        "area": "Doc Tabs",
        "location": "Doc-tab strip — '+' (new part) and 'x' (close tab) buttons",
        "launch": "Live app — open a part, click '+' then 'x' on the tab",
        "note": "**T-G2 NEEDS-LIVE #1**: code fix (NoBringToFrontOnFocus removed) in place; confirm click response",
    },
    {
        "id": "copilot_collapse_pill",
        "area": "Copilot Panel",
        "location": "Copilot header chevron → collapse to pill → click pill to re-expand",
        "launch": "Live app — Copilot panel visible, click chevron then pill",
        "note": "**T-G2 NEEDS-LIVE #2**: collapse-to-pill round trip requires live click",
    },
    {
        "id": "viewport_toolbar_clamp",
        "area": "Viewport Toolbar",
        "location": "Viewport toolbar left-edge clamp against FeatureManager column",
        "launch": "AI_CAD_SHOT=sketch — open sketch, pan a large sketch; check toolbar doesn't overlap FM",
        "note": "**T-G2 NEEDS-LIVE #3**: added in T-G1 polish; needs live sketch pan to confirm dodge",
    },
    {
        "id": "extrude_end_condition_dropdown",
        "area": "PM Form — Extrude",
        "location": "Extrude PM → End Condition dropdown (Direction 1)",
        "launch": "AI_CAD_SHOT=m1 (PM active after fillet edit) — or launch live and run Extrude",
        "note": "**T-G2 NEEDS-LIVE #4**: FrameBg fix applied; confirm dropdown text readable over light/dark",
    },
]


# ---------------------------------------------------------------------------
# Main emit
# ---------------------------------------------------------------------------

def emit_inventory(
    repo: Path,
    prev_ids: dict[str, str],
    out,
) -> dict:
    """
    Emit the full Markdown walkthrough checklist to `out` (file-like).
    Returns summary counts.
    """
    ribbon_cmds = parse_ribbon_def(repo)
    palette_cmds = parse_command_registry(repo)
    panel_forms = parse_panel_spec(repo)
    settings_entries = parse_settings(repo)

    counts = {
        "ribbon_rows": 0,
        "palette_rows": 0,
        "pm_forms": len(panel_forms),
        "pm_fields": sum(f["field_count"] for f in panel_forms),
        "settings_rows": 0,
        "needs_live_rows": len(NEEDS_LIVE_ROWS),
        "new_rows": 0,
        "removed_rows": 0,
    }

    seen_ids: set[str] = set()

    def write(*args):
        print(*args, file=out)

    def row_tag(elem_id: str) -> str:
        if not prev_ids:
            return ""
        if elem_id not in prev_ids:
            counts["new_rows"] += 1
            return "NEW"
        return ""

    # -----------------------------------------------------------------------
    # Header
    # -----------------------------------------------------------------------
    write("# AI-CAD UI Walkthrough Checklist — Round 1")
    write()
    write("**Instructions for Erik:**")
    write("1. Launch the app with the scenario shown in the *launch* column.")
    write("2. For each row, mark `keep`, `fix`, or `kill` and add free-text notes.")
    write("3. Return the annotated file as `work/questions/T-G12-feedback-round1.md`.")
    write("4. Must-fix findings block sign-off (T-G9/T-G11) until resolved.")
    write()
    write("**Convergence rule:** the loop repeats until a full pass yields zero new must-fix findings.")
    write("Each round regenerates this file from code via `python3 scripts/ui_inventory.py`.")
    write("The `--diff prev.md` flag highlights NEW / CHANGED / REMOVED rows.")
    write()

    # -----------------------------------------------------------------------
    # T-G2 NEEDS-LIVE items (highest priority — seed these first)
    # -----------------------------------------------------------------------
    write("---")
    write()
    write("## PRIORITY: T-G2 NEEDS-LIVE Click-Tests")
    write()
    write("These four items were flagged by T-G2 as requiring a live interactive session.")
    write("**Test these first** — they are the immediate unresolved debt from the last round.")
    write()
    write("| id | area | location | launch | keep/fix/kill | notes |")
    write("|----|------|----------|--------|---------------|-------|")
    for item in NEEDS_LIVE_ROWS:
        tag = row_tag(item["id"])
        tag_str = f" **{tag}**" if tag else ""
        note = item.get("note", "")
        write(f"| `{item['id']}`{tag_str} | {item['area']} | {item['location']} "
              f"| {item['launch']} | `[ ]` keep / `[ ]` fix / `[ ]` kill | {note} |")
        seen_ids.add(item["id"])
        counts["needs_live_rows"] += 0  # already counted above

    # -----------------------------------------------------------------------
    # Ribbon — grouped by tab
    # -----------------------------------------------------------------------
    write()
    write("---")

    tabs_seen: set[str] = set()
    tab_order: list[str] = []
    for cmd in ribbon_cmds:
        if cmd["tab"] not in tabs_seen:
            tabs_seen.add(cmd["tab"])
            tab_order.append(cmd["tab"])

    for tab in tab_order:
        tab_cmds = [c for c in ribbon_cmds if c["tab"] == tab]
        scn = TAB_SCENARIO.get(tab)
        launch_hint = KNOWN_SCENARIOS.get(scn, "Live app") if scn else "Live app"
        lines = section_header(
            f"Ribbon — {tab} Tab",
            f"Launch: `{launch_hint}`"
        )
        for ln in lines:
            write(ln)

        groups_seen: set[str] = set()
        group_order: list[str] = []
        for cmd in tab_cmds:
            if cmd["group"] not in groups_seen:
                groups_seen.add(cmd["group"])
                group_order.append(cmd["group"])

        for grp in group_order:
            grp_cmds = [c for c in tab_cmds if c["group"] == grp]
            for cmd in grp_cmds:
                elem_id = cmd["id"]
                seen_ids.add(elem_id)
                sub_note = f"sub-button of `{cmd['sub_of']}`" if cmd["sub_of"] else ""
                stretch_note = "⚠ stretch=disabled (Coming Soon)" if cmd["stretch"] else ""
                notes = "; ".join(filter(None, [sub_note, stretch_note]))
                tag = row_tag(elem_id)
                location = f"{tab} → {grp} → {cmd['label']}"
                if cmd["big"]:
                    location += " (big button)"
                write(make_row(elem_id, f"Ribbon/{tab}", location, f"`{scn or 'live'}`", tag)
                      .rstrip(" |") + f" {notes} |")
                counts["ribbon_rows"] += 1

    # -----------------------------------------------------------------------
    # Command Palette — deduplicated by id, grouped by workspace
    # -----------------------------------------------------------------------
    write()
    write("---")
    write()
    write("## Command Palette (⌘K) — All Commands")
    write()
    write("Searchable via ⌘K in the running app.  Top = primary button; Flyout = dropdown child; Variant = op variant.")
    write()
    write("| id | area | location | launch | keep/fix/kill | notes |")
    write("|----|------|----------|--------|---------------|-------|")

    palette_seen: set[str] = set()
    for cmd in palette_cmds:
        elem_id = f"palette/{cmd['id']}"
        if cmd["id"] in palette_seen:
            continue
        palette_seen.add(cmd["id"])
        tag = row_tag(elem_id)
        location = f"{cmd['workspace']} › {cmd['group']} › {cmd['label']} ({cmd['kind']})"
        kw_note = f"kw: {cmd['keywords']}" if cmd["keywords"] else ""
        parent_note = f"parent: `{cmd['parent']}`" if cmd["parent"] else ""
        notes = "; ".join(filter(None, [parent_note, kw_note]))
        write(make_row(elem_id, f"Palette/{cmd['workspace']}", location, "`⌘K` search", tag)
              .rstrip(" |") + f" {notes} |")
        counts["palette_rows"] += 1
        seen_ids.add(elem_id)

    # -----------------------------------------------------------------------
    # PM Forms + Fields
    # -----------------------------------------------------------------------
    write()
    write("---")
    write()
    write("## Property Manager (PM) Forms")
    write()
    write(f"Total: {len(panel_forms)} forms, {sum(f['field_count'] for f in panel_forms)} fields.  "
          "Each form opens when its ribbon command is clicked.")
    write()

    for form in panel_forms:
        ft = form["feature_type"]
        form_id = f"pm/{ft}"
        tag = row_tag(form_id)
        tag_str = f" **{tag}**" if tag else ""

        write()
        write(f"### PM Form: {form['title']} (`{ft}`)")
        write()
        write(f"*Launch:* ribbon command mapped to `{ft}` | {form['field_count']} fields")
        write()
        write("| id | area | location | launch | keep/fix/kill | notes |")
        write("|----|------|----------|--------|---------------|-------|")

        # Form-level row
        write(make_row(form_id + tag_str, f"PM/{ft}",
                       f"PM form — {form['title']} (header/title/OK-Cancel)",
                       f"Ribbon → {ft}", ""))
        seen_ids.add(form_id)
        counts["pm_forms"] += 0  # already counted

        for field in form["fields"]:
            fid = f"pm/{ft}/{field['key']}"
            f_tag = row_tag(fid)
            location = f"PM/{form['title']} → {field['group']} → {field['label']} ({field['kind']})"
            write(make_row(fid, f"PM/{ft}", location, f"Ribbon → {ft}", f_tag))
            seen_ids.add(fid)
        counts["pm_fields"] += 0  # already counted above

    # -----------------------------------------------------------------------
    # Settings tabs + options
    # -----------------------------------------------------------------------
    write()
    write("---")

    tabs_s: dict[str, list[dict]] = {}
    for e in settings_entries:
        tabs_s.setdefault(e["tab_name"], []).append(e)

    for tab_name, entries in tabs_s.items():
        lines = section_header(
            f"Settings — {tab_name} Tab",
            "Launch: ⌘K → 'Settings' or via top-menu gear icon"
        )
        for ln in lines:
            write(ln)
        for entry in entries:
            elem_id = f"settings/{tab_name.lower().replace(' ', '_')}/{entry['label'].lower().replace(' ', '_')}"
            tag = row_tag(elem_id)
            location = f"Settings → {tab_name} → {entry['label']}"
            write(make_row(elem_id, f"Settings/{tab_name}", location, "⌘K 'Settings'", tag))
            seen_ids.add(elem_id)
            counts["settings_rows"] += 1

    # -----------------------------------------------------------------------
    # Viewport / App chrome
    # -----------------------------------------------------------------------
    write()
    write("---")
    lines = section_header(
        "Viewport & App Chrome",
        "Elements visible in the 3D viewport and surrounding chrome."
    )
    for ln in lines:
        write(ln)

    chrome_items = [
        ("chrome/title_bar",       "Title Bar",         "App title bar (window drag, OS controls)",                   "Any scenario"),
        ("chrome/status_bar",      "Status Bar",         "Bottom status bar (messages, units, progress)",              "Any scenario"),
        ("chrome/ribbon_bar",      "Ribbon Bar",         "Tab strip + command groups at top",                          "m1"),
        ("chrome/fm_panel",        "Feature Manager",    "Left column: Feature tree / PropertyManager / Mate panel",   "m1"),
        ("chrome/copilot_panel",   "Copilot Panel",      "Right column: AI chat, composer, timeline",                  "activity"),
        ("chrome/doc_tabs",        "Doc Tabs",           "Doc-tab strip (part tabs, '+' new, 'x' close)",             "m1"),
        ("chrome/command_palette", "Command Palette",    "⌘K overlay — fuzzy search all commands + settings",          "Any (press ⌘K)"),
        ("chrome/viewport_triad",  "Viewport Triad",     "XYZ orientation triad in viewport corner",                   "m1"),
        ("chrome/view_cube",       "View Cube",          "ViewCube / orientation cube (if present)",                   "m1"),
        ("chrome/context_toolbar", "Context Toolbar",    "Floating toolbar above viewport (zoom, pan, orbit, section)","m1"),
        ("chrome/hud_pill",        "HUD Pill",           "Model info pill (material, bounding box dims) in viewport HUD","m1"),
        ("chrome/section_tool",    "Section Tool",       "Cross-section plane widget in viewport",                     "m1"),
        ("chrome/sketch_overlay",  "Sketch Overlay",     "Sketch entities + constraint overlay (inside sketch mode)",  "sketch"),
        ("chrome/part_appearance", "Part Appearance",    "Per-body swatch/glass colour overlay on viewport",           "appearance"),
        ("chrome/drawing_sheet",   "Drawing Sheet",      "Drawing sheet canvas (views, borders, title block)",         "drawing"),
        ("chrome/cam_panel",       "CAM Panel",          "CAM job tree + toolpath preview",                            "cam"),
        ("chrome/sim_panel",       "Simulation Panel",   "FEA study tree + results overlay",                           "fea"),
        ("chrome/equations_dialog","Equations Dialog",   "Equations / global variables dialog",                        "Live app — ⌘K 'Equations'"),
        ("chrome/cutlist_panel",   "Cut List Panel",     "Weldment cut list (structural members)",                     "Live app — weld_member then cut list"),
        ("chrome/config_panel",    "Configurations",     "Configuration manager panel",                                "Live app — ⌘K 'Configurations'"),
        ("chrome/design_table",    "Design Table",       "Design table (Excel-like config editor)",                    "Live app — ⌘K 'Design Table'"),
    ]

    for (elem_id, area, location, launch) in chrome_items:
        tag = row_tag(elem_id)
        write(make_row(elem_id, area, location, launch, tag))
        seen_ids.add(elem_id)

    # -----------------------------------------------------------------------
    # Removed items (in prev but not in current)
    # -----------------------------------------------------------------------
    if prev_ids:
        removed = [pid for pid in prev_ids if pid not in seen_ids]
        if removed:
            write()
            write("---")
            write()
            write("## REMOVED since previous round")
            write()
            write("These rows were in the previous checklist but are no longer in the codebase.")
            write()
            write("| id | previous location | notes |")
            write("|----|-------------------|-------|")
            for pid in removed:
                counts["removed_rows"] += 1
                write(f"| `{pid}` **REMOVED** | _(see prev round)_ | verify intentional deletion |")

    # -----------------------------------------------------------------------
    # Summary footer
    # -----------------------------------------------------------------------
    write()
    write("---")
    write()
    write("## Inventory Summary")
    write()
    write(f"| Category | Count |")
    write(f"|----------|-------|")
    write(f"| Ribbon commands (all tabs) | {counts['ribbon_rows']} |")
    write(f"| Palette commands (deduplicated) | {counts['palette_rows']} |")
    write(f"| PM forms | {len(panel_forms)} |")
    write(f"| PM fields (total across all forms) | {sum(f['field_count'] for f in panel_forms)} |")
    write(f"| Settings options | {counts['settings_rows']} |")
    write(f"| App chrome / viewport elements | {len(chrome_items)} |")
    write(f"| T-G2 NEEDS-LIVE seed items | {len(NEEDS_LIVE_ROWS)} |")
    if prev_ids:
        write(f"| NEW rows vs previous round | {counts['new_rows']} |")
        write(f"| REMOVED rows vs previous round | {counts['removed_rows']} |")
    write()
    write("*Generated from source by `scripts/ui_inventory.py`. Re-run after code changes to refresh.*")

    return counts


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Generate AI-CAD UI walkthrough checklist from source code."
    )
    parser.add_argument(
        "--repo", default=None,
        help="Path to the CAD repo root (default: auto-detect from script location)"
    )
    parser.add_argument(
        "--out", default=None,
        help="Output file path (default: stdout)"
    )
    parser.add_argument(
        "--diff", default=None,
        metavar="PREV_ROUND_MD",
        help="Previous round's checklist .md — rows absent here are marked NEW; "
             "rows absent there are marked REMOVED"
    )
    args = parser.parse_args()

    repo = repo_root(args.repo)
    print(f"[ui_inventory] repo root: {repo}", file=sys.stderr)

    prev_ids: dict[str, str] = {}
    if args.diff:
        prev_ids = load_prev_ids(args.diff)
        print(f"[ui_inventory] diff mode: {len(prev_ids)} ids from previous round", file=sys.stderr)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            counts = emit_inventory(repo, prev_ids, f)
        print(f"[ui_inventory] wrote {args.out}", file=sys.stderr)
    else:
        counts = emit_inventory(repo, prev_ids, sys.stdout)

    # Print summary to stderr so it's visible even when piping stdout
    print(file=sys.stderr)
    print("[ui_inventory] COUNTS:", file=sys.stderr)
    for k, v in counts.items():
        print(f"  {k}: {v}", file=sys.stderr)


if __name__ == "__main__":
    main()
