"""new_part must never breed duplicate documents.

Live run ~/.local/share/Chudware/cache/runs/run-138660919763973 called, in order,
import_part("Projects/Testing/mudbucket.step+") -> describe_part -> new_part("mudbucket")
-> add_features -> new_part("mudbucket") AGAIN -> add_features, and left THREE documents
in the user's tab strip: mudbucket, mudbucket and mudbucket2. The cause was that new_part
resumed an open part only when EXACTLY ONE open doc carried the name; the moment a
duplicate existed, the next call fell through to doc_new, whose uniquifier minted yet
another document. Duplicates therefore bred.

These pin the resolution rule against a fake doc_list: any number of name matches RESUMES
the best one, a lone file-stem match RESUMES too, ambiguous stems are never guessed, and
fresh=True is the only way to force a second document.

Run:  python3 -m pytest services/mcp/test_new_part_dedup.py -q -p no:randomly
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from services.mcp import cad_common as C  # noqa: E402


class _FakeEngine:
    """Minimal core-RPC stand-in driven by a fixed doc_list; records doc_new calls."""

    def __init__(self, docs):
        self.docs = list(docs)
        self.new_calls: list = []

    def call(self, method, params=None):
        p = params or {}
        if method == "doc_list":
            return {"docs": [dict(e) for e in self.docs]}
        if method == "doc_new":
            self.new_calls.append(p.get("name"))
            return {"doc_id": "doc-cppMINTED", "name": (p.get("name") or "part") + "2",
                    "revision": 0}
        if method == "doc_save":
            return {}
        return {}


_TOOL_CACHE: dict = {}


def _tool(name):
    """Grab the raw tool function registered on the FastMCP server (bypasses transport)."""
    if name not in _TOOL_CACHE:
        from services.agent.agent_turn import _bind_tool
        _TOOL_CACHE[name] = _bind_tool(name)
    return _TOOL_CACHE[name]


@pytest.fixture()
def engine(monkeypatch):
    """Install a fake engine over a caller-supplied doc list."""
    made: list = []

    def _install(docs):
        fake = _FakeEngine(docs)
        monkeypatch.setattr(C.cb, "call", fake.call)
        made.append(fake)
        return fake

    monkeypatch.delenv("CHUDWARE_PROJECT_SAVE_DIR", raising=False)
    monkeypatch.setitem(C._CUR, "doc_id", None)
    yield _install
    del made


def _new_part(**kw):
    return json.loads(_tool("new_part")(**kw))


def test_two_docs_of_the_same_name_resume_the_built_one(engine):
    """run-138660919763973: two open "mudbucket" docs, and new_part("mudbucket") minted a
    THIRD called mudbucket2. Any number of name matches must resume, never mint, and the
    doc picked must be the most-built one (highest feature_count)."""
    fake = engine([
        {"doc_id": "doc-cpp0000001f", "name": "mudbucket", "revision": 3,
         "feature_count": 7},
        {"doc_id": "doc-cpp00000023", "name": "mudbucket", "revision": 9,
         "feature_count": 0},
    ])
    r = _new_part(name="mudbucket")

    assert r["ok"] is True
    assert r["resumed"] is True and r["reused"] is True
    assert fake.new_calls == [], "a duplicate name must NEVER reach doc_new"
    assert r["duplicates"] == 2
    assert r["doc_id"] == "doc-cpp0000001f", "the most-built doc is the one meant"
    # The envelope has to be honest about the collision AND teach the way out.
    assert "doc-cpp00000023" in r["note"]
    assert "doc_id=" in r["note"] and "fresh=True" in r["note"]
    assert C._CUR["doc_id"] == "doc-cpp0000001f"


def test_single_open_doc_still_resumes(engine):
    """Regression guard: the one case that already worked before the run-138660919763973
    fix must be untouched — one name match resumes, with no duplicates key."""
    fake = engine([
        {"doc_id": "doc-cpp00000001", "name": "mudbucket", "revision": 2,
         "feature_count": 4},
    ])
    r = _new_part(name="mudbucket")

    assert r["doc_id"] == "doc-cpp00000001"
    assert r["resumed"] is True and r["reused"] is True
    assert fake.new_calls == []
    assert "duplicates" not in r
    assert r["feature_count"] == 4
    assert "ALREADY CARRIES 4 feature(s)" in r["note"]


def test_imported_file_stem_resumes_the_imported_doc(engine):
    """run-138660919763973 opened Projects/Testing/mudbucket.step+ via import_part; the doc
    can end up named after the file rather than "mudbucket". Asking for the stem must
    resume that import instead of modelling into a brand-new empty part."""
    fake = engine([
        {"doc_id": "doc-cpp00000005", "name": "Part1", "revision": 1,
         "feature_count": 1,
         "path": "/home/hunter/Projects/Testing/mudbucket.step+"},
    ])
    r = _new_part(name="mudbucket")

    assert fake.new_calls == [], "a stem-identified import must not be duplicated"
    assert r["doc_id"] == "doc-cpp00000005"
    assert r["resumed"] is True
    assert r["resumed_by"] == "path_stem"
    assert "mudbucket.step+" in r["note"]


def test_ambiguous_file_stems_are_not_guessed(engine):
    """Two imports stemming to the same name is a genuine ambiguity. Editing the WRONG
    part is worse than one extra document, so this falls through to the mint path."""
    fake = engine([
        {"doc_id": "doc-cpp00000005", "name": "Part1", "revision": 1,
         "path": "/home/hunter/Projects/Testing/mudbucket.step+"},
        {"doc_id": "doc-cpp00000006", "name": "Part2", "revision": 1,
         "path": "/home/hunter/Projects/Archive/mudbucket.stl+"},
    ])
    r = _new_part(name="mudbucket")

    assert fake.new_calls == ["mudbucket"], "ambiguous stems must not be resolved"
    assert r["doc_id"] == "doc-cppMINTED"
    assert r.get("resumed") is not True
    assert "resumed_by" not in r


def test_fresh_true_still_mints_over_a_collision(engine):
    """fresh=True is the deliberate escape hatch and keeps its old behaviour exactly:
    always doc_new, even against a pile of same-named documents."""
    fake = engine([
        {"doc_id": "doc-cpp0000001f", "name": "mudbucket", "revision": 3,
         "feature_count": 7},
        {"doc_id": "doc-cpp00000023", "name": "mudbucket", "revision": 9,
         "feature_count": 0},
    ])
    r = _new_part(name="mudbucket", fresh=True)

    assert fake.new_calls == ["mudbucket"]
    assert r["doc_id"] == "doc-cppMINTED"
    assert r.get("reused") is not True
    assert r["name"] == "mudbucket2", "doc_new's uniquified name is reported, not the ask"
