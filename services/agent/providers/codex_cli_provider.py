"""OpenAI Codex CLI headless provider.

Drives the installed ``codex`` binary via ``codex exec --json`` so a ChatGPT
Plus/Pro subscription (already authenticated by the CLI) can power the harness
without a metered API key.

Wire shape (v1):
  codex exec <prompt> --json [-m MODEL] [-s read-only] [-a never]
            [--skip-git-repo-check] [--ephemeral]

``supports_vision`` is False for v1 — image flags exist but harness vision is
not wired through the CLI path yet.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from typing import Any

from .base import Provider

log = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-6-sol"
DEFAULT_BINARY = "codex"
DEFAULT_TIMEOUT_S = 300.0
MISSING_CLI_MSG = (
    "codex CLI not installed (npm i -g @openai/codex)"
)


def _part_text(part: dict) -> str:
    if not isinstance(part, dict):
        return ""
    if part.get("type") == "text":
        return str(part.get("text") or "")
    if part.get("type") == "image":
        return f"[image omitted: {part.get('path', '?')}]"
    return str(part.get("text") or "")


def serialize_conversation(system: str, messages: list[dict]) -> str:
    """Flatten system + turns into a single prompt for ``codex exec``."""
    chunks: list[str] = []
    system = (system or "").strip()
    if system:
        chunks.append(f"System:\n{system}")
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
        chunks.append(f"{label}:\n{body}")
    # Soft instruction so the CLI agent returns text rather than editing the repo.
    chunks.append(
        "Assistant:\nRespond with the final answer text only. "
        "Do not edit files or run shell commands unless the user explicitly asked."
    )
    return "\n\n".join(chunks).strip() or "(empty)"


def parse_cli_output(stdout: str) -> str:
    """Extract the last agent message from Codex ``--json`` JSONL (or plain text).

    Observed event shapes (codex-cli 0.144+):
      {"type":"item.completed","item":{"type":"agent_message","text":"…"}}
      {"type":"message","role":"assistant","content":[…]}  (older / alt)
    """
    text = (stdout or "").strip()
    if not text:
        return ""

    last_msg = ""
    any_json = False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        any_json = True
        if not isinstance(data, dict):
            continue
        # Primary: item.completed / agent_message
        item = data.get("item")
        if isinstance(item, dict):
            itype = str(item.get("type") or "")
            if itype in ("agent_message", "message", "assistant_message"):
                t = item.get("text") or item.get("content")
                if isinstance(t, str) and t.strip():
                    last_msg = t
                elif isinstance(t, list):
                    parts = []
                    for block in t:
                        if isinstance(block, dict):
                            parts.append(str(block.get("text") or ""))
                        elif isinstance(block, str):
                            parts.append(block)
                    joined = "".join(parts).strip()
                    if joined:
                        last_msg = joined
        # Alt top-level shapes
        if data.get("type") in ("agent_message", "message") or data.get("role") == "assistant":
            t = data.get("text") or data.get("content")
            if isinstance(t, str) and t.strip():
                last_msg = t
            elif isinstance(t, list):
                parts = []
                for block in t:
                    if isinstance(block, dict):
                        parts.append(str(block.get("text") or ""))
                    elif isinstance(block, str):
                        parts.append(block)
                joined = "".join(parts).strip()
                if joined:
                    last_msg = joined
        # Some builds emit a final result object
        if data.get("type") in ("result", "turn.completed") and isinstance(data.get("result"), str):
            if data["result"].strip():
                last_msg = data["result"]

    if last_msg:
        return last_msg
    if any_json:
        # JSONL present but no message extracted — try whole-blob parse
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                for key in ("result", "text", "message"):
                    val = data.get(key)
                    if isinstance(val, str) and val.strip():
                        return val
        except json.JSONDecodeError:
            pass
        return ""
    return text


class CodexCliProvider(Provider):
    """Headless driver for the OpenAI Codex CLI (``codex exec --json``)."""

    name = "codex-cli"
    supports_vision = False
    offline_safe = False  # hits OpenAI cloud through the CLI

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

    def build_argv(self, prompt: str, max_tokens: int = 8000) -> list[str]:
        """Build the subprocess argv (prompt included; callers log a redacted copy)."""
        argv = [
            self.binary,
            "exec",
            prompt,
            "--json",
            # Minimize side effects: no writes, never ask for approval.
            "-s",
            "read-only",
            "-a",
            "never",
            "--skip-git-repo-check",
            "--ephemeral",
        ]
        if self.model:
            argv.extend(["-m", self.model])
        # codex exec has no max_tokens / budget flag; max_tokens is accepted on
        # complete() for Provider interface parity and ignored at the wire layer.
        _ = max_tokens
        return argv

    @staticmethod
    def argv_for_log(argv: list[str]) -> list[str]:
        """Return argv with the prompt body redacted for logs."""
        out: list[str] = []
        # codex exec <PROMPT> …  — prompt is the first non-flag arg after "exec"
        seen_exec = False
        redacted_prompt = False
        for tok in argv:
            if not seen_exec:
                out.append(tok)
                if tok == "exec":
                    seen_exec = True
                continue
            if not redacted_prompt and not tok.startswith("-"):
                out.append("<prompt>")
                redacted_prompt = True
                continue
            out.append(tok)
        return out

    def _run(self, argv: list[str]) -> str:
        binary = self._resolve_binary()
        run_argv = list(argv)
        run_argv[0] = binary
        log.info("codex-cli argv: %s", self.argv_for_log(run_argv))
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
                f"codex CLI timed out after {self.timeout:.0f}s"
            ) from exc
        # codex may write progress to stderr; treat non-zero as hard failure only
        # when stdout yielded no message.
        text = parse_cli_output(proc.stdout or "")
        if proc.returncode != 0 and not text:
            err = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(
                f"codex CLI exited {proc.returncode}: {err[:500] or '(no output)'}"
            )
        return text

    def complete(
        self,
        system: str,
        messages: list[dict],
        max_tokens: int = 8000,
    ) -> str:
        prompt = serialize_conversation(system, messages)
        argv = self.build_argv(prompt, max_tokens=max_tokens)
        return self._run(argv)

    def complete_ex(
        self,
        system: str,
        messages: list[dict],
        max_tokens: int = 8000,
    ) -> tuple[str, str]:
        return self.complete(system, messages, max_tokens=max_tokens), ""
