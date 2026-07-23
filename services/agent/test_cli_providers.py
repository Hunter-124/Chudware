"""Unit tests for headless CLI-driving providers (claude-cli / codex-cli).

Subprocess is mocked — no network, no real CLI required for the default suite.
Optional live smoke: set CHUDWARE_CLI_SMOKE=1 (skipped otherwise).
"""
from __future__ import annotations

import json
import os
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent import config as cfg
from agent.providers import base
from agent.providers.claude_cli_provider import (
    ClaudeCliProvider,
    MISSING_CLI_MSG as CLAUDE_MISSING,
    parse_cli_output as parse_claude,
    serialize_conversation as serialize_claude,
)
from agent.providers.codex_cli_provider import (
    CodexCliProvider,
    MISSING_CLI_MSG as CODEX_MISSING,
    parse_cli_output as parse_codex,
    serialize_conversation as serialize_codex,
)


# --------------------------------------------------------------------------- helpers

def _msgs(*turns: tuple[str, str]) -> list[dict]:
    out = []
    for role, text in turns:
        out.append({"role": role, "content": [base.text_part(text)]})
    return out


def _completed(stdout: str, returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


# --------------------------------------------------------------------------- serialization

def test_claude_serialize_system_and_turns():
    system, prompt = serialize_claude(
        "You are a CAD assistant.",
        _msgs(("user", "draw a cube"), ("assistant", "ok"), ("user", "fillet it")),
    )
    assert system == "You are a CAD assistant."
    assert "User:\ndraw a cube" in prompt
    assert "Assistant:\nok" in prompt
    assert "User:\nfillet it" in prompt


def test_codex_serialize_embeds_system():
    prompt = serialize_codex("sys", _msgs(("user", "hi")))
    assert "System:\nsys" in prompt
    assert "User:\nhi" in prompt


# --------------------------------------------------------------------------- output parsing

def test_claude_parse_json_result():
    payload = json.dumps({
        "type": "result",
        "subtype": "success",
        "result": "Hello from Claude",
        "is_error": False,
    })
    assert parse_claude(payload) == "Hello from Claude"


def test_claude_parse_plain_fallback():
    assert parse_claude("just plain text") == "just plain text"


def test_codex_parse_jsonl_agent_message():
    lines = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "t1"}),
        json.dumps({"type": "turn.started"}),
        json.dumps({
            "type": "item.completed",
            "item": {"id": "item_0", "type": "agent_message", "text": "hi there"},
        }),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1}}),
    ])
    assert parse_codex(lines) == "hi there"


def test_codex_parse_plain_fallback():
    assert parse_codex("plain reply") == "plain reply"


# --------------------------------------------------------------------------- argv construction

def test_claude_build_argv_flags():
    p = ClaudeCliProvider(model="sonnet", check_binary=False)
    argv = p.build_argv("SYS", "USER PROMPT", max_tokens=8000)
    assert argv[0] == "claude"
    assert "-p" in argv
    assert argv[argv.index("-p") + 1] == "USER PROMPT"
    assert "--output-format" in argv
    assert argv[argv.index("--output-format") + 1] == "json"
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert "--system-prompt" in argv
    assert argv[argv.index("--system-prompt") + 1] == "SYS"
    assert "--tools" in argv
    # log redaction hides the prompt body
    logged = p.argv_for_log(argv)
    assert "<prompt>" in logged
    assert "USER PROMPT" not in logged
    assert "SYS" not in logged


def test_codex_build_argv_flags():
    p = CodexCliProvider(model="gpt-5-codex", check_binary=False)
    argv = p.build_argv("do the thing", max_tokens=8000)
    assert argv[0] == "codex"
    assert argv[1] == "exec"
    assert argv[2] == "do the thing"
    assert "--json" in argv
    assert "-m" in argv
    assert argv[argv.index("-m") + 1] == "gpt-5-codex"
    assert "-s" in argv and argv[argv.index("-s") + 1] == "read-only"
    assert "-a" in argv and argv[argv.index("-a") + 1] == "never"
    assert "--skip-git-repo-check" in argv
    assert "--ephemeral" in argv
    logged = p.argv_for_log(argv)
    assert "<prompt>" in logged
    assert "do the thing" not in logged


# --------------------------------------------------------------------------- complete() with mocked subprocess

def test_claude_complete_parses_json(monkeypatch):
    p = ClaudeCliProvider(model="haiku", check_binary=False)
    p._binary_path = "/usr/bin/claude"
    payload = json.dumps({"type": "result", "result": "cube done"})

    def fake_run(argv, **kwargs):
        assert argv[0] == "/usr/bin/claude"
        assert "-p" in argv
        assert "--output-format" in argv
        return _completed(payload)

    with patch("agent.providers.claude_cli_provider.subprocess.run", side_effect=fake_run):
        out = p.complete("sys", _msgs(("user", "make a cube")))
    assert out == "cube done"


def test_codex_complete_parses_jsonl(monkeypatch):
    p = CodexCliProvider(model="gpt-5-codex", check_binary=False)
    p._binary_path = "/usr/bin/codex"
    payload = "\n".join([
        json.dumps({"type": "item.completed",
                    "item": {"type": "agent_message", "text": "filleted"}}),
    ])

    def fake_run(argv, **kwargs):
        assert argv[0] == "/usr/bin/codex"
        assert argv[1] == "exec"
        assert "--json" in argv
        return _completed(payload)

    with patch("agent.providers.codex_cli_provider.subprocess.run", side_effect=fake_run):
        out = p.complete("sys", _msgs(("user", "fillet")))
    assert out == "filleted"


