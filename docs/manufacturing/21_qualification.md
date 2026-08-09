# Manufacturing Gateway Qualification

## E3 private-daemon qualification

Run from the repository root after building the core daemon:

```sh
./build.sh core
python3 scripts/qualify_mfg_e3.py
# Equivalent thin wrapper:
scripts/qualify_mfg_e3.sh
```

The qualification is opt-in. It is not called by `scripts/gate.sh`, adds no normal-gate SKIP, and needs no device, network service, GPU, Python package, or sidecar. `--hostd PATH` may select a particular built daemon.

This is a process-boundary qualification, not a direct C++ fixture. The driver starts the real `chudware-hostd --foreground`, negotiates protocol 1, and sends newline-framed JSON RPC requests over its Unix IPC socket. It assigns a random `CHUDWARE_INSTANCE_KEY`, an explicit socket under a new temporary runtime directory, and a private `CHUDWARE_MFG_STATE_DIR`. The daemon is terminated as a process group and the entire temporary tree is removed in `finally`, including after an assertion or RPC failure. A surviving daemon or state directory makes the command fail.

The private allowlist contains one stable serial endpoint shape, `/dev/serial/by-id/chudware-e3-qualification`. The script never connects to or opens it. Its sole purpose is to prove that a genuinely physical candidate remains capability-clamped. The executable lane is always the built-in in-process emulator.

## What the nine steps prove

The command prints one `PASS` or `FAIL` row for every step and exits non-zero if any observation differs. Dependent steps are reported as not run after the first failure rather than being presented as evidence.

1. **Discovery is allowlist-only.** `mfg_device_list` returns the built-in `emulated`/`emulator` candidate with `allowlisted:true`, an empty lease, and an `unknown`, declared `FreshValue` with null `v`. The seeded physical serial candidate is also visible and allowlisted. No claim or transport open has occurred.
2. **Commander ownership is exclusive.** Owner A claims the emulator. Owner B's claim must refuse as `lease.takeover_refused` and name owner A's session. After an observed release, owner B can claim it.
3. **Connect identifies before publishing telemetry.** `mfg_device_connect` must return a SHA-256 controller fingerprint. Repeated `mfg_telemetry_snapshot` results must conform to the complete `FreshValue<T>` shape, use `src:"simulated"`, and carry non-null `v` exactly when `state=="ok"`; the value's device id must match the claim.
4. **Preflight is durable and closed-vocabulary.** A real NC artifact is registered and SHA-256-bound. `mfg_preflight_run` must return a nonzero seq and durable ref, cover exactly its required Stage-2 guards, and use only `pass`, `fail`, or `not_applicable`. The emulator preflight must pass.
5. **Approval is a prerequisite, not an optional field.** Starting the authored run with no approval must refuse by the exact name `approval.revoked_or_missing`.
6. **Proposal, human authority, and nonce semantics are distinct.** `mfg_approval_propose` leaves the run in `draft` and returns neither an approval id nor nonce authority. A human-authenticated `mfg_approval_create` with `exercisable_by:"human_only"` creates the binding. An agent start must refuse with a code naming `exercisable_by_permits_initiator`; a human start must enter `running` and report the nonce-burn seq. Reusing that approval must refuse with a code naming `approval.present_unexpired_unburned`, and inspection must show `status:"burned"`.
7. **The emulator and journal tell the same completed story.** Bounded `mfg_run_describe` and telemetry service ticks must drive the real Grbl adapter to terminal `completed`. `mfg_journal_query` from seq 1 must be strictly increasing through its head and contain the gateway epoch, lease, device state/snapshot, artifact, gate report, proposal, approval issue/burn, arbiter attempt, and run-state kinds. The primary run must replay `draft`, `running`, and `completed`.
8. **An alarm is a terminal discontinuity, never a transparent resume.** A separately authored and approved run is started, then `mfg_emulator_fault_inject` observes an alarm in simulated `FreshValue` telemetry. The run must become terminal `faulted`. `mfg_run_resume` must refuse as `no_alarm_since_hold` with `detail.discontinuity:"alarm"`, and reconnecting under the same terminal claim must refuse as `mfg_transport_terminal`. After releasing the terminal claim and reclaiming a fresh emulator transport, an old burned approval still refuses; only a new RunId, a new preflight, and a new human approval can start and complete another run.
9. **Capability is scoped and virtual qualification cannot leak to hardware.** The emulated subject may report emulator-scoped `execute:verified` at E3, because it names the emulated device, binds to no hardware, and retains `safety_integration:unsupported`. The allowlisted serial candidate is then claimed but never connected. Its capability report must show `execute:assumed`, `missing_level:"execute"`, `safety_integration:unsupported`, and must not advertise execute as its maximum.

The script never asserts a pass from configuration or an expected constant alone: every row is earned from a daemon response, terminal process state, journal replay, or filesystem teardown observation. It does not advertise physical `execute` or any `safety_integration`.

## Emulator and pilot stage ladder

| Stage | Prerequisite | Evidence earned | Where it runs | Capability consequence |
|---|---|---|---|---|
| **E0** | none | Grbl protocol parse/encode against golden transcripts | hardware-free normal tests | Protocol vocabulary only; no physical capability. |
| **E1** | none | Adapter state machine, send/ack accounting, error halt, and bounded polling | hardware-free normal tests | Controller-path behavior only; no physical capability. |
| **E2** | none | Durable journal seq/epoch ordering, reconciliation, and scoped invalidation | hardware-free normal tests | Replay/audit behavior only; no physical capability. |
| **E3** | none | Full claim → identify → artifact → gates → approval → nonce → execution → completion/fault → replay path on `EmulatorTransport`, plus the named physical capability refusal | this opt-in private-daemon qualification and hardware-free tests | `execute` may be verified only for machine class `emulated`. Every physical class remains below execute, and safety integration remains unsupported. |
| **E4** | bench controller, no motors | Real transport open/reset and controller identity | pilot qualification only | May begin physical transport evidence; never execute. |
| **E5** | bench controller plus motors, no tool | Motion, hold behavior, and alarm/discontinuity handling | pilot qualification only | Physical transport may be verified; execute remains unavailable. |
| **E6** | attended pilot machine and its safety case | End-to-end physical execution on the named commissioned machine/profile | pilot qualification only | May qualify physical execute for that exact scope. It does not imply safety integration. |

E0–E3 are a prerequisite for attempting pilot stages, not a substitute for them. No emulator stage unlocks `safety_integration`; that level requires separately commissioned physical protective functions and is outside this qualification.
