#!/usr/bin/env python3
"""Qualify the additive manufacturing lifecycle against a private real host daemon."""
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
sys.path.insert(0, str(REPO))

from fixtures.mfg.printers import VirtualPrinter

STEP_NAMES = [
    "printer discovery is allowlisted and unopened",
    "connect denial and virtual execute scope are named",
    "printer telemetry is FreshValue and failed responses become unknown",
    "print artifact preflight is seq-stamped",
    "human-only approval, nonce, and post-arbiter start are enforced",
    "hold pauses and abort cancels the printer",
    "printer failure is terminal and resume names the discontinuity",
    "journal replay is monotonic and failure raises attention",
]


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
    printer: VirtualPrinter
    state_dir: Path
    device_id: str = ""
    lease_id: str = ""
    artifact_id: str = ""
    primary: RunBinding | None = None
    failure_run: RunBinding | None = None
    observations: dict[str, Any] = field(default_factory=dict)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise QualificationFailure(message)


def owner(session: str) -> dict[str, str]:
    return {"client_kind": "qualification", "seat": "private-printer-seat", "session": session}


def human() -> dict[str, str]:
    return {"kind": "human", "identity": "qualification-operator",
            "seat": "private-printer-seat"}


def agent() -> dict[str, str]:
    return {"kind": "agent", "identity": "qualification-agent",
            "seat": "private-printer-agent-seat",
            "agent_run_id": "agent-run:printer-qualification"}


def lease_params(ctx: Context) -> dict[str, str]:
    return {"device_id": ctx.device_id, "lease_id": ctx.lease_id}


def claim(ctx: Context, device_id: str, session: str) -> str:
    result = ctx.client.call("mfg_device_claim", {
        "device_id": device_id, "owner": owner(session), "ttl_ms": 120_000,
    })
    lease = result.get("lease")
    require(isinstance(lease, dict), "claim returned no lease")
    lease_id = lease.get("lease_id")
    require(isinstance(lease_id, str) and lease_id, "claim returned an unnamed lease")
    return lease_id


def capability_level(report: dict[str, Any], name: str) -> dict[str, Any]:
    levels = report.get("levels")
    if isinstance(levels, dict):
        row = levels.get(name)
    elif isinstance(levels, list):
        row = next((item for item in levels
                    if isinstance(item, dict) and item.get("level") == name), None)
    else:
        row = None
    require(isinstance(row, dict), f"capability report has no {name!r} level")
    return row


def assert_fresh_value(row: Any, expected_src: str, *, lane_seq: bool = False) -> None:
    require(isinstance(row, dict), "telemetry row is not an object")
    required = {"v", "state", "t_mono_ns", "seq", "age_ms", "ttl_ms", "src", "why"}
    require(required <= row.keys(), f"FreshValue is missing {sorted(required - row.keys())}")
    state = row["state"]
    require(state in {"ok", "stale", "unknown"}, f"invalid FreshValue state {state!r}")
    require(row["src"] == expected_src,
            f"FreshValue source is {row['src']!r}, expected {expected_src!r}")
    require((row["v"] is not None) == (state == "ok"),
            "FreshValue.v must be non-null exactly when state is ok")
    if lane_seq:
        require(isinstance(row["seq"], int) and row["seq"] > 0,
                "FreshValue row was not stamped with a lane-owned seq")
    if state != "ok":
        require(isinstance(row["why"], str) and row["why"],
                "non-ok FreshValue did not explain why")


def telemetry_rows(ctx: Context) -> list[dict[str, Any]]:
    result = ctx.client.call("mfg_telemetry_snapshot", {"device_id": ctx.device_id})
    snapshot = result.get("snapshot")
    assert_fresh_value(snapshot, "simulated", lane_seq=True)
    require(snapshot["state"] == "ok", "connected printer snapshot is not fresh")
    value = snapshot.get("v")
    require(isinstance(value, dict) and value.get("device_id") == ctx.device_id,
            "printer snapshot is not bound to the claimed device")
    rows = value.get("rows")
    require(isinstance(rows, list) and rows, "printer snapshot has no telemetry rows")
    for row in rows:
        assert_fresh_value(row, "simulated", lane_seq=True)
    return rows


