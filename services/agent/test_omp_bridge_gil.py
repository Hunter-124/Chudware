"""A CAD tool that pins its worker's GIL must not fail because its own poll goes unanswered.

CAD tool bodies and the bridge's ControlServer share one worker process. A native call that
holds the GIL (a numpy sort, one JSON decode of a large engine reply) leaves the server
unable to accept, so the extension's poll for that very tool gets no answer. These tests run
the real ``omp_extension.ts`` under Bun, with its ``@oh-my-pi`` imports stubbed, against a real
``ExtensionBridge`` in this process whose tool really holds the GIL (``ctypes.PyDLL``
does not release it). They need ``bun`` and a Unix libc, and skip otherwise.
"""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from services.agent.omp_bridge import ExtensionBridge

pytestmark = [
    pytest.mark.skipif(os.name == "nt", reason="PyDLL(None).sleep is a POSIX libc call"),
    pytest.mark.skipif(shutil.which("bun") is None, reason="bun is required to run the extension"),
]

_EXTENSION = Path(__file__).with_name("omp_extension.ts")

# Loads the real extension with the OMP packages it imports stubbed (they are only used by
# settings/auth controls, never by a CAD tool call), executes one registered tool through
# its own bridge client, and prints {ok, ms, ...}.
_RUNNER = r'''
import { plugin } from "bun";

const none = () => undefined;
const stubs: Record<string, Record<string, unknown>> = {
  "@oh-my-pi/pi-utils": { getBlobsDir: () => "/tmp" },
  "@oh-my-pi/pi-coding-agent/config/settings-ui": { createSettingsHost: none },
  "@oh-my-pi/pi-tui/overlays/settings-defs": { getSettingDef: none, getSettingsForTab: none, SETTING_TABS: [] },
  "@oh-my-pi/pi-coding-agent/config/registry": { lookup: none },
  "@oh-my-pi/pi-coding-agent/config/settings": { findScopedSettings: none },
  "@oh-my-pi/pi-ai/oauth": { getOAuthCredentialProvider: none },
  "@oh-my-pi/pi-catalog/compat/auth": { authProviders: [] },
};
plugin({
  name: "omp-stubs",
  setup(build) {
    for (const [name, exports] of Object.entries(stubs)) build.module(name, () => ({ exports, loader: "object" }));
  },
});

type Tool = { name: string; execute(id: string, args: object, signal: AbortSignal, onUpdate: undefined, ctx: object): Promise<unknown> };
// The extension path is runtime-selected and must load after the stubs are installed.
const { default: cadExtension } = await import(process.env.EXT_PATH!);
const tools = new Map<string, Tool>();
await cadExtension({ on: none, registerTool: (definition: Tool) => tools.set(definition.name, definition) });
const context = { agent: { id: "root", parentId: null }, sessionManager: { getSessionId: () => "session" } };
const controller = new AbortController();
const abortAfter = Number(process.env.ABORT_AFTER_MS || 0);
if (abortAfter) setTimeout(() => controller.abort(), abortAfter);
const started = Date.now();
try {
  const result = await tools.get(process.env.TOOL_NAME!)!.execute("call", {}, controller.signal, undefined, context);
  console.log(JSON.stringify({ ok: true, ms: Date.now() - started, result }));
} catch (error) {
  console.log(JSON.stringify({ ok: false, ms: Date.now() - started, error: String(error) }));
}
// OMP outlives the call; let fire-and-forget bridge requests (a cancel) go out before exiting.
await Bun.sleep(500);
process.exit(0);
'''

_DONE = {"content": [{"type": "text", "text": "done"}], "details": {"ok": True}}


