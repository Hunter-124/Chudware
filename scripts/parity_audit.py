#!/usr/bin/env python3
"""Machine-checkable SolidWorks-parity audit for AI-opencad.

A "typechecker" for the parity standard in docs/SW_PARITY_SPEC.md (and its backlog twin
docs/SW_PARITY_GAPS.md). The spec is hand-maintained prose: every capability row carries a
status (`done` / `partial` / `missing` / `beyond`) and an Evidence cell naming the code that
backs it. Prose drifts — a row can stay `done` after its compiler is renamed, a ribbon command
deleted, or an RPC method removed. This script re-derives the machine-checkable subset of that
evidence FROM THE CODE and flags any `done`/`beyond` claim it cannot corroborate.

It is deliberately a SUBSET. Many acceptance conditions ("Newton/LM solver with analytic
Jacobians", "removes 3-4 DOF") are not mechanically checkable from a grep, and the script never
pretends otherwise — those rows are scored `unverifiable` (counted, never drift). What IS
checkable, and what we check, is the existence of the three concrete artifacts the spec's own
"engine + host where a UI is implied" rule promises:

  * an ENGINE COMPILER  — a ``@register("<type>")`` in engine/doc/features/*.py
  * a RIBBON COMMAND    — a row in host/src/ui/ribbon_def.cpp
  * a PM FORM           — a ``{ "<type>", ... }`` row / ``pmFindForm("<type>")`` in pm_forms.cpp
  * a WORKER RPC        — a ``"<method>": _fn`` entry in any engine/**/*.py WORKER_METHODS/METHODS

A row is matched to evidence by the artifact TOKENS it names (feature-type strings, ribbon
cmd_ids, RPC method names) appearing in its capability text + Evidence cell. When a `done`/`beyond`
row names a token whose KIND we can resolve but the artifact is absent from code, that is DRIFT.

Usage:
  python scripts/parity_audit.py                  # text scorecard, exit 0 (clean) / 1 (drift)
  python scripts/parity_audit.py --report md      # markdown report (docs / PR comment)
  python scripts/parity_audit.py --area Sketcher   # restrict to one area (substring match)
  python scripts/parity_audit.py --list-evidence  # dump the harvested code-evidence corpus

Exit code: 0 = no drift, 1 = drift found, 2 = bad invocation / parse error. Stdlib only.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = os.path.join(REPO, "docs", "SW_PARITY_SPEC.md")

# Code locations that back the spec's evidence cells.
FEATURES_DIR = os.path.join(REPO, "engine", "doc", "features")
RIBBON = os.path.join(REPO, "host", "src", "ui", "ribbon_def.cpp")
PM_FORMS = os.path.join(REPO, "host", "src", "ui", "pm_forms.cpp")
ENGINE_DIR = os.path.join(REPO, "engine")

STATUSES = ("done", "partial", "missing", "beyond")
# Statuses that make an affirmative code claim we can try to falsify. partial/missing don't:
# a partial/missing row is allowed to have no (or only stubbed) evidence by definition.
CLAIM_STATUSES = ("done", "beyond")


# ====================================================================================================
# 1. Harvest the code-evidence corpus.
#
# Each harvester returns the set of artifact tokens that actually exist in the tree. The audit then
# asks, per spec row, "does this row name a token of a kind I harvested, and is that token present?"
# ====================================================================================================

def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _iter_py(root: str):
    for dirpath, dirs, files in os.walk(root):
        # Skip caches / build trees so we only see live source.
        dirs[:] = [d for d in dirs if d not in ("__pycache__", "build", ".git", "node_modules")]
        for fn in files:
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


# --- engine feature compilers: @register("<type>") --------------------------------------------------

REGISTER_RE = re.compile(r'@register\(\s*["\']([a-zA-Z0-9_]+)["\']\s*\)')


def harvest_compilers() -> set[str]:
    """Every feature-type string with a registered compiler in engine/doc/features/*.py.

    This is the authoritative "does an engine compiler exist" oracle — the same decorator the
    compiler core (base.py register/get_compiler) keys on at runtime.
    """
    out: set[str] = set()
    if os.path.isdir(FEATURES_DIR):
        for fn in os.listdir(FEATURES_DIR):
            if fn.endswith(".py"):
                out.update(REGISTER_RE.findall(_read(os.path.join(FEATURES_DIR, fn))))
    return out


# Top-level ``def compile_<name>(...)`` entry points anywhere under engine/ (e.g. compile_document,
# compile_assembly, compile_boolean). Evidence cells cite these by name; they back a `done` claim
# just as a registered feature-type does, so a `compile_<x>` pointer is only drift when NEITHER a
# registered feature-type `<x>` NOR a ``def compile_<x>`` exists.
COMPILE_DEF_RE = re.compile(r'^\s*def\s+(compile_[a-z][a-z0-9_]+)\s*\(', re.M)


def harvest_compile_funcs() -> set[str]:
    """Every ``def compile_<name>`` function defined under engine/ (bare names incl. the prefix)."""
    out: set[str] = set()
    for path in _iter_py(ENGINE_DIR):
        out.update(COMPILE_DEF_RE.findall(_read(path)))
    return out


# --- ribbon commands: rows in ribbon_def.cpp --------------------------------------------------------
# Row shape: { "cmd_id", "Label", "icon", "Tab", "Group", big, sub_of, stretch }
RIBBON_ROW_RE = re.compile(r'^\s*\{\s*"([a-zA-Z0-9_]+)"\s*,\s*"[^"]*"\s*,\s*"[^"]*"\s*,\s*"[^"]*"\s*,')


def harvest_ribbon() -> set[str]:
    """Every cmd_id present as a row in the ribbon table (visible OR stretch-reserved)."""
    out: set[str] = set()
    for line in _read(RIBBON).splitlines():
        m = RIBBON_ROW_RE.match(line)
        if m:
            out.add(m.group(1))
    return out


# --- PM forms: form-type keys + pmFindForm("<type>") in pm_forms.cpp ---------------------------------
# kForms rows look like:  { "box", "Box", "extrude_boss", { ...fields... }},
PM_FORM_ROW_RE = re.compile(r'^\s*\{\s*"([a-zA-Z0-9_]+)"\s*,\s*"[^"]*"\s*,\s*"[^"]*"\s*,\s*\{')
PM_FIND_RE = re.compile(r'pmFindForm\(\s*"([a-zA-Z0-9_]+)"\s*\)')


def harvest_pm_forms() -> set[str]:
    """Every PropertyManager form-type the host can render (table rows + dispatched names)."""
    text = _read(PM_FORMS)
    out: set[str] = set()
    for line in text.splitlines():
        m = PM_FORM_ROW_RE.match(line)
        if m:
            out.add(m.group(1))
    out.update(PM_FIND_RE.findall(text))
    return out


# --- worker RPC methods: "<method>": _fn dict entries across engine/**/*.py -------------------------
# Catches the worker _METHODS literal, every WORKER_METHODS dict (drawing/instant3d/formats/...),
# and the ECAD METHODS dict — i.e. the full RPC surface, wherever it is declared. To avoid scooping
# unrelated ``"key": value`` literals out of plain data dicts, we only mine lines that fall INSIDE a
# dispatch-dict block: from a ``(WORKER_METHODS|_METHODS|METHODS) = {`` (or ``.update({``) opener
# down to the matching close. The value must be a bare function reference (``_fn`` / ``fn``), which
# excludes data rows whose values are strings, numbers, or further dicts.
RPC_OPEN_RE = re.compile(r'\b(?:WORKER_METHODS|_METHODS|METHODS)\b\s*(?:=|\.update\()\s*\{?\s*$')
# value is a bare function ref; allow a trailing comma and/or ``# comment`` (worker.py annotates
# several entries inline, e.g. ``"doc_sketch_regions": _fn,  # C51 ...``).
RPC_ENTRY_RE = re.compile(r'^\s*"([a-z][a-z0-9_]+)"\s*:\s*_?[A-Za-z][A-Za-z0-9_]*\s*,?\s*(?:#.*)?$')