def rows_by_field(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result = {row.get("field"): row for row in rows if isinstance(row.get("field"), str)}
    require(len(result) == len(rows), "printer telemetry has unnamed or duplicate rows")
    return result


def register_artifact(ctx: Context) -> str:
    result = ctx.client.call("mfg_artifact_register", {
        **lease_params(ctx),
        "artifact": {
            "kind": "print_job",
            "lines": ["G28", "M104 S205", "G1 X10 Y10 F1200"],
            "slicer_profile_hash": "slicer:printer-qualification",
        },
    })
    artifact = result.get("artifact")
    require(isinstance(artifact, dict), "artifact registration returned no artifact")
    artifact_id = artifact.get("artifact_id")
    require(isinstance(artifact_id, str) and artifact_id, "artifact has no id")
    require(isinstance(artifact.get("hash_sha256"), str) and len(artifact["hash_sha256"]) == 64,
            "print artifact was not SHA-256 bound")
    return artifact_id


def preflight(ctx: Context) -> dict[str, Any]:
    return ctx.client.call("mfg_preflight_run", {
        **lease_params(ctx), "artifact_id": ctx.artifact_id,
        "job": {"job_id": "job:printer-qualification", "setup_revision": 1, "quantity": 1},
    })


def propose_and_create(ctx: Context, run_id: str) -> RunBinding:
    proposed = ctx.client.call("mfg_approval_propose", {
        **lease_params(ctx), "run_id": run_id, "exercisable_by": "human_only",
        "expiry_ttl_ms": 60_000,
        "attendance": {"mode": "physical_attended", "attested_by": "qualification-operator"},
    })
    proposal = proposed.get("proposal")
    require(isinstance(proposal, dict), "approval proposal is missing")
    proposal_id = proposal.get("proposal_id")
    require(isinstance(proposal_id, str) and proposal_id, "approval proposal has no id")
    require("approval_id" not in proposal and "nonce" not in proposal,
            "draft proposal unexpectedly carries approval authority")
    created = ctx.client.call("mfg_approval_create", {
        **lease_params(ctx), "proposal_id": proposal_id,
    })
    approval = created.get("approval")
    require(isinstance(approval, dict), "approval binding is missing")
    approval_id = approval.get("approval_id")
    require(isinstance(approval_id, str) and approval_id, "approval binding has no id")
    return RunBinding(run_id, approval_id)


def start(ctx: Context, binding: RunBinding, _initiator: dict[str, str]) -> dict[str, Any]:
    return ctx.client.call("mfg_run_start", {
        **lease_params(ctx), "run_id": binding.run_id,
        "approval_id": binding.approval_id,
    })


def request_count(ctx: Context, method: str, path: str) -> int:
    return sum(row.get("method") == method and row.get("path") == path
               for row in ctx.printer.requests)


def step1(ctx: Context) -> None:
    listed = ctx.client.call("mfg_device_list")
    devices = listed.get("devices")
    require(isinstance(devices, list), "device list has no devices array")
    printer = next((row for row in devices
                    if row.get("machine_class") == "fdm_printer_enclosed"
                    and row.get("transport", {}).get("kind") == "tcp"), None)
    require(isinstance(printer, dict), "commissioned printer candidate is absent")
    require(printer.get("allowlisted") is True, "printer candidate is not allowlisted")
    require(printer.get("lease") == {}, "discovery unexpectedly created a printer lease")
    require(printer.get("transport", {}).get("endpoint_is_physical") is False,
            "virtual qualification endpoint was represented as physical")
    assert_fresh_value(printer.get("state"), "declared")
    require(printer["state"]["state"] == "unknown",
            "unopened printer discovery did not remain unknown")
    require(printer.get("transport", {}).get("address") ==
            ctx.printer.base_url.removeprefix("http://"),
            "discovered printer address does not name the virtual printer")
    require(not ctx.printer.requests, "device discovery contacted the virtual printer")
    ctx.device_id = printer["device_id"]


def step2(ctx: Context) -> None:
    ctx.client.refuse("mfg_device_claim", {
        "device_id": "dev:fdm_printer_enclosed:000000000000",
        "owner": owner("not-commissioned"), "ttl_ms": 120_000,
    }, code="mfg_device_not_allowlisted")
    require(not ctx.printer.requests, "denied printer claim probed the network")
    ctx.lease_id = claim(ctx, ctx.device_id, "printer-owner")
    connected = ctx.client.call("mfg_device_connect", lease_params(ctx))
    require(connected.get("connected") is True, "allowlisted printer did not connect")
    report = connected.get("capability")
    require(isinstance(report, dict), "printer connect returned no capability report")
    execute = capability_level(report, "execute")
    safety = capability_level(report, "safety_integration")
    require(execute.get("truth") == "verified",
            "virtual printer execute was not verified in simulation scope")
    require(safety.get("truth") == "unsupported",
            "printer incorrectly advertises safety integration")
    require(report.get("max_advertised") == "execute",
            "virtual qualification printer did not advertise simulated execute")
    require(request_count(ctx, "GET", "/printer/info") >= 1 and
            request_count(ctx, "GET", "/printer/objects/query") >= 1,
            "connect did not probe the real virtual Moonraker endpoints")


def step3(ctx: Context) -> None:
    before = rows_by_field(telemetry_rows(ctx))
    require(all(row.get("state") == "ok" for row in before.values()),
            "initial printer telemetry contains a non-fresh row")
    query_path = "/printer/objects/query"
    ctx.printer.html_error_paths.add(query_path)
    try:
        after = rows_by_field(telemetry_rows(ctx))
    finally:
        ctx.printer.html_error_paths.discard(query_path)
    require(after.keys() == before.keys(),
            "unreachable printer response dropped telemetry fields")
    for field, row in after.items():
        require(row.get("state") == "unknown" and row.get("v") is None,
                f"unreachable {field} telemetry repainted a last-good value")
        require(isinstance(row.get("why"), str) and row["why"],
                f"unreachable {field} telemetry did not explain why")
    recovered = rows_by_field(telemetry_rows(ctx))
    require(recovered.keys() == before.keys()
            and all(row.get("state") == "ok" for row in recovered.values()),
            "printer telemetry did not recover after responses resumed")


def step4(ctx: Context) -> None:
    ctx.artifact_id = register_artifact(ctx)
    result = preflight(ctx)
    report = result.get("gate_report")
    require(isinstance(report, dict), "preflight returned no gate report")
    require(isinstance(report.get("seq"), int) and report["seq"] > 0,
            "gate report is not seq-stamped")
    require(isinstance(report.get("ref"), str) and report["ref"],
            "gate report has no durable ref")
    stage2 = report.get("stage2")
    require(isinstance(stage2, list) and stage2, "gate report has no Stage-2 rows")
    verdicts = {row.get("verdict") for row in stage2 if isinstance(row, dict)}
    require(verdicts <= {"pass", "fail", "not_applicable"},
            f"Stage-2 emitted an open-vocabulary verdict: {sorted(verdicts)}")
    require(len(stage2) == len(report.get("required_set", [])),
            "Stage-2 does not cover exactly the required guard set")
    require(report.get("verdict") == "pass", "virtual printer preflight did not pass")
    run_id = result.get("run_id")
    require(isinstance(run_id, str) and run_id, "preflight run id is missing")
    ctx.observations["primary_run_id"] = run_id


def step5(ctx: Context) -> None:
    run_id = ctx.observations["primary_run_id"]
    ctx.client.refuse("mfg_run_start", {
        **lease_params(ctx), "run_id": run_id, "approval_id": "",
    }, code="approval.revoked_or_missing")
    binding = propose_and_create(ctx, run_id)
    starts_before = request_count(ctx, "POST", "/printer/print/start")
    ctx.client.refuse("mfg_run_start", {
        **lease_params(ctx), "run_id": run_id, "approval_id": binding.approval_id,
        "initiator": agent(),
    }, code="mfg_initiator_identity_assertion_forbidden")
    require(request_count(ctx, "POST", "/printer/print/start") == starts_before,
            "agent-refused start reached the printer adapter")
    started = start(ctx, binding, human())
    require(started.get("state") == "running", "human-authorized start did not enter running")
    require(isinstance(started.get("nonce_burned_at_seq"), int)
            and started["nonce_burned_at_seq"] > 0,
            "successful start did not burn the nonce")
    require(request_count(ctx, "POST", "/printer/print/start") == starts_before + 1,
            "post-arbiter printer start route was not observed")
    # MoonrakerAdapter refuses before issuing this route unless it received a non-empty token.
    # No approval or lease authority is sent to the vendor endpoint.
    ctx.client.refuse("mfg_run_start", {
        **lease_params(ctx), "run_id": run_id, "approval_id": binding.approval_id,
    }, code_contains="approval.present_unexpired_unburned")
    inspected = ctx.client.call("mfg_approval_inspect", {"approval_id": binding.approval_id})
    require(inspected.get("approval", {}).get("status") == "burned",
            "approval inspection does not show the burned nonce")
    ctx.primary = binding


def step6(ctx: Context) -> None:
    require(ctx.primary is not None, "primary printer run was not started")
    pauses = request_count(ctx, "POST", "/printer/print/pause")
    held = ctx.client.call("mfg_run_hold", {
        **lease_params(ctx), "run_id": ctx.primary.run_id, "kind": "feed_hold",
    })
    require(held.get("state") == "held", "printer hold did not enter held")
    require(request_count(ctx, "POST", "/printer/print/pause") == pauses + 1,
            "printer hold did not route to Moonraker pause")
    described = ctx.client.call("mfg_run_describe", {"run_id": ctx.primary.run_id})["run"]
    require(described.get("state") == "held", "held state was not reflected by run describe")

    cancels = request_count(ctx, "POST", "/printer/print/cancel")
    aborted = ctx.client.call("mfg_run_abort", {
        **lease_params(ctx), "run_id": ctx.primary.run_id,
    })
    require(aborted.get("state") in {"aborting", "aborted"},
            "printer abort did not enter an abort state")
    require(request_count(ctx, "POST", "/printer/print/cancel") == cancels + 1,
            "printer abort did not route to Moonraker cancel")
    described = ctx.client.call("mfg_run_describe", {"run_id": ctx.primary.run_id})["run"]
    require(described.get("state") == aborted.get("state"),
            "abort state was not reflected by run describe")


def step7(ctx: Context) -> None:
    result = preflight(ctx)
    run_id = result.get("run_id")
    require(isinstance(run_id, str) and ctx.primary is not None
            and run_id != ctx.primary.run_id, "failure scenario did not author a new RunId")
    binding = propose_and_create(ctx, run_id)
    require(start(ctx, binding, human()).get("state") == "running",
            "printer failure scenario did not start")
    ctx.printer.state = "error"
    rows = rows_by_field(telemetry_rows(ctx))
    require(rows.get("device_state", {}).get("v") == "fault",
            "printer-side error was not observed as device fault telemetry")
    described = ctx.client.call("mfg_run_describe", {"run_id": run_id})["run"]
    require(described.get("state") == "faulted",
            f"printer-side failure ended as {described.get('state')!r}, not faulted")
    refused = ctx.client.refuse("mfg_run_resume", {
        **lease_params(ctx), "run_id": run_id,
    }, code="no_alarm_since_hold")
    require(refused.get("detail", {}).get("discontinuity") == "alarm",
            "resume refusal did not name the alarm discontinuity")
    ctx.failure_run = binding


def step8(ctx: Context) -> None:
    require(ctx.primary is not None and ctx.failure_run is not None,
            "journal proof has no completed printer scenarios")
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
        "mfg.arbiter.attempt", "mfg.run.state", "mfg.attention",
    }
    require(required_kinds <= kinds,
            f"journal omitted printer lifecycle kinds {sorted(required_kinds - kinds)}")
    failure_states = [record.get("body", {}).get("state") for record in records
                      if record.get("kind") == "mfg.run.state"
                      and record.get("subject") == ctx.failure_run.run_id]
    for expected in ("draft", "running", "faulted"):
        require(expected in failure_states,
                f"journal did not replay failure run state {expected!r}")
    attention = ctx.client.call("mfg_attention_list", {"open_only": True})
    items = attention.get("attention")
    require(isinstance(items, list) and items, "printer failure raised no open attention item")
    matching = [item for item in items
                if item.get("run_id") == ctx.failure_run.run_id
                or item.get("run") == ctx.failure_run.run_id
                or item.get("device_id") == ctx.device_id
                or item.get("device") == ctx.device_id
                or item.get("subject_id") in {ctx.failure_run.run_id, ctx.device_id}]
    require(matching, "attention list did not bind the failure to its run or printer")
    require(any(item.get("severity") == "critical" for item in matching),
            "printer failure attention was not critical")