def _bridge(tmp_path: Path, hold_gil: int = 0, sleep: float = 0.0,
            entered: threading.Event | None = None,
            cancelled: threading.Event | None = None) -> ExtensionBridge:
    pinned = ctypes.PyDLL(None)  # PyDLL keeps the GIL across the call, like a native numpy/json call
    polled = threading.Event()

    def dispatch(name, _args, context):
        if entered is not None:
            entered.set()
        if name == "pinned_tool":
            # Pin the GIL while the extension sleeps between two polls, so no poll is
            # half-served when the stall starts (that one race would answer "timeout").
            polled.wait(10)
            time.sleep(0.05)
            pinned.sleep(hold_gil)
        elif context.cancel_event.wait(sleep) and cancelled is not None:
            cancelled.set()
        return _DONE

    bridge = ExtensionBridge(tmp_path, dispatch).start()
    bridge.publish_tools([{"name": name, "description": name, "input_schema": {"type": "object"},
                           "load_mode": "essential"} for name in ("pinned_tool", "slow_tool")])
    served = bridge._server._handler

    def handler(action, data):
        result = served(action, data)
        if action == "tool_poll":
            polled.set()
        return result

    bridge._server._handler = handler
    return bridge


def _execute(tmp_path: Path, bridge: ExtensionBridge, tool: str, **extra: str) -> dict:
    runner = tmp_path / "runner.ts"
    runner.write_text(_RUNNER)
    info = bridge.info()
    env = {**os.environ, "OMP_CAD_BRIDGE_ENDPOINT": info["endpoint"],
           "OMP_CAD_BRIDGE_TOKEN": info["token"], "OMP_CAD_BRIDGE_TRANSPORT": info["transport"],
           "EXT_PATH": str(_EXTENSION), "TOOL_NAME": tool, **extra}
    done = subprocess.run([shutil.which("bun"), "run", str(runner)], env=env, cwd=tmp_path,
                          capture_output=True, text=True, timeout=120, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_tool_survives_a_bridge_that_cannot_answer_its_poll_for_longer_than_five_seconds(tmp_path):
    bridge = _bridge(tmp_path, hold_gil=6)
    try:
        outcome = _execute(tmp_path, bridge, "pinned_tool")
    finally:
        bridge.stop()
    assert outcome["ok"], outcome
    assert outcome["result"] == _DONE
    assert outcome["ms"] >= 5500, "the tool must really have outlasted the 5 s request timeout"


def test_stop_does_not_wait_for_a_stalled_poll(tmp_path):
    bridge = _bridge(tmp_path, hold_gil=4)
    try:
        outcome = _execute(tmp_path, bridge, "pinned_tool", ABORT_AFTER_MS="1000")
    finally:
        bridge.stop()
    assert not outcome["ok"] and "cancelled" in outcome["error"], outcome
    assert outcome["ms"] < 2500, outcome


def test_a_dead_bridge_is_still_reported_promptly(tmp_path):
    entered = threading.Event()
    bridge = _bridge(tmp_path, sleep=10, entered=entered)

    def die_mid_call():
        entered.wait(30)
        time.sleep(1)
        bridge.stop()

    threading.Thread(target=die_mid_call, daemon=True).start()
    try:
        outcome = _execute(tmp_path, bridge, "slow_tool")
    finally:
        bridge.stop()
    assert not outcome["ok"] and "bridge unavailable" in outcome["error"], outcome
    assert outcome["ms"] < 5000, outcome


def test_a_failed_poll_cancels_the_call_it_abandons(tmp_path):
    cancelled = threading.Event()
    bridge = _bridge(tmp_path, sleep=20, cancelled=cancelled)
    served = bridge._server._handler
    polls = []

    def failing_poll(action, data):
        if action == "tool_poll":
            polls.append(action)
            if len(polls) > 1:
                raise RuntimeError("poll failed")
        return served(action, data)

    bridge._server._handler = failing_poll
    try:
        outcome = _execute(tmp_path, bridge, "slow_tool")
        assert not outcome["ok"] and "bridge rejected request" in outcome["error"], outcome
        assert cancelled.wait(5), "the running call was left unowned after its poll failed"
    finally:
        bridge.stop()