def harvest_rpc() -> set[str]:
    """Every RPC method name registered in a worker dispatch dict anywhere under engine/."""
    out: set[str] = set()
    for path in _iter_py(ENGINE_DIR):
        text = _read(path)
        if "WORKER_METHODS" not in text and "_METHODS" not in text and \
           not re.search(r'^\s*METHODS\s*=', text, re.M):
            continue
        depth = 0  # brace depth while inside a dispatch-dict block (0 == outside)
        for line in text.splitlines():
            if depth == 0:
                if RPC_OPEN_RE.search(line):
                    depth = 1
                continue
            # Inside a block: harvest method entries, track nesting so we exit at the close.
            m = RPC_ENTRY_RE.match(line)
            if m:
                out.add(m.group(1))
            depth += line.count("{") + line.count("(")
            depth -= line.count("}") + line.count(")")
            if depth <= 0:
                depth = 0
    return out


# ====================================================================================================
# 2. Parse the spec into capability rows.
# ====================================================================================================

# An area header: "## 1. Sketcher (2D) — 80%".  We strip the leading number and trailing "— NN%".
AREA_RE = re.compile(r'^##\s+\d+\.\s+(.*?)\s*(?:—.*)?$')
# A capability table row: | Capability | acceptance | status | evidence |
ROW_RE = re.compile(r'^\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*$')
# Status cell may be wrapped in ** ** for emphasis (the partial/missing rows are).
STATUS_RE = re.compile(r'\*{0,2}(done|partial|missing|beyond)\*{0,2}', re.I)


