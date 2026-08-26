"""Tool-call dialect normalization + missing-argument teaching errors.

Both defects were found live on 2026-07-27 with claude-haiku-4-5 on the general
lane, in a single run that made ten tool calls and failed seven of them:

  * The model wrote ``{"tool": "fs_list", "path": "/…/CDR"}`` — arguments FLAT
    beside the tool name. ``_parse_directive`` returned the dict untouched and
    the caller read ``parsed.get("args") or {}``, so every argument was dropped
    and the tool ran with nothing.
  * The resulting failure surfaced raw Python —
    "register_fs_tools.<locals>.fs_read() missing 1 required positional
    argument: 'path'" — which a small model cannot act on, so it reissued the
    identical empty call until the loop guard killed the run.

After the fix the same prompt completes in two calls with zero failures.
"""
from __future__ import annotations

import json

from services.agent.agent_turn import (
    _dispatch_tool,
    _normalize_directive,
    _parse_directive,
)


class TestDirectiveDialects:
    def test_flat_arguments_are_recovered(self):
        """The exact shape haiku emitted."""
        d = _parse_directive('```json\n{"tool": "fs_list", "path": "/x/CDR"}\n```')
        assert d["tool"] == "fs_list"
        assert d["args"] == {"path": "/x/CDR"}

    def test_canonical_nested_args_still_win(self):
        d = _parse_directive('{"tool": "fs_list", "args": {"path": "/x"}}')
        assert d["args"] == {"path": "/x"}

    def test_nested_args_are_not_polluted_by_stray_top_level_keys(self):
        """When a real args container exists it is authoritative — sibling keys
        must NOT be merged in, or a stray field becomes a bogus argument."""
        d = _parse_directive('{"tool": "fs_list", "args": {"path": "/x"}, "note": "hi"}')
        assert d["args"] == {"path": "/x"}

    def test_arguments_alias_including_json_string_form(self):
        assert _parse_directive(
            '{"tool": "fs_read", "arguments": {"path": "a.txt"}}')["args"] == {"path": "a.txt"}
        # the tool-calling wire form: arguments carried as a JSON *string*
        assert _parse_directive(
            '{"tool": "fs_read", "arguments": "{\\"path\\": \\"a.txt\\"}"}'
        )["args"] == {"path": "a.txt"}

    def test_parameters_alias(self):
        d = _parse_directive('{"tool": "run_shell", "parameters": {"command": "ls"}}')
        assert d["args"] == {"command": "ls"}

    def test_openai_function_shape(self):
        d = _parse_directive(
            '{"function": {"name": "fs_glob", "arguments": {"pattern": "*.step"}}}')
        assert d["tool"] == "fs_glob" and d["args"] == {"pattern": "*.step"}

    def test_control_keys_never_become_arguments(self):
        d = _parse_directive(
            '{"tool": "fs_list", "path": ".", "reasoning": "I should look", "step": 2}')
        assert d["args"] == {"path": "."}

    def test_finish_envelope_is_never_rewritten(self):
        """A done/handoff payload's top-level keys are content, not arguments."""
        raw = {"done": True, "handoff": {"summary": "x"}}
        assert _normalize_directive(raw) == raw
        assert "args" not in _normalize_directive({"done": True, "summary": "s"})

    def test_non_directive_dict_passes_through(self):
        assert _normalize_directive({"status": "thinking"}) == {"status": "thinking"}


