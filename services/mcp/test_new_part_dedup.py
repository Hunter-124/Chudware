"""new_part must never breed duplicate documents.

Live run ~/.local/share/Chudware/cache/runs/run-138660919763973 called, in order,
import_part("Projects/Testing/mudbucket.step+") -> describe_part -> new_part("mudbucket")
-> add_features -> new_part("mudbucket") AGAIN -> add_features, and left THREE documents
in the user's tab strip: mudbucket, mudbucket and mudbucket2. The cause was that new_part
resumed an open part only when EXACTLY ONE open doc carried the name; the moment a
duplicate existed, the next call fell through to doc_new, whose uniquifier minted yet
another document. Duplicates therefore bred.

These pin the resolution rule against a fake doc_list: any number of name matches RESUMES
the best one, a lone file-stem match RESUMES too, a bumped VERSION name resumes the series
it belongs to, ambiguous stems are never guessed, and fresh=True is the only way to force a
second document. new_assembly answers to the same contract.

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
    """Minimal core-RPC stand-in driven by a fixed doc_list; records doc_new MINTS.

    `doc_new` MIRRORS the engine's resume contract (core/doc/session.cpp
    resume_open_doc / doc_new): given `resume_if_open` it returns the most-built
    same-name, same-kind document marked `reused`, carrying `feature_count` and any
    `duplicate_doc_ids`; otherwise it mints a uniquified one.

    new_part no longer makes that decision in Python. It used to do an unlocked
    doc_list-then-doc_new pair, which cannot be race-free against the ONE shared
    chudware-hostd every process attaches to — concurrent same-name callers each passed
    the "not found" check and each minted (the live 3x-duplicate-tab bug). The choice
    moved into the engine, under a lock spanning both halves.

    So these tests cover new_part's REPLY plumbing on top of that contract; the RANKING
    is the engine's and is pinned in core/tests/test_document.cpp
    ("doc_new(resume_if_open) resumes the MOST BUILT of several same-name docs").
    `new_calls` records only real MINTS, so "must not mint" stays assertable even though
    doc_new is now always the call that is made.
    """

    def __init__(self, docs):
        self.docs = list(docs)
        self.new_calls: list = []

    def call(self, method, params=None):
        p = params or {}
        if method == "doc_list":
            return {"docs": [dict(e) for e in self.docs]}
        if method == "doc_new":
            want = p.get("name") or "part"
            kind = p.get("kind") or "part"
            hits = [e for e in self.docs
                    if e.get("name") == want and (e.get("kind") or "part") == kind]
            if p.get("resume_if_open"):
                if hits:
                    best = max(hits, key=lambda e: (e.get("feature_count") or 0,
                                                    e.get("revision") or 0))
                    resp = {"doc_id": best["doc_id"], "name": want,
                            "revision": best.get("revision", 0), "reused": True,
                            "feature_count": best.get("feature_count", 0)}
                    others = [e["doc_id"] for e in hits if e["doc_id"] != best["doc_id"]]
                    if others:
                        resp["duplicate_doc_ids"] = others
                    return resp
            self.new_calls.append(want)
            actual = want + "2" if hits else want
            self.docs.append({
                "doc_id": "doc-cppMINTED", "name": actual, "kind": kind,
                "revision": 0, "feature_count": 0,
            })
            return {"doc_id": "doc-cppMINTED", "name": actual, "revision": 0}
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
    def _install(docs):
        fake = _FakeEngine(docs)
        monkeypatch.setattr(C.cb, "call", fake.call)
        return fake

    monkeypatch.delenv("CHUDWARE_PROJECT_SAVE_DIR", raising=False)
    monkeypatch.setitem(C._CUR, "doc_id", None)
    yield _install


def _new_part(**kw):
    return json.loads(_tool("new_part")(**kw))


def _add_features(ops):
    return json.loads(_tool("add_features")(ops=ops))


def test_two_docs_of_the_same_name_resume_the_built_one(engine):
    """run-138660919763973: two open "mudbucket" docs, and new_part("mudbucket") minted a
    THIRD called mudbucket2. Any number of name matches must resume, never mint, and the
    doc picked must be the most-built one (highest feature_count).

    The resume/mint choice is the ENGINE's now (see _FakeEngine), so what this pins is
    that new_part asks for it and reports the resumed document faithfully — doc_id,
    counts, and a note naming the other claimant. The ranking itself is pinned in
    core/tests/test_document.cpp."""
    fake = engine([
        {"doc_id": "doc-cpp0000001f", "name": "mudbucket", "revision": 3,
         "feature_count": 7},
        {"doc_id": "doc-cpp00000023", "name": "mudbucket", "revision": 9,
         "feature_count": 0},
    ])
    r = _new_part(name="mudbucket")

    assert r["ok"] is True
    assert r["resumed"] is True and r["reused"] is True
    assert fake.new_calls == [], "a duplicate name must never MINT (doc_new resumes)"
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
         "path": "/home/user/Projects/Testing/mudbucket.step+"},
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
         "path": "/home/user/Projects/Testing/mudbucket.step+"},
        {"doc_id": "doc-cpp00000006", "name": "Part2", "revision": 1,
         "path": "/home/user/Projects/Archive/mudbucket.stl+"},
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


# ---------------------------------------------------------------------------
# Version series — "the next version" is a REVISION, not another document.
# ---------------------------------------------------------------------------
# Live 2026-08-05: one "make me an IWB holster for a Ruger LCP" request left NINE
# documents open in the daemon at once — ruger_lcp_iwb_holster_v3 … _v11, 2-6 features
# each, every one a successive iteration of the SAME design. The name-keyed resume
# contract above never fired because the model asked for a NEW name each time. The cost
# per extra document: a tab, a .step+ in the user's project folder, its own AI-VC delta
# chain (services/plm/ai_vc.py keys chains by doc_id), its own auto-registered PLM item
# (services/plm/vault.py registers every *.step+ under the project), and its own row in
# the next launch's "Recover unsaved parts?" prompt.


_HOLSTER = [
    {"doc_id": "doc-cpp0000000a", "name": "ruger_lcp_iwb_holster_v3", "revision": 0,
     "feature_count": 2},
    {"doc_id": "doc-cpp00000009", "name": "ruger_lcp_iwb_holster_v4", "revision": 1,
     "feature_count": 6},
    {"doc_id": "doc-cpp00000003", "name": "ruger_lcp_iwb_holster_v10", "revision": 0,
     "feature_count": 6},
]


def test_bumped_version_resumes_the_series_instead_of_minting(engine):
    """The reported bug, pinned: v11 lands in the most-built open member of the series."""
    fake = engine(_HOLSTER)
    r = _new_part(name="ruger_lcp_iwb_holster_v11")

    assert fake.new_calls == [], "a version bump must never MINT a tenth document"
    assert r["resumed"] is True and r["reused"] is True
    assert r["resumed_by"] == "version_series"
    assert r["version_series"] == "ruger_lcp_iwb_holster"
    assert r["requested_name"] == "ruger_lcp_iwb_holster_v11"
    # Ties on feature_count break toward the higher revision, so v4 (rev 1) beats
    # v10 (rev 0) — both carry 6 features.
    assert r["doc_id"] == "doc-cpp00000009"
    assert r["name"] == "ruger_lcp_iwb_holster_v4", "the REAL doc name, not the ask"
    assert C._CUR["doc_id"] == "doc-cpp00000009"
    # The reply has to teach the rule, or the model bumps again next turn.
    assert "plm_timeline" in r["note"] and "plm_rollback" in r["note"]
    assert "fresh=True" in r["note"]


def test_first_version_of_a_new_design_mints_the_bare_series_name(engine):
    """Nothing in the series is open: the first document mints under the series base,
    while the reply preserves the requested spelling so the normalization is visible."""
    fake = engine([
        {"doc_id": "doc-cpp00000001", "name": "unrelated_bracket", "feature_count": 8},
    ])
    r = _new_part(name="holster_v1")

    assert fake.new_calls == ["holster"]
    assert r["name"] == "holster"
    assert r["requested_name"] == "holster_v1"
    assert r.get("resumed") is not True


def test_unversioned_base_doc_absorbs_the_first_bump(engine):
    """The common shape: the model builds "holster", then asks for "holster_v2". The base
    document IS the series, so v2 is a revision of it."""
    fake = engine([
        {"doc_id": "doc-cpp00000004", "name": "holster", "revision": 2,
         "feature_count": 5},
    ])
    r = _new_part(name="holster_v2")

    assert fake.new_calls == []
    assert r["doc_id"] == "doc-cpp00000004"
    assert r["resumed_by"] == "version_series"
    assert "ALREADY CARRIES 5 feature(s)" in r["note"]


def test_a_trailing_part_number_is_not_a_version(engine):
    """PN-1235 is a DIFFERENT part from PN-1234, and 8 in "engine_v8" is a configuration
    only because the token says so. A bare trailing number carries no version claim, so
    it must mint — the whole reason the version token is required and separated."""
    fake = engine([
        {"doc_id": "doc-cpp00000007", "name": "PN-1234", "revision": 4,
         "feature_count": 11},
    ])
    r = _new_part(name="PN-1235")

    assert fake.new_calls == ["PN-1235"], "a part number must not be read as a version"
    assert r.get("resumed") is not True


def test_an_empty_series_sibling_is_never_the_resume_target(engine):
    """A zero-geometry leftover is not the design. Resuming one would hand the model an
    empty document while its real work sat in another tab; the built member wins and the
    empty one is swept."""
    fake = engine([
        {"doc_id": "doc-cpp00000011", "name": "holster_v2", "revision": 0,
         "feature_count": 0, "empty": True, "kind": "part"},
        {"doc_id": "doc-cpp00000012", "name": "holster_v3", "revision": 0,
         "feature_count": 4, "kind": "part"},
    ])
    r = _new_part(name="holster_v4")

    assert fake.new_calls == []
    assert r["doc_id"] == "doc-cpp00000012"
    assert r.get("discarded_orphans") == ["doc-cpp00000011"]


def test_an_assembly_is_never_a_version_of_a_part(engine):
    """kind is part of the identity: new_part must not steer a part build into an
    assembly document that happens to sit in the same name series."""
    fake = engine([
        {"doc_id": "doc-cpp00000021", "name": "gearbox_v1", "revision": 3,
         "feature_count": 0, "component_count": 6, "kind": "assembly"},
    ])
    r = _new_part(name="gearbox_v2")

    assert fake.new_calls == ["gearbox"]
    assert r["requested_name"] == "gearbox_v2"
    assert r.get("resumed") is not True


def test_fresh_true_overrides_resume_but_still_mints_the_bare_series_name(engine):
    """A deliberate second document is still possible, but PLM revision identity never
    leaks into that new document's name."""
    fake = engine(_HOLSTER)
    r = _new_part(name="ruger_lcp_iwb_holster_v11", fresh=True)

    assert fake.new_calls == ["ruger_lcp_iwb_holster"]
    assert r["name"] == "ruger_lcp_iwb_holster"
    assert r["requested_name"] == "ruger_lcp_iwb_holster_v11"
    assert r.get("reused") is not True