class Row:
    """One capability row from a spec area table."""

    __slots__ = ("area", "capability", "acceptance", "status", "evidence", "line")

    def __init__(self, area, capability, acceptance, status, evidence, line):
        self.area = area
        self.capability = capability
        self.acceptance = acceptance
        self.status = status
        self.evidence = evidence
        self.line = line

    # The text we scan for artifact tokens: capability name + evidence cell. (Acceptance is the
    # SW condition — prose, no code tokens — so it is intentionally excluded to cut false hits.)
    def haystack(self) -> str:
        return f"{self.capability}\n{self.evidence}"


def parse_spec() -> list[Row]:
    text = _read(SPEC)
    if not text:
        raise RuntimeError(f"could not read spec at {SPEC}")
    rows: list[Row] = []
    area = None
    for n, line in enumerate(text.splitlines(), 1):
        am = AREA_RE.match(line)
        if am:
            area = am.group(1).strip()
            continue
        if area is None or not line.lstrip().startswith("|"):
            continue
        m = ROW_RE.match(line)
        if not m:
            continue
        cap, acc, status_cell, evid = m.groups()
        # Skip the table header row and the |---|---| separator.
        if cap.lower() in ("capability", ":---", "---") or set(cap) <= {"-", ":", " "}:
            continue
        sm = STATUS_RE.search(status_cell)
        if not sm:
            continue  # not a real capability row (e.g. the scorecard summary table)
        rows.append(Row(area, cap, acc, sm.group(1).lower(), evid, n))
    if not rows:
        raise RuntimeError(f"parsed 0 capability rows from {SPEC} — spec format changed?")
    return rows


# ====================================================================================================
# 3. Match a row's named artifact tokens to the harvested evidence, and decide drift.
#
# We never demand evidence we cannot identify. A row is only checkable when its text names a token
# we can classify (a registered feature-type, a known ribbon cmd_id pattern, an RPC method name).
# If we cannot classify ANY token, the row is `unverifiable` and never counts as drift — that is the
# conservatism rule applied to the auditor itself: silence is not a violation.
# ====================================================================================================

# Tokens that look like artifact ids but are generic English / cross-cutting and must never be
# treated as a missing-compiler claim (they appear in many evidence cells as plain words).
TOKEN_STOPWORDS = {
    "done", "partial", "missing", "beyond", "true", "false", "none", "null", "int", "ref",
    "value", "expr", "field", "test", "tests", "core", "mode", "mid", "blind", "flip", "merge",
    "side", "kind", "name", "type", "axis", "edge", "face", "vertex", "plane", "point", "body",
    "bodies", "and", "the", "via", "per", "for", "with", "from", "into", "over", "only", "not",
}

# Strip source filenames (`sketch_mode.cpp`, `ops.py`) BEFORE tokenising — a filename stem is not
# an artifact id and `sketch_mode` etc. would otherwise look like an RPC. Also strip C++ scope-
# qualified names (`SketchTool::Line`) which name host symbols, not our four artifact kinds.
FILENAME_RE = re.compile(r'\b[A-Za-z0-9_./]+\.(?:cpp|py|h|hpp|cc|md|json)\b')
CPP_SCOPED_RE = re.compile(r'\b[A-Za-z_]\w*::[A-Za-z_]\w*\b')
# A bare snake_case identifier (>=2 segments) — the shape every artifact id (feature-type, ribbon
# cmd_id, RPC method, PM-form key) takes. Single-word camelCase host helpers are intentionally
# ignored: they are not one of the four machine-checkable artifact kinds.
IDENT_RE = re.compile(r'\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b')


