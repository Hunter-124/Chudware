# Manufacturing code review — 2026-08-08

Independent review of `core/device/**`, `core/manufacturing/**`, `core/hostd/mfg_lane.*`,
`core/twin/**`, `core/print/**`, `core/cam/{turn,cut}/**`, `services/manufacturing/**`.

**Verdict: NOT ready to sit behind real hardware.** Four CRITICAL findings gate the E4-E6
pilot. This file is the record; fixes are tracked against it.

```json
{
  "overall_correctness": "incorrect",
  "explanation": "The subsystem is not ready to sit behind real manufacturing hardware. The strongest abstractions exist, but the host lane still exempts physical printers from the execute ceiling, synthesizes emulated authorization facts, trusts caller-provided human provenance, and fails to bind approvals and runs to the live controller; printer lifecycle, serial liveness, monitoring freshness, and several boundedness/validation paths also need correction before hardware qualification.",
  "confidence": 0.99,
  "findings": [
    {
      "title": "Block physical printer execution before E6 [CRITICAL]",
      "body": "The physical-execute guard rejects only non-printers, so every allowlisted physical printer proceeds to upload/start even though the same lane advertises physical execute as assumed and max capability as transport. This is not rescued by preflight: `computePreflight` and `authorizationFor` hardcode `MachineClass::Emulated`, verified execute, E3, and passing interlock/feed-hold/metrology facts (`core/hostd/mfg_lane.cpp:802-932`). Block every `endpoint_is_physical` start until the hardware stage is qualified, and represent virtual-printer qualification endpoints as non-physical rather than exempting the entire printer class.",
      "priority": 1,
      "confidence": 1,
      "file_path": "core/hostd/mfg_lane.cpp",
      "line_start": 1532,
      "line_end": 1534
    },
    {
      "title": "Authenticate approvers and initiators from hostd [CRITICAL]",
      "body": "Approval creation treats caller-supplied `approver_auth.identity` and `auth` as authenticated when both strings are merely non-empty, while run start likewise parses its initiator from request JSON. Although leases are now connection-bound, `server.cpp:499-509` obtains the user name from the client's own `hello` payload, so a lease holder can claim to be any human and exercise a human-only approval. Construct approver and initiator provenance from an authenticated server-side identity or peer credential and bind it to the connection; never accept those identities as request assertions.",
      "priority": 1,
      "confidence": 0.99,
      "file_path": "core/hostd/mfg_lane.cpp",
      "line_start": 1439,
      "line_end": 1444
    },
    {
      "title": "Bind approvals to the observed controller fingerprint [CRITICAL]",
      "body": "The approval records `advertised_fingerprint_`, which is initialized from the built-in emulator identity and is not recomputed when `selectTransport` changes machine class/endpoint (`core/hostd/mfg_lane.cpp:182-193,351-354`). A physical Grbl fingerprint is observed during connect but only returned in that RPC (`core/hostd/mfg_lane.cpp:1143-1167`); gates compare the advertised value to itself. A controller swapped at the same endpoint can therefore execute an approval bound to the wrong machine. Persist the identified adapter fingerprint as the current machine fingerprint and use it for describe, preflight, approval, and start revalidation.",
      "priority": 1,
      "confidence": 0.99,
      "file_path": "core/hostd/mfg_lane.cpp",
      "line_start": 1452,
      "line_end": 1453
    },
    {
      "title": "Reject runs belonging to a previously selected device [CRITICAL]",
      "body": "Run start verifies that the IDs exist but never checks `run.device_id`, its artifact's `device_id`, or the approval's device against the currently claimed `device_id_`. Runs, artifacts, approvals, and arbiter registrations survive release and `selectTransport`, so after claiming device B a caller can start an approved artifact authored for device A; the arbiter's current inputs are the old approval values and therefore do not detect the switch. Enforce all device bindings at start and invalidate device-scoped records whenever the selected device changes.",
      "priority": 1,
      "confidence": 0.99,
      "file_path": "core/hostd/mfg_lane.cpp",
      "line_start": 1536,
      "line_end": 1542
    },
    {
      "title": "Implement lease heartbeats before using 15-second leases [HIGH]",
      "body": "Claims default to a 15-second TTL and advertise a 3-second heartbeat, but the lane has no `mfg_device_heartbeat` dispatch and never calls `LeaseRegistry::renew`; the normal UI claim also supplies no longer TTL (`host/src/ui/manufacture/manufacture_panel.cpp:138-141`). Once the lease expires, hold and abort are refused even if the physical job is still running. Add the contracted heartbeat RPC, renew only for the authenticated connection owner, and have command clients send it at the returned heartbeat interval; expiry must fail the active claim safely.",
      "priority": 1,
      "confidence": 1,
      "file_path": "core/hostd/mfg_lane.cpp",
      "line_start": 1074,
      "line_end": 1078
    },
    {
      "title": "Close the active printer when releasing its lease [HIGH]",
      "body": "Release removes only the lease and leaves the transport/worker adapter connected. On the next claim, `selectTransport` closes only the C++ adapter/transport, not the Python printer adapter; the next worker open then reports that the same adapter ID is already open. More seriously, releasing during a print abandons the running printer without a command owner. Close/fault the active device and terminalize any active run before releasing ownership, or explicitly refuse release while a run is active.",
      "priority": 1,
      "confidence": 0.99,
      "file_path": "core/hostd/mfg_lane.cpp",
      "line_start": 1095,
      "line_end": 1106
    },
    {
      "title": "Route printer job and unknown states into run lifecycle [CRITICAL]",
      "body": "The printer snapshot consumer handles only string-valued `device_state` rows. Canonical `job_state` values such as `completed`, `aborted`, and `faulted` are silently ignored, and the finalized outage path emits unknown rows with `v: null`, which this filter also skips; the outer snapshot is then unconditionally labeled `state: ok` at lines 1283-1287. Consequently a completed or unreachable printer can leave the run Running and the claim connected. Dispatch both fields explicitly, fail unknown device/job telemetry closed, transition terminal run states, and make the outer freshness state reflect its rows.",
      "priority": 1,
      "confidence": 1,
      "file_path": "core/hostd/mfg_lane.cpp",
      "line_start": 1203,
      "line_end": 1213
    },
    {
      "title": "Monitor printers independently of UI snapshot requests [HIGH]",
      "body": "`serviceDevice` immediately returns for printers because they have no C++ `adapter_`; the only worker snapshot call is inside the `mfg_telemetry_snapshot` RPC. If the GUI stops polling or disconnects while a print continues, hostd never observes completion, alarm, disconnect, or outage and never raises the new critical attention. Add a bounded host-owned polling/event loop for each connected printer claim and drive the same canonical lifecycle dispatcher from it; UI reads should consume state, not be responsible for producing safety observations.",
      "priority": 1,
      "confidence": 0.99,
      "file_path": "core/hostd/mfg_lane.cpp",
      "line_start": 550,
      "line_end": 554
    },
    {
      "title": "Keep runs Holding until hold is observed [CRITICAL]",
      "body": "`Arbiter::hold` transitions Running→Holding→Held and journals `hold_acknowledged` immediately. The lane invokes this before sending the printer pause or Grbl feed-hold command (`core/hostd/mfg_lane.cpp:1626-1638`), and `GrblAdapter::requestHold` only writes the realtime byte without waiting for a `Hold:0` status. A failed, delayed, or lost command is therefore reported as safely Held while motion may continue. Return Holding after command issue and transition to Held only from fresh controller/printer telemetry that acknowledges the hold.",
      "priority": 1,
      "confidence": 1,
      "file_path": "core/manufacturing/arbiter.cpp",
      "line_start": 336,
      "line_end": 342
    },
    {
      "title": "Record unknown command outcome after nonce burn [CRITICAL]",
      "body": "The arbiter burns the nonce and reaches Running before upload/start I/O, but the lane updates its own run state only after those calls succeed. Upload/start failure returns a refusal while arbiter and lane disagree; if the printer executed the request but the worker response was lost, the machine runs while the client is told start was refused. On every post-burn failure, journal an explicit command-outcome-unknown state, raise critical attention, prevent reuse, and reconcile through fresh device telemetry rather than returning an ordinary refusal.",
      "priority": 1,
      "confidence": 0.99,
      "file_path": "core/hostd/mfg_lane.cpp",
      "line_start": 1568,
      "line_end": 1584
    },
    {
      "title": "Make max-deadline serial polls nonblocking [HIGH]",
      "body": "Every `GrblAdapter::service` calls `transport_.poll(steady_clock::time_point::max())`. TCP explicitly interprets that sentinel as a zero-timeout service poll, but SerialTransport converts it to an `INT_MAX` millisecond OS poll (and its Windows path loops until that deadline). A silent or wedged serial controller can therefore block one hostd request—and the lane mutex—for roughly 24.8 days. Give SerialTransport the same sentinel semantics as TCP or pass a bounded/current deadline from the adapter.",
      "priority": 1,
      "confidence": 1,
      "file_path": "core/device/grbl_adapter.cpp",
      "line_start": 97,
      "line_end": 102
    },
    {
      "title": "Require a typed OPC UA value before marking it usable [HIGH]",
      "body": "The Machine Tools mapper defines `usable` solely from Good status plus a source timestamp. A `DataValue` with no Variant consequently produces all-null values but an `ok`, evidence-eligible observation; arrays are accepted for scalar profile nodes, and a wrong-type empty string can be coerced to a cleared emergency stop. Extend each closed shape with allowed built-in types/cardinality, require a non-null conforming value, reject non-finite numbers, and emit unknown with a named type/shape reason on mismatch.",
      "priority": 1,
      "confidence": 0.99,
      "file_path": "services/manufacturing/monitor/opcua/machine_tools.py",
      "line_start": 258,
      "line_end": 267
    },
    {
      "title": "Apply a TTL before making OPC UA values evidence-eligible [HIGH]",
      "body": "Evidence eligibility requires only a probed source and the `usable` predicate, and that predicate accepts any syntactically valid source timestamp regardless of its age or whether it lies implausibly in the future. A server's old retained Good value is therefore promoted to current evidence. Carry collection time and an explicit TTL through the sole freshness shape, compute age, and set stale/future observations to unknown with `evidence_eligible=False`.",
      "priority": 1,
      "confidence": 0.99,
      "file_path": "services/manufacturing/monitor/opcua/machine_tools.py",
      "line_start": 340,
      "line_end": 349
    },
    {
      "title": "Reset OPC UA secure-channel state before reconnecting [MEDIUM]",
      "body": "`_drop_transport` clears only the socket/boolean flags and leaves `_last_server_sequence`, channel/token IDs, and authentication token intact. Reusing the client after `close()` opens a new secure channel whose server sequence normally starts at 1, but `_receive_service` compares it against the previous channel's last sequence plus one and rejects the reconnect as non-contiguous. Reset all channel/session-scoped state before a new connect while retaining only configuration.",
      "priority": 2,
      "confidence": 0.98,
      "file_path": "services/manufacturing/monitor/opcua/client.py",
      "line_start": 221,
      "line_end": 230
    },
    {
      "title": "Renew OPC UA secure channels before token expiry [MEDIUM]",
      "body": "The client decodes `ChannelSecurityToken.created_at` and `revised_lifetime` but stores only channel/token IDs and opens the channel once during connect. A server that grants a short revised lifetime—or any session used beyond the default ten minutes—will reject later browse/read calls after token expiry. Track the granted lifetime and issue an OpenSecureChannel Renew request before expiry, switching tokens according to the UA overlap rules.",
      "priority": 2,
      "confidence": 0.97,
      "file_path": "services/manufacturing/monitor/opcua/client.py",
      "line_start": 284,
      "line_end": 291
    },
    {
      "title": "Reject malformed numeric MTConnect samples [HIGH]",
      "body": "`_convert_number` accepts `NaN`/infinity, while an unparseable numeric sample simply falls back to `v_text`; neither case lowers quality. The row remains `state: ok` and evidence-eligible when timestamped, so values such as `ActualSpeed=NaN` or `ActualSpeed=garbage` can enter probed evidence. For definitions whose value kind is numeric/sample, require complete finite conversion (including finite conversion factors/offsets), otherwise clear values and emit unknown/bad quality with a named reason.",
      "priority": 1,
      "confidence": 0.99,
      "file_path": "services/manufacturing/monitor/mtconnect.py",
      "line_start": 490,
      "line_end": 499
    },
    {
      "title": "Require exactly three ordered axes before indexing the twin [MEDIUM]",
      "body": "Reset validates each supplied axis but never validates the vector length, uniqueness, or X/Y/Z order, then unconditionally indexes elements 0, 1, and 2. An empty, one-axis, or two-axis configuration passes the loop and causes out-of-bounds undefined behavior; three reordered axes silently map positions incorrectly. Require the supported unique ordered X/Y/Z set before copying/indexing it.",
      "priority": 2,
      "confidence": 1,
      "file_path": "core/twin/twin.cpp",
      "line_start": 713,
      "line_end": 716
    },
    {
      "title": "Close checkpoint descriptors exactly once [MEDIUM]",
      "body": "`persist_head` closes `fd`, then performs operations that can throw; its catch block closes the same integer again. In the multithreaded daemon another thread may acquire that descriptor number between the successful close and a rename failure, causing the catch path to close an unrelated file/socket. Use an RAII descriptor owner or invalidate the integer immediately after close and close only while valid.",
      "priority": 2,
      "confidence": 0.98,
      "file_path": "core/manufacturing/journal.cpp",
      "line_start": 206,
      "line_end": 220
    },
    {
      "title": "Apply journal query limits while reading [MEDIUM]",
      "body": "`Journal::replay` delegates to `read_records`, which materializes every matching record in a vector. The lane applies its 1000-record query limit only after that full replay, `mfg_result_invalidations` does the same, and startup attention restoration replays the entire journal. As the sole durable journal grows, a small bounded query or daemon restart can consume memory proportional to all history. Add a streaming/bounded replay API that applies sequence, kind, subject, and limit during file iteration, plus an explicit retention/compaction policy.",
      "priority": 2,
      "confidence": 0.99,
      "file_path": "core/hostd/mfg_lane.cpp",
      "line_start": 1846,
      "line_end": 1857
    },
    {
      "title": "Reject non-finite lathe topology and workholding geometry [MEDIUM]",
      "body": "Topology checks use `fabs(value) > tolerance`, which is false for NaN, so NaN travel or WCS direction components pass validation. `body_face_z_mm` and `safety_margin_mm` are also never checked before `minimum_safe_z` derives a clearance coordinate. Add explicit finite checks for every machine/WCS/workholding coordinate and require a finite non-negative safety margin before accepting the process.",
      "priority": 2,
      "confidence": 0.98,
      "file_path": "core/cam/turn/process.cpp",
      "line_start": 25,
      "line_end": 33
    },
    {
      "title": "Reject non-finite cut heights and path coordinates [MEDIUM]",
      "body": "Plasma pierce/cut heights are compared without finite validation, so NaN passes, and `plan_vector` never checks that input points are finite. Such a plan reaches the posts, which serialize `nan` coordinates into NC instead of refusing the operation. Validate finite, non-negative process heights and every path coordinate before area/offset calculations or motion construction.",
      "priority": 2,
      "confidence": 0.99,
      "file_path": "core/cam/cut/process.cpp",
      "line_start": 119,
      "line_end": 126
    }
  ]
}
```