def test_fresh_version_mint_explains_engine_uniquifier_and_plm_alternative(engine):
    """The replay's version strip can expose the engine's older collision numbering."""
    base = "ruger_lcp380_iwb_holster"
    fake = engine([
        {"doc_id": "doc-cpp00000041", "name": base, "revision": 6,
         "feature_count": 18},
    ])
    requested = f"{base}_v2"
    r = _new_part(name=requested, fresh=True)

    assert fake.new_calls == [base]
    assert r["requested_name"] == requested
    assert r["name"] == f"{base}2"
    assert f'document named "{base}" was already open' in r["note"]
    assert f'numbered this fresh document "{base}2"' in r["note"]
    assert "plm_checkpoint" in r["note"] and "plm_timeline" in r["note"]


def test_exact_name_match_still_goes_through_the_engine_mint_lock(engine):
    """The version path must not shadow the engine's own resume. Asking for a name that
    IS open takes doc_new(resume_if_open) — the unlocked check-then-mint pair it replaced
    is what produced three tabs for one part (run-42508173312214)."""
    fake = engine(_HOLSTER)
    r = _new_part(name="ruger_lcp_iwb_holster_v10")

    assert fake.new_calls == []
    assert r["doc_id"] == "doc-cpp00000003", "the engine's exact-name resume, not a sibling"
    assert "resumed_by" not in r


