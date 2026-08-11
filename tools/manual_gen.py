#!/usr/bin/env python3
"""tools/manual_gen.py — T-Q4 user-manual auto-generation.

Composes ``docs/manual/*.md`` (and, best-effort, a rendered ``user_manual`` PDF/DOCX via
``deliverables.make_report``) from three truth sources already in the repo — never invented
prose:

- ``scripts/feature_surface_audit.ribbon_rows`` — every ribbon command (id/label/tab/group),
  so the "Commands Reference" section covers 100% of the UI surface.
- ``services.mcp.cad_tools.cad_guide_text`` — the agent's own modeling-guide prose (overview,
  simulation, intake, gsl, cam, narration, deliverables, reverse-engineering) and, per-command,
  its per-feature cheatsheets (``fillet``, ``chamfer``, ``shell``, ...) when one exists for a
  ribbon command's id.
- ``contracts/*.md`` — a one-line-per-contract summary (filename + its first ``# `` heading).

Provenance rule (no fabrication): a ribbon command gets a real description ONLY when
``cad_guide_text(command_id)`` resolves to actual cheatsheet prose (checked by absence of the
"No guide for" fallback marker cad_guide_text returns for an unknown topic). Every other command
gets a visibly generic AUTO-STUB sourced only from its own tab/group — the inventory is the truth
source, so the manual never states a capability nobody wrote down.

Output is fully deterministic (no timestamps, no wallclock, sorted/stable iteration everywhere)
so ``--check`` can byte-compare freshly generated markdown against the committed baseline as a
drift gate (wired into ``scripts/gate.sh`` as a non-fatal advisory stage).

CLI:
    python -m tools.manual_gen            regenerate docs/manual/*.md, print coverage
    python -m tools.manual_gen --check    drift + coverage check only; exit 1 if not clean
    python -m tools.manual_gen --pdf      also render the user_manual report (best-effort)
"""
from __future__ import annotations

import sys
from pathlib import Path

# --------------------------------------------------------------------------- repo-root plumbing

def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _ensure_sys_path(repo: Path) -> None:
    s = str(repo)
    if s not in sys.path:
        sys.path.insert(0, s)


# --------------------------------------------------------------------------- guide topics

# Fixed, documented order (matches the topic list in services.mcp.cad_tools.cad_guide_text's own
# "No guide for ..." fallback message) — deterministic regardless of dict/set iteration.
GUIDE_TOPICS = [
    "overview", "review", "simulation", "intake", "gsl", "cam", "narration",
    "deliverables", "reverse",
]

_NO_GUIDE_MARKER = "No guide for"

_AUTO_STUB_TEMPLATE = "_Available from the {tab} ▸ {group} ribbon group._"


# --------------------------------------------------------------------------- collect

def collect(repo: Path) -> dict:
    """Gather ribbon commands, cad_guide prose, and a contracts summary. Never raises."""
    repo = Path(repo)
    _ensure_sys_path(repo)

    ribbon: list[dict] = []
    try:
        # scripts/ui_inventory.py was retired with the ui/imgui shell; the live
        # ribbon parser is feature_surface_audit.ribbon_rows (cmd → id mapping here).
        from scripts.feature_surface_audit import ribbon_rows  # noqa: PLC0415
        ribbon = [
            {"id": r["cmd"], "label": r["label"], "tab": r["tab"], "group": r["group"]}
            for r in ribbon_rows()
        ]
    except Exception:  # noqa: BLE001 - inventory is best-effort; never raise
        ribbon = []

    try:
        from services.mcp.cad_tools import cad_guide_text  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - guide is best-effort; the inventory is the real spine
        cad_guide_text = None

    guide: dict[str, str] = {}
    if cad_guide_text is not None:
        for topic in GUIDE_TOPICS:
            try:
                text = cad_guide_text(topic)
            except Exception:  # noqa: BLE001 - skip a topic that errors, per spec
                continue
            if isinstance(text, str) and text.strip():
                guide[topic] = text.strip()

    # Per-command cheatsheet mapping: only a REAL hit (no "No guide for" fallback) counts.
    command_guides: dict[str, str] = {}
    if cad_guide_text is not None:
        for row in ribbon:
            cid = row.get("id")
            if not cid or cid in command_guides:
                continue
            try:
                text = cad_guide_text(cid)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(text, str) and text.strip() and not text.startswith(_NO_GUIDE_MARKER):
                command_guides[cid] = text.strip()

    contracts = []
    cdir = repo / "contracts"
    if cdir.is_dir():
        for f in sorted(cdir.glob("*.md"), key=lambda p: p.name):
            heading = ""
            try:
                with f.open("r", encoding="utf-8") as fh:
                    for _ in range(5):
                        line = fh.readline()
                        if not line:
                            break
                        line = line.strip()
                        if line.startswith("# "):
                            heading = line[2:].strip()
                            break
            except Exception:  # noqa: BLE001
                pass
            contracts.append({"filename": f.name, "heading": heading})

    return {"ribbon": ribbon, "guide": guide, "command_guides": command_guides,
            "contracts": contracts}


