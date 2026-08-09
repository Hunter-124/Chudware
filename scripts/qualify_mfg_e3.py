#!/usr/bin/env python3
"""Qualify the manufacturing E3 lifecycle against a private real host daemon."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parents[1]
STEP_NAMES = [
    "discovery is allowlist-only",
    "commander claim is exclusive",
    "connect identifies and publishes FreshValue telemetry",
    "artifact preflight is seq-stamped and closed-vocabulary",
    "start without approval is refused by name",
    "human-only approval and one-shot nonce are enforced",
    "emulator completion and monotonic journal replay",
    "alarm is terminal and requires a newly authorized run",
    "capability scope and physical execute clamp",
]
TERMINAL_RUN_STATES = {"completed", "faulted", "aborted", "abandoned"}


class QualificationFailure(AssertionError):
    pass


class RpcRefusal(RuntimeError):
    def __init__(self, method: str, body: dict[str, Any]):
        self.method = method
        self.body = body
        super().__init__(f"{method} refused [{body.get('code', '<missing code>')}]: "
                         f"{body.get('message', '<missing message>')}")


class RpcClient:
    def __init__(self, path: Path, timeout_s: float = 10.0):
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._socket.settimeout(timeout_s)
        self._socket.connect(str(path))
        self._file = self._socket.makefile("rw", encoding="utf-8", newline="\n")
        self._next_id = 1

    def close(self) -> None:
        self._file.close()
        self._socket.close()

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._file.write(json.dumps({"id": request_id, "method": method,
                                     "params": params or {}}, separators=(",", ":")) + "\n")
        self._file.flush()
        while True:
            line = self._file.readline()
            if not line:
                raise QualificationFailure(f"daemon closed while waiting for {method}")
            message = json.loads(line)
            if "event" in message:
                continue
            require(message.get("id") == request_id,
                    f"{method}: response id {message.get('id')!r} != {request_id}")
            if "error" in message:
                body = message["error"]
                require(isinstance(body, dict), f"{method}: non-object error envelope")
                raise RpcRefusal(method, body)
            require("result" in message and isinstance(message["result"], dict),
                    f"{method}: missing object result envelope")
            return message["result"]

    def refuse(self, method: str, params: dict[str, Any], code: str | None = None,
               code_contains: str | None = None) -> dict[str, Any]:
        try:
            self.call(method, params)
        except RpcRefusal as refusal:
            actual = refusal.body.get("code")
            require(isinstance(actual, str) and actual,
                    f"{method}: refusal did not have a named code")
            if code is not None:
                require(actual == code, f"{method}: expected refusal {code!r}, got {actual!r}")
            if code_contains is not None:
                require(code_contains in actual,
                        f"{method}: refusal {actual!r} does not name {code_contains!r}")
            return refusal.body
        raise QualificationFailure(f"{method}: unexpectedly succeeded")


@dataclass
class RunBinding:
    run_id: str
    approval_id: str


@dataclass
class Context:
    client: RpcClient
    state_dir: Path
    emulator_id: str = ""
    physical_id: str = ""
    lease_id: str = ""
    artifact_id: str = ""
    primary: RunBinding | None = None
    recovery: RunBinding | None = None
    observations: dict[str, Any] = field(default_factory=dict)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise QualificationFailure(message)


def owner(session: str) -> dict[str, str]:
    return {"client_kind": "qualification", "seat": "private-e3-seat", "session": session}


def human() -> dict[str, str]:
    return {"kind": "human", "identity": "qualification-operator", "seat": "private-e3-seat"}


def agent() -> dict[str, str]:
    return {"kind": "agent", "identity": "qualification-agent", "seat": "private-e3-agent-seat",
            "agent_run_id": "agent-run:e3-qualification"}


def lease_params(ctx: Context) -> dict[str, str]:
    return {"device_id": ctx.emulator_id, "lease_id": ctx.lease_id}


def claim(ctx: Context, device_id: str, session: str) -> str:
    result = ctx.client.call("mfg_device_claim", {
        "device_id": device_id, "owner": owner(session), "ttl_ms": 120_000,
    })
    lease = result.get("lease")
    require(isinstance(lease, dict), "claim returned no lease")
    lease_id = lease.get("lease_id")
    require(isinstance(lease_id, str) and lease_id, "claim returned an unnamed lease")
    return lease_id


def assert_fresh_value(row: Any, expected_src: str) -> None:
    require(isinstance(row, dict), "telemetry snapshot is not an object")
    required = {"v", "state", "t_mono_ns", "seq", "age_ms", "ttl_ms", "src", "why"}
    require(required <= row.keys(), f"FreshValue is missing {sorted(required - row.keys())}")
    state = row["state"]
    require(state in {"ok", "stale", "unknown"}, f"invalid FreshValue state {state!r}")
    require(row["src"] == expected_src,
            f"FreshValue source is {row['src']!r}, expected {expected_src!r}")
    require((row["v"] is not None) == (state == "ok"),
            "FreshValue.v must be non-null exactly when state is ok")


def register_artifact(ctx: Context) -> str:
    result = ctx.client.call("mfg_artifact_register", {
        **lease_params(ctx),
        "artifact": {
            "kind": "nc",
            "lines": ["G90", "G0 X1", "G1 X2 F100", "G0 X0"],
            "post_profile_id": "e3-emulator",
        },
    })
    artifact = result.get("artifact")
    require(isinstance(artifact, dict), "artifact registration returned no artifact")
    artifact_id = artifact.get("artifact_id")
    require(isinstance(artifact_id, str) and artifact_id, "artifact has no id")
    require(isinstance(artifact.get("hash_sha256"), str) and len(artifact["hash_sha256"]) == 64,
            "artifact was not SHA-256 bound")
    return artifact_id


def preflight(ctx: Context, run_id: str | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {
        **lease_params(ctx), "artifact_id": ctx.artifact_id,
        "job": {"job_id": "job:e3-qualification", "setup_revision": 1, "quantity": 1},
        "snapshot": {
            "tool_table_hash": "e3-tools", "fixture_hash": "e3-fixture",
            "wcs": {"system": "G54", "offset": [0, 0, 0],
                    "established_by": "qualification-emulator-origin"},
        },
    }
    if run_id is not None:
        params["run_id"] = run_id
    return ctx.client.call("mfg_preflight_run", params)


def propose_and_create(ctx: Context, run_id: str, exercisable_by: str) -> tuple[dict[str, Any], str]:
    proposed = ctx.client.call("mfg_approval_propose", {
        **lease_params(ctx), "run_id": run_id, "exercisable_by": exercisable_by,
        "expiry_ttl_ms": 60_000,
        "attendance": {"mode": "physical_attended", "attested_by": "qualification-operator"},
    })
    proposal = proposed.get("proposal")
    require(isinstance(proposal, dict), "approval proposal is missing")
    proposal_id = proposal.get("proposal_id")
    require(isinstance(proposal_id, str) and proposal_id, "approval proposal has no id")
    created = ctx.client.call("mfg_approval_create", {
        **lease_params(ctx), "proposal_id": proposal_id,
    })
    approval = created.get("approval")
    require(isinstance(approval, dict), "approval binding is missing")
    approval_id = approval.get("approval_id")
    require(isinstance(approval_id, str) and approval_id, "approval binding has no id")
    return proposal, approval_id


def start(ctx: Context, binding: RunBinding, _initiator: dict[str, str]) -> dict[str, Any]:
    return ctx.client.call("mfg_run_start", {
        **lease_params(ctx), "run_id": binding.run_id,
        "approval_id": binding.approval_id,
    })


def wait_run(ctx: Context, run_id: str, wanted: set[str], timeout_s: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        result = ctx.client.call("mfg_run_describe", {"run_id": run_id})
        last = result.get("run", {})
        if last.get("state") in wanted:
            return last
        # Telemetry is an actual service tick, not a sleep-based state assertion.
        ctx.client.call("mfg_telemetry_snapshot", {"device_id": ctx.emulator_id})
        time.sleep(0.01)
    raise QualificationFailure(
        f"run {run_id} did not reach {sorted(wanted)}; last state={last.get('state')!r}")


def step1(ctx: Context) -> None:
    listed = ctx.client.call("mfg_device_list")
    devices = listed.get("devices")
    require(isinstance(devices, list), "device list has no devices array")
    emulator = next((row for row in devices
                     if row.get("machine_class") == "emulated"
                     and row.get("transport", {}).get("kind") == "emulator"), None)
    require(isinstance(emulator, dict), "built-in emulator candidate is absent")
    require(emulator.get("allowlisted") is True, "emulator candidate is not allowlisted")
    require(emulator.get("lease") == {}, "discovery unexpectedly created a lease")
    assert_fresh_value(emulator.get("state"), "declared")
    require(emulator["state"]["state"] == "unknown",
            "unopened emulator discovery did not remain unknown")
    ctx.emulator_id = emulator["device_id"]
    physical = next((row for row in devices
                    if row.get("transport", {}).get("kind") == "serial"
                    and row.get("transport", {}).get("address") ==
                    "/dev/serial/by-id/chudware-e3-qualification"), None)
    require(isinstance(physical, dict) and physical.get("allowlisted") is True,
            "seeded physical serial candidate is absent or not allowlisted")
    ctx.physical_id = physical["device_id"]


def step2(ctx: Context) -> None:
    first = claim(ctx, ctx.emulator_id, "owner-a")
    refused = ctx.client.refuse("mfg_device_claim", {
        "device_id": ctx.emulator_id, "owner": owner("owner-b"), "ttl_ms": 120_000,
    }, code="lease.takeover_refused")
    current_owner = refused.get("detail", {}).get("current_owner", {})
    require(str(current_owner.get("session", "")).startswith("hostd-client:")
            and str(current_owner.get("seat", "")).startswith("uid:")
            and current_owner.get("session") != "owner-b",
            "takeover refusal did not name the server-authenticated current owner")
    released = ctx.client.call("mfg_device_release", {
        "device_id": ctx.emulator_id, "lease_id": first,
    })
    require(released.get("released") is True, "first owner release was not acknowledged")
    ctx.lease_id = claim(ctx, ctx.emulator_id, "owner-b")


def step3(ctx: Context) -> None:
    connected = ctx.client.call("mfg_device_connect", lease_params(ctx))
    require(connected.get("connected") is True, "emulator did not connect")
    fingerprint = connected.get("fingerprint_sha256")
    require(isinstance(fingerprint, str) and len(fingerprint) == 64,
            "connect did not complete identity/fingerprint probing")
    for _ in range(3):
        telemetry = ctx.client.call("mfg_telemetry_snapshot", {"device_id": ctx.emulator_id})
        assert_fresh_value(telemetry.get("snapshot"), "simulated")
        require(telemetry["snapshot"]["state"] == "ok", "connected telemetry is not fresh")
        require(telemetry["snapshot"]["v"].get("device_id") == ctx.emulator_id,
                "telemetry identity does not match the claimed emulator")


def step4(ctx: Context) -> None:
    ctx.artifact_id = register_artifact(ctx)
    result = preflight(ctx)
    report = result.get("gate_report")
    require(isinstance(report, dict), "preflight returned no gate report")
    require(isinstance(report.get("seq"), int) and report["seq"] > 0,
            "gate report is not seq-stamped")
    require(isinstance(report.get("ref"), str) and report["ref"], "gate report has no durable ref")
    stage2 = report.get("stage2")
    require(isinstance(stage2, list) and stage2, "gate report has no Stage-2 rows")
    verdicts = {row.get("verdict") for row in stage2 if isinstance(row, dict)}
    require(len(verdicts) <= 3 and verdicts <= {"pass", "fail", "not_applicable"},
            f"Stage-2 emitted an open-vocabulary verdict: {sorted(verdicts)}")
    require(len(stage2) == len(report.get("required_set", [])),
            "Stage-2 does not cover exactly the required guard set")
    require(report.get("verdict") == "pass", "emulator preflight did not pass")
    ctx.observations["run_id"] = result.get("run_id")


def step5(ctx: Context) -> None:
    run_id = ctx.observations["run_id"]
    require(isinstance(run_id, str) and run_id, "preflight run id is missing")
    ctx.client.refuse("mfg_run_start", {
        **lease_params(ctx), "run_id": run_id, "approval_id": "",
    }, code="approval.revoked_or_missing")


def step6(ctx: Context) -> None:
    run_id = ctx.observations["run_id"]
    proposed = ctx.client.call("mfg_approval_propose", {
        **lease_params(ctx), "run_id": run_id, "exercisable_by": "human_only",
        "expiry_ttl_ms": 60_000,
        "attendance": {"mode": "physical_attended", "attested_by": "qualification-operator"},
    })
    proposal = proposed.get("proposal")
    require(isinstance(proposal, dict), "approval proposal is missing")
    described = ctx.client.call("mfg_run_describe", {"run_id": run_id})["run"]
    require(described.get("state") == "draft", "proposal alone changed the run out of draft")
    require("approval_id" not in proposal and "nonce" not in proposal,
            "draft proposal unexpectedly carries approval authority")
    created = ctx.client.call("mfg_approval_create", {
        **lease_params(ctx), "proposal_id": proposal["proposal_id"],
    })
    approval = created.get("approval")
    require(isinstance(approval, dict), "approval creation returned no binding")
    binding = RunBinding(run_id, approval["approval_id"])
    ctx.client.refuse("mfg_run_start", {
        **lease_params(ctx), "run_id": run_id, "approval_id": binding.approval_id,
        "initiator": agent(),
    }, code="mfg_initiator_identity_assertion_forbidden")
    started = start(ctx, binding, human())
    require(started.get("state") == "running", "human-authorized start did not enter running")
    require(isinstance(started.get("nonce_burned_at_seq"), int)
            and started["nonce_burned_at_seq"] > 0, "successful start did not burn the nonce")
    ctx.client.refuse("mfg_run_start", {
        **lease_params(ctx), "run_id": run_id, "approval_id": binding.approval_id,
    }, code_contains="approval.present_unexpired_unburned")
    inspected = ctx.client.call("mfg_approval_inspect", {"approval_id": binding.approval_id})
    require(inspected.get("approval", {}).get("status") == "burned",
            "approval inspection does not show the burned nonce")
    ctx.primary = binding


def step7(ctx: Context) -> None:
    require(ctx.primary is not None, "primary run was not started")
    terminal = wait_run(ctx, ctx.primary.run_id, TERMINAL_RUN_STATES)
    require(terminal.get("state") == "completed",
            f"ordinary emulator run ended as {terminal.get('state')!r}, not completed")
    replay = ctx.client.call("mfg_journal_query", {"from_seq": 1, "limit": 10_000})
    records = replay.get("records")
    require(isinstance(records, list) and records, "journal replay returned no records")
    seqs = [record.get("seq") for record in records]
    require(all(isinstance(seq, int) for seq in seqs), "journal contains an unsequenced row")
    require(all(a < b for a, b in zip(seqs, seqs[1:])),
            "journal replay seq is not strictly monotonic")
    require(replay.get("head_seq") == seqs[-1], "journal head does not match replay tail")
    kinds = {record.get("kind") for record in records}
    required_kinds = {
        "mfg.gateway.epoch", "mfg.device.lease", "mfg.device.state",
        "mfg.device.snapshot", "mfg.artifact.registered", "mfg.run.gate_report",
        "mfg.approval.proposed", "mfg.approval.issued", "mfg.approval.burned",
        "mfg.arbiter.attempt", "mfg.run.state",
    }
    require(required_kinds <= kinds,
            f"journal omitted lifecycle kinds {sorted(required_kinds - kinds)}")
    run_states = [record.get("body", {}).get("state") for record in records
                  if record.get("kind") == "mfg.run.state"
                  and record.get("subject") == ctx.primary.run_id]
    for expected in ("draft", "running", "completed"):
        require(expected in run_states, f"journal did not replay primary run state {expected!r}")


def step8(ctx: Context) -> None:
    fault_preflight = preflight(ctx)
    fault_run = fault_preflight.get("run_id")
    require(isinstance(fault_run, str) and fault_run != ctx.primary.run_id,
            "fault scenario did not author a new RunId")
    _, fault_approval = propose_and_create(ctx, fault_run, "human_only")
    fault_binding = RunBinding(fault_run, fault_approval)
    require(start(ctx, fault_binding, human()).get("state") == "running",
            "fault scenario did not start")
    injected = ctx.client.call("mfg_emulator_fault_inject", {
        **lease_params(ctx), "fault": "alarm",
    })
    require(injected.get("injected") is True, "alarm injection was not acknowledged")
    assert_fresh_value(injected.get("snapshot"), "simulated")
    require(injected["snapshot"]["v"].get("state") == "alarm",
            "injected alarm was not observed in the controller snapshot")
    terminal = wait_run(ctx, fault_run, TERMINAL_RUN_STATES)
    require(terminal.get("state") == "faulted",
            f"alarm run ended as {terminal.get('state')!r}, not faulted")
    refused = ctx.client.refuse("mfg_run_resume", {
        **lease_params(ctx), "run_id": fault_run,
    }, code="no_alarm_since_hold")
    require(refused.get("detail", {}).get("discontinuity") == "alarm",
            "resume refusal did not name the alarm discontinuity")
    ctx.client.refuse("mfg_device_connect", lease_params(ctx),
                      code="mfg_transport_terminal")


    # The alarm makes this claim terminal. Releasing and reclaiming creates a fresh
    # emulator transport, but carries no authorization forward.
    ctx.client.call("mfg_device_release", lease_params(ctx))
    ctx.lease_id = claim(ctx, ctx.emulator_id, "owner-after-alarm")
    ctx.client.call("mfg_device_connect", lease_params(ctx))
    recovery_preflight = preflight(ctx)
    recovery_run = recovery_preflight.get("run_id")
    require(isinstance(recovery_run, str) and recovery_run not in
            {ctx.primary.run_id, fault_run}, "recovery reused an old RunId")
    ctx.client.refuse("mfg_run_start", {
        **lease_params(ctx), "run_id": recovery_run, "approval_id": fault_approval,
    }, code="mfg_run_approval_mismatch")
    old_approval = ctx.client.call(
        "mfg_approval_inspect", {"approval_id": fault_approval})
    require(old_approval.get("approval", {}).get("status") == "burned",
            "fault run approval unexpectedly became reusable")
    _, recovery_approval = propose_and_create(ctx, recovery_run, "human_only")
    recovery = RunBinding(recovery_run, recovery_approval)
    require(start(ctx, recovery, human()).get("state") == "running",
            "newly authored and freshly approved recovery run did not start")
    require(wait_run(ctx, recovery_run, TERMINAL_RUN_STATES).get("state") == "completed",
            "freshly authorized recovery run did not complete")
    ctx.recovery = recovery


def step9(ctx: Context) -> None:
    emulated = ctx.client.call("mfg_device_capability", {"device_id": ctx.emulator_id})
    report = emulated.get("capability")
    require(isinstance(report, dict), "emulator capability report is missing")
    require(report.get("subject_id") == ctx.emulator_id
            and report.get("emulator_stage_passed") == "E3",
            "capability report does not make its E3 emulator subject scope explicit")
    require(report.get("max_advertised") == "execute",
            "emulated machine did not retain emulator-scoped execute")
    require(report.get("levels", {}).get("execute", {}).get("truth") == "verified",
            "emulator-scoped execute is not verified")
    require(report.get("levels", {}).get("safety_integration", {}).get("truth") == "unsupported",
            "emulator incorrectly advertises safety integration")

    ctx.client.call("mfg_device_release", lease_params(ctx))
    physical_lease = claim(ctx, ctx.physical_id, "physical-capability-read")
    physical = ctx.client.call("mfg_device_capability", {"device_id": ctx.physical_id})
    physical_report = physical.get("capability", {})
    execute = physical_report.get("levels", {}).get("execute", {})
    safety = physical_report.get("levels", {}).get("safety_integration", {})
    require(execute.get("truth") == "assumed" and execute.get("missing_level") == "execute",
            "unconnected physical serial candidate did not clamp execute to assumed/missing execute")
    require(safety.get("truth") == "unsupported",
            "physical candidate incorrectly advertises safety integration")
    require(physical_report.get("max_advertised") != "execute",
            "physical candidate advertises execute before a pilot qualification")
    ctx.client.call("mfg_device_release", {
        "device_id": ctx.physical_id, "lease_id": physical_lease,
    })


STEPS: list[Callable[[Context], None]] = [
    step1, step2, step3, step4, step5, step6, step7, step8, step9,
]


def find_hostd(requested: str | None) -> Path:
    candidates = ([Path(requested)] if requested else [
        REPO / "core/build-linux/bin/chudware-hostd",
        REPO / "core/build-occt-linux/bin/chudware-hostd",
    ])
    for candidate in candidates:
        path = candidate.expanduser().resolve()
        if path.is_file() and os.access(path, os.X_OK):
            return path
    raise QualificationFailure("no executable chudware-hostd found; run ./build.sh core")


def wait_for_socket(proc: subprocess.Popen[bytes], path: Path, timeout_s: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise QualificationFailure(f"private chudware-hostd exited early with {proc.returncode}")
        if path.exists():
            try:
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                probe.settimeout(0.2)
                probe.connect(str(path))
                probe.close()
                return
            except OSError:
                pass
        time.sleep(0.02)
    raise QualificationFailure(f"private chudware-hostd did not listen at {path}")


def stop_daemon(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        proc.wait()
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=5)


def print_table(results: list[tuple[str, str, str]]) -> None:
    print("\nManufacturing E3 qualification")
    print("#  RESULT  PROOF")
    for index, (name, status, detail) in enumerate(results, 1):
        suffix = f" — {detail}" if detail else ""
        print(f"{index:<2} {status:<6}  {name}{suffix}")


def run(hostd: Path) -> int:
    temp_path = Path(tempfile.mkdtemp(prefix="chudware-mfg-e3-"))
    socket_path = temp_path / "hostd.sock"
    state_dir = temp_path / "state"
    runtime_dir = temp_path / "runtime"
    appdata_dir = temp_path / "appdata"
    state_dir.mkdir()
    runtime_dir.mkdir(mode=0o700)
    appdata_dir.mkdir()
    # Seed one genuinely physical endpoint shape. It is discovered and capability-read,
    # but deliberately never opened; qualification remains hardware-free.
    (state_dir / "device_allowlist.tsv").write_text(
        "# Chudware manufacturing commissioning allowlist v1\n"
        "serial\taddress=/dev/serial/by-id/chudware-e3-qualification"
        "\tmachine_class=open_frame_mill\tbaud=115200"
        "\tnote=E3 physical capability clamp (never opened)\n",
        encoding="utf-8",
    )
    log_path = temp_path / "hostd.log"
    results: list[tuple[str, str, str]] = []
    proc: subprocess.Popen[bytes] | None = None
    client: RpcClient | None = None
    failure = ""
    cleanup_ok = False
    try:
        env = dict(os.environ)
        env.update({
            "CHUDWARE_INSTANCE_KEY": "mfg-e3-" + uuid.uuid4().hex,
            "CHUDWARE_HOSTD_SOCKET": str(socket_path),
            "CHUDWARE_MFG_STATE_DIR": str(state_dir),
            "CHUDWARE_APPDATA": str(appdata_dir),
            "XDG_RUNTIME_DIR": str(runtime_dir),
            "CHUDWARE_HOSTD_LINGER_SECS": "3600",
        })
        library = hostd.parent / "libchudware.so"
        if library.is_file():
            env["CHUDWARE_LIBCHUDWARE"] = str(library)
        with log_path.open("wb") as log:
            proc = subprocess.Popen([str(hostd), "--foreground"], cwd=temp_path,
                                    env=env, stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=subprocess.STDOUT,
                                    start_new_session=True)
            wait_for_socket(proc, socket_path)
            client = RpcClient(socket_path)
            hello = client.call("hello", {"client": "mfg-e3-qualification",
                                           "pid": os.getpid(), "proto": 1})
            require(hello.get("proto") == 1, "private daemon hello negotiation failed")
            ctx = Context(client=client, state_dir=state_dir)
            for index, action in enumerate(STEPS):
                try:
                    action(ctx)
                    results.append((STEP_NAMES[index], "PASS", ""))
                except Exception as error:  # emit the table before returning non-zero
                    results.append((STEP_NAMES[index], "FAIL", str(error)))
                    for blocked in STEP_NAMES[index + 1:]:
                        results.append((blocked, "FAIL", "not run after prior failure"))
                    failure = str(error)
                    break
    except Exception as error:
        failure = str(error)
        if not results:
            results = [(name, "FAIL", failure if i == 0 else "not run after prior failure")
                       for i, name in enumerate(STEP_NAMES)]
    finally:
        if client is not None:
            try:
                client.close()
            except Exception as error:
                failure = failure or f"client close failed: {error}"
        if proc is not None:
            try:
                stop_daemon(proc)
            except Exception as error:
                failure = failure or f"daemon teardown failed: {error}"
        log_tail = ""
        try:
            if log_path.exists():
                log_tail = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-20:])
        except OSError:
            pass
        shutil.rmtree(temp_path, ignore_errors=True)
        cleanup_ok = (proc is None or proc.poll() is not None) and not temp_path.exists()

    print_table(results)
    print(f"Cleanup: {'PASS' if cleanup_ok else 'FAIL'} (private daemon stopped; temp state removed)")
    if failure and log_tail:
        print("\nPrivate daemon log tail:", file=sys.stderr)
        print(log_tail, file=sys.stderr)
    if failure:
        print(f"\nQUALIFICATION FAILED: {failure}", file=sys.stderr)
    elif not cleanup_ok:
        print("\nQUALIFICATION FAILED: private daemon or temp state survived teardown", file=sys.stderr)
    else:
        print("\nQUALIFICATION PASSED: 9/9 steps")
    return 0 if not failure and cleanup_ok and len(results) == len(STEPS) \
        and all(status == "PASS" for _, status, _ in results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hostd", help="path to chudware-hostd (default: core build output)")
    args = parser.parse_args()
    try:
        hostd = find_hostd(args.hostd)
    except QualificationFailure as error:
        print(f"QUALIFICATION FAILED: {error}", file=sys.stderr)
        return 2
    started = time.monotonic()
    result = run(hostd)
    print(f"Elapsed: {time.monotonic() - started:.2f}s")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