def _candidate_tokens(row: Row) -> set[str]:
    """Snake_case identifiers named in the row's capability + evidence text.

    Source = capability title + evidence cell, with filenames and C++ scoped names removed first.
    These multi-segment snake_case tokens are exactly the shape of our four artifact ids; stopwords
    (generic English / shared param names) are dropped to avoid false matches.
    """
    text = f"{row.capability} {row.evidence}"
    text = FILENAME_RE.sub(" ", text)
    text = CPP_SCOPED_RE.sub(" ", text)
    toks: set[str] = set()
    for ident in IDENT_RE.findall(text.lower()):
        if ident in TOKEN_STOPWORDS:
            continue
        toks.add(ident)
    return toks


class Evidence:
    """The harvested code corpus, with helpers to classify + look up a token."""

    def __init__(self) -> None:
        self.compilers = harvest_compilers()
        self.compile_funcs = harvest_compile_funcs()
        self.ribbon = harvest_ribbon()
        self.pm_forms = harvest_pm_forms()
        self.rpc = harvest_rpc()

    def classify(self, tok: str) -> str | None:
        """Return the artifact kind a token belongs to, or None if it is not an artifact id.

        A token is an artifact id only if it matches something we harvested. This is what keeps
        unrelated identifiers (`sketch_mode`, `bend_allowance`) from being mistaken for artifacts.
        """
        if tok in self.compilers:
            return "compiler"
        # `compile_boolean` / `compile_document` style references in evidence cells: a registered
        # feature-type OR a real top-level compile function both count as a compiler artifact.
        if tok.startswith("compile_") and (
                tok in self.compile_funcs or tok[len("compile_"):] in self.compilers):
            return "compiler"
        if tok in self.ribbon:
            return "ribbon"
        if tok in self.rpc:
            return "rpc"
        if tok in self.pm_forms:
            return "pm_form"
        return None


# A capability is VERIFIED when at least one artifact it names is present in the harvested corpus.
#
# Drift detection is deliberately PRECISE rather than fuzzy: the only unambiguous broken pointer we
# can prove is an explicit ``compile_<type>`` reference whose ``<type>`` has no registered compiler.
# (Fuzzy "this word looks like an RPC prefix" matching produces false positives — e.g. `sketch_mode`
# is a host file, not an RPC — so we do not do it.) This keeps every reported drift actionable.
COMPILE_REF_RE = re.compile(r'\bcompile_([a-z][a-z0-9_]+)\b')


def audit_row(row: Row, ev: Evidence) -> dict:
    """Classify a single spec row against the code evidence.

    Returns a dict: {result, found:[...], drift:[...], tokens:[...]}.
      result = "verified"      — a named artifact exists in code (claim corroborated)
               "unverifiable"  — the row names no machine-checkable artifact (counted, never drift)
               "drift"         — a done/beyond row names an artifact id provably ABSENT from code
               "n/a"           — partial/missing: not a falsifiable code claim here
    """
    toks = _candidate_tokens(row)
    found: list[tuple[str, str]] = []   # (token, kind) present in code
    drift: list[tuple[str, str]] = []   # (token, expected_kind) named but absent

    for tok in sorted(toks):
        kind = ev.classify(tok)
        if kind:
            found.append((tok, kind))

    # Precise drift: an explicit `compile_<type>` pointer that resolves to NEITHER a registered
    # feature-type NOR a real ``def compile_<type>`` function. Only a done/beyond row's broken
    # pointer counts (partial/missing rows may legitimately name not-yet-built things).
    if row.status in CLAIM_STATUSES:
        for ftype in COMPILE_REF_RE.findall(f"{row.capability} {row.evidence}"):
            ref = f"compile_{ftype}"
            if ftype not in ev.compilers and ref not in ev.compile_funcs:
                drift.append((ref, "compiler"))

    if drift:
        result = "drift"
    elif found:
        result = "verified"
    elif row.status in CLAIM_STATUSES:
        # done/beyond row but no machine-checkable artifact token was identifiable.
        result = "unverifiable"
    else:
        result = "n/a"

    return {"result": result, "found": found, "drift": drift, "tokens": sorted(toks)}