# ---------------------------------------------------------------------------
# new_assembly answers to the same contract.
# ---------------------------------------------------------------------------


def _new_assembly(**kw):
    return json.loads(_tool("new_assembly")(**kw))


def test_new_assembly_resumes_an_open_assembly_of_the_same_name(engine):
    """It used to call doc_new with no resume at all, so a retried brief left
    "motor_mount" and "motor_mount2" side by side."""
    fake = engine([
        {"doc_id": "doc-cpp00000031", "name": "motor_mount", "kind": "assembly",
         "revision": 2, "component_count": 3},
    ])
    r = _new_assembly(name="motor_mount")

    assert fake.new_calls == []
    assert r["doc_id"] == "doc-cpp00000031"
    assert r["resumed"] is True and r["kind"] == "assembly"


def test_new_assembly_fresh_mints_alongside_an_open_assembly(engine):
    fake = engine([
        {"doc_id": "doc-cpp00000034", "name": "hinge0", "kind": "assembly",
         "revision": 2, "component_count": 3},
    ])
    resumed = _new_assembly(name="hinge0")
    assert resumed["reused"] is True and resumed["component_count"] == 3
    assert "3 components" in resumed["note"] and "fresh=True" in resumed["note"]

    r = _new_assembly(name="hinge0", fresh=True)
    assert fake.new_calls == ["hinge0"]
    assert r["doc_id"] != "doc-cpp00000034" and not r.get("reused")
    assert r["fresh"] is True


