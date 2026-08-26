"""tool_guidance.manufacturing — curated options for the manufacturing tool family."""
from __future__ import annotations

from typing import Any

ENTRIES: dict[str, dict[str, Any]] = {
    "mfg_approval_inspect": {
        "prompt_note": (
            "Pass exactly one approval_id or proposal_id. A frozen approval read adds "
            "active/burned/revoked status; a proposal remains unapproved, and this "
            "read cannot create or validate either one."
        ),
    },
    "mfg_approval_propose": {
        "prompt_note": (
            "Use only after the draft run has preflight evidence. The result is an "
            "UNAPPROVED proposal for a human seat, not authority; a human creates the "
            "binding and the arbiter alone validates it at start."
        ),
    },
    "mfg_artifact_register": {
        "prompt_note": (
            "For NC/G-code, run cam_post then cam_audit first. This call only hashes "
            "immutable content under the command lease; it performs no device I/O, "
            "run creation, or authorization."
        ),
    },
    "mfg_device_capability": {
        "prompt_note": (
            "Treat all nine capability truth levels literally; device presence never "
            "promotes transport to execute. Physical execute and safety_integration "
            "remain unadvertised before the qualified pilot."
        ),
    },
    "mfg_device_describe": {
        "prompt_note": (
            "Read identity/fingerprint, transport, allowlist, capability and policy "
            "together before planning a lane. connected=true is not execute authority, "
            "and physical execute/safety_integration remain unadvertised."
        ),
    },
    "mfg_device_list": {
        "prompt_note": (
            "Discovery before any manufacturing request: each candidate reports its "
            "allowlist decision, FreshValue lane state and capability ceiling. Physical "
            "endpoints are denied by default; a listed device grants no authority."
        ),
    },
    "mfg_gate_report": {
        "prompt_note": (
            "Read the gate_report_ref returned by preflight or a start refusal. The "
            "result is seq-stamped Tier-0/two-stage evidence, but remains advisory until "
            "the arbiter revalidates a current human approval."
        ),
    },
    "mfg_journal_query": {
        "prompt_note": (
            "Requires command-lane ownership and returns ordered records, head_seq and "
            "waiver_rates. Journal seq values are the sole durable ordering truth; never "
            "synthesize seq from telemetry time."
        ),
    },
    "mfg_preflight_run": {
        "prompt_note": (
            "Call after immutable artifact registration on a connected, identified "
            "device. It creates or refreshes a draft run and returns run_id plus Tier-0/"
            "two-stage gates; it cannot authorize start."
        ),
    },
    "mfg_run_describe": {
        "prompt_note": (
            "Use a run_id from preflight/history to read state, artifact, gate, approval "
            "and initiator history. A held or discontinuous run is not resumable; author "
            "a new run with fresh inspection, preflight and approval."
        ),
    },
    "mfg_run_list": {
        "prompt_note": (
            "Durable run history, optionally filtered by device/state; it is not current "
            "device telemetry. Use the returned run_id for a full run description rather "
            "than inferring approval or initiator from the state label."
        ),
    },
    "mfg_telemetry_snapshot": {
        "prompt_note": (
            "The snapshot is a FreshValue envelope (v/time/seq/age/ttl/src/state/why), "
            "with gaps left unknown and raw rows separate. Never infer physical execute "
            "or safety_integration from connected/probed telemetry."
        ),
    },
    "request_mfg_abort": {
        "prompt_note": (
            "REQUEST only: the daemon lane asks the arbiter to validate state/initiator, "
            "then the device adapter performs a controlled abort; approval authority "
            "remains the arbiter's. This is not an E-stop or safety-rated stop."
        ),
    },
    "request_mfg_hold": {
        "prompt_note": (
            "REQUEST only: the daemon lane asks the arbiter first, then the device adapter "
            "performs feed_hold/controlled_stop/door_suspend; approval authority remains "
            "the arbiter's. This software hold is not an E-stop or safety function."
        ),
    },
    "request_mfg_run_start": {
        "prompt_note": (
            "REQUEST only: the arbiter alone revalidates the current human approval, "
            "binding, fingerprint and gates before the daemon device adapter may start. "
            "Physical execute/safety_integration remain unadvertised and are refused."
        ),
    },
}
