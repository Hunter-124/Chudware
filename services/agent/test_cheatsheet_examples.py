"""Every op array printed in a cheatsheet must actually build.

A cheatsheet is not prose, it is a SPEC: it is the only thing the model has when
it writes its first batch. A live Opus run followed
``prompts/shared/cheatsheets/sketch.md`` to the letter and lost three turns,
because the file was wrong in two ways at once — it declared ``"plane.top"`` the
required spelling while a Python guardrail rejected that exact string, and not
one of its three examples showed the extrude that turns a sketch into a solid,
so the model had to invent the op shape.

Neither failure is catchable by reading. This test executes the examples.

It extracts every fenced JSON array from the cheatsheet, feeds it to the real
``add_features`` through the same ``_bind_tool`` path the built-in agent uses,
and requires a solid out the other end. If an example stops being buildable —
because the schema moved, or because someone edits the doc without running it —
this goes red instead of the next model run.
"""
from __future__ import annotations

import json
import pathlib
import re

import pytest

from services.agent import agent_turn as at

CHEATSHEETS = pathlib.Path(__file__).resolve().parent / "prompts" / "shared" / "cheatsheets"

# A cheatsheet example is a bare JSON array on its own lines, starting at column
# 0 with "[" and ending at column 0 with "]".
_BLOCK = re.compile(r"^\[\n(.*?)^\]", re.M | re.S)


def _examples(path: pathlib.Path) -> list[tuple[int, list]]:
    """(line number, parsed ops) for every runnable op array in *path*."""
    text = path.read_text()
    out: list[tuple[int, list]] = []
    for m in _BLOCK.finditer(text):
        raw = m.group(0)
        try:
            ops = json.loads(raw)
        except ValueError:
            continue  # a prose bracket, not an op array
        if isinstance(ops, list) and ops and isinstance(ops[0], dict) and "op" in ops[0]:
            out.append((text[:m.start()].count("\n") + 1, ops))
    return out


SKETCH_MD = CHEATSHEETS / "sketch.md"
SKETCH_EXAMPLES = _examples(SKETCH_MD) if SKETCH_MD.is_file() else []


def test_the_sketch_cheatsheet_has_runnable_examples():
    """Guards the extractor itself: a silent zero would make this file vacuous."""
    assert len(SKETCH_EXAMPLES) >= 3, (
        f"expected the sketch cheatsheet's worked examples, found "
        f"{len(SKETCH_EXAMPLES)} — the extractor or the doc format changed")


@pytest.fixture
def sketch_tools():
    """Bind the live tools and discard every scratch document after its example."""
    new_part = at._bind_tool("new_part")
    add_features = at._bind_tool("add_features")
    close_part = at._bind_tool("close_part")
    if new_part is None or add_features is None or close_part is None:
        pytest.skip("engine unavailable")

    doc_ids: list[str] = []

    def new_scratch(name: str) -> str | None:
        doc_id = json.loads(new_part(name=name, fresh=True)).get("doc_id")
        if doc_id:
            doc_ids.append(doc_id)
        return doc_id

    yield new_scratch, add_features

    for doc_id in reversed(doc_ids):
        result = json.loads(close_part(doc_id=doc_id, discard=True))
        assert result.get("ok"), (
            f"failed to discard cheatsheet scratch document {doc_id}: "
            f"{result.get('error')}")


@pytest.mark.parametrize("line,ops", SKETCH_EXAMPLES,
                         ids=[f"sketch.md:L{n}" for n, _ in SKETCH_EXAMPLES])
def test_sketch_cheatsheet_example_builds_a_solid(line, ops, sketch_tools):
    new_scratch, add_features = sketch_tools

    # fresh=True is REQUIRED, not tidiness: new_part(name=...) resumes an
    # already-open part of that name by design, so on the second run against a
    # live daemon this example would build its cylinder inside the cylinder the
    # previous run left behind and die on "boss added no material" — a green
    # first run and a red every run after, blaming the doc for a test bug.
    doc_id = new_scratch(f"cheatsheet_L{line}")
    assert doc_id, "new_part did not return a doc_id"

    result = json.loads(add_features(doc_id=doc_id, ops=ops))
    assert result.get("ok"), (
        f"sketch.md line {line} does not build: {result.get('error')}")

    # Every sketch example in this file is a sketch->solid worked example, so a
    # zero-volume result means the doc taught the model to stop halfway — the
    # exact failure this cheatsheet edit exists to prevent.
    assert (result.get("volume_mm3") or 0) > 0.0, (
        f"sketch.md line {line} applied cleanly but produced NO SOLID — a "
        f"sketch-only example teaches the model to stop before the extrude")


def test_examples_use_the_spelling_the_cheatsheet_mandates():
    """The doc must not preach one plane spelling and demonstrate another."""
    text = SKETCH_MD.read_text()
    for _line, ops in SKETCH_EXAMPLES:
        for op in ops:
            plane = (op.get("feature") or op).get("refs", {}).get("plane")
            if plane is None:
                continue
            assert plane.startswith("plane."), (
                f"example uses {plane!r} while the prose calls "
                f"'plane.top'/'plane.front'/'plane.right' canonical")
    assert "plane.top" in text


DRAWING_MD = CHEATSHEETS / "drawing.md"
MANUFACTURING_PLANNING_MD = CHEATSHEETS / "manufacturing_planning.md"
_BACKTICKED_TOOL = re.compile(r"`([a-z][a-z0-9_]+)`")
_REQUIRED_MANUFACTURING_TOOLS = frozenset({
    "audit_drawing",
    "dfm_check",
    "feeds_speeds",
    "fit_calc",
    "machining_ref_get",
    "machining_ref_index_pdf",
    "machining_ref_search",
    "machining_ref_search_pdf",
    "make_work_instruction",
    "mfg_plan_check",
    "mfg_plan_get",
    "mfg_plan_render",
    "mfg_plan_upsert",
    "recommend_fit",
    "research_material_lookup",
    "research_thread_spec",
    "resolve_process",
    "set_process",
    "tap_drill",
})


@pytest.mark.parametrize(
    "topic,path",
    [
        ("drawing", DRAWING_MD),
        ("manufacturing_planning", MANUFACTURING_PLANNING_MD),
    ],
)
def test_named_cheatsheet_loads_through_cad_guide(topic, path):
    """New sheets must use the same filename-based cad_guide path as drawing."""
    from services.mcp.cad_guide import cad_guide_text

    assert path.is_file()
    assert cad_guide_text(topic) == path.read_text(encoding="utf-8")


def test_manufacturing_planning_cheatsheet_names_only_bindable_tools():
    """Every code-formatted tool in the planning workflow must dispatch live."""
    text = MANUFACTURING_PLANNING_MD.read_text(encoding="utf-8")
    tool_names = frozenset(_BACKTICKED_TOOL.findall(text))
    assert tool_names == _REQUIRED_MANUFACTURING_TOOLS

    unresolved = sorted(name for name in tool_names if at._bind_tool(name) is None)
    assert not unresolved, f"manufacturing planning names unbound tools: {unresolved}"