def test_new_assembly_bumped_version_resumes_the_series(engine):
    """An assembly iteration costs the user the same duplicate tab, file, PLM item and
    recovery row as a part iteration."""
    fake = engine([
        {"doc_id": "doc-cpp00000032", "name": "motor_mount", "kind": "assembly",
         "revision": 4, "component_count": 5},
    ])
    r = _new_assembly(name="motor_mount_v2")

    assert fake.new_calls == []
    assert r["doc_id"] == "doc-cpp00000032"
    assert r["resumed_by"] == "version_series"
    assert r["name"] == "motor_mount", "the REAL doc name, not the ask"
    assert C._CUR["doc_id"] == "doc-cpp00000032"


def test_new_assembly_is_never_steered_into_a_part(engine):
    """A part in the same name series must not absorb an assembly build."""
    fake = engine([
        {"doc_id": "doc-cpp00000033", "name": "motor_mount", "kind": "part",
         "revision": 4, "feature_count": 9},
    ])
    r = _new_assembly(name="motor_mount_v2")

    assert fake.new_calls == ["motor_mount"]
    assert r["requested_name"] == "motor_mount_v2"
    assert r.get("resumed") is not True


# ---------------------------------------------------------------------------
# Version tokens are normalized before the first document is minted.
# ---------------------------------------------------------------------------


