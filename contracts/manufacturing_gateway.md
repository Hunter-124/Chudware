# Contract: Manufacturing Gateway — v1

This is the canonical wire and durable-state contract for the separate manufacturing gateway. Public
`libchudware` exposes no `mfg_*` C ABI. Nothing in this foundation opens a device or advertises physical
`execute` capability.

## 1. Reserved namespaces

RPC methods use `mfg_*`; events use `mfg.*`; all ten domains share the default-off MCP family
`manufacture`.

| domain | RPC | event | durable kind prefix |
|---|---|---|---|
| gateway | `mfg_device_*`, `mfg_run_*`, `mfg_approval_*`, `mfg_capability_*`, `mfg_event_*`, `mfg_policy_*`, `mfg_tier0_*`, `mfg_result_*` | `mfg.device.*`, `mfg.run.*`, `mfg.approval.*`, `mfg.result.*`, `mfg.gateway.*` | unprefixed gateway kinds below |
| twin | `mfg_twin_*` | `mfg.twin.*` | `twin_*` |
| metrology | `mfg_metrology_*` | `mfg.metrology.*` | `metrology.*` |
| additive | `mfg_print_*` | `mfg.print.*` | `print.*` |
| perception | `mfg_perception_*` | `mfg.perception.*` | `perception.*` |
| monitoring | `mfg_monitor_*` | `mfg.monitor.*` | `monitor_*` |
| cutting | `mfg_cut_*` | `mfg.cut.*`, `mfg.plasma.*` | `cam.cut.*`, `cam.plasma.*` |
| turning | `mfg_turn_*` | `mfg.turn.*` | `cam.turn.*` |
| PnP | `mfg_pnp_*` | `mfg.pnp.*` | `pnp.*` |
| robot | `mfg_robot_*` | `mfg.robot.*` | `robot_*` |

An unreserved prefix is a contract failure (`MFG-PREFIX-1`).

## 2. Identities and machine fingerprint

Canonical identifiers are:

- `DeviceId`: `dev:<machine_class>:<fingerprint-sha256-first-12-lowerhex>`.
- `LeaseId`: `lease:<ULID>`.
- `RunId`: `run:<ULID>`.
- `ApprovalId`: `appr:<ULID>`.
- `ResultRef`: `res:<kind>:<ULID>`.
- `LaneEpoch`: `{process_start_id:<ULID>,lane_id:<string>,epoch:<u32>}`.
- `Seq`: unsigned 64-bit, strictly monotonic per gateway install, never reused or reset.

The canonical `MachineClass` strings are `open_frame_router`, `open_frame_mill`,
`enclosed_interlocked_mill`, `fdm_printer_open`, `fdm_printer_enclosed`, and `emulated`.

`MachineFingerprint` is lower-case SHA-256 of compact canonical JSON (object keys sorted recursively,
array order retained) for the identity snapshot. Controller identity contains
`{firmware_banner,build_options,settings,axis_count,machine_class,transport_kind,
transport_endpoint_id,installed_accessories}`. Printer identity contains
`{firmware_name_or_software_version,config_hash,kinematics,extruder_count,adapter_kind}`. The
fingerprint is recomputed rather than accepted from a caller.

## 3. Normalized states

`DeviceState` is exactly `disconnected|connecting|identifying|idle|running|hold|door|probing|homing|
alarm|fault|unknown`.

`RunState` is exactly `draft|awaiting_approval|approved|arming|queued|running|holding|held|resuming|
completing|completed|aborting|aborted|faulted|abandoned`.

The allowed run edges are:

`draft→awaiting_approval→approved→arming`; `arming→queued|faulted`; `queued→running|faulted`;
`running→holding|completing|aborting|faulted`; `holding→held|faulted`;
`held→resuming|abandoned|aborting|faulted`;
`resuming→running|faulted`; `completing→completed|faulted`; `aborting→aborted|faulted`.
`completed`, `aborted`, `faulted`, and `abandoned` are terminal. Reset, alarm, hard limit, disconnect,
or power loss after hold forbids `held→resuming`; continuing requires a new `RunId`, inspection,
preflight, and approval.

`ControllerSnapshot` is
`{device_id,state,substate?,mpos[3],wpos[3],wco[3],feed_rpm{feed,spindle},
overrides{feed,rapid,spindle},inputs{x,y,z,probe,door,hold,reset,start},
buffer{planner?,rx?},alarm?,line?,raw}`. Optional buffer and line fields never clock streaming and
never form a resume checkpoint. Raw vendor status is retained.

## 4. Capability truth