class TestDirectiveIsNotDroppedByALeadingPlanBlock:
    """Found live 2026-08-04 with claude-sonnet-5 on the general lane (run
    ``lane-delegate-1``), asked to delegate TWO parts to modelling sub-agents.

    The model replied with its plan as a bare ``{"task_list": {...}}`` fence
    followed by a second fence carrying ``{"tool": "delegate", ...}`` for the
    second part. ``_parse_directive`` returned the FIRST block that merely parsed
    as JSON — the plan — which carries no ``tool``, so the delegate never ran.
    One of the two parts was silently dropped and the turn still closed
    ``success=True stop_reason=confirmed`` while its own task list still read
    ``"Delegate Part B" : running``.
    """

    #: The reply shape, reduced from the live trace.
    LIVE_REPLY = (
        '```json\n'
        '{"task_list": {"items": ['
        '{"id": "1", "text": "Publish task list", "status": "done"},'
        '{"id": "3", "text": "Delegate Part B", "status": "running"}'
        '], "title": "Bolted joint"}}\n'
        '```\n\n'
        '```json\n'
        '{"tool": "delegate", "args": {"role": "model3d", "brief": "Create CoverB"}}\n'
        '```\n'
    )

    def test_a_later_tool_call_is_not_lost_behind_a_non_directive_block(self):
        """A non-directive first fence must never shadow a real call after it."""
        reply = (
            '```json\n{"note": "thinking about the plate"}\n```\n\n'
            '```json\n{"tool": "delegate", "args": {"role": "model3d"}}\n```\n'
        )
        d = _parse_directive(reply)
        assert d["tool"] == "delegate"
        assert d["args"] == {"role": "model3d"}

    def test_a_later_finish_envelope_is_not_lost_either(self):
        reply = ('```json\n{"note": "n"}\n```\n\n'
                 '```json\n{"done": true, "handoff": {"summary": "s"}}\n```\n')
        d = _parse_directive(reply)
        assert d.get("done") is True

    def test_the_live_reply_yields_an_actionable_directive(self):
        """Whatever we do with the plan, the turn must not end on a block that
        cannot be dispatched — the failure mode was a non-directive finish."""
        from services.agent.agent_turn import _is_directive

        d = _parse_directive(self.LIVE_REPLY)
        assert _is_directive(d), d

    def test_a_bare_task_list_payload_becomes_a_task_list_call(self):
        """The plan itself is not lost: the bare payload is the flat dialect of a
        ``task_list`` call, and its inner dict is already that tool's kwargs."""
        d = _parse_directive(self.LIVE_REPLY)
        assert d["tool"] == "task_list"
        assert [i["id"] for i in d["args"]["items"]] == ["1", "3"]
        assert d["args"]["title"] == "Bolted joint"

    def test_junk_only_reply_still_reaches_the_junk_json_floor(self):
        """No regression for mega-24c: with no directive anywhere, the first
        parsed object is still returned so the floor can classify it."""
        d = _parse_directive('```json\n{"foo": 1}\n```')
        assert d == {"foo": 1}

    def test_task_list_key_inside_a_finish_envelope_is_not_hijacked(self):
        d = _parse_directive(
            '```json\n{"done": true, "task_list": {"items": []}}\n```')
        assert d.get("done") is True and "tool" not in d


class TestMissingArgumentErrors:
    def test_empty_call_names_the_tool_and_the_missing_field(self):
        r = _dispatch_tool("fs_read", {})
        assert r["ok"] is False and r["status"] == "missing_argument"
        assert "path" in r["error"] and "fs_read" in r["error"]
        assert r["required"] == ["path"]
        assert r["example"] == {"path": "<path>"}

    def test_error_never_leaks_python_internals(self):
        """`register_fs_tools.<locals>.fs_read()` is meaningless to a model."""
        for tool in ("fs_read", "fs_glob", "run_shell"):
            err = _dispatch_tool(tool, {})["error"]
            assert "<locals>" not in err and "positional argument" not in err

    def test_error_tells_the_model_not_to_retry_identically(self):
        assert "will not help" in _dispatch_tool("fs_glob", {})["error"]

    def test_valid_call_is_unaffected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CHUDWARE_FS_ROOT", str(tmp_path))
        r = _dispatch_tool("fs_list", {"path": "."})
        assert (r if isinstance(r, dict) else {}).get("ok") is True


class TestEmptyFinishMessage:
    """A finish envelope carrying only structured outputs rendered as a BLANK
    chat bubble even though the work succeeded (live 2026-07-27, haiku: a built
    cube came back as {part_name, mass_g, volume_mm3, saved_path} with no prose
    key at all)."""

    def test_outputs_are_reported_when_there_is_no_prose(self):
        from services.agent.agent_turn import _summarize_outputs
        s = _summarize_outputs({"part_name": "Cube", "mass_g": 172.8,
                                "volume_mm3": 64000, "saved_path": "/tmp/c.step+"})
        assert "Cube" in s and "172.8" in s and "64000" in s

    def test_outputs_nested_under_outputs_are_found(self):
        from services.agent.agent_turn import _summarize_outputs
        assert "P" in _summarize_outputs({"outputs": {"part_name": "P", "mass_g": 1.5}})

    def test_nothing_concrete_yields_nothing(self):
        """Never invent prose — an envelope with no reportable field stays empty
        so the honesty gates still see it as content-free."""
        from services.agent.agent_turn import _summarize_outputs
        assert _summarize_outputs({}) == ""
        assert _summarize_outputs({"typed_refusal": {"reason": "x"}}) == ""
        assert _summarize_outputs("not a dict") == ""

    def test_real_prose_is_left_alone(self):
        """_handoff_text still wins; synthesis only fills a genuine gap."""
        from services.agent.agent_turn import _handoff_text
        assert _handoff_text({"summary": "I built the cube."}) == "I built the cube."


