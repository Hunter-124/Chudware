#!/usr/bin/env python3
"""Headless mission driver: run ONE ``agent_admin_turn`` against the services worker.

This is the terminal-side equivalent of the GUI composer: it spawns the persistent
services worker (``python -m services.worker_main``), issues exactly one
``agent_admin_turn`` with the prompt, streams every worker event to a JSONL log while
printing a compact human progress line, and exits non-zero unless the turn came back
``ok`` with a terminal ``agent_end``.

Single-turn by construction. A follow-up turn is a NEW invocation that passes
``--resume <chudcode_session_file>`` (printed at the end of the previous run) plus
``--session-id`` so the conversation continues instead of starting over. There is no
``--steer`` flag: steering belongs to a live GUI/control client, and this driver owns
its worker for exactly one turn. Ctrl-C sends ``agent_cancel`` through the private
control socket (``worker_control_info``) and then shuts the worker down.

``--result-json <path>`` additionally writes the terminal record (driver verdict, the raw
``agent_admin_turn`` response with ok/reason/stop_reason/turn/usage/persistence, the
``run_finished`` event and timing) for machine consumers such as
``benchmarks/artifact_validity/runner.py``.

Example
-------
    scripts/run_mission.py \
        --workspace scratch/mission_dry \
        --sessions-dir scratch/mission_dry/.sessions \
        --prompt-file scratch/mission_dry/prompt.txt \
        --instance-key mission-dry \
        --hostd-bin core/build-occt-linux/bin/chudware-hostd \
        --log scratch/mission_dry/run.jsonl
"""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKER_PY = REPO_ROOT / ".venv-mcp" / "bin" / "python"
PREVIEW = 110


# ---------------------------------------------------------------- worker plumbing


class WorkerError(RuntimeError):
    pass