Levels, in order, are `plan`, `generate`, `post`, `parse`, `simulate`, `monitor`, `transport`,
`execute`, `safety_integration`. Truth values are `verified`, `declared`, `assumed`, `unsupported`,
`not_applicable`.

```json
{"level":"execute","truth":"assumed","evidence_ref":[],"emulator_stage":"E3",
 "missing_level":"execute","why":"physical execute requires E6"}
```

`CapabilityTruth` carries `level`, `truth`, `evidence_ref[]`, `emulator_stage` (`none|E0..E6`),
`missing_level?`, and `why`. `assumed` requires `missing_level` and `why`, and disables exactly like an
absent capability. Every non-`verified` truth requires `why`. Evidence references name seq-stamped
durable results. Only `verified` enables an action. A `CapabilityReport` contains
`{subject_id,levels,max_advertised,emulator_stage_passed}` and is total over all nine levels.

Procedural facts may verify only `plan|generate|post|parse|simulate`. The four device-side levels
`monitor|transport|execute|safety_integration` require fresh probed evidence. Simulation, advisory
perception, declaration, and assumption never satisfy an execute-side gate.

## 5. Pure transport boundary

`IReadTransport` has only `open(Endpoint)`, `close()`, `poll(Deadline)`, and `kind()`. `ITransport`
extends it with `write(bytes)`. A collector implements only `IReadTransport`, takes no command lease,
and has no write dispatch path. `TransportKind` is exactly
`serial|tcp|websocket|emulator|file_drop|http_adapter|opcua_read`.

`Endpoint` includes required `endpoint_is_physical`. Provenance is computed by the gateway: physical
serial/TCP/WebSocket/HTTP-adapter/OPC-UA-read endpoints are `probed`; every virtual, emulated,
recorded, replayed, loopback, or omitted-flag endpoint is `simulated`. Callers never supply `src`.

## 6. Sole freshness envelope

Every external numeric or enum value a UI paints uses:

```json
{"v":null,"state":"ok|stale|unknown|refused|absent","t_mono_ns":0,"seq":0,
 "age_ms":0,"ttl_ms":null,"src":"probed|simulated|advisory|declared","why":"...",
 "last_ok":{"v":{},"t_mono_ns":0,"seq":0}}
```

`v` is null unless `state=="ok"`. Non-ok states require `why`. `last_ok` is display-only and is never
admitted by a gate. A sequence gap forces `state:"unknown"`, clears `v`, and sets
`why:"sequence gap <expected>..<last-missing>"`. A durable non-decaying row uses `ttl_ms:null` with
`state:"ok"`; on telemetry, a null TTL means the interval is unknown and forces `unknown`.
`core/manufacturing/fresh_value.h` is the sole encoder (`MFG-FRESH-1`).

## 7. Journal and durable sequence

The journal is a single-writer append-only JSONL store. It is the sole source of durable `seq`
(`MFG-SEQ-1`). Each record is:

```json
{"seq":1,"epoch":{"process_start_id":"01...","lane_id":"gateway","epoch":1},
 "kind":"run.transition","subject":"run:01...","body":{},"raw":null,"t_mono_ns":0}
```

Append reserves a never-reused sequence checkpoint, appends one complete line, synchronizes it to
stable storage, and only then returns. Reopen recovers the greater of the checkpoint and the highest
valid record. A torn trailing line is removed before the writer accepts appends. Replay never mutates
the journal and repeated replay from the same seq returns the same ordered records. No update or erase
operation exists.

Every RPC result has `{result_seq,epoch,...}` and every referenceable result row has its establishing
`seq`. A UI-paintable seq-less row is refused `mfg_internal_unstamped_result`.

## 8. Invalidation

The only invalidation envelope is:

```json
{"class":"falsified|superseded","reason":"stable_reason_code","why":"operator text",
 "affected":["res:..."],"at_seq":123,"superseded_by":"res:..."}
```

`superseded_by` is required for `superseded` and absent for `falsified`. Consumers apply a notice only
to affected rows with `seq<=at_seq`; replay is idempotent. `falsified` clears the value and current
claim. `superseded` retains the value, marks it non-current, points forward, and is never alarm-styled.
Falsification is stronger and a later supersession cannot resurrect a cleared row. Producers push;
consumers do not wait for polling.

## 9. Durable-result registry

Every row declares both an epoch policy and runtime invalidation triggers (`MFG-KIND-1`). The epoch
vocabulary is `falsify|reestablish|epoch_invariant`; `retain_as_last_ok` is an attribute of falsify.
An unlisted kind is refused `mfg_unregistered_result_kind` (`MFG-KIND-2`). Every
`*procedural_fact` follows its parent; every capability kind explicitly does not.

