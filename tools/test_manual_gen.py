"""tools/test_manual_gen.py — T-Q4 user-manual generator tests.

Collects from the real repo truth sources (ribbon inventory, cad_guide prose, contracts/*.md)
read-only; every write goes to a tmp directory — the committed docs/manual/ is never touched.
"""
from __future__ import annotations

import re
import sys

import pytest

import scripts.feature_surface_audit as feature_surface_audit
import services.mcp.cad_tools as cad_tools
import tools.manual_gen as manual_gen
from tools.manual_gen import (
    _AUTO_STUB_TEMPLATE,
    GUIDE_TOPICS,
    OVERVIEW_TOPIC,
    _repo_root,
    build_markdown,
    build_report_spec,
    collect,
    coverage,
    drift,
    render_pdf,
    write_manual,
)

REPO = _repo_root()


@pytest.fixture(scope="module")
def data():
    return collect(REPO)


@pytest.fixture(scope="module")
def docs(data):
    return build_markdown(data)


def test_determinism(data, docs):
    assert build_markdown(collect(REPO)) == docs


def test_overview_emitted_once_and_index_links_every_page(data, docs):
    assert "guide-overview.md" not in docs
    overview = data["guide"][OVERVIEW_TOPIC]
    assert [name for name, text in docs.items() if overview in text] == ["overview.md"]

    links = re.findall(r"\]\(([^)]+)\)", docs["index.md"])
    assert len(links) == len(set(links))
    assert sorted(links) == sorted(name for name in docs if name != "index.md")


def test_write_then_drift_tracks_hand_edits_missing_and_stale_files(docs, tmp_path):
    out = tmp_path / "manual"
    write_manual(out, docs)
    assert drift(out, docs) == []

    (out / "overview.md").write_text("hand edit\n", encoding="utf-8")
    (out / "commands.md").unlink()
    (out / "guide-overview.md").write_text("stale duplicate\n", encoding="utf-8")
    unrelated = {
        "notes.md": b"# User notes\nKeep this page.\n",
        "guide-custom.md": b"# User guide\nNot a generated topic.\n",
        "notes.txt": b"not generator-owned\n",
        "raw.md": b"\xff\xfe",
    }
    for name, content in unrelated.items():
        (out / name).write_bytes(content)
    assert drift(out, docs) == ["commands.md", "guide-overview.md", "overview.md"]

    write_manual(out, docs)
    assert drift(out, docs) == []
    assert sorted(p.name for p in out.glob("*.md")) == sorted(
        set(docs) | {name for name in unrelated if name.endswith(".md")})
    assert not (out / "guide-overview.md").exists()
    for name, content in unrelated.items():
        assert (out / name).read_bytes() == content


def test_drift_ignores_unmanaged_pages_before_write(docs, tmp_path):
    (tmp_path / "notes.md").write_text("# User notes\n", encoding="utf-8")
    assert drift(tmp_path, docs) == sorted(docs)
    write_manual(tmp_path, docs)
    assert drift(tmp_path, docs) == []
    assert (tmp_path / "notes.md").read_text(encoding="utf-8") == "# User notes\n"