def test_claude_complete_ex_empty_thinking():
    p = ClaudeCliProvider(check_binary=False)
    p._binary_path = "/usr/bin/claude"
    with patch(
        "agent.providers.claude_cli_provider.subprocess.run",
        return_value=_completed(json.dumps({"result": "ok"})),
    ):
        text, thinking = p.complete_ex("s", _msgs(("user", "q")))
    assert text == "ok"
    assert thinking == ""


# --------------------------------------------------------------------------- binary-missing error shape

def test_claude_missing_binary_at_construct():
    with patch("agent.providers.claude_cli_provider.shutil.which", return_value=None):
        with pytest.raises(RuntimeError) as ei:
            ClaudeCliProvider(check_binary=True)
    assert str(ei.value) == CLAUDE_MISSING
    assert "npm i -g @anthropic-ai/claude-code" in str(ei.value)


def test_codex_missing_binary_at_construct():
    with patch("agent.providers.codex_cli_provider.shutil.which", return_value=None):
        with pytest.raises(RuntimeError) as ei:
            CodexCliProvider(check_binary=True)
    assert str(ei.value) == CODEX_MISSING
    assert "npm i -g @openai/codex" in str(ei.value)


def test_claude_missing_binary_on_first_call():
    p = ClaudeCliProvider(check_binary=False)
    with patch("agent.providers.claude_cli_provider.shutil.which", return_value=None):
        with pytest.raises(RuntimeError) as ei:
            p.complete("s", _msgs(("user", "q")))
    assert str(ei.value) == CLAUDE_MISSING


def test_claude_file_not_found_maps_to_missing():
    p = ClaudeCliProvider(check_binary=False)
    p._binary_path = "/no/such/claude"
    with patch(
        "agent.providers.claude_cli_provider.subprocess.run",
        side_effect=FileNotFoundError("gone"),
    ):
        with pytest.raises(RuntimeError) as ei:
            p.complete("s", _msgs(("user", "q")))
    assert str(ei.value) == CLAUDE_MISSING


# --------------------------------------------------------------------------- offline refusal + config wiring

@pytest.mark.parametrize("name", ["claude-cli", "codex-cli"])
def test_offline_mode_refuses_cli_providers(name):
    with pytest.raises(cfg.OfflineModeError) as ei:
        cfg.get_provider(name, {"offline_mode": True, "model": "x"})
    assert "offline mode" in str(ei.value).lower()
    assert name in str(ei.value)


@pytest.mark.parametrize("name", ["claude-cli", "codex-cli"])
def test_cli_providers_never_offline_safe(name):
    stub = SimpleNamespace(name=name, offline_safe=False)
    assert cfg.provider_offline_safe(stub) is False


def test_get_provider_claude_cli_constructs(monkeypatch):
    monkeypatch.setattr(
        "agent.providers.claude_cli_provider.shutil.which",
        lambda b: f"/usr/bin/{b}",
    )
    p = cfg.get_provider("claude-cli", {"model": "opus"})
    assert isinstance(p, ClaudeCliProvider)
    assert p.name == "claude-cli"
    assert p.supports_vision is False
    assert p.model == "opus"
    assert cfg.provider_offline_safe(p) is False


def test_get_provider_codex_cli_constructs(monkeypatch):
    monkeypatch.setattr(
        "agent.providers.codex_cli_provider.shutil.which",
        lambda b: f"/usr/bin/{b}",
    )
    p = cfg.get_provider("codex-cli", {"model": "gpt-5-codex"})
    assert isinstance(p, CodexCliProvider)
    assert p.name == "codex-cli"
    assert p.supports_vision is False
    assert p.model == "gpt-5-codex"


def test_routing_kind_maps_cli_providers():
    assert cfg._ROUTING_KIND_TO_PROVIDER_NAME["claude-cli"] == "claude-cli"
    assert cfg._ROUTING_KIND_TO_PROVIDER_NAME["codex-cli"] == "codex-cli"


# --------------------------------------------------------------------------- optional live smoke (gated)

@pytest.mark.skipif(
    os.environ.get("CHUDWARE_CLI_SMOKE") != "1",
    reason="set CHUDWARE_CLI_SMOKE=1 to run live CLI smoke",
)
def test_live_claude_cli_smoke():
    if not __import__("shutil").which("claude"):
        pytest.skip("claude binary not on PATH")
    p = ClaudeCliProvider(model="haiku", timeout=60.0)
    text = p.complete(
        "Reply with exactly the word pong and nothing else.",
        _msgs(("user", "ping")),
        max_tokens=32,
    )
    assert text and isinstance(text, str)


@pytest.mark.skipif(
    os.environ.get("CHUDWARE_CLI_SMOKE") != "1",
    reason="set CHUDWARE_CLI_SMOKE=1 to run live CLI smoke",
)
def test_live_codex_cli_smoke():
    if not __import__("shutil").which("codex"):
        pytest.skip("codex binary not on PATH")
    p = CodexCliProvider(timeout=90.0)
    text = p.complete(
        "Reply with exactly the word pong and nothing else.",
        _msgs(("user", "ping")),
        max_tokens=32,
    )
    assert text and isinstance(text, str)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
