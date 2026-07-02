#!/usr/bin/env python3
"""GUI sizing / icon / theme linter for the AI-CAD host (Dear ImGui, C++).

A lightweight "typechecker" for the GUI sizing standard in docs/GUI_STYLE_GUIDE.md. It catches the
exact bug classes the 2026-06 GUI audit found, so they cannot silently ship again and so the sizing
backlog can be cleared (and kept clear) deterministically:

  rule=icons  every getIcon/treeIcon/asmIcon/hasIcon/huBtn("name") and every ribbon_def.cpp row icon
              field (host/src) must name an icon in host/assets/icons/atlas.json. Additionally,
              every icons::draw/icons::inlineIcon/icons::drawGlass("name") call under ui/imgui/src
              (Liquid Glass v2 N3 — SDF icon pipeline) must name an icon in
              ui/imgui/resources/icons/atlas.json UNION the hand-drawn glyph names UNION the alias
              map names (both hardcoded below, kept in sync with ui/imgui/src/theme/icons.cpp).
              This is the bug that rendered the PM OK/Cancel buttons as a magenta "?": names
              'ok'/'cancel' that the atlas never had.
  rule=theme  flags applyTheme(..., 1.0f) literal scale (collapses padding to 100% on HiDPI and
              clobbers ui::uiScale) and any io.FontGlobalScale assignment (runtime font blur).
  rule=scale  flags raw pixel literals passed to ImGui geometry calls that are NOT multiplied by a
              scale token (ui::uiScale / uiScale / sc / s / rsc / scale) or the metrics helpers
              (px()/U()), and not derived from a pre-scaled metric (CalcTextSize / GetFrameHeight /
              GetStyle() / GetContentRegion* / GetTextLineHeight*). This is heuristic; suppress a
              legitimate case with a trailing  // gui-lint: allow <reason>  comment, or absorb the
              existing-but-accepted set into a baseline (see --baseline / --update-baseline).

Usage:
  python scripts/gui_lint.py [ROOT ...]            # lint host/src (default), text output
  python scripts/gui_lint.py --rule icons          # one rule only (icons|theme|scale|all)
  python scripts/gui_lint.py --report md           # markdown report (for docs / PR comment)
  python scripts/gui_lint.py --baseline scripts/gui_lint_baseline.json
  python scripts/gui_lint.py --update-baseline scripts/gui_lint_baseline.json   # snapshot current

Exit code: 0 = clean (after baseline), 1 = violations, 2 = bad invocation. Stdlib only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ATLAS = os.path.join(REPO, "host", "assets", "icons", "atlas.json")
DEFAULT_ROOTS = [os.path.join(REPO, "host", "src")]

# ui/imgui (Liquid Glass v2) icon pipeline — separate atlas + namespaced call forms (contract §3).
UI_ATLAS = os.path.join(REPO, "ui", "imgui", "resources", "icons", "atlas.json")
UI_IMGUI_ROOT = os.path.join(REPO, "ui", "imgui", "src")

# Hand-drawn glyph names rendered by icons::drawGlyph() (ui/imgui/src/theme/icons.cpp) — these
# never appear in atlas.json but are valid icons::draw()/drawGlass() names. Keep in sync manually.
ICONS_HAND_DRAWN = {
    "ok_check", "success", "cancel_x", "win_close", "failure", "tab_close",
    "win_min", "win_max", "win_restore", "menu", "search", "rail_search",
    "tab_add", "plus", "chevron_down", "expand", "agent_send", "agent_stop",
    "agent", "sparkles", "thinking", "running", "action",
}

# Alias names accepted by icons::draw()/drawGlass() via resolveAtlasName()'s aliasMap()
# (ui/imgui/src/theme/icons.cpp) — the LHS "friendly" name resolves to a different atlas cell.
# Keep in sync manually.
ICONS_ALIAS_NAMES = {
    "extrude", "revolve", "hole", "coil", "pattern_linear", "pattern_circular",
    "prim_box", "prim_cylinder", "prim_sphere", "prim_cone",
    "analyze_thickness", "analyze_draft", "analyze_curvature",
    "sk_line", "sk_rect", "sk_circle", "sk_arc", "sk_polygon", "sk_slot", "sk_spline",
    "sk_ellipse", "sk_point", "sk_text", "sk_trim", "sk_extend", "sk_offset", "sk_convert",
    "sk_construction",
    "ws_sketch", "ws_model", "ws_assemble", "ws_simulate", "ws_draft", "ws_schematic",
    "ws_board", "ws_sheetmetal", "ws_weldment", "ws_surface",
    "sim_static", "sim_modal", "sim_flow", "sim_thermal", "sim_fatigue",
    "std_parts", "dwg_view", "dwg_projected", "dwg_section", "dwg_detail",
    "gdt", "datum", "balloon", "note", "title_block",
    "sch_symbol", "sch_wire", "net_label", "power_port", "pcb_route", "via", "copper_zone",
    "autoroute", "drc", "gerber", "board_3d", "ai_design",
    "file_part", "file_assembly", "file_drawing", "file_schematic", "file_pcb", "folder",
    "rail_tree", "rail_bodies", "rail_files", "rail_configs",
}

# Icon-name literal violations pre-dating this lint extension, kept green with a TODO rather than
# failing the gate (contract §3). Empty for now — add {"rel/path.cpp:line"} entries here if a
# future scan surfaces a pre-existing bad name; see finding text for the exact key to add.
# TODO(icons-lint): populate if/when a legitimate pre-existing violation is found.
ICONS_UI_ALLOWLIST: set[str] = set()

# ---- helpers --------------------------------------------------------------------------------------

SCALE_TOKENS = ("ui::uiScale", "uiScale", "rsc", "sc", "scale", "s", "S", "px(", "U(",
                "ui::metrics::px(", "ui::metrics::U(", "dpiS", "pxScale")
# Metric accessors whose result is already DPI-correct (pre-scaled fonts / scaled style).
METRIC_TOKENS = ("CalcTextSize", "GetFrameHeight", "GetTextLineHeight", "GetContentRegion",
                 "GetFontSize", "GetStyle(", "CalcItemWidth", "GetWindowWidth", "GetWindowHeight",
                 "GetCursorPos", "GetContentRegionAvail", "ItemSpacing", "FramePadding",
                 "ItemInnerSpacing", "IndentSpacing")

# ImGui calls whose numeric literal arguments are PIXEL GEOMETRY and therefore must be scaled.
GEOM_FUNCS = [
    "ImVec2", "SameLine", "Indent", "Unindent", "SetNextItemWidth", "Dummy",
    "AddLine", "AddRectFilled", "AddRect", "AddCircleFilled", "AddCircle", "AddNgon",
    "AddTriangleFilled", "AddTriangle", "SetCursorPosX", "SetCursorPosY",
    "SetNextWindowSize", "ImageButton", "PushTextWrapPos",
]
# These literals are ratios / sentinels / counts, not pixels — never flag.
SAFE_LITERALS = {0.0, 1.0, -1.0}
# A literal below this magnitude is treated as a ratio/multiplier (e.g. 0.5f, 0.3f) — not a pixel.
PIXEL_MIN = 2.0

# Files whose pixel literals are WORLD-SPACE draw-list art (canvas geometry drawn via a
# world->screen transform, NOT DPI chrome) or the authored base ImGuiStyle (scaled once by
# ScaleAllSizes). The scale rule produces only false positives here, so its findings in these files
# are reported under the NON-GATING "scale-ws" rule instead of "scale" — visible for audit, but they
# do not fail the gate. (Chrome exceptions inside these files can still be fixed; add a per-line
# "// gui-lint: allow" if a specific art literal is mis-flagged elsewhere.)
SCALE_WORLDSPACE_FILES = {
    "host/src/ui/sketch_mode.cpp",        # 2D sketch canvas — world (model) coordinates
    "host/src/ui/ecad/ecad_canvas.cpp",   # schematic / PCB canvas — world coordinates
    "host/src/ui/drawing_panel.cpp",      # drawing sheet — paper-space coordinates
    "host/src/ui_theme.cpp",              # base ImGuiStyle authored at 1.0x (ScaleAllSizes scales)
}

NUM_RE = re.compile(r"(?<![\w.])(-?\d+\.?\d*)f?(?![\w.\d])")
SUPPRESS_RE = re.compile(r"gui-lint:\s*(allow|ok)")
SCALE_MUL_RE = re.compile(
    r"(\*\s*(?:ui::uiScale|uiScale|rsc|sc|scale|s|S|dpiS|pxScale)\b)|"
    r"((?:ui::uiScale|uiScale|rsc|sc|scale|s|S|dpiS|pxScale)\b\s*\*)|"
    r"(?:ui::metrics::)?(?:px|U)\s*\("
)
COLOR_MACRO_RE = re.compile(r"\bIM_COL32\s*\([^)]*\)")


def load_atlas_names(path: str) -> set[str]:
    """Return the set of icon base-names. atlas.json is {"atlas_px":N, "cell":{"name@size":{...}}}."""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    cells = {}
    if isinstance(data, dict):
        cells = data.get("cell") or data.get("cells") or {}
    names: set[str] = set()
    for key in cells:
        names.add(key.split("@", 1)[0])
    if not names:
        print(f"gui_lint: WARNING — parsed 0 icon names from {path}; check atlas structure",
              file=sys.stderr)
    return names


def iter_source_files(roots: list[str]):
    for root in roots:
        if os.path.isfile(root):
            yield root
            continue
        for dirpath, _dirs, files in os.walk(root):
            if "build" in dirpath.split(os.sep):
                continue
            for fn in files:
                if fn.endswith((".cpp", ".h", ".hpp", ".cc")):
                    yield os.path.join(dirpath, fn)


def extract_call_args(line: str, func: str):
    """Yield the argument substring for each `func(` occurrence on the line (paren-balanced within
    the line; truncated at line end if the call spans lines — good enough for a heuristic)."""
    start = 0
    while True:
        idx = line.find(func + "(", start)
        if idx < 0:
            return
        # Reject as a substring of a longer identifier (e.g. "MyImVec2(").
        if idx > 0 and (line[idx - 1].isalnum() or line[idx - 1] == "_"):
            start = idx + 1
            continue
        i = idx + len(func) + 1
        depth = 1
        while i < len(line) and depth:
            c = line[i]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            i += 1
        yield line[idx + len(func) + 1: i - 1 if depth == 0 else len(line)]
        start = i


def literal_is_scaled(args: str, mstart: int, mend: int) -> bool:
    """True if the numeric literal at args[mstart:mend] is multiplied by a scale token, or the
    surrounding call clearly derives from a pre-scaled metric."""
    ctx = args[max(0, mstart - 16): min(len(args), mend + 16)]
    if SCALE_MUL_RE.search(ctx):
        return True
    # Lenient: if the whole call references a pre-scaled metric, assume the author is mixing
    # intentionally (these are rarer and noisier to flag); the audit covers the double-scale cases.
    if any(tok in args for tok in METRIC_TOKENS):
        return True
    return False


def literal_is_ratio(args: str, mstart: int, mend: int) -> bool:
    """Treat simple multiply/divide factors as ratios/counts, not pixel dimensions.

    Examples: `hitR * 2`, `bw * 3.0f`, `(p0.y + p1.y) * 0.5f`. Raw offsets like `x + 2` still flag.
    """
    left = args[max(0, mstart - 3):mstart]
    right = args[mend:min(len(args), mend + 3)]
    return "*" in left or "*" in right or "/" in left or "/" in right


def strip_color_macros(args: str) -> str:
    return COLOR_MACRO_RE.sub("IM_COL32()", args)


# ---- rules ----------------------------------------------------------------------------------------

ICON_CALL_RE = re.compile(r'\b(?:getIcon|treeIcon|asmIcon|hasIcon|huBtn)\s*\(\s*"([^"]+)"')
# ribbon_def.cpp rows: { "cmd_id", "Label", "icon", "Tab", ... } — capture the 3rd quoted string.
RIBBON_ROW_RE = re.compile(r'^\s*\{\s*"[^"]*"\s*,\s*"[^"]*"\s*,\s*"([^"]*)"\s*,')
APPLY_THEME_RE = re.compile(r'applyTheme\s*\([^,]+,\s*1(?:\.0*)?f?\s*\)')
FONT_GLOBAL_RE = re.compile(r'FontGlobalScale\s*=')

# ui/imgui (Liquid Glass v2 N3) icon call forms — see extract_call_args() for the paren-balanced
# argument extraction; the icon name is the first quoted literal within the call (icons::draw and
# icons::drawGlass take a leading ImDrawList* arg before the name; icons::inlineIcon does not).
UI_ICON_FUNCS = ("icons::draw", "icons::inlineIcon", "icons::drawGlass")
FIRST_STR_RE = re.compile(r'"([^"]*)"')


def lint_file(path: str, atlas: set[str], ui_names: set[str], rules: set[str], findings: list):
    rel = os.path.relpath(path, REPO).replace(os.sep, "/")
    is_ribbon_def = rel.endswith("ribbon_def.cpp")
    is_ui_imgui = rel.startswith("ui/imgui/src/")
    # World-space / base-style files report scale findings under the non-gating "scale-ws" rule.
    scale_rule = "scale-ws" if rel in SCALE_WORLDSPACE_FILES else "scale"
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()
    for n, raw in enumerate(lines, 1):
        line = raw.rstrip("\n")
        code = line.split("//", 1)[0]
        suppressed = bool(SUPPRESS_RE.search(line))

        if "icons" in rules:
            if is_ui_imgui:
                # Liquid Glass v2 N3: icons::draw/inlineIcon/drawGlass against the ui/imgui atlas
                # union hand-drawn glyphs union alias names (NOT the legacy host atlas — a
                # different, unrelated icon set lives at the same free-function names here, e.g.
                # ui::getIcon/treeIcon/asmIcon, which this rule intentionally does not re-validate;
                # contract §3 scopes the new check to icons::draw/inlineIcon/drawGlass only).
                for func in UI_ICON_FUNCS:
                    for args in extract_call_args(code, func):
                        sm = FIRST_STR_RE.search(args)
                        if not sm:
                            continue
                        name = sm.group(1)
                        key = f"{rel}:{n}"
                        if name and name not in ui_names and key not in ICONS_UI_ALLOWLIST:
                            findings.append((rel, n, "icons",
                                             f'icon "{name}" not in ui/imgui atlas '
                                             f'\u222a hand-drawn \u222a alias names', line.strip()))
            else:
                for m in ICON_CALL_RE.finditer(code):
                    name = m.group(1)
                    if name and name not in atlas:
                        findings.append((rel, n, "icons", f'icon "{name}" not in atlas.json',
                                         line.strip()))
                if is_ribbon_def:
                    rm = RIBBON_ROW_RE.match(code)
                    if rm:
                        name = rm.group(1)
                        if name and name not in atlas:
                            findings.append((rel, n, "icons",
                                             f'ribbon row icon "{name}" not in atlas.json', line.strip()))

        if "theme" in rules and not suppressed:
            if APPLY_THEME_RE.search(code):
                findings.append((rel, n, "theme",
                                 "applyTheme(..., 1.0f) — pass st.scale (collapses HiDPI sizing)",
                                 line.strip()))
            if FONT_GLOBAL_RE.search(code):
                findings.append((rel, n, "theme",
                                 "io.FontGlobalScale assignment — runtime font scaling blurs text",
                                 line.strip()))

        if "scale" in rules and not suppressed:
            for func in GEOM_FUNCS:
                if func + "(" not in code:
                    continue
                for args in extract_call_args(code, func):
                    args = strip_color_macros(args)
                    for nm in NUM_RE.finditer(args):
                        try:
                            val = abs(float(nm.group(1)))
                        except ValueError:
                            continue
                        if val < PIXEL_MIN or val in SAFE_LITERALS:
                            continue
                        if literal_is_ratio(args, nm.start(), nm.end()):
                            continue
                        if literal_is_scaled(args, nm.start(), nm.end()):
                            continue
                        findings.append((rel, n, scale_rule,
                                         f'raw px literal {nm.group(1)} in {func}(...) not *uiScale',
                                         line.strip()))


# ---- baseline -------------------------------------------------------------------------------------

def finding_key(f) -> str:
    rel, _line, rule, msg, _snip = f
    return hashlib.sha1(f"{rel}|{rule}|{msg}".encode()).hexdigest()[:16]


def load_baseline(path):
    if not path or not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return set(json.load(fh).get("keys", []))


# ---- main -----------------------------------------------------------------------------------------

def main(argv) -> int:
    ap = argparse.ArgumentParser(description="AI-CAD GUI sizing/icon/theme linter")
    ap.add_argument("roots", nargs="*", default=None)
    ap.add_argument("--rule", default="all", choices=["all", "icons", "theme", "scale"])
    ap.add_argument("--report", default="text", choices=["text", "md"])
    ap.add_argument("--baseline", default=None, help="JSON baseline to ignore known findings")
    ap.add_argument("--update-baseline", default=None, help="write current findings as baseline")
    args = ap.parse_args(argv)

    if not os.path.exists(ATLAS):
        print(f"gui_lint: atlas not found at {ATLAS}", file=sys.stderr)
        return 2
    atlas = load_atlas_names(ATLAS)
    ui_names: set[str] = set()
    if os.path.exists(UI_ATLAS):
        ui_names = load_atlas_names(UI_ATLAS) | ICONS_HAND_DRAWN | ICONS_ALIAS_NAMES
    rules = {"icons", "theme", "scale"} if args.rule == "all" else {args.rule}

    explicit_roots = bool(args.roots)
    roots = args.roots if explicit_roots else list(DEFAULT_ROOTS)

    findings: list = []
    for path in iter_source_files(roots):
        lint_file(path, atlas, ui_names, rules, findings)
    # The icons rule also covers ui/imgui/src (Liquid Glass v2 N3) by default — but only when the
    # caller didn't already scope roots explicitly (an explicit self-check invocation covers
    # exactly what it asked for; this avoids double-scanning the same files).
    if "icons" in rules and not explicit_roots:
        for path in iter_source_files([UI_IMGUI_ROOT]):
            lint_file(path, atlas, ui_names, {"icons"}, findings)
    findings.sort(key=lambda f: (f[2], f[0], f[1]))

    if args.update_baseline:
        keys = sorted({finding_key(f) for f in findings})
        with open(args.update_baseline, "w", encoding="utf-8") as fh:
            json.dump({"keys": keys, "count": len(keys)}, fh, indent=1)
        print(f"gui_lint: wrote baseline with {len(keys)} keys -> {args.update_baseline}")
        return 0

    baseline = load_baseline(args.baseline)
    if baseline is not None:
        findings = [f for f in findings if finding_key(f) not in baseline]

    by_rule = {}
    for f in findings:
        by_rule.setdefault(f[2], []).append(f)

    if args.report == "md":
        print(f"# GUI lint report\n\n**{len(findings)} finding(s)** "
              f"({', '.join(f'{k}: {len(v)}' for k, v in sorted(by_rule.items())) or 'none'})\n")
        for rule in sorted(by_rule):
            print(f"\n## rule: {rule} ({len(by_rule[rule])})\n")
            for rel, line, _r, msg, snip in by_rule[rule]:
                print(f"- `{rel}:{line}` — {msg}\n  ```cpp\n  {snip}\n  ```")
    else:
        for rel, line, rule, msg, _snip in findings:
            print(f"{rel}:{line}: [{rule}] {msg}")
        print(f"\n{len(findings)} finding(s): "
              f"{', '.join(f'{k}={len(v)}' for k, v in sorted(by_rule.items())) or 'clean'}")

    # "scale-ws" (world-space draw-list art / base style) is informational, not a gate failure.
    gating = [f for f in findings if f[2] != "scale-ws"]
    return 1 if gating else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
