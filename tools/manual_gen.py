#!/usr/bin/env python3
"""tools/manual_gen.py — user-manual auto-generation.

Composes ``docs/manual/*.md`` (and, on request, a rendered ``user_manual`` PDF/DOCX via
``deliverables.make_report``) from three truth sources already in the repo — never invented
prose:

- ``scripts/feature_surface_audit.ribbon_rows`` — every ribbon command (id/label/tab/group),
  so the "Commands Reference" section covers 100% of the UI surface.
- ``services.mcp.cad_tools.cad_guide_text`` — the agent's own modeling-guide prose: the
  ``overview`` topic becomes ``overview.md``; every topic in ``GUIDE_TOPICS`` becomes
  ``guide-<topic>.md``. Per-command, its per-feature cheatsheets (``fillet``, ``chamfer``,
  ``shell``, ...) are used when one exists for a ribbon command's id.
- ``contracts/*.md`` — a one-line-per-contract summary (filename + its first ``# `` heading).

Provenance rule (no fabrication): a ribbon command gets a real description ONLY when
``cad_guide_text(command_id)`` resolves to actual cheatsheet prose (checked by absence of the
"No guide for" fallback marker cad_guide_text returns for an unknown topic). Every other command
gets a visibly generic AUTO-STUB sourced only from its own tab/group — the inventory is the truth
source, so the manual never states a capability nobody wrote down.

Missing truth sources are errors, not empty sections: ``collect`` raises when the ribbon
inventory or a guide topic cannot be loaded, so a broken import can never regenerate (or pass
``--check`` against) a silently hollowed-out manual.

Writing updates generated pages and removes obsolete pages only when their names are known
generator outputs; unrelated files in ``docs/manual/`` are preserved and ignored by the drift
check. Output is fully deterministic (no timestamps, no wallclock, sorted/stable iteration
everywhere) so ``--check`` can byte-compare freshly generated markdown against the committed
baseline as a drift gate (wired into ``scripts/gate.sh`` as a non-fatal advisory stage).

CLI:
    python -m tools.manual_gen            regenerate docs/manual/*.md, print coverage
    python -m tools.manual_gen --check    drift + coverage check only; exit 1 if not clean
    python -m tools.manual_gen --pdf      also render the user_manual report; exit 1 if it fails
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


MANUAL_SUBDIR = Path("docs") / "manual"

# --------------------------------------------------------------------------- guide topics

# The overview topic is emitted once, as overview.md.
OVERVIEW_TOPIC = "overview"

# The cad_guide topics published as guide-<topic>.md, in fixed order — deterministic regardless
# of dict/set iteration. A curated subset of the topics cad_guide_text serves.
GUIDE_TOPICS = [
    "review", "simulation", "intake", "gsl", "cam", "narration", "deliverables", "reverse",
    "piping", "industrial", "civil", "drawings",
]

# Known output names, including the retired duplicate overview. A guide- prefix alone
# does not establish generator ownership of an unrelated Markdown file.
_MANAGED_NAMES = frozenset({
    "index.md", "overview.md", "commands.md", "contracts.md", "guide-overview.md",
    *(f"guide-{topic}.md" for topic in GUIDE_TOPICS),
})

_NO_GUIDE_MARKER = "No guide for"

_AUTO_STUB_TEMPLATE = "_Available from the {tab} ▸ {group} ribbon group._"


# --------------------------------------------------------------------------- collect

def collect(repo: Path) -> dict:
    """Gather ribbon commands, cad_guide prose, and a contracts summary.

    Raises RuntimeError when a truth source is missing so no caller can emit a hollow manual.
    """
    repo = Path(repo)
    _ensure_sys_path(repo)

    # scripts/ui_inventory.py was retired with the ui/imgui shell; the live
    # ribbon parser is feature_surface_audit.ribbon_rows (cmd → id mapping here).
    from scripts.feature_surface_audit import ribbon_rows  # noqa: PLC0415
    from services.mcp.cad_tools import cad_guide_text  # noqa: PLC0415

    ribbon = [
        {"id": r["cmd"], "label": r["label"], "tab": r["tab"], "group": r["group"]}
        for r in ribbon_rows()
    ]
    if not ribbon:
        raise RuntimeError("ribbon inventory is empty: feature_surface_audit.ribbon_rows() "
                           "parsed no commands")

    guide: dict[str, str] = {}
    for topic in (OVERVIEW_TOPIC, *GUIDE_TOPICS):
        text = cad_guide_text(topic)
        text = text.strip() if isinstance(text, str) else ""
        if not text or text.startswith(_NO_GUIDE_MARKER):
            raise RuntimeError(f"cad_guide_text({topic!r}) returned no guide prose")
        guide[topic] = text

    # Per-command cheatsheet mapping: only a REAL hit (no "No guide for" fallback) counts.
    command_guides: dict[str, str] = {}
    for row in ribbon:
        cid = row["id"]
        if not cid or cid in command_guides:
            continue
        text = cad_guide_text(cid)
        if isinstance(text, str) and text.strip() and not text.startswith(_NO_GUIDE_MARKER):
            command_guides[cid] = text.strip()

    contracts = []
    cdir = repo / "contracts"
    if cdir.is_dir():
        for f in sorted(cdir.glob("*.md"), key=lambda p: p.name):
            heading = ""
            with f.open("r", encoding="utf-8") as fh:
                for _ in range(5):
                    line = fh.readline()
                    if not line:
                        break
                    line = line.strip()
                    if line.startswith("# "):
                        heading = line[2:].strip()
                        break
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
    ribbon = _sorted_ribbon(data["ribbon"])
    guide = data["guide"]
    command_guides = data["command_guides"]
    contracts = data["contracts"]

    docs: dict[str, str] = {}

    # ---- index.md ----
    lines = ["# Chudware User Manual", "", "Auto-generated from the ribbon command inventory, the",
             "in-app modeling guide, and the frozen contract set — see `tools/manual_gen.py`.",
             "", "## Contents", "", "- [Overview](overview.md)", "- [Commands Reference](commands.md)"]
    for topic in GUIDE_TOPICS:
        lines.append(f"- [Guide: {topic.capitalize()}](guide-{topic}.md)")
    lines.append("- [Contracts Summary](contracts.md)")
    lines.append("")
    docs["index.md"] = "\n".join(lines) + "\n"

    # ---- overview.md ----
    lines = ["# Overview", "", "```text", guide[OVERVIEW_TOPIC], "```", ""]
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

def write_manual(out_dir: Path, docs: dict[str, str]) -> list[Path]:
    """Write generated pages and prune only obsolete, known generator-owned Markdown files."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in sorted(out_dir.glob("*.md")):
        if stale.is_file() and stale.name in _MANAGED_NAMES and stale.name not in docs:
            stale.unlink()
    written = []
    for name in sorted(docs):
        p = out_dir / name
        p.write_text(docs[name], encoding="utf-8")
        written.append(p)
    return written


