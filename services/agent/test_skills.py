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
    project = tmp_path / "proj" / ".billet" / "skills"
    user = tmp_path / "user_skills"
    builtin = tmp_path / "builtin"
    project.mkdir(parents=True)
    user.mkdir(parents=True)
    builtin.mkdir(parents=True)
    monkeypatch.setenv("AICAD_SKILLS_PROJECT_DIR", str(project))
    monkeypatch.setenv("AICAD_SKILLS_USER_DIR", str(user))
    monkeypatch.setenv("AICAD_SKILLS_BUILTIN_DIR", str(builtin))
    sk.clear_cache()
    sk.reset_session()
    yield {"project": project, "user": user, "builtin": builtin}
    sk.clear_cache()
    sk.reset_session()


def _write_skill(root: Path, name: str, description: str, *, inject: str = "manual",
                 keywords=None, tools=None, phases=None, extra_body: str = "Body.\n") -> Path:
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
    text = (
        f"---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"inject: {inject}\n"
        f"{triggers}"
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
    for k in ("AICAD_SKILLS_PROJECT_DIR", "AICAD_SKILLS_USER_DIR", "AICAD_SKILLS_BUILTIN_DIR"):
        os.environ.pop(k, None)
    sk.clear_cache()
    skills = sk.discover_skills(force=True)
    names = {s.name for s in skills if s.source == "builtin"}
    expected = {
        "edit-part", "edit-assembly", "edit-sketch", "edit-feature",
        "simulate", "assess-model-state", "source-parts", "ecad-board",
        "skill-creator",
        "make-drawing", "cam-job", "sheet-metal", "reverse-engineer", "plm-workflow",
    }
    assert expected <= names, f"missing builtins: {expected - names}"
    # skills_list shape
    listing = sk.list_skills()
    assert len([e for e in listing if e["name"] in expected]) == len(expected)


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
    for k in ("AICAD_SKILLS_PROJECT_DIR", "AICAD_SKILLS_USER_DIR", "AICAD_SKILLS_BUILTIN_DIR"):
        os.environ.pop(k, None)
    sk.clear_cache()
    from engine_oracle.parts_hub.skills_rpc import SKILLS_METHODS
    out = SKILLS_METHODS["skills_list"]({}, lambda *a, **k: None)
    assert out["ok"] is True
    names = {e["name"] for e in out["skills"]}
    expected = {
        "edit-part", "edit-assembly", "edit-sketch", "edit-feature",
        "simulate", "assess-model-state", "source-parts", "ecad-board",
        "skill-creator",
        "make-drawing", "cam-job", "sheet-metal", "reverse-engineer", "plm-workflow",
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