class TestArgSynonyms:
    """Live 2026-07-27 (claude-opus, flyswatter run): export_part(format="step")
    was rejected with "no parameter of that name and no unambiguous synonym"
    and cost a billed completion, even though `format` IS the canonical
    English name of the tool's `fmt` parameter."""

    @staticmethod
    def _export_spec():
        from services.agent import agent_turn as at
        fn = at._bind_tool("export_part")
        assert fn is not None, "export_part does not resolve in the MCP registry"
        return at, fn

    def test_format_maps_to_fmt(self):
        from services.agent.agent_turn import _reconcile_kwargs
        at, fn = self._export_spec()
        kwargs, notes, err = _reconcile_kwargs(
            "export_part", fn,
            {"doc_id": "d1", "path": "/tmp/x.step", "format": "step"})
        assert err is None, err
        assert kwargs["fmt"] == "step"
        assert kwargs["path"] == "/tmp/x.step"
        assert any("format" in n and "fmt" in n for n in notes)

    def test_file_format_maps_to_fmt(self):
        from services.agent.agent_turn import _reconcile_kwargs
        at, fn = self._export_spec()
        kwargs, _notes, err = _reconcile_kwargs(
            "export_part", fn, {"doc_id": "d1", "file_format": "stl"})
        assert err is None, err
        assert kwargs["fmt"] == "stl"

    def test_type_maps_to_fmt_for_export_part_only(self):
        """`type` is too generic to map globally; export_part has no other
        parameter a str type= could mean, so the per-tool table maps it."""
        from services.agent.agent_turn import _reconcile_kwargs
        at, fn = self._export_spec()
        kwargs, _notes, err = _reconcile_kwargs(
            "export_part", fn, {"doc_id": "d1", "type": "step"})
        assert err is None, err
        assert kwargs["fmt"] == "step"
        # A tool with no `fmt` parameter must NOT swallow `type` silently.
        fs = at._bind_tool("fs_read")
        _k, _n, err2 = _reconcile_kwargs("fs_read", fs,
                                         {"path": "a.txt", "type": "step"})
        assert err2 is not None and "type" in err2["unknown_params"]

    def test_canonical_fmt_is_untouched(self):
        from services.agent.agent_turn import _reconcile_kwargs
        _at, fn = self._export_spec()
        kwargs, notes, err = _reconcile_kwargs(
            "export_part", fn, {"doc_id": "d1", "fmt": "step"})
        assert err is None and kwargs["fmt"] == "step" and not notes


class TestOpToolMigration:
    """Live 2026-07-27 (claude-opus, flyswatter run): a top-level
    edit_feature(doc_id, feature="f2", params={...}) call was rejected as a
    non-existent tool and cost a full completion even though the mapping to
    add_features(ops=[...]) is total and unambiguous. Clean shapes now
    migrate and execute; ambiguous ones still get the teaching error."""

    def test_edit_feature_with_feature_key(self):
        """The verbatim recorded call shape."""
        from services.agent.agent_turn import _migrate_op_tool
        out = _migrate_op_tool("edit_feature",
                               {"doc_id": "d1", "feature": "f2",
                                "params": {"radius": 3}})
        assert out is not None
        kwargs, note = out
        assert kwargs == {"doc_id": "d1", "ops": [
            {"op": "edit_feature", "id": "f2", "params": {"radius": 3}}]}
        assert "edit_feature" in note and "add_features" in note

    def test_delete_feature_with_feature_id_alias(self):
        from services.agent.agent_turn import _migrate_op_tool
        kwargs, _note = _migrate_op_tool(
            "delete_feature", {"doc_id": "d1", "feature_id": "f7"})
        assert kwargs == {"doc_id": "d1",
                          "ops": [{"op": "delete_feature", "id": "f7"}]}

    def test_add_feature_flat_shape(self):
        from services.agent.agent_turn import _migrate_op_tool
        kwargs, _note = _migrate_op_tool(
            "add_feature",
            {"doc_id": "d1", "type": "box", "name": "Lid",
             "params": {"length": 10, "width": 5, "height": 2}})
        assert kwargs == {"doc_id": "d1", "ops": [{"op": "add_feature",
            "feature": {"type": "box", "name": "Lid",
                        "params": {"length": 10, "width": 5, "height": 2}}}]}

    def test_add_feature_feature_dict_shape(self):
        from services.agent.agent_turn import _migrate_op_tool
        feat = {"type": "cylinder", "params": {"radius": 2, "height": 9}}
        kwargs, _note = _migrate_op_tool("add_feature",
                                         {"doc_id": "d1", "feature": feat})
        assert kwargs == {"doc_id": "d1",
                          "ops": [{"op": "add_feature", "feature": feat}]}

    def test_ambiguous_shapes_fall_back_to_the_teaching_error(self):
        """No silent guesses: anything that does not map cleanly returns None
        so the dispatch path emits the redirect teaching error instead."""
        from services.agent.agent_turn import _migrate_op_tool
        # an edit carrying nothing to change
        assert _migrate_op_tool("edit_feature",
                                {"doc_id": "d1", "feature": "f2"}) is None
        # leftover keys the migration cannot account for
        assert _migrate_op_tool("delete_feature",
                                {"doc_id": "d1", "id": "f2",
                                 "extra": 1}) is None
        # two different target spellings = ambiguity
        assert _migrate_op_tool("edit_feature",
                                {"doc_id": "d1", "feature": "f2", "id": "f3",
                                 "params": {"r": 1}}) is None
        # no doc_id at all
        assert _migrate_op_tool("delete_feature", {"id": "f2"}) is None
        # a name that is not op-shaped
        assert _migrate_op_tool("explode_part",
                                {"doc_id": "d1", "id": "f2"}) is None

    def test_dispatch_migrates_and_executes(self):
        """End-to-end through _dispatch_tool: the call reaches add_features
        (which then fails honestly on the unknown doc — an ENGINE error about
        the document, never an unknown_tool rejection of the name)."""
        from services.agent.agent_turn import _dispatch_tool
        try:
            r = _dispatch_tool("edit_feature",
                               {"doc_id": "no_such_doc_xyz",
                                "feature": "f2", "params": {"radius": 3}})
        except Exception as exc:  # engine error = the migration executed
            text = str(exc)
        else:
            text = r if isinstance(r, str) else json.dumps(r)
        assert "unknown_tool" not in text
        assert "there is no 'edit_feature' tool" not in text
        assert "no_such_doc_xyz" in text or "no such" in text.lower()

    def test_dispatch_ambiguous_shape_still_teaches(self):
        from services.agent.agent_turn import _dispatch_tool
        r = _dispatch_tool("edit_feature", {"doc_id": "d1", "feature": "f2"})
        assert r["ok"] is False and r["status"] == "unknown_tool"
        assert "editing is an OP" in r["error"]

    def test_delete_and_add_redirects_exist_for_the_fallback(self):
        from services.agent.agent_turn import _unresolved_tool_error
        assert "delete_feature" in _unresolved_tool_error("delete_feature")["error"]
        assert "add_features" in _unresolved_tool_error("add_feature")["error"]