# ====================================================================================================
# 4. Scorecard + reporting.
# ====================================================================================================

class AreaScore:
    __slots__ = ("area", "done", "partial", "missing", "beyond", "verified",
                 "unverifiable", "drift", "drift_rows")

    def __init__(self, area: str) -> None:
        self.area = area
        self.done = self.partial = self.missing = self.beyond = 0
        self.verified = self.unverifiable = self.drift = 0
        self.drift_rows: list[tuple[Row, dict]] = []

    @property
    def total(self) -> int:
        return self.done + self.partial + self.missing + self.beyond

    @property
    def parity(self) -> float:
        """Same formula the spec uses: (done + beyond + 0.5*partial) / total."""
        if not self.total:
            return 0.0
        return (self.done + self.beyond + 0.5 * self.partial) / self.total

    @property
    def claims(self) -> int:
        """done + beyond rows = the rows that make a falsifiable code claim."""
        return self.done + self.beyond

    @property
    def coverage(self) -> float:
        """Fraction of claim rows whose code evidence we could actually corroborate."""
        if not self.claims:
            return 1.0
        return self.verified / self.claims


def score(rows: list[Row], ev: Evidence) -> dict[str, AreaScore]:
    scores: dict[str, AreaScore] = {}
    for row in rows:
        sc = scores.setdefault(row.area, AreaScore(row.area))
        setattr(sc, row.status, getattr(sc, row.status) + 1)
        if row.status not in CLAIM_STATUSES:
            continue
        res = audit_row(row, ev)
        if res["result"] == "verified":
            sc.verified += 1
        elif res["result"] == "unverifiable":
            sc.unverifiable += 1
        elif res["result"] == "drift":
            sc.drift += 1
            sc.drift_rows.append((row, res))
    return scores


def _bar(frac: float, width: int = 14) -> str:
    fill = int(round(frac * width))
    return "#" * fill + "-" * (width - fill)


def report_text(scores: dict[str, AreaScore], total_rows: int) -> None:
    print("SolidWorks parity audit — machine-checkable evidence for done/beyond claims\n")
    header = (f"{'Area':<28}{'parity':>8}{'claims':>8}{'verifd':>8}"
              f"{'unver':>7}{'DRIFT':>7}  coverage")
    print(header)
    print("-" * len(header))
    tot = AreaScore("TOTAL")
    for area in sorted(scores):
        sc = scores[area]
        for f in ("done", "partial", "missing", "beyond", "verified", "unverifiable", "drift"):
            setattr(tot, f, getattr(tot, f) + getattr(sc, f))
        tot.drift_rows.extend(sc.drift_rows)
        flag = "  <-- DRIFT" if sc.drift else ""
        print(f"{sc.area[:27]:<28}{sc.parity*100:>7.0f}%{sc.claims:>8}{sc.verified:>8}"
              f"{sc.unverifiable:>7}{sc.drift:>7}  {_bar(sc.coverage)} {sc.coverage*100:>3.0f}%{flag}")
    print("-" * len(header))
    print(f"{'TOTAL':<28}{tot.parity*100:>7.0f}%{tot.claims:>8}{tot.verified:>8}"
          f"{tot.unverifiable:>7}{tot.drift:>7}  {_bar(tot.coverage)} {tot.coverage*100:>3.0f}%")

    print(f"\n{total_rows} capability rows parsed; {tot.claims} make a done/beyond code claim "
          f"({tot.verified} corroborated, {tot.unverifiable} not machine-checkable).")

    if tot.drift_rows:
        print(f"\nDRIFT — {len(tot.drift_rows)} 'done/beyond' row(s) name an artifact absent from code:")
        for row, res in tot.drift_rows:
            miss = ", ".join(f"{t} (expected {k})" for t, k in res["drift"])
            print(f"  [{row.area}] {row.capability}")
            print(f"      SW_PARITY_SPEC.md:{row.line}  status={row.status}  missing: {miss}")
    else:
        print("\nNo drift: every machine-checkable done/beyond claim resolved to real code.")