def test_new_part_first_mint_drops_the_version_suffix(engine):
    fake = engine([])
    r = _new_part(name="widget_v1")

    assert fake.new_calls == ["widget"]
    assert r["name"] == "widget"
    assert r["requested_name"] == "widget_v1"
    assert "plm_checkpoint" in r["note"] and "plm_timeline" in r["note"]


def test_new_part_fresh_mint_also_drops_the_version_suffix(engine):
    fake = engine([])
    r = _new_part(name="widget_v1", fresh=True)

    assert fake.new_calls == ["widget"]
    assert r["name"] == "widget"
    assert r["requested_name"] == "widget_v1"


def test_new_part_unversioned_name_is_unchanged(engine):
    fake = engine([])
    r = _new_part(name="widget")

    assert fake.new_calls == ["widget"]
    assert r["name"] == "widget"
    assert "requested_name" not in r


def test_new_assembly_first_mint_drops_the_version_suffix(engine):
    fake = engine([])
    r = _new_assembly(name="rig-rev2")

    assert fake.new_calls == ["rig"]
    assert r["name"] == "rig"
    assert r["requested_name"] == "rig-rev2"
    assert "plm_checkpoint" in r["note"] and "plm_timeline" in r["note"]


def test_add_features_new_part_head_drops_the_version_suffix(engine):
    fake = engine([])
    r = _add_features([
        {"op": "new_part", "name": "bracket_gen3"},
        {"op": "add_feature", "feature": {
            "type": "box", "name": "Base",
            "params": {"length": 40, "width": 30, "height": 20}}},
    ])

    assert fake.new_calls == ["bracket"]
    assert r["doc_id"] == "doc-cppMINTED"
    assert r["requested_name"] == "bracket_gen3"
    assert "plm_checkpoint" in r["note"] and "plm_timeline" in r["note"]


def test_bumped_version_still_resumes_an_open_bare_document(engine):
    fake = engine([
        {"doc_id": "doc-cpp00000044", "name": "widget", "revision": 3,
         "feature_count": 5},
    ])
    r = _new_part(name="widget_v4")

    assert fake.new_calls == []
    assert r["doc_id"] == "doc-cpp00000044"
    assert r["resumed_by"] == "version_series"
    assert r["name"] == "widget"


# ---------------------------------------------------------------------------
# …but a document that is ALREADY open under the version-suffixed name is that
# document, and the engine's resume matches it on the name VERBATIM. Stripping
# the token on that call hands doc_new a name nothing is open under, and the
# "resume" mints instead — the very duplication the strip exists to prevent.
# ---------------------------------------------------------------------------


def test_add_features_head_keeps_a_name_that_is_already_open_verbatim(engine):
    fake = engine([
        {"doc_id": "doc-cpp00000051", "name": "bracket_v2", "revision": 2,
         "feature_count": 4},
    ])
    r = _add_features([
        {"op": "new_part", "name": "bracket_v2"},
        {"op": "add_feature", "feature": {
            "type": "box", "name": "Base",
            "params": {"length": 40, "width": 30, "height": 20}}},
    ])

    assert fake.new_calls == [], "the open bracket_v2 is the document, not a new bracket"
    assert r["doc_id"] == "doc-cpp00000051"


def test_new_assembly_keeps_a_name_that_is_already_open_verbatim(engine):
    fake = engine([
        {"doc_id": "doc-cpp00000052", "name": "rig_v2", "kind": "assembly",
         "revision": 2, "component_count": 3},
    ])
    r = _new_assembly(name="rig_v2")

    assert fake.new_calls == []
    assert r["doc_id"] == "doc-cpp00000052"
    assert r["reused"] is True
    assert "requested_name" not in r