| domain | kind | epoch policy | runtime invalidation triggers |
|---|---|---|---|
| gateway | `controller_snapshot` | `falsify + retain_as_last_ok` | gap, expiry, reset, alarm, transport loss |
| gateway | `device_identity` | `falsify` | transport loss, re-probe mismatch, fingerprint change |
| gateway | `capability_report` | `epoch_invariant` | cited evidence or emulator-stage change; re-derive |
| gateway | `lease` | `falsify` | release, expiry, preemption, process death |
| gateway | `preflight_evidence` | `falsify` | setup, telemetry, bound input, expiry |
| gateway | `approval_binding` | `reestablish` | revoke, expiry, nonce burn, fingerprint/bound input change |
| gateway | `run_record` | `reestablish` | terminal invariant; non-terminal process death falsifies and abandons |
| gateway | `nc_artifact_hash` | `epoch_invariant` | hash mismatch; re-post supersedes |
| gateway | `post_check_result` | `epoch_invariant` | artifact/rule change; re-run supersedes |
| gateway | `gate_report` | `reestablish` | evidence invalidation; re-evaluation supersedes |
| gateway | `procedural_fact` | `epoch_invariant` | failing re-run or parent invalidation/supersession |
| gateway | `waiver` | `epoch_invariant` | explicit revocation only |
| gateway | `simulation_conclusion` | `epoch_invariant` | artifact mismatch; newer conclusion |
| twin | `twin_run`, `twin_trace_verification` | `epoch_invariant` | binding/fidelity/trace change; newer derivation |
| twin | `twin_procedural_fact` | `epoch_invariant` | parent/fidelity invalidation; follows parent |
| twin | `twin_capability` | `epoch_invariant` | cited evidence change; re-derive |
| metrology | `metrology.probe_def` | `epoch_invariant` | upsert/incompatibility |
| metrology | `metrology.probe_result` | `epoch_invariant` | retract/rerun |
| metrology | `metrology.observation` | `epoch_invariant` | correction/supersession |
| metrology | `metrology.qualification` | `epoch_invariant` | withdraw/expire/requalify |
| metrology | `metrology.calibration` | `epoch_invariant` | withdraw/recalibrate |
| metrology | `metrology.station_profile` | `epoch_invariant` | falsify/recalibrate |
| metrology | `metrology.transform_edge` | `epoch_invariant` | falsify/remeasure |
| metrology | `metrology.registration` | `epoch_invariant` | falsify/reprobe |
| metrology | `metrology.offset_proposal` | `epoch_invariant` | cited evidence closure; newer proposal |
| metrology | `metrology.offset_approval` | `epoch_invariant` | revoke/newer approval |
| metrology | `metrology.offset_apply` | `epoch_invariant` | readback mismatch, revoke, stale write, newer record |
| metrology | `metrology.inspection_plan` | `epoch_invariant` | incompatible/revised |
| metrology | `metrology.inspection_result` | `epoch_invariant` | cited observations; rerun; follows plan |
| metrology | `metrology.procedural_fact` | `epoch_invariant` | failing rerun; follows parent |
| metrology | `metrology.capability` | `epoch_invariant` | cited evidence; re-derive |
| additive | `print.slice_result` | `epoch_invariant` | sidecar binary hash change |
| additive | `print.artifact` | `epoch_invariant` | missing/moved/hash mismatch; re-slice |
| additive | `print.procedural_fact` | `epoch_invariant` | failing rerun/profile/config/parent change |
| additive | `print.preflight` | `epoch_invariant` | bound/live evidence change; newer preflight |
| additive | `print.run_link` | `epoch_invariant` | none; immutable join |
| additive | `print.capability` | `epoch_invariant` | cited evidence; re-derive |
| additive | `print.media_source` | `epoch_invariant` | endpoint/security/device reconfiguration |
| perception | `perception.event` | `epoch_invariant` | corrupt frames, identity/config/calibration/qualification, refinement/model change |
| perception | `perception.evidence_clip` | `epoch_invariant` | hash/identity mismatch; correction |
| perception | `perception.model_export` | `epoch_invariant` | parity/spec/load failure; newer export |
| perception | `perception.capability` | `epoch_invariant` | cited evidence; re-derive |
| monitor | `monitor_observation` | `falsify + retain_as_last_ok` | source loss; newer observation |
| monitor | `monitor_condition` | `falsify + retain_as_last_ok` | source loss; newer transition |
| monitor | `monitor_source_state` | `falsify` | disconnect/epoch change |
| monitor | `monitor_sequence_gap` | `epoch_invariant` | later contiguous run only |
| monitor | `monitor_capability` | `epoch_invariant` | evidence/stage/profile/proof/trust change; re-derive |
| cut | `cam.cut.recipe` | `epoch_invariant` | value/revision change |
| cut | `cam.cut.plan`, `cam.cut.post`, `cam.cut.parse`, `cam.cut.sim`, `cam.cut.emulate` | `epoch_invariant` | bound input/model/rule/stage change; declared parent propagation |
| cut | `cam.plasma.replay` | `epoch_invariant` | ledger hash or rerun; declared parent propagation |
| cut | `cam.cut.procedural_fact` | `epoch_invariant` | cited artifact; follows parent |
| cut | `cam.cut.capability` | `epoch_invariant` | cited evidence; re-derive |
| turn | `cam.turn.profile`, `cam.turn.workholding` | `epoch_invariant` | source/setup change or replacement |
| turn | `cam.turn.sim_remnant`, `cam.turn.validation_report` | `epoch_invariant` | bound input or rerun; declared parent propagation |
| turn | `cam.turn.procedural_fact` | `epoch_invariant` | artifact/config/snapshot; follows parent |
| turn | `cam.turn.capability` | `epoch_invariant` | stage/dialect/evidence; re-derive |
| pnp | `pnp.board_revision`, `pnp.panel_definition`, `pnp.machine_profile` | `epoch_invariant` | re-import/source/array/revision change |
| pnp | `pnp.feeder_assignment`, `pnp.fiducial_fit` | `epoch_invariant` | incompatibility/rejected refit |
| pnp | `pnp.job_artifact`, `pnp.sim_result`, `pnp.gate_row`, `pnp.export_artifact` | `epoch_invariant` | bound input/subject/declared parent propagation |
| pnp | `pnp.q1_procedural_fact` | `epoch_invariant` | failing rerun; follows parent |
| pnp | `pnp.capability` | `epoch_invariant` | cited evidence; re-derive |
| robot | `robot_cell`, `robot_reach`, `robot_analysis`, `robot_plan`, `robot_traj`, `robot_mockrun` | `epoch_invariant` | asset/config/parent/newer analysis/input change as declared |
| robot | `robot_procedural_fact` | `epoch_invariant` | failing rerun; follows parent |
| robot | `robot_capability` | `epoch_invariant` | cell/cited evidence; re-derive |