class TestToolResultSerialization:
    """Live 2026-07-28: a render tool's `[Image, caption]` return sat verbatim in
    turn.result.calls[*].result and blew up at the worker RPC boundary with
    "Object of type Image is not JSON serializable" — AFTER all the model's work,
    surfacing to the user as non_modeling_request. Normalise at dispatch."""

    def test_fastmcp_image_becomes_a_plain_dict(self):
        import json
        from pathlib import Path
        from services.agent.agent_turn import _json_safe_result

        class FakeImage:        # duck type of mcp.server.fastmcp.Image
            def __init__(self, p):
                self.path = Path(p)
                self.data = None
                self._mime_type = "image/png"

        safe = _json_safe_result([FakeImage("/tmp/x.png"), "caption"])
        json.dumps(safe)        # must not raise
        assert safe == [{"kind": "image", "path": "/tmp/x.png",
                         "mime": "image/png"}, "caption"]

    def test_unknown_object_degrades_to_str_instead_of_raising(self):
        import json
        from services.agent.agent_turn import _json_safe_result

        class Weird:
            pass

        json.dumps(_json_safe_result({"r": Weird()}))

    def test_plain_results_pass_through_untouched(self):
        from services.agent.agent_turn import _json_safe_result
        payload = {"ok": True, "doc_id": "d1", "n": [1, 2, 3], "s": None}
        assert _json_safe_result(payload) == payload

    def test_image_tool_result_reaches_the_next_provider_message(self, tmp_path):
        from services.agent.agent_turn import _tool_result_parts

        png = tmp_path / "drawing.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n")
        result = [{"kind": "image", "path": str(png), "mime": "image/png"},
                  '{"views":[{"view_id":"v1"}]}']
        parts = _tool_result_parts("Result of review_drawing:", result)
        assert parts[0]["type"] == "text"
        assert "view_id" in parts[0]["text"]
        assert parts[1] == {
            "type": "image", "path": str(png), "mime": "image/png"}

    def test_drawing_export_requires_review_after_the_last_mutation(self):
        from services.agent.agent_turn import _drawing_export_review_block

        doc = "dwg-1"
        calls = [
            {"tool": "set_titleblock", "args": {"doc_id": doc}, "ok": True},
            {"tool": "review_drawing", "args": {"doc_id": doc}, "ok": True},
        ]
        assert _drawing_export_review_block(calls, {"doc_id": doc}) is None
        calls.append(
            {"tool": "add_dimension", "args": {"doc_id": doc}, "ok": True})
        blocked = _drawing_export_review_block(calls, {"doc_id": doc})
        assert "changed after its last review" in blocked

    def test_drawing_export_without_any_review_is_blocked(self):
        from services.agent.agent_turn import _drawing_export_review_block

        blocked = _drawing_export_review_block([], {"doc_id": "dwg-1"})
        assert "not been visually reviewed" in blocked