# --------------------------------------------------------------------------- markdown builders

def _command_one_liner(row: dict, command_guides: dict) -> str:
    cid = row.get("id")
    hit = command_guides.get(cid)
    if hit:
        # First non-empty line of the real guide text — already a compact one-liner for the
        # cheatsheets that exist (e.g. "FILLET - round off edges with a radius.").
        for line in hit.splitlines():
            line = line.strip()
            if line:
                return line
    return _AUTO_STUB_TEMPLATE.format(tab=row.get("tab", "?"), group=row.get("group", "?"))


def _sorted_ribbon(ribbon: list[dict]) -> list[dict]:
    return sorted(ribbon, key=lambda r: (r.get("tab", ""), r.get("group", ""),
                                          r.get("label", ""), r.get("id", "")))


def build_markdown(data: dict) -> dict[str, str]:
    """Compose deterministic markdown docs/manual/*.md content. No timestamps, no wallclock."""
    ribbon = _sorted_ribbon(data.get("ribbon", []))
    guide = data.get("guide", {})
    command_guides = data.get("command_guides", {})
    contracts = data.get("contracts", [])

    docs: dict[str, str] = {}

    # ---- index.md ----
    lines = ["# Chudware User Manual", "", "Auto-generated from the ribbon command inventory, the",
             "in-app modeling guide, and the frozen contract set — see `tools/manual_gen.py`.",
             "", "## Contents", "", "- [Overview](overview.md)", "- [Commands Reference](commands.md)"]
    for topic in GUIDE_TOPICS:
        if topic in guide:
            lines.append(f"- [Guide: {topic.capitalize()}](guide-{topic}.md)")
    lines.append("- [Contracts Summary](contracts.md)")
    lines.append("")
    docs["index.md"] = "\n".join(lines) + "\n"

    # ---- overview.md ----
    lines = ["# Overview", ""]
    if "overview" in guide:
        lines += ["```text", guide["overview"], "```", ""]
    else:
        lines += ["_No overview guide available._", ""]
    docs["overview.md"] = "\n".join(lines) + "\n"

    # ---- commands.md ----
    lines = ["# Commands Reference", "",
             f"Every ribbon command ({len(ribbon)} total), grouped by tab and ribbon group.", ""]
    cur_tab = None
    cur_group = None
    for row in ribbon:
        tab = row.get("tab", "?")
        group = row.get("group", "?")
        if tab != cur_tab:
            lines.append(f"## {tab}")
            lines.append("")
            cur_tab = tab
            cur_group = None
        if group != cur_group:
            lines.append(f"### {group}")
            lines.append("")
            cur_group = group
        lines.append(f"#### {row.get('label', row.get('id', '?'))}")
        lines.append("")
        lines.append(_command_one_liner(row, command_guides))
        lines.append("")
    docs["commands.md"] = "\n".join(lines) + "\n"

    # ---- guide-<topic>.md ----
    for topic in GUIDE_TOPICS:
        if topic not in guide:
            continue
        lines = [f"# Guide: {topic.capitalize()}", "", "```text", guide[topic], "```", ""]
        docs[f"guide-{topic}.md"] = "\n".join(lines) + "\n"

    # ---- contracts.md ----
    lines = ["# Contracts Summary", "",
             f"{len(contracts)} frozen contracts under `contracts/`:", ""]
    for c in contracts:
        heading = c["heading"] or "(no heading found)"
        lines.append(f"- `{c['filename']}` — {heading}")
    lines.append("")
    docs["contracts.md"] = "\n".join(lines) + "\n"

    return docs


# --------------------------------------------------------------------------- write / drift

def write_manual(repo: Path, data: dict) -> list[Path]:
    repo = Path(repo)
    out_dir = repo / "docs" / "manual"
    out_dir.mkdir(parents=True, exist_ok=True)
    docs = build_markdown(data)
    written = []
    for name in sorted(docs):
        p = out_dir / name
        p.write_text(docs[name], encoding="utf-8")
        written.append(p)
    return written