The capability kinds covering all ten domains are `capability_report`, `twin_capability`,
`metrology.capability`, `print.capability`, `perception.capability`, `monitor_capability`,
`cam.cut.capability`, `cam.turn.capability`, `pnp.capability`, and `robot_capability`.

## 10. Gateway RPC allocation

All results use the journal envelope and structured worker error envelope. The gateway methods are:

- Device: `mfg_device_list`, `mfg_device_describe`, `mfg_device_claim`, `mfg_device_release`,
  `mfg_device_heartbeat`, `mfg_device_snapshot`, `mfg_device_subscribe`.
- Truth/policy: `mfg_capability_report`, `mfg_policy_get`.
- Run: `mfg_run_propose`, `mfg_run_submit`, `mfg_run_start`, `mfg_run_hold`, `mfg_run_resume`,
  `mfg_run_abort`, `mfg_run_describe`, `mfg_run_list`.
- Approval: `mfg_approval_issue`, `mfg_approval_revoke`, `mfg_approval_describe`.
- Journal: `mfg_event_query`, `mfg_event_subscribe`, `mfg_event_append`.
- Shared registration/invalidation: `mfg_tier0_register`, `mfg_result_invalidate`.

`mfg_run_propose` and `mfg_run_submit` do not touch a device. No gateway method jogs, homes, zeroes,
unlocks, changes an override, or writes controller settings.

`mfg_event_append{domain_id,kind,subject,result_kind,body,raw?,derived_from?}` is the only domain seam
that returns a durable seq. It refuses unregistered kinds and caller-supplied provenance. The gateway
computes invalidation dependency closure; `affected[]` is never a domain's guess.

## 11. Events and subscription lane

Canonical gateway events are `mfg.gateway.epoch`, `mfg.device.state`, `mfg.device.snapshot`,
`mfg.device.alarm`, `mfg.device.lease`, `mfg.device.foreign_client_detected`,
`mfg.device.capability_changed`, `mfg.run.state`, `mfg.run.gate_report`, `mfg.approval.issued`,
`mfg.approval.burned`, `mfg.approval.revoked`, `mfg.result.falsified`, and
`mfg.result.superseded`.

Accepted `mfg_device_subscribe` and `mfg_event_subscribe` calls establish subscription lanes. Unlike a
normal worker request, that lane may emit event lines after the accepting call's final response, for
its lifetime. A client declaring `client:"observer"` can subscribe/read but reaches no write dispatch
path and can never own a command lease.
