#!/usr/bin/env python3
"""
ui_style_check.py — T-U1 style-conformance checker for the liquid-glass panel kit.

Enforces the three rules docs/UI_PATTERN_LIBRARY.md commits every new feature panel
(T-U2..T-U8) to, so parallel agents can't quietly drift into eight different styles:

  rule=color     no literal `IM_COL32(...)` / `ImVec4(a,b,c,d)` color construction in
                 ui/imgui/src/ui/** or ui/abstract/** — colors come from theme:: / tokens.h
                 accessors only (docs/GLASS_DESIGN_SPEC.md §10). A fully-transparent literal
                 (all four components 0) is exempt — that's "clear this style slot", not a
                 color choice (e.g. `ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0,0,0,0))`).
  rule=ifdef     no OS `#ifdef _WIN32` / `#ifdef __APPLE__` / `#ifdef __linux__` /
                 `#if defined(...)` outside ui/imgui/src/platform/** — OS branching belongs in
                 the platform layer, not scattered through panel code.
  rule=viewmodel every ui/imgui/src/ui/*.cpp that instantiates `PanelScaffoldSpec` (i.e. is
                 built on the T-U1 scaffold, panel_scaffold.h) must #include at least one
                 header that resolves under ui/abstract/ — the panel's per-frame state lives
                 there as a pure struct (ui/abstract/artifact_view.h precedent), not inlined
                 in the .cpp. Panels NOT built on the scaffold are not checked by this rule
                 (retrofitting every pre-T-U1 panel is out of scope for this check).

Like scripts/gui_lint.py, pre-existing findings are absorbed into a baseline
(scripts/ui_style_check_baseline.json) so the check gates only NEW drift, not the entire
pre-refactor host/ history. Stdlib only.

Usage:
  python3 scripts/ui_style_check.py                         # all rules, baseline-filtered
  python3 scripts/ui_style_check.py --rule color
  python3 scripts/ui_style_check.py --report md
  python3 scripts/ui_style_check.py --update-baseline scripts/ui_style_check_baseline.json
  python3 scripts/ui_style_check.py --selftest              # seeds a violation in a temp
                                                              # file and asserts detection

Exit code: 0 = clean (after baseline), 1 = violations, 2 = bad invocation / selftest failure.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ROOTS = [
    os.path.join(REPO, "ui", "imgui", "src", "ui"),
    os.path.join(REPO, "ui", "abstract"),
]
PLATFORM_DIR = os.path.join(REPO, "ui", "imgui", "src", "platform")
ABSTRACT_DIR = os.path.join(REPO, "ui", "abstract")
DEFAULT_BASELINE = os.path.join(REPO, "scripts", "ui_style_check_baseline.json")

# ---- rule: color ------------------------------------------------------------------------------

COLOR_CALL_RE = re.compile(r"\b(IM_COL32|ImVec4)\s*\(([^)]*)\)")
NUM_RE = re.compile(r"-?\d+\.?\d*f?")


def _is_all_zero(args_src: str) -> bool:
    parts = [p.strip() for p in args_src.split(",")]
    if len(parts) != 4:
        return False
    for p in parts:
        m = NUM_RE.fullmatch(p)
        if not m:
            return False
        try:
            if float(p.rstrip("f")) != 0.0:
                return False
        except ValueError:
            return False
    return True


def _all_numeric_literals(args_src: str) -> bool:
    parts = [p.strip() for p in args_src.split(",")]
    if not parts or parts == [""]:
        return False
    return all(NUM_RE.fullmatch(p) for p in parts)


def check_color(rel: str, lines: list[str], findings: list):
    if rel.startswith("ui/imgui/src/theme/"):
        return  # theme/tokens.h IS the authorized place to define palette color literals
    for n, raw in enumerate(lines, 1):
        code = raw.split("//", 1)[0]
        if SUPPRESS_RE.search(raw):
            continue
        for m in COLOR_CALL_RE.finditer(code):
            func, args = m.group(1), m.group(2)
            if not _all_numeric_literals(args):
                continue  # e.g. IM_COL32(r,g,b,a) built from variables — not a literal color
            if _is_all_zero(args):
                continue  # transparent style-slot clear, not a color choice
            findings.append((rel, n, "color",
                             f"literal {func}({args.strip()}) — use theme::/tokens.h accessors",
                             raw.strip()))


# ---- rule: ifdef -------------------------------------------------------------------------------

IFDEF_RE = re.compile(r'^\s*#\s*if(?:def)?\s+(?:defined\s*\(?\s*)?(_WIN32|__APPLE__|__linux__|__unix__)\b')


def check_ifdef(rel: str, lines: list[str], findings: list):
    for n, raw in enumerate(lines, 1):
        if SUPPRESS_RE.search(raw):
            continue
        m = IFDEF_RE.match(raw)
        if m:
            findings.append((rel, n, "ifdef",
                             f"OS #ifdef {m.group(1)} outside ui/imgui/src/platform/",
                             raw.strip()))


# ---- rule: viewmodel ----------------------------------------------------------------------------

# Matches a local-variable declaration/construction (`PanelScaffoldSpec spec;` /
# `PanelScaffoldSpec spec{...}`) — NOT a `const PanelScaffoldSpec&` reference parameter (as seen
# in panel_scaffold.h/.cpp's own signatures, which implement the type and must not self-flag).
SCAFFOLD_SPEC_RE = re.compile(r"\bPanelScaffoldSpec\s+\w+\s*[;{]")
INCLUDE_RE = re.compile(r'^\s*#\s*include\s*"([^"]+)"')


def check_viewmodel(rel: str, path: str, lines: list[str], findings: list):
    if not rel.endswith(".cpp"):
        return
    text = "".join(lines)
    if not SCAFFOLD_SPEC_RE.search(text):
        return  # not a scaffold-built panel — rule doesn't apply
    src_dir = os.path.dirname(path)
    for raw in lines:
        m = INCLUDE_RE.match(raw)
        if not m:
            continue
        inc = m.group(1)
        candidates = [
            os.path.normpath(os.path.join(src_dir, inc)),
            os.path.normpath(os.path.join(ABSTRACT_DIR, os.path.basename(inc))),
        ]
        for cand in candidates:
            if os.path.commonpath([cand, ABSTRACT_DIR]) == ABSTRACT_DIR and os.path.exists(cand):
                return  # found a real ui/abstract/ header — conformant
    findings.append((rel, 1, "viewmodel",
                     "instantiates PanelScaffoldSpec but includes no ui/abstract/ view-model header",
                     ""))


# ---- driver -------------------------------------------------------------------------------------

SUPPRESS_RE = re.compile(r"ui-style:\s*(allow|ok)")


def iter_source_files(roots: list[str]):
    for root in roots:
        if os.path.isfile(root):
            yield root
            continue
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            if "build" in dirpath.split(os.sep):
                continue
            for fn in files:
                if fn.endswith((".cpp", ".h", ".hpp", ".cc")):
                    yield os.path.join(dirpath, fn)


def lint_file(path: str, rules: set[str], findings: list):
    rel = os.path.relpath(path, REPO).replace(os.sep, "/")
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()
    if "color" in rules:
        check_color(rel, lines, findings)
    if "ifdef" in rules:
        check_ifdef(rel, lines, findings)
    if "viewmodel" in rules:
        check_viewmodel(rel, path, lines, findings)


def finding_key(f) -> str:
    rel, _line, rule, msg, _snip = f
    return hashlib.sha1(f"{rel}|{rule}|{msg}".encode()).hexdigest()[:16]


def load_baseline(path):
    if not path or not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return set(json.load(fh).get("keys", []))


def run(roots: list[str], rules: set[str]) -> list:
    findings: list = []
    for path in iter_source_files(roots):
        lint_file(path, rules, findings)
    findings.sort(key=lambda f: (f[2], f[0], f[1]))
    return findings


# ---- selftest -------------------------------------------------------------------------------------

def selftest() -> int:
    """Seed one violation per rule in a scratch dir and assert this checker catches all three,
    then assert a clean file (correct usage) produces zero findings. No repo files touched."""
    with tempfile.TemporaryDirectory() as td:
        ui_dir = os.path.join(td, "ui", "imgui", "src", "ui")
        abstract_dir = os.path.join(td, "ui", "abstract")
        os.makedirs(ui_dir)
        os.makedirs(abstract_dir)

        bad_path = os.path.join(ui_dir, "seeded_bad_panel.cpp")
        with open(bad_path, "w", encoding="utf-8") as fh:
            fh.write(
                '#include "panel_scaffold.h"\n'
                "#if defined(_WIN32)\n"
                "static const bool kWin = true;\n"
                "#endif\n"
                "void drawSeededBad() {\n"
                "    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.6f, 0.6f, 0.6f, 1.0f));\n"
                "    ui::PanelScaffoldSpec spec;\n"
                "}\n"
            )

        good_path = os.path.join(ui_dir, "seeded_good_panel.cpp")
        with open(good_path, "w", encoding="utf-8") as fh:
            fh.write(
                '#include "panel_scaffold.h"\n'
                '#include "seeded_good_view.h"\n'
                "void drawSeededGood() {\n"
                "    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));\n"
                "    ImGui::TextColored(TVEC(txt1), \"ok\");\n"
                "    ui::PanelScaffoldSpec spec;\n"
                "}\n"
            )
        with open(os.path.join(abstract_dir, "seeded_good_view.h"), "w", encoding="utf-8") as fh:
            fh.write("#pragma once\nstruct SeededGoodModel {};\n")

        global ABSTRACT_DIR
        saved_abstract = ABSTRACT_DIR
        ABSTRACT_DIR = abstract_dir
        try:
            bad_findings = run([bad_path], {"color", "ifdef", "viewmodel"})
            good_findings = run([good_path], {"color", "ifdef", "viewmodel"})
        finally:
            ABSTRACT_DIR = saved_abstract

    bad_rules = {f[2] for f in bad_findings}
    ok = True
    for expect in ("color", "ifdef", "viewmodel"):
        if expect not in bad_rules:
            print(f"selftest FAIL: rule={expect} did not fire on the seeded violation", file=sys.stderr)
            ok = False
    if good_findings:
        print(f"selftest FAIL: conformant panel produced {len(good_findings)} finding(s): "
              f"{good_findings}", file=sys.stderr)
        ok = False

    if ok:
        print(f"selftest PASS: seeded violations caught ({sorted(bad_rules)}); "
              "conformant panel clean")
        return 0
    return 2


# ---- CLI -------------------------------------------------------------------------------------

def main(argv) -> int:
    ap = argparse.ArgumentParser(description="AI-CAD T-U1 panel style-conformance checker")
    ap.add_argument("roots", nargs="*", default=None)
    ap.add_argument("--rule", default="all", choices=["all", "color", "ifdef", "viewmodel"])
    ap.add_argument("--report", default="text", choices=["text", "md"])
    ap.add_argument("--baseline", default=DEFAULT_BASELINE,
                     help="JSON baseline to ignore known findings (default: %(default)s)")
    ap.add_argument("--no-baseline", action="store_true", help="ignore --baseline, show everything")
    ap.add_argument("--update-baseline", default=None, help="write current findings as baseline")
    ap.add_argument("--selftest", action="store_true", help="run the seeded-violation self-test")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    rules = {"color", "ifdef", "viewmodel"} if args.rule == "all" else {args.rule}
    roots = args.roots if args.roots else list(DEFAULT_ROOTS)

    findings = run(roots, rules)

    if args.update_baseline:
        keys = sorted({finding_key(f) for f in findings})
        with open(args.update_baseline, "w", encoding="utf-8") as fh:
            json.dump({"keys": keys, "count": len(keys)}, fh, indent=1)
        print(f"ui_style_check: wrote baseline with {len(keys)} keys -> {args.update_baseline}")
        return 0

    baseline = None if args.no_baseline else load_baseline(args.baseline)
    if baseline is not None:
        findings = [f for f in findings if finding_key(f) not in baseline]

    by_rule: dict = {}
    for f in findings:
        by_rule.setdefault(f[2], []).append(f)

    if args.report == "md":
        print(f"# UI style-conformance report\n\n**{len(findings)} finding(s)** "
              f"({', '.join(f'{k}: {len(v)}' for k, v in sorted(by_rule.items())) or 'none'})\n")
        for rule in sorted(by_rule):
            print(f"\n## rule: {rule} ({len(by_rule[rule])})\n")
            for rel, line, _r, msg, snip in by_rule[rule]:
                loc = f"{rel}:{line}" if line else rel
                print(f"- `{loc}` — {msg}" + (f"\n  ```cpp\n  {snip}\n  ```" if snip else ""))
    else:
        for rel, line, rule, msg, _snip in findings:
            loc = f"{rel}:{line}" if line else rel
            print(f"{loc}: [{rule}] {msg}")
        print(f"\n{len(findings)} finding(s): "
              f"{', '.join(f'{k}={len(v)}' for k, v in sorted(by_rule.items())) or 'clean'}")

    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