def report_md(scores: dict[str, AreaScore], total_rows: int) -> None:
    drift_rows: list[tuple[Row, dict]] = []
    print("# SolidWorks parity audit\n")
    print("Machine-checkable evidence check for every `done`/`beyond` capability in "
          "`docs/SW_PARITY_SPEC.md`. *Coverage* = fraction of claim rows whose named artifact "
          "(engine compiler / ribbon command / PM form / worker RPC) was found in code.\n")
    print("| Area | Parity % | Claims | Verified | Unverifiable | Drift | Coverage |")
    print("|------|---------:|-------:|---------:|-------------:|------:|---------:|")
    tot = AreaScore("TOTAL")
    for area in sorted(scores):
        sc = scores[area]
        for f in ("done", "partial", "missing", "beyond", "verified", "unverifiable", "drift"):
            setattr(tot, f, getattr(tot, f) + getattr(sc, f))
        drift_rows.extend(sc.drift_rows)
        print(f"| {sc.area} | {sc.parity*100:.0f}% | {sc.claims} | {sc.verified} | "
              f"{sc.unverifiable} | {sc.drift} | {sc.coverage*100:.0f}% |")
    print(f"| **TOTAL** | **{tot.parity*100:.0f}%** | **{tot.claims}** | **{tot.verified}** | "
          f"**{tot.unverifiable}** | **{tot.drift}** | **{tot.coverage*100:.0f}%** |")
    print(f"\n_{total_rows} capability rows parsed._\n")
    if drift_rows:
        print(f"\n## Drift ({len(drift_rows)})\n")
        print("A `done`/`beyond` row names an artifact id of a known family that is **absent** "
              "from the code tree:\n")
        for row, res in drift_rows:
            miss = ", ".join(f"`{t}` ({k})" for t, k in res["drift"])
            print(f"- **{row.area} — {row.capability}** "
                  f"(`SW_PARITY_SPEC.md:{row.line}`, status `{row.status}`): missing {miss}")
    else:
        print("\n## Drift (0)\n\nNo drift — every machine-checkable claim resolved to real code.")


def report_evidence(ev: Evidence) -> None:
    print("Harvested code-evidence corpus:\n")
    for label, items in (("engine compilers (@register)", ev.compilers),
                         ("compile_* functions (engine/**)", ev.compile_funcs),
                         ("ribbon commands (ribbon_def.cpp)", ev.ribbon),
                         ("PM forms (pm_forms.cpp)", ev.pm_forms),
                         ("worker RPC methods (engine/**)", ev.rpc)):
        print(f"## {label} — {len(items)}")
        for it in sorted(items):
            print(f"  {it}")
        print()


# ====================================================================================================
# 5. main
# ====================================================================================================

def main(argv) -> int:
    ap = argparse.ArgumentParser(description="AI-opencad SolidWorks-parity drift auditor")
    ap.add_argument("--report", default="text", choices=["text", "md"],
                    help="output format (default text)")
    ap.add_argument("--area", default=None,
                    help="restrict to areas whose name contains this substring (case-insensitive)")
    ap.add_argument("--list-evidence", action="store_true",
                    help="dump the harvested code-evidence corpus and exit")
    args = ap.parse_args(argv)

    if not os.path.exists(SPEC):
        print(f"parity_audit: spec not found at {SPEC}", file=sys.stderr)
        return 2

    ev = Evidence()
    if not ev.compilers:
        print(f"parity_audit: WARNING — harvested 0 feature compilers from {FEATURES_DIR}; "
              "evidence matching will be unreliable", file=sys.stderr)

    if args.list_evidence:
        report_evidence(ev)
        return 0

    try:
        rows = parse_spec()
    except Exception as exc:  # noqa: BLE001
        print(f"parity_audit: {exc}", file=sys.stderr)
        return 2

    if args.area:
        needle = args.area.lower()
        rows = [r for r in rows if needle in r.area.lower()]
        if not rows:
            print(f"parity_audit: no area matched '{args.area}'", file=sys.stderr)
            return 2

    scores = score(rows, ev)
    if args.report == "md":
        report_md(scores, len(rows))
    else:
        report_text(scores, len(rows))

    drift = sum(sc.drift for sc in scores.values())
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