def drift(manual_dir: Path, docs: dict[str, str]) -> list[str]:
    """Sorted names of generated pages that are hand-edited, missing, or obsolete.

    Unknown files are not generator-owned drift; empty means all managed pages are clean.
    """
    manual_dir = Path(manual_dir)
    on_disk = {
        p.name: p.read_text(encoding="utf-8")
        for p in manual_dir.glob("*.md")
        if p.is_file() and (p.name in docs or p.name in _MANAGED_NAMES)
    } if manual_dir.is_dir() else {}
    return sorted(name for name in set(on_disk) | set(docs)
                  if on_disk.get(name) != docs.get(name))


def coverage(data: dict, docs: dict[str, str]) -> dict:
    """Report which ribbon command labels have no ``#### <label>`` heading in commands.md."""
    headings = {ln[len("#### "):] for ln in docs.get("commands.md", "").splitlines()
                if ln.startswith("#### ")}
    ribbon = data["ribbon"]
    missing = [row["label"] for row in ribbon if row["label"] not in headings]
    total = len(ribbon)
    return {"total": total, "covered": total - len(missing), "missing": missing}


# --------------------------------------------------------------------------- report spec / pdf

def build_report_spec(data: dict, fmt: str = "pdf") -> dict:
    """A make_report spec for the ``user_manual`` template — free prose only, no lb slots."""
    ribbon = _sorted_ribbon(data["ribbon"])
    guide = data["guide"]
    command_guides = data["command_guides"]
    contracts = data["contracts"]

    outline = [
        {"type": "title", "body": [
            "Chudware User Manual",
            "Auto-generated from the ribbon command inventory, the in-app modeling guide, "
            "and the frozen contract set.",
        ]},
        {"type": "section", "heading": "Overview",
         "body": [ln for ln in guide[OVERVIEW_TOPIC].splitlines() if ln.strip()]},
    ]

    cmd_body = [f"{row.get('label', row.get('id', '?'))} — "
                f"{_command_one_liner(row, command_guides)}" for row in ribbon]
    outline.append({"type": "section", "heading": "Commands Reference", "body": cmd_body})

    for topic in GUIDE_TOPICS:
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


def render_pdf(spec: dict) -> dict:
    """Render via deliverables.make_report; failures come back as a contract envelope, never raise."""
    try:
        from services.deliverables import make_report  # noqa: PLC0415
    except ImportError as e:
        return {"ok": False, "reason": "import_error", "hint": str(e)}
    try:
        return make_report(spec)
    except Exception as e:  # noqa: BLE001 - report the failure as an envelope; markdown is primary
        return {"ok": False, "reason": "render_error", "hint": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------------------- CLI

def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="drift + coverage check only")
    ap.add_argument("--pdf", action="store_true", help="also render the user_manual report")
    args = ap.parse_args(argv)

    repo = _repo_root()
    manual_dir = repo / MANUAL_SUBDIR
    data = collect(repo)
    docs = build_markdown(data)
    cov = coverage(data, docs)

    if args.check:
        drifted = drift(manual_dir, docs)
        print(f"drift: {'CLEAN' if not drifted else 'DRIFT ' + ', '.join(drifted)}")
        print(f"coverage: {cov['covered']}/{cov['total']} (missing={len(cov['missing'])})")
        if cov["missing"]:
            print(f"missing labels: {cov['missing']}")
        return 0 if not drifted and not cov["missing"] else 1

    written = write_manual(manual_dir, docs)
    print(f"wrote {len(written)} files to {MANUAL_SUBDIR.as_posix()}/")
    print(f"coverage: {cov['covered']}/{cov['total']} (missing={len(cov['missing'])})")

    if args.pdf:
        record = render_pdf(build_report_spec(data))
        if not record.get("ok"):
            print(f"pdf: FAILED -> {record.get('reason')}: {record.get('hint')}")
            return 1
        print(f"pdf: ok -> {record.get('path')}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