@pytest.mark.parametrize("pdf_ok", [False, True])
def test_pdf_cli_exit_reflects_render_result_and_collects_once(
    data, docs, tmp_path, monkeypatch, capsys, pdf_ok,
):
    collected = []
    rendered = []

    def collect_once(repo):
        collected.append(repo)
        return data

    def render(spec):
        rendered.append(spec)
        return ({"ok": True, "path": "manual.pdf"} if pdf_ok else
                {"ok": False, "reason": "render_error", "hint": "renderer unavailable"})

    monkeypatch.setattr(manual_gen, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(manual_gen, "collect", collect_once)
    monkeypatch.setattr(manual_gen, "render_pdf", render)

    assert manual_gen.main(["--pdf"]) == (0 if pdf_ok else 1)
    assert collected == [tmp_path]
    assert rendered == [build_report_spec(data)]
    output = capsys.readouterr().out
    assert ("pdf: ok" if pdf_ok else "pdf: FAILED") in output
    out = tmp_path / manual_gen.MANUAL_SUBDIR
    assert {p.name: p.read_text(encoding="utf-8") for p in out.glob("*.md")} == docs


def test_check_cli_reports_only_managed_drift_without_writing(
    data, docs, tmp_path, monkeypatch, capsys,
):
    out = tmp_path / manual_gen.MANUAL_SUBDIR
    write_manual(out, docs)
    (out / "notes.md").write_text("# User notes\n", encoding="utf-8")
    collected = []

    def collect_once(repo):
        collected.append(repo)
        return data

    monkeypatch.setattr(manual_gen, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(manual_gen, "collect", collect_once)
    assert manual_gen.main(["--check"]) == 0
    assert "drift: CLEAN" in capsys.readouterr().out
    assert collected == [tmp_path]

    (out / "guide-overview.md").write_text("stale duplicate\n", encoding="utf-8")
    collected.clear()
    assert manual_gen.main(["--check"]) == 1
    assert "drift: DRIFT guide-overview.md" in capsys.readouterr().out
    assert collected == [tmp_path]
    assert (out / "guide-overview.md").read_text(encoding="utf-8") == "stale duplicate\n"
    assert (out / "notes.md").read_text(encoding="utf-8") == "# User notes\n"


def test_coverage_reports_label_missing_from_commands(data, docs):
    assert coverage(data, docs)["missing"] == []

    label = data["ribbon"][0]["label"]
    commands = "\n".join(ln for ln in docs["commands.md"].splitlines() if ln != f"#### {label}")
    cov = coverage(data, {**docs, "commands.md": commands})
    assert label in cov["missing"]
    assert cov["covered"] == cov["total"] - len(cov["missing"])


def test_collect_reads_contracts_from_given_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))  # collect() prepends its repo
    cdir = tmp_path / "contracts"
    cdir.mkdir()
    (cdir / "b.md").write_text("intro line\n# Beta Contract\nbody\n", encoding="utf-8")
    (cdir / "a.md").write_text("no heading here\n", encoding="utf-8")

    got = collect(tmp_path)
    assert got["contracts"] == [{"filename": "a.md", "heading": ""},
                                {"filename": "b.md", "heading": "Beta Contract"}]
    contracts_md = build_markdown(got)["contracts.md"]
    assert "- `a.md` — (no heading found)" in contracts_md
    assert "- `b.md` — Beta Contract" in contracts_md


def test_collect_refuses_missing_guide_topic(monkeypatch):
    real = cad_tools.cad_guide_text
    missing = GUIDE_TOPICS[0]

    def fake(topic="overview"):
        return f"No guide for {topic!r}." if topic == missing else real(topic)

    monkeypatch.setattr(cad_tools, "cad_guide_text", fake)
    with pytest.raises(RuntimeError, match=missing):
        collect(REPO)


def test_collect_refuses_empty_ribbon(monkeypatch):
    monkeypatch.setattr(feature_surface_audit, "ribbon_rows", lambda: [])
    with pytest.raises(RuntimeError, match="ribbon"):
        collect(REPO)


def test_no_fabrication_autostub(data, docs):
    commands_md = docs["commands.md"]
    command_guides = data["command_guides"]

    stubbed = [row for row in data["ribbon"] if row["id"] not in command_guides]
    # Most ribbon commands have no per-feature cheatsheet, so stubs must actually be exercised,
    # and every stub must be the visibly-generic template string — never invented prose.
    assert stubbed
    for row in stubbed:
        expected_stub = _AUTO_STUB_TEMPLATE.format(tab=row["tab"], group=row["group"])
        assert expected_stub in commands_md, (
            f"auto-stub for {row['label']!r} missing/mismatched in commands.md")


def test_report_spec_carries_overview_once(data):
    spec = build_report_spec(data)
    headings = [b["heading"] for b in spec["outline"] if b["type"] == "section"]
    assert headings.count("Overview") == 1
    assert "Guide: Overview" not in headings
    commands = next(b for b in spec["outline"] if b.get("heading") == "Commands Reference")
    assert len(commands["body"]) == len(data["ribbon"])


def test_render_pdf_degrades_cleanly(data, tmp_path, monkeypatch):
    project_dir = tmp_path / "chudware_project"
    project_dir.mkdir()
    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(project_dir))
    monkeypatch.setenv("CHUDWARE_ARTIFACTS_DIR", str(tmp_path / "artifacts"))

    record = render_pdf(build_report_spec(data))

    if record["ok"]:
        from services.deliverables.project import resolve_project_path
        p = resolve_project_path(record["path"])
        assert p.exists()
    else:
        # Contract-shaped failure envelope (contracts/deliverables.md §6) — never a traceback.
        assert record["reason"]


def test_spec_validates_without_render_engine(data):
    # The provenance rule is the load-bearing part of this contract: prove the spec passes
    # validate_and_resolve (kind/template/block-shape/slot rules) on its own, independent of
    # whether reportlab/python-docx happen to be installed on the box running the render. Since
    # user_manual declares lb_slots={} (all free prose), zero provenance refs should be consumed.
    from services.deliverables.spec import validate_and_resolve
    from services.deliverables.templates import get_template

    spec = build_report_spec(data)
    template = get_template("user_manual", "report")
    source_refs = []
    resolved = validate_and_resolve(spec, "report", template, source_refs)
    assert resolved["blocks"]
    assert resolved["title"] == spec["title"]
    assert source_refs == []