def check_drift(repo: Path) -> bool:
    """True if the committed docs/manual/*.md matches a fresh in-memory regen, byte-for-byte."""
    repo = Path(repo)
    fresh = build_markdown(collect(repo))
    manual_dir = repo / "docs" / "manual"
    if not manual_dir.is_dir():
        return False
    on_disk = {}
    for p in manual_dir.glob("*.md"):
        on_disk[p.name] = p.read_text(encoding="utf-8")
    return on_disk == fresh


def coverage(repo: Path) -> dict:
    """Assert every ribbon command label appears in the generated commands.md."""
    repo = Path(repo)
    data = collect(repo)
    docs = build_markdown(data)
    commands_md = docs.get("commands.md", "")
    ribbon = data.get("ribbon", [])
    total = len(ribbon)
    missing = []
    for row in ribbon:
        label = row.get("label", "")
        if f"#### {label}" not in commands_md:
            missing.append(label)
    covered = total - len(missing)
    return {"total": total, "covered": covered, "missing": missing}


# --------------------------------------------------------------------------- report spec / pdf

def build_report_spec(data: dict, fmt: str = "pdf") -> dict:
    """A make_report spec for the ``user_manual`` template — free prose only, no lb slots."""
    ribbon = _sorted_ribbon(data.get("ribbon", []))
    guide = data.get("guide", {})
    command_guides = data.get("command_guides", {})
    contracts = data.get("contracts", [])

    outline = [
        {"type": "title", "body": [
            "Chudware User Manual",
            "Auto-generated from the ribbon command inventory, the in-app modeling guide, "
            "and the frozen contract set.",
        ]},
    ]

    if "overview" in guide:
        outline.append({"type": "section", "heading": "Overview",
                         "body": [ln for ln in guide["overview"].splitlines() if ln.strip()]})

    cmd_body = [f"{row.get('label', row.get('id', '?'))} — "
                f"{_command_one_liner(row, command_guides)}" for row in ribbon]
    outline.append({"type": "section", "heading": "Commands Reference", "body": cmd_body})

    for topic in GUIDE_TOPICS:
        if topic == "overview" or topic not in guide:
            continue
        outline.append({"type": "section", "heading": f"Guide: {topic.capitalize()}",
                         "body": [ln for ln in guide[topic].splitlines() if ln.strip()]})

    contracts_body = [f"{c['filename']} — {c['heading'] or '(no heading found)'}"
                       for c in contracts]
    outline.append({"type": "section", "heading": "Contracts Summary", "body": contracts_body})

    return {
        "kind": "report",
        "title": "Chudware User Manual",
        "template": "user_manual",
        "format": fmt,
        "outline": outline,
        "attached_results": {},
    }


def render_pdf(repo: Path, spec: dict) -> dict:
    """Best-effort render via deliverables.make_report; never raises."""
    try:
        from services.deliverables import make_report  # noqa: PLC0415
    except ImportError as e:
        return {"ok": False, "reason": "import_error", "hint": str(e)}
    try:
        return make_report(spec)
    except Exception as e:  # noqa: BLE001 - the markdown is primary, the PDF is best-effort
        return {"ok": False, "reason": "render_error", "hint": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------------------- CLI

def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="drift + coverage check only")
    ap.add_argument("--pdf", action="store_true", help="also render the user_manual report")
    args = ap.parse_args(argv)

    repo = _repo_root()

    if args.check:
        drift_clean = check_drift(repo)
        cov = coverage(repo)
        print(f"drift: {'CLEAN' if drift_clean else 'DRIFT'}")
        print(f"coverage: {cov['covered']}/{cov['total']} (missing={len(cov['missing'])})")
        if cov["missing"]:
            print(f"missing labels: {cov['missing']}")
        ok = drift_clean and not cov["missing"]
        return 0 if ok else 1

    data = collect(repo)
    written = write_manual(repo, data)
    cov = coverage(repo)
    print(f"wrote {len(written)} files to docs/manual/")
    print(f"coverage: {cov['covered']}/{cov['total']} (missing={len(cov['missing'])})")

    if args.pdf:
        spec = build_report_spec(data)
        record = render_pdf(repo, spec)
        if record.get("ok"):
            print(f"pdf: ok -> {record.get('path')}")
        else:
            print(f"pdf: degraded -> {record.get('reason')}: {record.get('hint')}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
