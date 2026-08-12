#!/usr/bin/env python3
"""GUI sizing / icon / theme linter for the Chudware host (Dear ImGui, C++).

A lightweight "typechecker" for the GUI sizing standard in docs/GUI_STYLE_GUIDE.md. It catches the
exact bug classes the 2026-06 GUI audit found, so they cannot silently ship again and so the sizing
backlog can be cleared (and kept clear) deterministically:

  rule=icons  every getIcon/treeIcon/asmIcon/hasIcon/huBtn("name") and every ribbon_def.cpp row icon
              field (host/src) must name an icon in assets/icons/atlas.json. This is the bug that
              rendered the PM OK/Cancel buttons as a magenta "?": names 'ok'/'cancel' that the
              atlas never had.
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
ATLAS = os.path.join(REPO, "assets", "icons", "atlas.json")  # T-G11 de-dup: icons moved host/assets -> shared assets/
DEFAULT_ROOTS = [
    os.path.join(REPO, "host", "src"),
    os.path.join(REPO, "ui", "viewport"),
]
# Keep unscoped runs on the production GUI trees: the retired ui/imgui root made iter_source_files()
# yield nothing, so both scripts/check.py GUI gates stayed green without opening a source file.
DEFAULT_BASELINE = os.path.join(REPO, "scripts", "gui_lint_baseline.json")

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
    # ui/imgui (retired, see docs/PORT_LEDGER.md) — these paths are inert but kept for
    # historical reference in case ui/imgui commits are inspected via git show.
    # host/ equivalents should be added here when known.
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

# ---- rule "color": literal colors that bypass ui::palette / ui::kit::tone* -------------------------
# The theme rule only ever caught applyTheme(...,1.0f) and FontGlobalScale, so the actual house rule
# ("all panels read ONLY role fields — never hardcode ImVec4/ImU32 colors in panel code",
# ui_theme.h:1-3) was unenforced. These three patterns are the ways it actually gets broken.
#   IM_COL32(r,g,b,a) with NUMERIC channels — a literal color. IM_COL32 over a variable is fine.
#   ImVec4(r,g,b,a) with >=3 numeric channels inside a color context (PushStyleColor/TextColored/
#     ColorConvert/AddRect*/Add*Filled) — same thing spelled as floats.
#   GetStyleColorVec4(<not ImGuiCol_Text>) — substitutes ImGui's own style for the palette role.
COLOR_LITERAL_MACRO_RE = re.compile(
    r"\bIM_COL32\s*\(\s*\d[\d.f]*\s*,\s*\d[\d.f]*\s*,\s*\d[\d.f]*\s*(?:,\s*[\d.f]+\s*)?\)")
COLOR_LITERAL_VEC_RE = re.compile(
    r"\bImVec4\s*\(\s*[\d.f]+\s*,\s*[\d.f]+\s*,\s*[\d.f]+\s*,\s*[\d.f]+\s*\)")
COLOR_CONTEXT_RE = re.compile(
    r"\b(?:PushStyleColor|TextColored|ColorConvertFloat4ToU32|AddRectFilled|AddRect|AddCircleFilled|"
    r"AddCircle|AddLine|AddText|AddTriangleFilled|AddConvexPolyFilled)\s*\(")
# GetStyleColorVec4(ImGuiCol_Text) is the DOCUMENTED adaptive idiom, not a bypass: it reads the ink
# the caller pushed so a widget stays legible inside a glass bubble, a docked panel or a popup,
# where a fixed palette.textDim washes out (see ui::kit::inkSurface / dimText). Every OTHER
# ImGuiCol_* read IS a bypass — ImGuiCol_TextDisabled in particular substitutes ImGui's own style
# for the palette role and drifts from the theme.
GET_STYLE_COLOR_RE = re.compile(r"\bGetStyleColorVec4\s*\(\s*(?!ImGuiCol_Text\s*\))")
# An IM_COL32 on an AddImage* line is a TINT, and (255,255,255,255) there means "identity, draw the
# texture as authored" — not a color choice at all.
COLOR_IMAGE_TINT_RE = re.compile(r"\bAddImage(?:Rounded|Quad)?\s*\(")
# Fully-transparent and fully-opaque white/black are structural sentinels, not theme colors: a
# transparent ChildBg/WindowBg is "draw nothing", and 1,1,1,1 as a TINT is identity for AddImage.
COLOR_SENTINELS = ("ImVec4(0, 0, 0, 0)", "ImVec4(0,0,0,0)", "ImVec4(0.0f, 0.0f, 0.0f, 0.0f)",
                   "ImVec4(1, 1, 1, 1)", "ImVec4(1,1,1,1)", "ImVec4(1.0f, 1.0f, 1.0f, 1.0f)")
# Files that AUTHOR colors rather than consume roles. ui_theme.cpp defines the palettes; icons*.cpp
# bakes atlas tints; provider_logos.cpp and tesseract.cpp draw BRAND marks, whose colors are owned
# by the vendor / the logo spec and must NOT track the user's theme.
COLOR_AUTHORING_FILES = {
    "host/src/ui_theme.cpp",
    "host/src/ui/icons.cpp",
    "host/src/ui/icons_sdf.cpp",
    "host/src/ui/provider_logos.cpp",
    "host/src/ui/tesseract.cpp",
}

# ---- rule "glyph": codepoints the UI font does not carry ------------------------------------------
# This is an ALLOWLIST transcribed from the source of truth, ui_theme.cpp uiGlyphRanges() (line 662)
# and cadSymbolRanges() (675) — not a guessed blocklist. Anything outside these ranges was never
# requested from the TTF, so ImGui has no cell for it and draws the notdef: a literal '?' in the
# shipping UI. ui_theme.cpp:659-661 states the omission is deliberate ("Rubik does not ship them, so
# leaving them out keeps the atlas small"), so the fix is always at the call site, never the font.
#
# Consequence worth knowing, because it is counter-intuitive and has caused over-correction: the
# font DOES carry '…' U+2026, '—' U+2014, '·' U+00B7, '×' U+00D7 and '•' U+2022 — those are in
# range and render correctly, and the app ships them. What it does NOT carry is every arrow,
# checkmark, cross, filled/hollow circle, box-drawing rule and dingbat. Status marks belong in
# ui::kit::statusGlyph (vector) or the icon atlas, never in a string. Shipped as a '?' at least
# twice already: the 3D toggle's U+25CF and the PLM breadcrumb's U+25A3 + U+203A.
GLYPH_ALLOWED_RANGES = (
    (0x0000, 0x00FF),  # uiGlyphRanges: ASCII + Latin-1
    (0x2010, 0x2027),  # uiGlyphRanges: general punctuation (dashes, quotes, bullet, ellipsis)
    (0x2070, 0x209F),  # uiGlyphRanges: super/subscripts
    (0x2205, 0x2205),  # CADSymbols: empty set / diameter
    (0x2225, 0x2225),  # CADSymbols: parallel
    (0x2261, 0x2261),  # CADSymbols: identical to
    (0x2295, 0x2295),  # CADSymbols: circled plus
    (0x2299, 0x2299),  # CADSymbols: circled dot
    (0x22A5, 0x22A5),  # CADSymbols: perpendicular
    (0x2300, 0x2300),  # CADSymbols: diameter sign
    (0x25C6, 0x25C7),  # CADSymbols: filled/hollow diamond
)
UNICODE_ESCAPE_RE = re.compile(r"\\u[0-9a-fA-F]{4}")
STRING_LITERAL_RE = re.compile(r'"(?:[^"\\]|\\.)*"')


def glyph_supported(cp: int) -> bool:
    return any(lo <= cp <= hi for lo, hi in GLYPH_ALLOWED_RANGES)


def glyph_offenders(code: str):
    """Yield (kind, detail) for font-unsupported codepoints inside string literals on this line."""
    for lit in STRING_LITERAL_RE.finditer(code):
        text = lit.group(0)
        for esc in UNICODE_ESCAPE_RE.finditer(text):
            cp = int(esc.group(0)[2:], 16)
            if not glyph_supported(cp):
                yield ("escape", f"{esc.group(0)} (U+{cp:04X})")
        for ch in text:
            cp = ord(ch)
            if cp > 0x7F and not glyph_supported(cp):
                yield ("literal", f"U+{cp:04X} {ch!r}")


# Strings that never reach the font: log/trace sinks and model-facing prompt text. A '→' in a
# CEF_TRACE line or in a tool DESCRIPTION sent to the model is correct usage, not a render bug.
GLYPH_NONUI_RE = re.compile(
    r"\b(?:CEF_TRACE|addLog|std::printf|std::fprintf|printf|fprintf|snprintf_log|LOG_[A-Z]+|"
    r"TRACE|logLine|appendLog)\s*\(")
# tool_panel_tables.h is the MCP tool catalogue: every string in it is prompt text handed to the
# model, not an ImGui label. Ranges/arrows there are meaningful to the model and must stay.
GLYPH_NONUI_FILES = {"host/src/ui/tool_panel_tables.h"}
# ---- rule "dupe": a second implementation of a ui::kit primitive ----------------------------------
# The unification's whole point: ONE painter per composite widget. Per-domain entry points are
# welcome (hundreds of call sites spell them plmEmptyState / kit::EmptyState / awEmptyState), but
# their BODY must forward to ui::kit. A definition whose body never mentions ui::kit is a second
# implementation, and second implementations are how the app drifted into eight empty-state
# painters, six section headers and six spinner idioms in the first place.
KIT_PRIMITIVE_DEF_RE = re.compile(
    r"^\s*(?:static\s+|inline\s+)*(?:bool|void|float|int|ImU32|ImVec4|std::string)\s+"
    r"(\w*(?:[Ss]ectionHeader|[Ee]mptyState|[Bb]lockedState|[Ss]pinner|[Pp]rimaryButton|"
    r"[Ss]econdaryButton|[Dd]angerButton|[Cc]ardBegin|[Ss]tatusGlyph|[Pp]rogressBar|"
    r"[Ss]egmentedControl|[Ii]nlineStatus|[Vv]alueChip|[Tt]oggleChip))\s*\(")
KIT_FORWARD_WINDOW = 14   # lines of body inspected for a ui::kit:: call
KIT_OWN_FILES = {
    "host/src/ui/widgets/kit.cpp",
    "host/src/ui/widgets/kit.h",
    # The brand mark is the PRIMITIVE ui::kit::spinner and statusGlyph(Status::Running) are built
    # ON, not a duplicate of them. Forwarding it to the kit would be circular.
    "host/src/ui/tesseract.cpp",
    "host/src/ui/tesseract.h",
}


def is_kit_declaration(lines: list[str], idx: int) -> bool:
    """True when the construct starting at lines[idx] is a PROTOTYPE, not a definition.

    A declaration's statement terminates in ';'; a definition opens a body with '{'. Testing only
    whether the FIRST line ends in ';' was wrong: a prototype whose parameter list wraps (every
    multi-line signature in reveng_widgets.h / parts_widgets.h) has no ';' on line one, so seven
    headers were reported as second implementations of themselves. Scan forward instead and take
    whichever terminator appears first.
    """
    for raw in lines[idx:idx + 8]:
        code = raw.split("//", 1)[0]
        for ch in code:
            if ch == ";":
                return True
            if ch == "{":
                return False
    return False

def lint_file(path: str, atlas: set[str], rules: set[str], findings: list):
    rel = os.path.relpath(path, REPO).replace(os.sep, "/")
    is_ribbon_def = rel.endswith("ribbon_def.cpp")
    # World-space / base-style files report scale findings under the non-gating "scale-ws" rule.
    scale_rule = "scale-ws" if rel in SCALE_WORLDSPACE_FILES else "scale"
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()
    for n, raw in enumerate(lines, 1):
        line = raw.rstrip("\n")
        code = line.split("//", 1)[0]
        suppressed = bool(SUPPRESS_RE.search(line))

        if "icons" in rules:
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

        if "color" in rules and not suppressed and rel not in COLOR_AUTHORING_FILES:
            if COLOR_LITERAL_MACRO_RE.search(code) and not COLOR_IMAGE_TINT_RE.search(code):
                findings.append((rel, n, "color",
                                 "IM_COL32 with literal channels — use ui::palette role / "
                                 "ui::kit::toneU32 / maxContrastTextOn", line.strip()))
            if COLOR_CONTEXT_RE.search(code):
                for m in COLOR_LITERAL_VEC_RE.finditer(code):
                    if m.group(0).replace(" ", "") in [s.replace(" ", "") for s in COLOR_SENTINELS]:
                        continue
                    findings.append((rel, n, "color",
                                     f"literal {m.group(0)} in a color call — use a palette role "
                                     "or the AdaptiveSurface ink", line.strip()))
            if GET_STYLE_COLOR_RE.search(code):
                findings.append((rel, n, "color",
                                 "GetStyleColorVec4 — reads ImGui state, not the palette role; "
                                 "ignores the adaptive-surface ink", line.strip()))

        if ("glyph" in rules and not suppressed and rel not in GLYPH_NONUI_FILES
                and not GLYPH_NONUI_RE.search(code)):
            for kind, detail in glyph_offenders(code):
                findings.append((rel, n, "glyph",
                                 f"font has no glyph for {detail} ({kind}) — renders as '?'; "
                                 "use ui::kit::statusGlyph or an atlas icon", line.strip()))

        if "dupe" in rules and not suppressed and rel not in KIT_OWN_FILES:
            dm = KIT_PRIMITIVE_DEF_RE.match(code)
            if dm and not is_kit_declaration(lines, n - 1):
                body = "".join(lines[n - 1:n - 1 + KIT_FORWARD_WINDOW])
                if "ui::kit::" not in body and "kit::" not in body.split("(", 1)[-1]:
                    findings.append((rel, n, "dupe",
                                     f"{dm.group(1)}() paints its own {dm.group(1)} instead of "
                                     "forwarding to ui::kit — second implementations are how the "
                                     "app drifted into 8 empty-state painters", line.strip()))


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
    ap = argparse.ArgumentParser(description="Chudware GUI sizing/icon/theme linter")
    ap.add_argument("roots", nargs="*", default=None)
    ap.add_argument("--rule", default="all",
                    choices=["all", "icons", "theme", "scale", "color", "glyph", "dupe"])
    ap.add_argument("--report", default="text", choices=["text", "md"])
    ap.add_argument("--baseline", default=DEFAULT_BASELINE,
                    help="JSON baseline to ignore known findings (default: %(default)s)")
    ap.add_argument("--no-baseline", action="store_true",
                    help="ignore --baseline and show every finding")
    ap.add_argument("--update-baseline", default=None, help="write current findings as baseline")
    args = ap.parse_args(argv)

    if not os.path.exists(ATLAS):
        print(f"gui_lint: atlas not found at {ATLAS}", file=sys.stderr)
        return 2
    atlas = load_atlas_names(ATLAS)
    rules = ({"icons", "theme", "scale", "color", "glyph", "dupe"}
             if args.rule == "all" else {args.rule})

    explicit_roots = bool(args.roots)
    roots = args.roots if explicit_roots else list(DEFAULT_ROOTS)

    findings: list = []
    for path in iter_source_files(roots):
        lint_file(path, atlas, rules, findings)
    findings.sort(key=lambda f: (f[2], f[0], f[1]))

    if args.update_baseline:
        keys = sorted({finding_key(f) for f in findings})
        with open(args.update_baseline, "w", encoding="utf-8") as fh:
            json.dump({"keys": keys, "count": len(keys)}, fh, indent=1)
        print(f"gui_lint: wrote baseline with {len(keys)} keys -> {args.update_baseline}")
        return 0

    baseline = None if args.no_baseline else load_baseline(args.baseline)
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