class Worker:
    """Newline-delimited JSON-RPC client for services/worker_main.py."""

    def __init__(self, proc: subprocess.Popen, log: Any, quiet: bool) -> None:
        self._proc = proc
        self._log = log
        self._quiet = quiet
        self._next_id = 0
        self._lock = threading.Lock()
        self._pending: dict[int, dict] = {}
        self._done: dict[int, threading.Event] = {}
        self._events: list[tuple[int, dict]] = []
        self._event_cb = None
        self._reader = threading.Thread(target=self._read_loop, name="worker-reader")
        self._reader.daemon = True
        self._reader.start()
        self._stderr = threading.Thread(
            target=self._stderr_loop, name="worker-stderr", daemon=True
        )
        self._stderr.start()
        self.stderr_tail: list[str] = []

    # -- lifecycle -------------------------------------------------------------

    def _read_loop(self) -> None:
        try:
            for line in self._proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    self._record({"kind": "_worker_stdout_noise", "line": line[:2000]})
                    continue
                if "event" in msg:
                    data = msg.get("data") or {}
                    self._record(data)
                    cb = self._event_cb
                    if cb is not None:
                        try:
                            cb(data)
                        except Exception as exc:  # noqa: BLE001 - never kill the reader
                            print(f"  [driver] event handler failed: {exc}", flush=True)
                    continue
                rid = msg.get("id")
                if isinstance(rid, int):
                    with self._lock:
                        self._pending[rid] = msg
                        event = self._done.get(rid)
                    if event is not None:
                        event.set()
        finally:
            with self._lock:
                for event in self._done.values():
                    event.set()

    def _stderr_loop(self) -> None:
        for line in self._proc.stderr:
            line = line.rstrip("\n")
            self.stderr_tail.append(line)
            del self.stderr_tail[:-200]
            if not self._quiet:
                print(f"  [worker] {line}", flush=True)

    def _record(self, data: dict) -> None:
        if self._log is None:
            return
        self._log.write(json.dumps(data, default=str) + "\n")
        self._log.flush()

    def on_event(self, callback) -> None:
        self._event_cb = callback

    # -- calls -----------------------------------------------------------------

    def call(self, method: str, params: dict, timeout: float | None = None) -> dict:
        with self._lock:
            self._next_id += 1
            rid = self._next_id
            done = threading.Event()
            self._done[rid] = done
        payload = json.dumps({"id": rid, "method": method, "params": params})
        try:
            self._proc.stdin.write(payload + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            raise WorkerError(f"worker stdin closed while calling {method}") from exc
        if not done.wait(timeout):
            raise WorkerError(f"{method} timed out after {timeout}s")
        with self._lock:
            msg = self._pending.pop(rid, None)
            self._done.pop(rid, None)
        if msg is None:
            raise WorkerError(f"worker exited before answering {method}")
        if not msg.get("ok"):
            raise WorkerError(f"{method} failed: {msg.get('error')}")
        return msg.get("result") or {}

    def shutdown(self, grace: float = 15.0) -> None:
        try:
            self._proc.stdin.write(json.dumps({"id": 0, "method": "shutdown"}) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, ValueError, AttributeError):
            pass
        try:
            self._proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()


def control_request(info: dict, action: str, params: dict, timeout: float = 10.0) -> dict:
    """One bounded request on the worker's private control lane."""
    transport = str(info.get("transport") or "")
    endpoint = str(info.get("endpoint") or "")
    if transport == "unix":
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        target: Any = endpoint
    else:
        host, _, port = endpoint.rpartition(":")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        target = (host, int(port))
    sock.settimeout(timeout)
    try:
        sock.connect(target)
        body = json.dumps(
            {"token": info.get("token"), "action": action, "params": params}
        )
        sock.sendall(body.encode("utf-8") + b"\n")
        chunks = bytearray()
        while b"\n" not in chunks and len(chunks) < 1 << 20:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.extend(chunk)
        text = bytes(chunks).split(b"\n", 1)[0].decode("utf-8", "replace")
        return json.loads(text) if text else {}
    finally:
        sock.close()


# ---------------------------------------------------------------- progress view


def _short(value: Any, limit: int = PREVIEW) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _session_provider_error(session_file: Any) -> str:
    """Last provider-side error recorded in a Chudcode session JSONL, if any.

    Chudcode records a failed provider call as an assistant message with
    ``stopReason: "error"`` and closes the run normally, so this is the only place
    a 400/auth failure is visible after the fact.
    """
    if not isinstance(session_file, str) or not session_file:
        return ""
    path = Path(session_file)
    if not path.is_file():
        return ""
    found = ""
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                message = entry.get("message")
                if not isinstance(message, dict):
                    continue
                if message.get("stopReason") == "error":
                    found = str(message.get("errorMessage") or "provider error")
    except OSError:
        return ""
    return _short(found, 600)



class Progress:
    """Compact one-line-per-event console view; assistant deltas coalesce."""

    def __init__(self) -> None:
        self._buf: list[str] = []
        self._channel = ""
        self._t0 = time.monotonic()
        self.terminal: dict | None = None
        self.saw_output = False
        self.failed = ""

    def _stamp(self) -> str:
        return f"{time.monotonic() - self._t0:7.1f}s"

    def _emit(self, label: str, text: str = "") -> None:
        print(f"{self._stamp()}  {label}{(' ' + text) if text else ''}", flush=True)

    def flush_text(self) -> None:
        if not self._buf:
            return
        text = "".join(self._buf).strip()
        channel = self._channel or "text"
        self._buf.clear()
        self._channel = ""
        if text:
            self._emit(f"{channel}:", _short(text, 400))

    def handle(self, data: dict) -> None:
        kind = str(data.get("kind") or "")
        if kind == "assistant_delta":
            channel = str(data.get("channel") or "text")
            if channel != self._channel and self._buf:
                self.flush_text()
            self._channel = channel
            text = str(data.get("text") or "")
            if text.strip():
                self.saw_output = True
            self._buf.append(text)
            return
        self.flush_text()
        if kind == "agent.tool_call":
            self.saw_output = True
            ok = "ok " if data.get("ok") else "FAIL"
            latency = data.get("latency_ms")
            suffix = f" {latency}ms" if isinstance(latency, int) else ""
            detail = data.get("args_preview") or data.get("target") or ""
            if not data.get("ok"):
                detail = data.get("error_preview") or detail
            self._emit(
                f"tool {ok} {data.get('tool_name') or '?'}{suffix}", _short(detail)
            )
        elif kind == "agent.provider_usage":
            self._emit(
                "usage",
                f"{data.get('provider') or '?'}/{data.get('model') or '?'} "
                f"in={data.get('prompt_tokens')} out={data.get('completion_tokens')} "
                f"ctx={data.get('context_tokens')}",
            )
        elif kind == "agent.context":
            self._emit("context", _short(data.get("context_tokens")))
        elif kind == "status":
            if data.get("phase") == "error":
                self.failed = _short(
                    data.get("error_detail") or data.get("text") or "agent error", 600
                )
            self._emit(f"· {data.get('phase') or 'status'}", _short(data.get("text")))
        elif kind == "run_started":
            self._emit("run started", _short(data.get("model") or ""))
        elif kind == "run_finished":
            self.terminal = dict(data)
            if str(data.get("status") or "") in {"failed", "error"}:
                self.failed = _short(
                    data.get("error_detail")
                    or data.get("stop_reason")
                    or "run finished with a failure status",
                    600,
                )
            self._emit(
                f"run finished [{data.get('status')}]",
                _short(data.get("stop_reason") or data.get("error_detail") or ""),
            )
        elif kind == "sub_run":
            self._emit(
                f"sub_run [{data.get('status') or '?'}]",
                _short(data.get("purpose") or data.get("label") or data.get("detail")),
            )
        elif kind == "agent.spawn":
            self._emit("spawn", _short(data.get("label") or data.get("spawn_kind")))
        elif kind == "plan_state":
            self._emit("plan", _short(data.get("items") or data))
        else:
            self._emit(kind or "event", _short({k: v for k, v in data.items() if k != "kind"}))


# ---------------------------------------------------------------- driver


def build_env(args: argparse.Namespace, workspace: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["CHUDWARE_ENGINE_DAEMON"] = "1"
    env["CHUDWARE_PROJECT_DIR"] = str(workspace)
    env["CHUDWARE_FS_ROOT"] = str(workspace)
    env["CHUDWARE_PROJECT_SAVE_DIR"] = str(workspace)
    env["CHUDWARE_DISPLAY_QUALITY"] = "fine"
    if args.instance_key:
        env["CHUDWARE_INSTANCE_KEY"] = args.instance_key
    if args.hostd_bin:
        env["CHUDWARE_HOSTD_BIN"] = str(Path(args.hostd_bin).expanduser().resolve())
    return env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_mission.py",
        description="Run one headless agent_admin_turn against the services worker.",
        epilog=(
            "Single-turn by design. For a follow-up, invoke again with "
            "--resume <chudcode_session_file> --session-id <same id> and a new prompt."
        ),
    )
    parser.add_argument("--workspace", required=True, help="project/workspace directory")
    parser.add_argument("--sessions-dir", required=True, help="Chudcode session dir")
    parser.add_argument("--prompt-file", required=True, help="file holding the prompt")
    parser.add_argument("--role", default="general_agent")
    # A headless mission has nobody to check with: the interactive `work_style` preference
    # (default check_with_me → plan mode) must not govern it. Three headless mission turns
    # produced nothing but blocked_by_mode refusals when it did (2026-09-07).
    parser.add_argument("--mode", default="agent", choices=("agent", "plan", "ask"),
                        help="copilot mode for this turn (headless default: agent)")
    parser.add_argument("--resume", default="", help="chudcode_session_file to resume")
    parser.add_argument("--session-id", default="", help="Chudware conversation id")
    parser.add_argument("--provider", default="", help="override the configured provider")
    parser.add_argument("--model", default="", help="override the configured model")
    parser.add_argument("--reasoning-effort", default="",
                        help="pin the thinking effort (low|medium|high|xhigh|max), as the GUI "
                             "composer does; default: the turn policy's effort")
    parser.add_argument("--log", default="", help="JSONL event log (default: <sessions-dir>/<session>.events.jsonl)")
    parser.add_argument("--instance-key", default="")
    parser.add_argument("--hostd-bin", default="")
    parser.add_argument(
        "--timeout-s", type=float, default=3600.0,
        help="turn budget in seconds; the run is aborted when it expires. 0 = no budget "
             "(the turn ends at its own finish, Ctrl-C, or worker death)",
    )
    parser.add_argument("--quiet-worker", action="store_true", help="hide worker stderr")
    parser.add_argument(
        "--result-json", default="",
        help="also write the machine-readable terminal record (verdict, the raw "
             "agent_admin_turn response with ok/reason/stop_reason/turn/usage/"
             "persistence, run_finished event, timing) to this path",
    )
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout_s) or args.timeout_s < 0:
        parser.error("--timeout-s must be a finite number >= 0 (0 = no budget)")
    budget_s = args.timeout_s or None

    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.is_dir():
        print(f"error: workspace is not a directory: {workspace}", file=sys.stderr)
        return 2
    sessions_dir = Path(args.sessions_dir).expanduser().resolve()
    sessions_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = Path(args.prompt_file).expanduser().resolve()
    if not prompt_path.is_file():
        print(f"error: prompt file not found: {prompt_path}", file=sys.stderr)
        return 2
    prompt = prompt_path.read_text(encoding="utf-8").strip()
    if not prompt:
        print(f"error: prompt file is empty: {prompt_path}", file=sys.stderr)
        return 2
    if not WORKER_PY.exists():
        print(f"error: runtime venv missing: {WORKER_PY}", file=sys.stderr)
        return 2
    session_id = args.session_id or f"mission-{time.time_ns():x}"
    run_id = f"mission-run-{time.time_ns():x}"
    log_path = Path(args.log).expanduser() if args.log else sessions_dir / f"{session_id}.events.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"workspace   {workspace}")
    print(f"sessions    {sessions_dir}")
    print(f"session_id  {session_id}")
    print(f"run_id      {run_id}")
    print(f"log         {log_path}")
    print(f"prompt      {_short(prompt, 200)}")
    print("-" * 72, flush=True)

    started_at = time.time()
    proc = subprocess.Popen(
        [str(WORKER_PY), "-m", "services.worker_main"],
        cwd=str(REPO_ROOT),
        env=build_env(args, workspace),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        # Ctrl-C hits the whole foreground process group. The worker must NOT die
        # with it: cancel travels over the control socket, and killing the worker
        # first tears that socket down (connection reset) and abandons the run.
        start_new_session=True,
    )

    exit_code = 1
    with log_path.open("w", encoding="utf-8") as log_file:
        worker = Worker(proc, log_file, args.quiet_worker)
        progress = Progress()
        worker.on_event(progress.handle)

        control_info: dict = {}
        try:
            control_info = worker.call("worker_control_info", {}, timeout=60)
        except WorkerError as exc:
            print(f"warning: control lane unavailable ({exc}); Ctrl-C will only stop the worker")

        cancelled = threading.Event()

        def on_sigint(_sig, _frame) -> None:
            if cancelled.is_set():
                raise KeyboardInterrupt
            cancelled.set()
            print("\n[driver] SIGINT — cancelling the run…", flush=True)
            if control_info:
                try:
                    reply = control_request(control_info, "cancel", {"run_id": run_id})
                    print(f"[driver] cancel -> {_short(reply)}", flush=True)
                except Exception as exc:  # noqa: BLE001
                    print(f"[driver] cancel failed: {exc}", flush=True)

        previous = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, on_sigint)

        # `timeout` is the host's turn budget (chudcode_host aborts the run when it
        # expires and reports stop_reason "turn_budget"). The RPC wait below is the same
        # number plus slack, so the host always reports first. With --timeout-s 0 neither
        # is sent: the turn has no wall-clock stop and Ctrl-C (cancel over the control
        # lane) or worker death ends the wait.
        params: dict[str, Any] = {
            "request": prompt,
            "role": args.role,
            "mode": args.mode,
            "run_id": run_id,
            "session_id": session_id,
            "workspace_dir": str(workspace),
            "sessions_dir": str(sessions_dir),
        }
        if budget_s is not None:
            params["timeout"] = budget_s
        if args.provider:
            params["provider"] = args.provider
        if args.model:
            params["model"] = args.model
        if args.reasoning_effort:
            params["reasoning_effort"] = args.reasoning_effort
        if args.resume:
            params["chudcode_session_file"] = str(
                Path(args.resume).expanduser().resolve()
            )

        result: dict = {}
        failure = ""
        try:
            result = worker.call(
                "agent_admin_turn", params,
                timeout=None if budget_s is None else budget_s + 60.0,
            )
        except WorkerError as exc:
            failure = str(exc)
        except KeyboardInterrupt:
            failure = "interrupted"
        finally:
            signal.signal(signal.SIGINT, previous)
            progress.flush_text()

        print("-" * 72, flush=True)
        turn = result.get("turn") if isinstance(result.get("turn"), dict) else {}
        terminal = bool(turn.get("terminal"))
        verdict, verdict_detail = "ok", ""
        provider_detail = ""
        if failure:
            verdict, verdict_detail = "driver_failure", failure
            print(f"FAILED: {failure}")
            tail = "\n".join(worker.stderr_tail[-15:])
            if tail:
                print("worker stderr tail:\n" + tail)
        elif not result.get("ok"):
            verdict = "not_ok"
            verdict_detail = str(result.get("reason") or "turn returned ok=false")
            print(f"FAILED: {verdict_detail}")
        elif not terminal:
            verdict, verdict_detail = "not_terminal", "turn ended without a terminal agent_end"
            print("FAILED: turn ended without a terminal agent_end")
        elif turn.get("cancelled"):
            verdict, verdict_detail = "cancelled", str(turn.get("stop_reason") or "")
            print(f"CANCELLED: stop_reason={turn.get('stop_reason')}")
        elif progress.failed:
            verdict, verdict_detail = "run_failed", progress.failed
            print(f"FAILED: {progress.failed}")
        elif not (progress.saw_output or str(turn.get("result") or "").strip()):
            # A provider 400/auth failure can still close the run cleanly with an
            # empty assistant message; a mission that produced nothing is not a pass.
            provider_detail = _session_provider_error(turn.get("chudcode_session_file"))
            verdict, verdict_detail = "no_output", "the turn produced no assistant output"
            print("FAILED: the turn produced no assistant output")
            if provider_detail:
                print(f"provider error: {provider_detail}")
        else:
            exit_code = 0
            print("OK")

        if turn:
            text = str(turn.get("result") or "")
            if text:
                print(f"\nfinal text:\n{text}\n")
            print(f"stop_reason           {turn.get('stop_reason')}")
            print(f"chudcode_session_id   {turn.get('chudcode_session_id')}")
            print(f"chudcode_session_file {turn.get('chudcode_session_file')}")
            if turn.get("chudcode_session_file"):
                print(
                    "\nfollow-up:\n"
                    f"  {sys.argv[0]} --workspace {workspace} "
                    f"--sessions-dir {sessions_dir} --session-id {session_id} "
                    f"--resume {turn.get('chudcode_session_file')} --prompt-file <next.txt>"
                )
        print(f"\nevent log: {log_path}")
        worker.shutdown()
        if args.result_json:
            record = {
                "schema": "chudware.run_mission.result/1",
                "exit_code": exit_code,
                "verdict": verdict,
                "verdict_detail": verdict_detail,
                "provider_error": provider_detail,
                "run_id": run_id,
                "session_id": session_id,
                "request": {k: v for k, v in params.items() if k != "request"},
                "response": result,
                "run_finished": progress.terminal,
                "saw_output": progress.saw_output,
                "started_at": started_at,
                "ended_at": time.time(),
                "worker_returncode": proc.poll(),
                "worker_stderr_tail": worker.stderr_tail[-40:],
                "event_log": str(log_path),
            }
            out = Path(args.result_json).expanduser()
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(record, indent=1, default=str), encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