STEPS: list[Callable[[Context], None]] = [
    step1, step2, step3, step4, step5, step6, step7, step8,
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
    print("\nManufacturing printer qualification")
    print("#  RESULT  PROOF")
    for index, (name, status, detail) in enumerate(results, 1):
        suffix = f" — {detail}" if detail else ""
        print(f"{index:<2} {status:<6}  {name}{suffix}")


def run(hostd: Path) -> int:
    temp_path = Path(tempfile.mkdtemp(prefix="chudware-mfg-printer-"))
    socket_path = temp_path / "hostd.sock"
    state_dir = temp_path / "state"
    runtime_dir = temp_path / "runtime"
    appdata_dir = temp_path / "appdata"
    state_dir.mkdir()
    runtime_dir.mkdir(mode=0o700)
    appdata_dir.mkdir()
    log_path = temp_path / "hostd.log"
    results: list[tuple[str, str, str]] = []
    proc: subprocess.Popen[bytes] | None = None
    client: RpcClient | None = None
    printer: VirtualPrinter | None = None
    failure = ""
    cleanup_ok = False
    try:
        printer = VirtualPrinter("moonraker")
        printer.__enter__()
        port = int(printer.base_url.rsplit(":", 1)[1])
        (state_dir / "device_allowlist.tsv").write_text(
            "# Chudware manufacturing commissioning allowlist v1\n"
            f"tcp\taddress=127.0.0.1\tport={port}"
            "\tmachine_class=fdm_printer_enclosed"
            "\tadapter_kind=moonraker\tauth_mode=trusted_host"
            "\torigin=virtual\tnote=private virtual printer qualification\n",
            encoding="utf-8",
        )
        env = dict(os.environ)
        env.update({
            "CHUDWARE_INSTANCE_KEY": "mfg-printer-" + uuid.uuid4().hex,
            "CHUDWARE_HOSTD_SOCKET": str(socket_path),
            "CHUDWARE_MFG_STATE_DIR": str(state_dir),
            "CHUDWARE_APPDATA": str(appdata_dir),
            "CHUDWARE_REPO": str(REPO),
            "CHUDWARE_PYTHON": sys.executable,
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
            hello = client.call("hello", {"client": "mfg-printer-qualification",
                                           "pid": os.getpid(), "proto": 1})
            require(hello.get("proto") == 1, "private daemon hello negotiation failed")
            ctx = Context(client=client, printer=printer, state_dir=state_dir)
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
        printer_stopped = True
        if printer is not None:
            try:
                printer.__exit__(None, None, None)
            except Exception as error:
                printer_stopped = False
                failure = failure or f"virtual printer teardown failed: {error}"
        log_tail = ""
        try:
            if log_path.exists():
                log_tail = "\n".join(log_path.read_text(
                    encoding="utf-8", errors="replace").splitlines()[-20:])
        except OSError:
            pass
        shutil.rmtree(temp_path, ignore_errors=True)
        cleanup_ok = (proc is None or proc.poll() is not None) and printer_stopped \
            and not socket_path.exists() and not temp_path.exists()

    print_table(results)
    print(f"Cleanup: {'PASS' if cleanup_ok else 'FAIL'} "
          "(private daemon and virtual printer stopped; temp state removed)")
    if failure and log_tail:
        print("\nPrivate daemon log tail:", file=sys.stderr)
        print(log_tail, file=sys.stderr)
    if failure:
        print(f"\nQUALIFICATION FAILED: {failure}", file=sys.stderr)
    elif not cleanup_ok:
        print("\nQUALIFICATION FAILED: daemon, printer, socket, or temp state survived teardown",
              file=sys.stderr)
    else:
        print("\nQUALIFICATION PASSED: 8/8 steps")
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
