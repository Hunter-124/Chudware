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

    def test_valid_call_is_unaffected(self):
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