@pytest.mark.parametrize(
    ("requested", "base"),
    [
        ("sixaxis_arm_revD_final5", "sixaxis_arm"),
        ("base_revD", "base"),
        ("arm-rev C", "arm"),
        ("part_revisionB", "part"),
        ("part_final2", "part"),
    ],
)
def test_revision_letter_and_final_requests_resume_existing_design(engine, requested, base):
    fake = engine([
        {"doc_id": "doc-existing", "name": base, "revision": 6,
         "feature_count": 12, "kind": "part"},
    ])

    r = _new_part(name=requested)

    assert fake.new_calls == []
    assert r["doc_id"] == "doc-existing"
    assert r["resumed_by"] == "version_series"
    assert r["version_series"] == base
    assert r["requested_name"] == requested
    assert fake.docs[0]["name"] == base


@pytest.mark.parametrize(
    ("requested", "base"),
    [
        ("sixaxis_arm_revD_final5", "sixaxis_arm"),
        ("base_revD", "base"),
        ("arm-rev C", "arm"),
        ("part_revisionB", "part"),
        ("part_final2", "part"),
    ],
)
def test_revision_letter_and_final_first_mint_uses_clean_name(engine, requested, base):
    fake = engine([])

    r = _new_part(name=requested)

    assert fake.new_calls == [base]
    assert r["name"] == base
    assert r["requested_name"] == requested


def test_revision_letter_and_final_resume_older_versioned_sibling(engine):
    fake = engine([
        {"doc_id": "doc-old", "name": "sixaxis_arm_revC_final4", "revision": 4,
         "feature_count": 15, "kind": "part"},
    ])

    r = _new_part(name="sixaxis_arm_revD_final5")

    assert fake.new_calls == []
    assert r["doc_id"] == "doc-old"
    assert r["version_series"] == "sixaxis_arm"
    assert r["name"] == "sixaxis_arm_revC_final4"


def test_exact_open_revision_letter_name_uses_engine_mint_lock(engine):
    fake = engine([
        {"doc_id": "doc-base", "name": "sixaxis_arm", "feature_count": 20},
        {"doc_id": "doc-exact", "name": "sixaxis_arm_revD_final5",
         "feature_count": 3},
    ])

    r = _new_part(name="sixaxis_arm_revD_final5")

    assert fake.new_calls == []
    assert r["doc_id"] == "doc-exact"
    assert "resumed_by" not in r
    assert len(fake.docs) == 2


def test_fresh_revision_letter_mints_even_with_existing_series(engine):
    fake = engine([
        {"doc_id": "doc-base", "name": "sixaxis_arm", "feature_count": 20},
    ])

    r = _new_part(name="sixaxis_arm_revD_final5", fresh=True)

    assert fake.new_calls == ["sixaxis_arm"]
    assert r["name"] == "sixaxis_arm2"
    assert r["requested_name"] == "sixaxis_arm_revD_final5"


@pytest.mark.parametrize(
    "requested",
    ["PN-1235", "part_revisionBracket", "part_finalBracket", "armrevD",
     "base_revD_mount"],
)
def test_unmarked_designators_remain_distinct(engine, requested):
    fake = engine([])

    r = _new_part(name=requested)

    assert fake.new_calls == [requested]
    assert r["name"] == requested
    assert "requested_name" not in r


def test_assembly_and_batch_head_share_revision_letter_series(engine):
    fake = engine([
        {"doc_id": "doc-assembly", "name": "sixaxis_arm", "kind": "assembly",
         "component_count": 5},
        {"doc_id": "doc-part", "name": "part", "kind": "part",
         "feature_count": 7},
    ])

    assembly = _new_assembly(name="sixaxis_arm_revD_final5")
    batch = _add_features([
        {"op": "new_part", "name": "part_revisionB"},
        {"op": "add_feature", "feature": {
            "type": "box", "name": "Base",
            "params": {"length": 40, "width": 30, "height": 20}}},
    ])

    assert fake.new_calls == []
    assert assembly["doc_id"] == "doc-assembly"
    assert assembly["resumed_by"] == "version_series"
    assert batch["doc_id"] == "doc-part"