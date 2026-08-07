"""tests for services.agent.skills — contracts/skills.md v1.

Coverage: discovery precedence, trigger match, once-per-session, slash
resolution, traversal guard, builtin catalog size, user save/delete.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from services.agent import skills as sk


# --------------------------------------------------------------------------- fixtures


@pytest.fixture()
def skill_roots(tmp_path, monkeypatch):
    """Isolate project/user/builtin roots under tmp_path."""
    project = tmp_path / "proj" / ".chudware" / "skills"
    user = tmp_path / "user_skills"
    builtin = tmp_path / "builtin"
    project.mkdir(parents=True)
    user.mkdir(parents=True)
    builtin.mkdir(parents=True)
    monkeypatch.setenv("CHUDWARE_SKILLS_PROJECT_DIR", str(project))
    monkeypatch.setenv("CHUDWARE_SKILLS_USER_DIR", str(user))
    monkeypatch.setenv("CHUDWARE_SKILLS_BUILTIN_DIR", str(builtin))
    sk.clear_cache()
    sk.reset_session()
    yield {"project": project, "user": user, "builtin": builtin}
    sk.clear_cache()
    sk.reset_session()


def _write_skill(root: Path, name: str, description: str, *, inject: str = "manual",
                 keywords=None, tools=None, phases=None, roles=None,
                 extra_body: str = "Body.\n") -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    triggers = ""
    if keywords or tools or phases:
        triggers = "triggers:\n"
        if keywords:
            triggers += "  keywords: [" + ", ".join(keywords) + "]\n"
        if tools:
            triggers += "  tools: [" + ", ".join(tools) + "]\n"
        if phases:
            triggers += "  phases: [" + ", ".join(phases) + "]\n"
    roles_line = "roles: [" + ", ".join(roles or []) + "]\n" if roles else ""
    text = (
        f"---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"inject: {inject}\n"
        f"{triggers}"
        f"{roles_line}"
        f"---\n\n"
        f"# {name}\n\n{extra_body}"
    )
    p = d / "SKILL.md"
    p.write_text(text, encoding="utf-8")
    return p


# --------------------------------------------------------------------------- discovery + precedence


def test_discover_builtins_from_repo():
    """Without env overrides, the real builtin set has the v1 + domain skills."""
    # Clear any test overrides from other tests in-process.
    import os
    for k in ("CHUDWARE_SKILLS_PROJECT_DIR", "CHUDWARE_SKILLS_USER_DIR", "CHUDWARE_SKILLS_BUILTIN_DIR"):
        os.environ.pop(k, None)
    sk.clear_cache()
    skills = sk.discover_skills(force=True)
    names = {s.name for s in skills if s.source == "builtin"}
    expected = {
        "edit-part", "edit-assembly", "edit-sketch", "edit-feature",
        "simulate", "assess-model-state", "source-parts", "ecad-board",
        "skill-creator",
        "make-drawing", "cam-job", "sheet-metal", "reverse-engineer", "plm-workflow",
        "surface-modeling", "weldment-frame", "design-for-manufacturability",
    }
    assert expected <= names, f"missing builtins: {expected - names}"
    # skills_list shape
    listing = sk.list_skills()
    assert len([e for e in listing if e["name"] in expected]) == len(expected)


def test_skill_creator_teaches_only_agent_reachable_write_surface():
    creator = sk.get_skill("skill-creator")
    assert creator is not None
    assert "fs_write" in creator.body
    assert "skills_save" not in creator.body
    assert "skills_list" not in creator.body


def test_precedence_project_over_user_over_builtin(skill_roots):
    _write_skill(skill_roots["builtin"], "alpha", "builtin alpha")
    _write_skill(skill_roots["user"], "alpha", "user alpha")
    _write_skill(skill_roots["project"], "alpha", "project alpha")
    _write_skill(skill_roots["builtin"], "beta", "builtin beta only")
    _write_skill(skill_roots["user"], "gamma", "user gamma")
    sk.clear_cache()
    by = {s.name: s for s in sk.discover_skills(force=True)}
    assert by["alpha"].source == "project"
    assert by["alpha"].description == "project alpha"
    assert by["beta"].source == "builtin"
    assert by["gamma"].source == "user"


def test_missing_description_skipped(skill_roots):
    d = skill_roots["builtin"] / "bad"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: bad\n---\n\nno desc\n", encoding="utf-8")
    sk.clear_cache()
    names = {s.name for s in sk.discover_skills(force=True)}
    assert "bad" not in names


# --------------------------------------------------------------------------- triggers + once-per-session


def test_keyword_trigger_auto_inject_once(skill_roots):
    _write_skill(
        skill_roots["builtin"], "edit-part", "part skill",
        inject="auto", keywords=["bracket", "part"],
        extra_body="PART BODY\n",
    )
    sk.clear_cache()
    sk.reset_session("s1")
    bodies = sk.activate_for_prompt("please design a bracket", session_id="s1")
    assert len(bodies) == 1
    assert "PART BODY" in bodies[0]
    # second time: once-per-session
    bodies2 = sk.activate_for_prompt("another bracket please", session_id="s1")
    assert bodies2 == []
    # other session still fires
    bodies3 = sk.activate_for_prompt("bracket again", session_id="s2")
    assert len(bodies3) == 1


def test_reinject_after_compaction(skill_roots):
    _write_skill(
        skill_roots["builtin"], "sim", "sim skill",
        inject="auto", keywords=["fea"],
        extra_body="SIM BODY\n",
    )
    sk.clear_cache()
    sk.reset_session("c1")
    assert sk.activate_for_prompt("run fea now", session_id="c1")
    assert sk.activate_for_prompt("run fea now", session_id="c1") == []
    sk.mark_compacted("c1")
    again = sk.activate_for_prompt("run fea now", session_id="c1")
    assert len(again) == 1
    assert "SIM BODY" in again[0]


def test_tool_and_phase_triggers(skill_roots):
    _write_skill(
        skill_roots["builtin"], "asm", "asm skill",
        inject="auto", tools=["asm_mate"], phases=["assembly"],
        extra_body="ASM BODY\n",
    )
    sk.clear_cache()
    sk.reset_session("t1")
    assert sk.activate_for_prompt("", session_id="t1", tools_about_to_run=["asm_mate"])
    sk.reset_session("t2")
    assert sk.activate_for_prompt("", session_id="t2", phase="assembly")
    # manual inject skills never auto-fire
    _write_skill(
        skill_roots["builtin"], "manual-only", "manual",
        inject="manual", keywords=["manual-only-kw"],
    )
    sk.clear_cache()
    sk.reset_session("t3")
    assert sk.activate_for_prompt("manual-only-kw please", session_id="t3") == []

def test_role_allowlist_is_enforced_for_auto_and_explicit_activation(skill_roots):
    _write_skill(
        skill_roots["builtin"],
        "review-only",
        "reviewer guidance",
        inject="auto",
        keywords=["prove criteria"],
        roles=["reviewer"],
        extra_body="REVIEW ONLY BODY\n",
    )

    sk.reset_session("role-gate")
    assert sk.activate_for_prompt(
        "prove criteria", session_id="role-gate", role="feature_modeler"
    ) == []
    assert sk.activate_for_prompt(
        "/review-only", session_id="role-gate", role="feature_modeler"
    ) == []
    bodies = sk.activate_for_prompt(
        "prove criteria", session_id="role-gate", role="reviewer"
    )
    assert len(bodies) == 1
    assert "REVIEW ONLY BODY" in bodies[0]


# --------------------------------------------------------------------------- slash resolution


def test_slash_skill_colon_and_short(skill_roots):
    _write_skill(skill_roots["builtin"], "edit-part", "part skill", extra_body="P\n")
    _write_skill(skill_roots["builtin"], "simulate", "sim skill", extra_body="S\n")
    sk.clear_cache()

    cleaned, hits = sk.resolve_slash("/skill:edit-part focus on fillets")
    assert hits == [("edit-part", "focus on fillets")]
    assert "edit-part" not in cleaned

    cleaned2, hits2 = sk.resolve_slash("/simulate check drag\nthen report")
    assert hits2 == [("simulate", "check drag")]
    assert "then report" in cleaned2

    # unknown bare slash left alone
    cleaned3, hits3 = sk.resolve_slash("/help me")
    assert hits3 == []
    assert "/help me" in cleaned3

    # unknown /skill: still stripped (explicit form)
    cleaned4, hits4 = sk.resolve_slash("/skill:nope-xyz")
    assert hits4 == []
    assert cleaned4 == ""


def test_slash_activates_with_force(skill_roots):
    _write_skill(
        skill_roots["builtin"], "edit-part", "part",
        inject="auto", keywords=["part"],
        extra_body="BODY\n",
    )
    sk.clear_cache()
    sk.reset_session("sl")
    # keyword first
    sk.activate_for_prompt("make a part", session_id="sl")
    # explicit slash still re-injects (force)
    bodies = sk.activate_for_prompt("/skill:edit-part again", session_id="sl")
    assert len(bodies) == 1


# --------------------------------------------------------------------------- skill:// traversal guard


def test_skill_uri_resolve_and_traversal(skill_roots):
    p = _write_skill(skill_roots["builtin"], "edit-part", "part", extra_body="HELLO\n")
    # companion file
    (p.parent / "notes.md").write_text("deep notes\n", encoding="utf-8")
    sk.clear_cache()

    r = sk.resolve_skill_uri("skill://edit-part")
    assert r["ok"] and "HELLO" in r["content"]

    r2 = sk.resolve_skill_uri("skill://edit-part/notes.md")
    assert r2["ok"] and "deep notes" in r2["content"]

    with pytest.raises(sk.SkillResolveError, match="traversal|Absolute|not allowed"):
        sk.resolve_skill_uri("skill://edit-part/../../../etc/passwd")

    with pytest.raises(sk.SkillResolveError, match="traversal|Absolute|not allowed"):
        sk.resolve_skill_uri("skill://edit-part/foo/../../builtin/edit-part/SKILL.md")

    with pytest.raises(sk.SkillResolveError, match="Unknown skill"):
        sk.resolve_skill_uri("skill://does-not-exist")


# --------------------------------------------------------------------------- catalog + CRUD


def test_catalog_block_lists_skills(skill_roots):
    _write_skill(skill_roots["builtin"], "a", "desc a")
    _write_skill(skill_roots["builtin"], "b", "desc b")
    sk.clear_cache()
    block = sk.catalog_block()
    assert "[SKILLS]" in block
    assert "- a: desc a" in block
    assert "- b: desc b" in block


def test_save_and_delete_user_skill(skill_roots):
    body = "---\nname: my-user\ndescription: a user skill\ninject: manual\n---\n\n# Hi\n"
    res = sk.save_user_skill("my-user", body)
    assert res["ok"] is True
    sk.clear_cache()
    s = sk.get_skill("my-user")
    assert s is not None and s.source == "user"
    # cannot delete builtin-only names that aren't in user dir
    assert sk.delete_user_skill("nope")["ok"] is False
    assert sk.delete_user_skill("my-user")["ok"] is True
    sk.clear_cache()
    assert sk.get_skill("my-user") is None


def test_skills_rpc_list_nine_builtins():
    """RPC list returns the v1 + domain builtins when no overrides hide them."""
    import os
    for k in ("CHUDWARE_SKILLS_PROJECT_DIR", "CHUDWARE_SKILLS_USER_DIR", "CHUDWARE_SKILLS_BUILTIN_DIR"):
        os.environ.pop(k, None)
    sk.clear_cache()
    from services.agent.worker_rpc import SKILLS_METHODS
    out = SKILLS_METHODS["skills_list"]({}, lambda *a, **k: None)
    assert out["ok"] is True
    names = {e["name"] for e in out["skills"]}
    expected = {
        "edit-part", "edit-assembly", "edit-sketch", "edit-feature",
        "simulate", "assess-model-state", "source-parts", "ecad-board",
        "skill-creator",
        "make-drawing", "cam-job", "sheet-metal", "reverse-engineer", "plm-workflow",
        "surface-modeling", "weldment-frame", "design-for-manufacturability",
    }
    assert expected <= names
    assert out["count"] >= len(expected)


def test_session_system_block_includes_catalog_and_activation(skill_roots):
    _write_skill(
        skill_roots["builtin"], "edit-part", "part skill",
        inject="auto", keywords=["bracket"],
        extra_body="PART BODY\n",
    )
    sk.clear_cache()
    sk.reset_session("sys")
    block = sk.session_system_block("sys", user_text="design a bracket")
    assert "[SKILLS]" in block
    assert "PART BODY" in block
    assert "edit-part" in block


# ------------------------------------------------- real builtin keyword phrasings


@pytest.fixture()
def real_builtins(monkeypatch):
    """Drop root overrides so the shipped builtin SKILL.md files are the catalog."""
    for k in ("CHUDWARE_SKILLS_PROJECT_DIR", "CHUDWARE_SKILLS_USER_DIR",
              "CHUDWARE_SKILLS_BUILTIN_DIR"):
        monkeypatch.delenv(k, raising=False)
    sk.clear_cache()
    sk.reset_session()
    yield
    sk.clear_cache()
    sk.reset_session()


def _fires(text: str, session_id: str) -> set[str]:
    sk.reset_session(session_id)
    return {s.name for s in sk.match_keyword_triggers(text, session_id=session_id)}


@pytest.mark.parametrize("text", [
    "make me a mudbucket for oil drilling and rigging",
    "build me a bracket",
    "build a bracket",
    "create a housing for the pump",
    "create me a spacer",
    "model me a flange",
    "design me a gearbox case",
    "3d print a knob",
    "machine a fixture plate",
])
def test_builtin_edit_part_fires_on_natural_build_phrasing(real_builtins, text):
    """The build-phrasing family must reach edit-part; it teaches new_part → add_features → verify."""
    assert "edit-part" in _fires(text, "kwphrase")


@pytest.mark.parametrize("text,expected_absent", [
    # File housekeeping: "parts" plural must not trip the bare `part` keyword.
    ("organize my files and archive old parts", "edit-part"),
    # Bare verbs are deliberately not keywords.
    ("build the firmware and make it louder", "edit-part"),
    ("create the release notes", "edit-part"),
])
def test_builtin_build_phrasing_does_not_overfire(real_builtins, text, expected_absent):
    assert expected_absent not in _fires(text, "kwguard")


def test_builtin_fea_phrasing_routes_to_simulate_only(real_builtins):
    hits = _fires("run fea on this bracket", "kwfea")
    assert "simulate" in hits
    assert "edit-part" not in hits


def test_bare_part_keyword_does_not_match_plural_parts(real_builtins):
    """`\\bpart\\b` is literal: 'parts' is a different token and must not activate edit-part."""
    assert "edit-part" in _fires("check the part thickness", "kwsing")
    assert "edit-part" not in _fires("archive old parts", "kwplur")


def test_builtin_assembly_and_sketch_build_phrasings(real_builtins):
    assert "edit-assembly" in _fires("put together the gearbox", "kwasm")
    assert "edit-assembly" in _fires("assemble the bracket and plate", "kwasm2")
    assert "edit-sketch" in _fires("draw a profile on the top plane", "kwskt")
    assert "edit-sketch" in _fires("draw me a hex outline", "kwskt2")


@pytest.mark.parametrize("text", ["add a fillet to the top edge", "add a hole through the boss",
                                  "add a chamfer on that edge"])
def test_builtin_edit_feature_already_catches_add_phrasings(real_builtins, text):
    """The bare fillet/hole/chamfer nouns already cover 'add a <feature>' — no keyword needed."""
    assert "edit-feature" in _fires(text, "kwfeat")


def test_builtin_build_phrasing_injects_once_per_session(real_builtins):
    sk.reset_session("kwonce")
    first = sk.activate_for_prompt("make me a mudbucket for oil drilling", session_id="kwonce")
    assert any("# Edit Part" in b for b in first)
    again = sk.activate_for_prompt("build me a bracket too", session_id="kwonce")
    assert not any("# Edit Part" in b for b in again)
    # A different session still gets it.
    sk.reset_session("kwonce2")
    other = sk.activate_for_prompt("build me a bracket", session_id="kwonce2")
    assert any("# Edit Part" in b for b in other)


# ------------------------------- measured coverage gaps (28-prompt matrix)
#
# A 28-prompt realistic-phrasing matrix scored 21/28 on the shipped keyword
# lists. These 7 prompts fired NOTHING for the skill that owns the workflow
# (except "check the part for problems", which reached edit-part only), so the
# model went in without the checklist it needed. The keywords below were added
# to close them; the overfire guard that follows is the other half of the
# bargain — a keyword generic enough to fire on everything burns the
# once-per-session budget on the wrong skill.


@pytest.mark.parametrize("expected,text", [
    # "model a" existed but English needs "an" before a vowel.
    ("edit-part", "model an enclosure for a pi"),
    # Plain-language strength question: no fea/stress/load-case token at all.
    ("simulate", "will this hold 200N?"),
    # PCB layout verbs, none of which are schematic/pcb/ecad.
    ("ecad-board", "route the board and add a ground plane"),
    # The verb the user actually types, plus the catalog-hardware designation.
    ("source-parts", "source a NEMA 17 stepper"),
    # Vault check-in phrased as English, not as "checkpoint".
    ("plm-workflow", "check in the changes"),
    # "model state" did not cover "state of this model".
    ("assess-model-state", "what is the state of this model?"),
    # Previously reached edit-part only — the health-check skill is the point.
    ("assess-model-state", "check the part for problems"),
])
def test_builtin_previously_missed_phrasings_now_activate(real_builtins, expected, text):
    assert expected in _fires(text, "kwgap")


@pytest.mark.parametrize("text,expected_absent", [
    # "create an"/"model an"/"design an" must not swallow the -an- prefix of a
    # longer word; the trailing \b is what makes the widening safe.
    ("create another revision note in the changelog", "edit-part"),
    ("the model answers the load question", "edit-part"),
    ("design analysis is queued", "edit-part"),
    # Bare "hold" is not a keyword — only the strength-question phrases are.
    ("hold the plate down with two screws", "simulate"),
    ("the buckle strap mounts here", "simulate"),
    # Mechanical planes and non-PCB routing stay out of ecad-board.
    ("sketch on the front plane", "ecad-board"),
    ("route the wire harness through the channel", "ecad-board"),
    ("reroute the coolant tubing", "ecad-board"),
    # "nut"/"nema"/"stepper"/"source a" inside longer tokens.
    ("wait a minute before rebuilding", "source-parts"),
    ("open the source assembly file", "source-parts"),
    ("step through the feature tree", "source-parts"),
    ("render the cinema-quality view", "source-parts"),
    # The classic: "check in" must not fire on "check interference".
    ("check interference between the parts", "plm-workflow"),
    # "check the part" must not fire on the plural housekeeping phrasing.
    ("check the parts list", "assess-model-state"),
    ("many errors were fixed last week", "assess-model-state"),
    ("extrude the profile 10mm", "assess-model-state"),
])
def test_builtin_widened_keywords_do_not_overfire(real_builtins, text, expected_absent):
    assert expected_absent not in _fires(text, "kwgapguard")


def test_phrase_keyword_matches_across_whitespace_runs(real_builtins):
    """Multi-word triggers are now the bulk of the widening, so a wrapped or
    double-spaced prompt must still match ("will this hold" over a newline)."""
    assert "simulate" in _fires("will this\nhold 200N under that bracket?", "kwws")
    assert "ecad-board" in _fires("please route  the  board next", "kwws2")


def test_mid_session_turn_still_keyword_activates_while_catalog_is_suppressed(real_builtins):
    """The static skills catalog is sent once per session (agent_turn
    ~900-1000 tokens/turn saving), but keyword activation is a sibling branch,
    not gated on it: a LATER user turn ("now make a drawing of it") must still
    pull make-drawing in. Pins both halves so the saving cannot be widened to
    swallow activation.
    """
    from services.agent import agent_turn as at

    sid = "midsession-kw"
    key = "midsession-catalog-key"
    sk.reset_session(sid)
    at._CATALOG_INJECTED_SESSIONS.discard(key)
    try:
        # Turn 1: catalog is due, and the build phrasing pulls edit-part.
        assert at._catalog_due(key) is True
        first = sk.activate_for_prompt("make me a bracket 50mm long", session_id=sid)
        assert any("# Edit Part" in b for b in first)

        # Turn 2, same session: catalog suppressed, keyword activation still live.
        assert at._catalog_due(key) is False
        second = sk.activate_for_prompt("now make a 2D drawing of it", session_id=sid)
        assert any("# Make Drawing" in b for b in second)
        # ...and the once-per-session guard keeps the earlier skill from resending.
        assert not any("# Edit Part" in b for b in second)
    finally:
        at._CATALOG_INJECTED_SESSIONS.discard(key)
        sk.reset_session(sid)


def test_plain_3d_printing_does_not_activate_reverse_engineering():
    import os
    for key in ("CHUDWARE_SKILLS_PROJECT_DIR", "CHUDWARE_SKILLS_USER_DIR",
                "CHUDWARE_SKILLS_BUILTIN_DIR"):
        os.environ.pop(key, None)
    sk.clear_cache()
    sk.reset_session("print-not-re")
    bodies = sk.activate_for_prompt(
        "Design a new enclosure for FDM 3D printing", session_id="print-not-re")
    assert not any("# Reverse Engineer" in body for body in bodies)


def test_new_domain_skills_activate_on_specific_intent():
    import os
    for key in ("CHUDWARE_SKILLS_PROJECT_DIR", "CHUDWARE_SKILLS_USER_DIR",
                "CHUDWARE_SKILLS_BUILTIN_DIR"):
        os.environ.pop(key, None)
    sk.clear_cache()
    cases = [
        ("surface", "build a boundary surface and thicken it", "# Surface Modeling"),
        ("weld", "make a square tube welded frame", "# Weldment Frame"),
        ("dfm", "run a design for manufacturability review", "# Design for Manufacturability"),
    ]
    for session, prompt, heading in cases:
        sk.reset_session(session)
        assert any(heading in body for body in
                   sk.activate_for_prompt(prompt, session_id=session))
