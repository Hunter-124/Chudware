"""Claude Code CLI headless provider.

Drives the installed ``claude`` binary in non-interactive print mode so a Claude
Pro/Max subscription (already authenticated by the CLI) can power the harness
without a metered API key.

Wire shape (v1):
  claude -p <prompt> --output-format json [--model …] [--system-prompt …]
         --tools "" [--max-budget-usd …]

``supports_vision`` is False for v1 — CLI image attachment is uneven across
versions and the harness treats missing vision as stats-only feedback.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from typing import Any

from .base import Provider

log = logging.getLogger(__name__)

DEFAULT_MODEL = "sonnet"
DEFAULT_BINARY = "claude"
DEFAULT_TIMEOUT_S = 300.0
MISSING_CLI_MSG = (
    "claude CLI not installed (npm i -g @anthropic-ai/claude-code)"
)


def _part_text(part: dict) -> str:
    if not isinstance(part, dict):
        return ""
    if part.get("type") == "text":
        return str(part.get("text") or "")
    if part.get("type") == "image":
        # Vision off in v1 — surface a placeholder so the model still sees context.
        return f"[image omitted: {part.get('path', '?')}]"
    return str(part.get("text") or "")


def serialize_conversation(system: str, messages: list[dict]) -> tuple[str, str]:
    """Return ``(system_prompt, user_prompt)`` for the CLI.

    System is passed via ``--system-prompt`` when non-empty. Conversation turns
    are flattened into a single user prompt the CLI can consume in print mode.
    """
    turns: list[str] = []
    for msg in messages or []:
        role = str(msg.get("role") or "user").strip().lower() or "user"
        content = msg.get("content") or []
        if isinstance(content, str):
            body = content
        else:
            body = "\n".join(
                t for t in (_part_text(p) for p in content) if t
            ).strip()
        if not body:
            continue
        label = "User" if role == "user" else "Assistant" if role == "assistant" else role.title()
        turns.append(f"{label}:\n{body}")
    user_prompt = "\n\n".join(turns).strip() or "(empty)"
    return (system or "").strip(), user_prompt


def parse_cli_output(stdout: str) -> str:
    """Extract assistant text from Claude CLI stdout (json or plain).

    JSON print mode emits a single object with a top-level ``result`` string
    (and sometimes ``type``/``subtype``). Fall back to stripped plain text when
    the payload is not JSON or has no ``result``.
    """
    text = (stdout or "").strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # stream-json / multi-line: try last non-empty JSON object line
        last_obj: Any = None
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                last_obj = json.loads(line)
            except json.JSONDecodeError:
                continue
        if last_obj is None:
            return text
        data = last_obj
    if isinstance(data, dict):
        for key in ("result", "text", "content", "message"):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return val
            if isinstance(val, dict):
                inner = val.get("text") or val.get("content")
                if isinstance(inner, str) and inner.strip():
                    return inner
        # nested content blocks (Anthropic-shaped)
        content = data.get("content")
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
                elif isinstance(block, str):
                    parts.append(block)
            joined = "".join(parts).strip()
            if joined:
                return joined
    if isinstance(data, str):
        return data
    return text


class ClaudeCliProvider(Provider):
    """Headless driver for the Claude Code CLI (``claude -p``)."""

    name = "claude-cli"
    supports_vision = False
    offline_safe = False  # hits Anthropic cloud through the CLI

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        binary: str = DEFAULT_BINARY,
        timeout: float = DEFAULT_TIMEOUT_S,
        check_binary: bool = True,
    ) -> None:
        self.model = model
        self.binary = binary or DEFAULT_BINARY
        self.timeout = float(timeout)
        self._binary_path: str | None = None
        if check_binary:
            self._resolve_binary()

    def _resolve_binary(self) -> str:
        if self._binary_path:
            return self._binary_path
        path = shutil.which(self.binary)
        if not path:
            raise RuntimeError(MISSING_CLI_MSG)
        self._binary_path = path
        return path

    def build_argv(
        self,
        system: str,
        user_prompt: str,
        max_tokens: int = 8000,
    ) -> list[str]:
        """Build the subprocess argv (prompt included; callers log a redacted copy)."""
        argv = [
            self.binary,
            "-p",
            user_prompt,
            "--output-format",
            "json",
            # Disable tools: we want a pure completion, not an agent session.
            "--tools",
            "",
            "--no-session-persistence",
        ]
        if self.model:
            argv.extend(["--model", self.model])
        if system:
            argv.extend(["--system-prompt", system])
        # No direct max_tokens flag; approximate via budget only when the caller
        # passes a very small cap (keeps long runs unconstrained by default).
        # Claude Code exposes --max-budget-usd, not token count.
        if max_tokens and max_tokens > 0 and max_tokens < 500:
            # Tiny completion budgets ≈ sub-cent for cheap models; soft guard only.
            argv.extend(["--max-budget-usd", "0.05"])
        return argv

    @staticmethod
    def argv_for_log(argv: list[str]) -> list[str]:
        """Return argv with prompt / system-prompt bodies redacted for logs."""
        out: list[str] = []
        redact_next = False
        redact_flags = {"-p", "--system-prompt", "--append-system-prompt"}
        for i, tok in enumerate(argv):
            if redact_next:
                out.append("<prompt>")
                redact_next = False
                continue
            if tok in redact_flags:
                out.append(tok)
                # -p takes the next arg as prompt; also handle -p=<not used>
                if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                    redact_next = True
                continue
            out.append(tok)
        return out

    def _run(self, argv: list[str]) -> str:
        binary = self._resolve_binary()
        # Ensure argv[0] is the resolved path for reliability.
        run_argv = list(argv)
        run_argv[0] = binary
        log.info("claude-cli argv: %s", self.argv_for_log(run_argv))
        try:
            proc = subprocess.run(  # noqa: S603 — fixed binary, argv built here
                run_argv,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(MISSING_CLI_MSG) from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"claude CLI timed out after {self.timeout:.0f}s"
            ) from exc
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(
                f"claude CLI exited {proc.returncode}: {err[:500] or '(no output)'}"
            )
        return parse_cli_output(proc.stdout or "")

    def complete(
        self,
        system: str,
        messages: list[dict],
        max_tokens: int = 8000,
    ) -> str:
        sys_p, user_p = serialize_conversation(system, messages)
        argv = self.build_argv(sys_p, user_p, max_tokens=max_tokens)
        return self._run(argv)

    def complete_ex(
        self,
        system: str,
        messages: list[dict],
        max_tokens: int = 8000,
    ) -> tuple[str, str]:
        return self.complete(system, messages, max_tokens=max_tokens), ""
