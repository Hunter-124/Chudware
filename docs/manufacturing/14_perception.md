# Forecast-to-Compress — Manufacturing Latent Dynamics Perceptron (MLDP)

Design snapshot 2026-08-08. Design-only: no repository files edited, no gates run.
Owner: `PerceptronArchitecture`. Capability level: **`monitor` only**. Every learned output is
`src:"advisory"` and can never gate an approval.

**Amendment, 2026-08-08.** This revision closes the findings `ArchitectureConsistencyReviewer` assigned
to this slice in `local://manufacturing-plan-review.md`: **B10** (perception half — durable-result-kind
table with both gateway §24.1 columns plus a capability kind), **B12** (one shared `manufacture` MCP
family), **C3** (one invalidation event carrying `class`/`reason`) and **C8** (durable rows journaled
*and* published wrapped, without wall-clock decay). Product scope is unchanged: capability level
`monitor` only, advisory-only output, no gate participation at either stage, and DoD v4's locked
product choice — **the MLDP model/training core is published** — carried through unaltered. See §14
for the closure table.

Sources folded in: `agent://LocalPerceptionPlan` yield (recovered from its session log — the artifact
itself did not persist) plus its two follow-ups; `local://manufacturing-decisions.md`; reads of
`services/vision/frames.py`, `services/vision/measure.py`, `services/vision/fiducial.py`,
`training/jobs/schema.md`, `training/recipes/sft_lora.yaml`; and the cross-slice rulings from
`Main`, `ExecutionPlanner`, `ManufactureUxArchitecture`, `MetrologyArchitecture`,
`AdditiveArchitecture`, `GatewayArchitecture` and `TwinArchitecture`.

---

## 0. The novel contribution, stated precisely

Three claims, each measurable, none of them safety claims.

**C1 — Control-conditioned causal latent forecasting.** Generic video anomaly detection forecasts the
future from the past. A machine tool does not have to guess its future: the approved, immutable
program is already queued, so the *commanded* trajectory for the next 30 s is a **known exogenous
input**. MLDP forecasts the *observed* latent conditioned on the *commanded* future. Surprise is
therefore "the machine is not doing what it was told to do", not "the pixels changed". This is the
structural fact that makes the model manufacturing-specific rather than a re-skinned video model.

**C2 — Forecast residual as the retention policy (Forecast-to-Compress).** The same forward pass that
produces the forecast produces the calibrated negative log-likelihood of what actually happened, and
the retention head is trained *against that residual plus labelled event intervals* under an explicit
byte budget. Compression is not a heuristic bolted on after detection; it is the training objective's
dual. The headline curve is **bytes retained vs. event-interval recall**, reported as an AUC against
motion-trigger, fixed-interval, continuous-H.264 and supervised-detector-only baselines.

**C3 — Camera relevance as measured predictive information.** "Which camera matters right now" is
defined as the measured leave-one-camera-out increase in forecast NLL, computed offline and distilled
into a relevance head. It is a number with a validation procedure (Spearman ρ against the measured
LOCO delta), not an aesthetics score.

Scheduling consequence that matters: **C1 and C2 train with no human annotation** — the forecast
target is the future of the recording. 25–50 h of ordinary recorded production video plus the
gateway's own controller logs and queued programs is a complete Stage-0 training set. Labels buy the
supervised state/anomaly/importance heads, not the core.

Explicit non-claims: this does not beat H.264 as a codec (it decides *what* to keep; the codec decides
how many bytes); it is not a foundation model; it is not a safety function.

---

## 1. Ownership and boundaries

| Thing | Owner | This design's relationship |
|---|---|---|
| `FreshValue<T>` painted-field envelope | ManufactureUxArchitecture | **Consumed verbatim. No perception-local envelope exists.** |
| Controller wire/state schema, `ControllerSnapshot`/`ControllerEvent`, device/run/event identity, tick→`t_mono_ns` stamping | GatewayArchitecture | Consumed by foreign key and as model input features. No competing vocabulary. |
| `MeasurementObservation` (`aicad.metrology_observation/1`, `id` = `"sha256:<hex>"`), `evidence_class` ∈ {`qualified`,`advisory`,`simulated`}, `accuracy_tier` ladder {`A0_indicative`,`A1_scaled`,`A2_station`,`Q0_unqualified`,`Q1_repeatable`,`Q2_qualified`,`Q3_traceable`,`S_twin`}, `qualification`, offsets, `services/vision/calibrate.py` · `station.py` · `artifact.py` | MetrologyArchitecture | Referenced by content-addressed `id`; values never copied. **Never an offset source.** Perception derives `advisory` / `A2_station` from device kind and caps there. |
| `MediaSourceRef` camera discovery | AdditiveArchitecture | Consumed as the sole frame-source record. Perception never re-derives discovery. |
| `chudware::twin::VirtualFrame` / `VirtualFrameRequest` (`core/twin/vcamera.h`) | TwinArchitecture | Optional Wave-5 auxiliary supervision. Not a critical-path dependency. |
| Print-project/artifact schemas | AdditiveArchitecture | Consumed by id. Vision-based print-failure detection is **mine**, per Additive's non-goals. |
| `services/vision/**` calibrated primitives | Metrology-owned, hardened in place | **Additive consumption only. Zero signature changes from this slice.** |

Perception advertises exactly one capability level: **`monitor`**, and it advertises it at truth
`declared` — never `verified`, because gateway §24.3(i) flips `monitor` only on fresh `probed`
evidence from an open `ITransport` and this slice holds no transport of any kind. It never advertises
`execute`, `transport` or `safety_integration`, never proposes a device transition, and its RPCs are
read-only. Because it does advertise a level above `unsupported`, it registers the durable kind
`perception.capability` (§4.8) as gateway §24.11(4) requires. Its four read-only MCP tools register
into the single shared **`manufacture`** family (gateway §17), not a family of their own.

---

## 2. Inputs, tick grid, causal windowing

### 2.1 Frame sourcing and lane isolation

Frames arrive only via Additive's `MediaSourceRef` (`{media_id, device_id, kind, stream_url,
snapshot_url, rotation, discovered_from, credentials_ref, verified_at, transport_secure}`). Two
constraints are honoured structurally, not by convention:

- **Media never shares the control lane.** `services/perception/source.py` runs its own bounded pull
  worker with an independent rate budget (default 4 Hz analysis / 2 Hz detector), an explicit
  bytes-per-minute ceiling per `media_id`, and backpressure that drops to snapshot polling before it
  ever competes with control traffic. `transport_secure:false` (e.g. Prusa Buddy3D LAN RTSP) is
  propagated into every packet's provenance and rendered by UX.
- **Synthetic frames are quarantined.** `CameraFrame::synthetic` (`host/src/platform/camera.h` v2) is
  carried through the entire pipeline. A synthetic or twin-sourced frame may never be persisted as a
  capture, may never be attached as evidence, and forces `src:"simulated"` on every derived field.
  `test_synthetic_quarantine.py` asserts the ring buffer refuses to commit a synthetic segment and
  that `packet.build()` raises on a synthetic evidence reference.

### 2.2 Analysis grid

Fixed **4 Hz integer tick grid** (Δ = 250 ms), aligned to the gateway's monotonic tick so perception
ticks and controller ticks join directly. Every stream is *resampled onto* the grid — never
interpolated across a gap. A tick with no fresh sample carries `mask=0`, `value=0`,
`age_ticks=<real age>`. There is no silent imputation anywhere in the pipeline.

### 2.3 Feature groups (per camera `c`, per tick `t`)

| Group | Dim | Source | Contents |
|---|---|---|---|
| `z_img` | 128 | frozen encoder + trained linear projection | MobileNetV3-Small 576-d pooled → `Linear(576→128)` (projection trained from scratch) |
| `z_det` | 48 | YOLOX-Tiny + ByteTrack (deterministic) | per class-slot count, mean/max confidence, bbox area mean/σ, centroid drift, track count, mean track speed, mean dwell, new-track rate, lost-track rate |
| `z_geo` | 24 | `services/vision/fiducial.py`, `frames.blur_score`, Metrology's `calibrate.py`/`station.py` | `px_per_mm`, **`planarity_cov`**, marker count, homography condition number, reprojection RMS, pose delta vs. station extrinsics (mm, deg), `blur_score`, over/under-exposed fractions, ROI coverage, occluded fraction, intrinsics validity, station-profile match |

Per-camera feature width **`Dc = 200`**.

**Fail-closed on unknown planarity.** Metrology is making `Detection.planarity_cov` `Optional`, with
`None` meaning *unknown* rather than today's `0.0`. `0.0` reads as "perfectly in plane", which is
fail-open. `features.py` maps `None` → `mask=0` for that channel **and** raises the camera's health to
`not_calibrated`; it never substitutes a numeric value, never treats unknown as flat, and the
downstream tick is scored `unknown`.

| Group | Dim | Source | Contents |
|---|---|---|---|
| `z_ctl` | 32 | Gateway `ControllerSnapshot` | one-hot machine state, feed/spindle override, commanded vs. reported axis position deltas, planner buffer depth, alarm/limit flags, program line index, run elapsed |
| `z_snr` | ≤32 | synchronized sensors where they exist | spindle current RMS, accel band powers (4), chamber/nozzle/bed temperature, flow, acoustic RMS |
| `z_cmd` | 16 | queued immutable program (future-known) | commanded feed, spindle rpm, per-axis velocity, arc/linear flag, tool-change flag, dwell flag, layer index, Z height, remaining-in-move |

Process width **`Dp = 64`**. Exogenous width **`Dq = 16`**.

`z_cmd` is the C1 input: read **ahead** of `t0`, for ticks `t0+1 … t0+Hmax`, from the approved
artifact already resident in the gateway. When lookahead is unavailable, `cmd_mask=0` and the model
degrades to unconditioned forecasting — reported in `input_health`, never hidden.

Every `FreshValue<T>`-wrapped input is unwrapped by exactly one rule: **`state=="ok"` ⇒ use `v`;
anything else ⇒ `mask=0`.** `last_ok` is never read into a feature. A `seq` gap on a lane flips that
lane's features to `mask=0` for the affected ticks.

### 2.4 Windowing

- Window `W = 64` ticks = **16 s** of context. Stride 4 ticks (1 s) training, 1 tick inference.
- Strictly causal: all operators left-padded; the window's last tick is `t0`; every prediction is for
  `t0 + k`, `k > 0`. Enforced by `tests/test_causality.py`, which perturbs ticks after `t0` and
  asserts bitwise-identical outputs.
- Horizons **`K = {1, 4, 20, 120}` ticks = {0.25 s, 1 s, 5 s, 30 s}**; `Hmax = 120`.
- Camera slots `C = 4`, handled by per-slot embedding + gated pooling; absent slots masked.

---

## 3. Model tensor contract

`feature_spec_id` = sha256 over the ordered feature specification, stamped into every checkpoint,
ONNX graph and evidence packet. A mismatch between packet and running extractor is refused at load,
never coerced.

### 3.1 Forward signature

```
INPUTS
  cam_feat    f32 [B, C=4, W=64, Dc=200]   per-camera per-tick features
  cam_mask    u8  [B, 4, 64]               1 = a real, non-synthetic frame backed this tick
  cam_age     f32 [B, 4, 64]               ticks since last real frame / 32.0, clamped [0,4]
  proc_feat   f32 [B, 64, Dp=64]           controller + sensor features
  proc_mask   u8  [B, 64, 64]              per-field validity
  proc_age    f32 [B, 64, 64]              per-field staleness, same normalisation
  cmd_future  f32 [B, Hmax=120, Dq=16]     known commanded trajectory t0+1..t0+120
  cmd_mask    u8  [B, 120]                 1 = program lookahead available for that tick

OUTPUTS
  z_hat        f32 [B, K=4, C=4, Dz=128]   forecast latent mean
  z_logvar     f32 [B, 4, 4, 128]          heteroscedastic log-variance (floored at log 1e-4)
  snr_hat      f32 [B, 4, S=32]            sensor forecast mean
  snr_logvar   f32 [B, 4, 32]
  state_logit  f32 [B, 4, Ns=24]           multi-label observable process state, per horizon
  anomaly      f32 [B]                     supervised anomaly logit
  event_bnd    f32 [B, 64, 3]              per-tick {start, inside, end} logits
  importance   f32 [B]                     event importance in [0,1] (sigmoid)
  cam_rel      f32 [B, 4]                  camera relevance logits (softmax over usable slots)
  retain       f32 [B, 64]                 per-tick retention utility logit
```

Everything else — running surprise, the hysteresis state machine, the byte budget, the packet, the
`FreshValue` wrapping — is **deterministic host code outside the graph**. The ONNX graph is a pure
function with fully static shapes.

### 3.2 Derived scalars (host, deterministic)

At tick `t` the runtime compares the forecast issued at `t−k` against the latent now observed:

```
nll_k(t) = 0.5 * mean_d [ (z_obs[d] − z_hat_k[d])^2 / exp(z_logvar_k[d]) + z_logvar_k[d] ]
surprise_raw(t) = Σ_k w_k · nll_k(t),   w = {0.10, 0.20, 0.35, 0.35}
surprise_q(t)   = F̂_{machine,state}( surprise_raw(t) )
```

`surprise_q ∈ [0,1]` is the only surprise number that leaves the service; the raw NLL travels in the
packet for forensics. `F̂` is a fixed-size P² quantile sketch per (machine, coarse state), cold for the
first `N_warm = 4096` ticks — during which `surprise_q` is emitted as
`{v:null, state:"unknown", why:"Surprise calibration is still warming up on this machine."}`, **not**
`0.5`.

### 3.3 Trunk (train-from-scratch, ~3.1 M parameters)

```
per-camera:  Linear(200→192) → LayerNorm → GELU      [masked ticks zeroed; mask+age concatenated]
camera fuse: gate_c(t) = softmax_c( w·[health_c(t), cam_age, relevance_prior] )
             u_cam(t)  = Σ_c gate_c(t) · h_c(t)                        → 192
process:     Linear(64→64) → LayerNorm → GELU
tokens:      concat → 256 per tick, W=64 tokens
trunk:       6 × CausalMixerBlock
               time-mix : depthwise causal conv-MLP over W, dilation d ∈ {1,2,4,8,16,32}
                          (left-padded; receptive field 64 ticks = the full window)
               chan-mix : Linear(256→512) → GELU → Linear(512→256)
               pre-LayerNorm, residual on both
exog:        cmd_future → Conv1d stack → per-horizon FiLM (γ_k, β_k) ∈ R^256 ×2
pool:        h = LayerNorm( h_last_tick + masked_mean(h) )              → 256
heads:       forecast_k : FiLM_k(h) → Linear(256→2·128) per camera slot
             others     : shallow MLPs off h (event_bnd off the token sequence)
```

Chosen over attention deliberately: at `W=64`, dilated causal mixing reaches the full window at a
fraction of the cost, exports to ONNX with **static shapes** (no dynamic-axis or attention-mask export
traps), and yields a trivially portable numpy reference (§8.3).

Optional companion (declared ablation, not default): a 2–8 M-parameter convolutional normality
autoencoder over `z_img`, whose residual fuses with `surprise_q`. Benchmarked *against* Anomalib
PatchCore/PaDiM, not assumed superior.

### 3.4 From scratch vs. reused

**From scratch (the contribution):** all input projections; mask/age encoding; camera gating and the
relevance head; the causal mixer trunk; the FiLM exogenous conditioner; every forecast head; state /
anomaly / event-boundary / importance / retention heads; heteroscedastic variance heads; the post-hoc
calibrator; the optional normality autoencoder.

**Reused, frozen (or lightly tuned as a separate job):** MobileNetV3-Small or EfficientNet-B0
(torchvision, BSD-3-Clause code); YOLOX-Tiny (Apache-2.0) or upstream RT-DETR-R18 (Apache-2.0);
RAFT-Small ROI-only, off by default.

**Never trained:** ByteTrack association (MIT); ChArUco/ArUco detection, intrinsics and the px→mm
homography (`services/vision/fiducial.py` + Metrology's `calibrate.py`/`station.py`, OpenCV
Apache-2.0); `blur_score`; clip encoding.

---

## 4. Event scoring, retention, evidence — deterministic host code

### 4.1 Health, six distinguishable operator sentences

`mfg.perception_frame_health` per camera per second. UX asked to distinguish five conditions and a
sixth follows from `GrabResult::corrupt`; each is a distinct `state`/`why` pair on the camera's
`FreshValue`, never collapsed:

| Condition | `state` | `src` | `why` (operator sentence) | Overlay effect |
|---|---|---|---|---|
| camera not present | `unsupported` | `declared` | "No camera is configured for this device." | no overlay |
| not calibrated | `unknown` | `advisory` | "This camera has no station calibration, so nothing can be measured or overlaid." | overlay suppressed |
| **calibration expired** | `stale` | `advisory` | "Camera calibration expired on <date>; existing overlays are no longer valid." | **any painted advisory overlay is invalidated and cleared, not left on screen** |
| stream stale | `stale` | `advisory` | "No frame received for <age> s." | overlay demoted, `last_ok` shown demoted |
| frames OK, model unavailable | `unknown` | `advisory` | "Frames are arriving but the perception model is not loaded, so nothing is being assessed." | frames render, no advisory content |
| **corrupt frames** | `unknown` | `advisory` | "This camera is returning corrupt frames; nothing on screen reflects it." | overlay suppressed |

The sixth row lands with `GrabResult::corrupt` in `host/src/platform/camera.h` (UX's Wave-1 item).
Today a corrupt V4L2 buffer returns `again`, so a broken camera presents as "no frame yet" — the most
reassuring possible rendering of a hardware failure. `source.py` must map `corrupt` to its own state
the moment the enum exists, and must never fold it into stream-stale.

Group age budgets before scoring: `cam` 8 ticks, `ctl` 4, `snr` 12, `cmd` 40. Any **required** group
over budget ⇒ the tick's process state is `unknown`, scoring suppressed. There is no code path that
produces "normal" from missing data, and `v` is null whenever `state != "ok"` — `last_ok` carries the
demoted value and is never gated on.

#### One invalidation event, `class` + `reason` — and a separate lifecycle event

A `stale` flag says "this may be old"; it does not say "what is on screen right now is wrong". Per
UX's producer obligation, every retroactive path emits an explicit push rather than waiting for the
consumer's next poll. An earlier draft of this slice split that push across three event names
(`mfg.perception_invalidated` / `_superseded` / `_evidence`). **That name split is withdrawn.** The
review's C3 finding is correct and decides it: the consuming dispatch point is the host lane drain,
and a name-keyed consumer there silently drops any event name added later — the exact failure mode
this slice cites elsewhere as the thing to avoid. DoD 9 already carries the distinction *inside* the
envelope, so the distinction belongs there and nowhere else.

**One invalidation event: `mfg.perception.invalidated`**, carrying the DoD 9 / gateway §5 envelope
verbatim — `{class:"falsified"|"superseded", reason, why, affected:[ids], at_seq, superseded_by?}`.
Consumers branch on `class`, never on an event-name table. `class` carries alarm-vs-quiet; `reason`
carries the per-trigger distinction; `superseded_by` is mandatory when `class=="superseded"` and
absent otherwise. The cry-wolf concern that motivated the earlier split is fully served by `class`: a
`superseded` message is a quiet row update, only `falsified` stops an operator, and an event added
later that forgets to set `class` fails immediately instead of falling silently outside a
name-matching table.

`affected[]` is **the closure the gateway computed**, never this slice's guess (gateway §24.1):
perception supplies `derived_from[]` edges on every `mfg_event_append` and names only the root
subject on `mfg_result_invalidate`.

**`class:"falsified"`** — the painted value is *wrong* and must be cleared.

| `reason` | Trigger | What the GUI must clear |
|---|---|---|
| `calibration_expired` | station calibration passed `expires_at` | every advisory overlay derived from that station profile |
| `qualification_withdrawn` | an inbound Metrology qualification change (event name owned by Metrology), or `qualification.state` leaves `live` | same, scoped to that profile |
| `config_invalid` | `feature_spec_id` mismatch against the live extractor, failed/corrupt model load, or a calibration discovered to have been fitted on the wrong split | every painted score produced under that configuration |
| `camera_identity_changed` | a camera's `MediaSourceRef` is withdrawn or its `media_id` rebound | every surface attributed to that camera — it may now be a *different* physical camera |
| `frames_were_corrupt` | `GrabResult::corrupt` retroactively covers frames already scored | scores derived from the corrupt span |
| `event_falsified` | a packet is corrected because its scores were wrong | the event card, cleared not replaced |

**`class:"superseded"`**, always carrying `superseded_by` — the prior value *remains true* about what
it described; it is simply no longer current. A row update, never an alarm.

| `reason` | Trigger |
|---|---|
| `newer_model` | a newer `model_id` deployed; prior scores stay honest statements about what the prior model saw |
| `newer_calibration` | a newer `calibration_id` / `thresholds_id` fitted |
| `refined_event` | a packet re-emitted with more data, its predecessor still accurate |

**Routine lifecycle is not an invalidation.** `mfg.perception_evidence`, `action` ∈
{`clip_committed`, `evidence_expired`}, stays a separate lifecycle event and carries **no**
invalidation envelope and no `class`. Ring eviction of an un-referenced clip is *expected behaviour on
a 90 s buffer*, not news: it marks the link unresolvable and never reaches
`mfg.perception.invalidated` under either `class`. Routing a scheduled eviction onto the invalidation
event would be the clearest possible way to train operators to ignore invalidations.

**Seq-scoping (binding, DoD 9 third sentence).** Every `mfg.perception.invalidated` message carries
`at_seq`, and every result this slice publishes — packets, painted scores, overlays — carries the
`established_seq` at which it was established (Gateway stamps `seq` at durable-append time). A
message applies **only to results with `established_seq <= at_seq`**. This is not bookkeeping: UX
found, and ExecutionPlanner verified in source, that `st.rpc.drain()` runs earlier in the frame than
the event tick (`host/src/core/engine_events.h:3`, `engine_events.cpp:907-909`, call site
`host/src/app.cpp:3859`), so within a single frame an RPC response is applied *before* a lane
invalidation caused by that same call — and an unscoped invalidation would clear the very result the
call legitimately produced. Three consequences this slice takes advantage of: **no synchronous hold
is required anywhere** for invalidation ordering (emit-before-lapse is a quality-of-service goal, not
a correctness dependency); **replay is idempotent**, so a re-delivered invalidation is a safe no-op;
and **crash-recovery drain becomes safe** rather than dangerous.

`test_invalidation_paths.py` enumerates every trigger above and asserts five things: exactly **one**
event name exists for invalidation, so no name-keyed dispatch is possible; each trigger sets the
**correct `class`**, and no quiet or lifecycle condition can reach `class:"falsified"`; a routine ring
eviction reaches `mfg.perception_evidence` only and never the invalidation event; application is
seq-scoped, so an invalidation at `at_seq = N` leaves a result with `established_seq = N+1`
untouched; and replaying the same invalidation twice changes nothing.

#### Freshness admits; it does not approve — and no unfireable guards

Perception fields are `src:"advisory"`, so they fail UX's **Stage 1**
(`state=="ok" && src=="probed"`, necessary but never sufficient) by construction and never reach
domain evaluation at all. This slice therefore defines **no Stage-2 guard**, and it **never presents a
Stage-1 pass as a verdict** anywhere in the brief or the GUI payload. A perception finding may raise an
attention item and may be attached as supporting context; it can never be phrased, rendered or
serialized as an approvable result.

**Guard audit, run against the canonical test.** The test is: *could a value that FAILS this guard
ever have been ADMITTED by stage 1?* If not, the guard is dead. Result for this slice: **zero
stage-2 guards, so the list is trivially clean** — but the audit did retire one. An earlier draft
carried an `evidence_class_is_advisory` validation guard; it is **deleted**, and it is precisely the
dead class UX identified, because `evidence_class` maps 1:1 onto `src` (`advisory↔advisory`,
`simulated↔simulated`, `qualified↔probed`), so anything not advisory had already failed stage 1. It
was doubly dead here: `evidence_class` and `accuracy_tier` are *derived* from the source device kind
inside `packet.build()`, making a `qualified` perception packet unconstructible rather than merely
rejected. Structural impossibility beats a check, and the check must then go rather than linger as
decoration diluting the named-guard rendering that stage 2 depends on.

**Durable-result re-establishment.** Four things this slice publishes are durable results a consumer
may still be painting across a process restart, and per-field lane-epoch handling structurally cannot
cover them: committed `aicad.perception_event/1` packets, `EvidenceClip` rows, the deployed
model/calibration binding, and the capability row. All four are registered durable kinds with an
`epoch_invariant` epoch policy (§4.8), which is what makes Gateway's process-start sweep able to
re-establish them as-is rather than clear or re-derive them, and all four take their
`established_seq` from `mfg_event_append` so seq-scoping stays sound across the restart. Both
obligations are named here as inbound dependencies rather than assumed.

### 4.2 Eventizer (`services/perception/eventizer.py`)

A pure state machine over `(surprise_q, anomaly, event_bnd)`: dual enter/exit thresholds, minimum
dwell (8 ticks), refractory (40 ticks), apex = argmax `surprise_q` in the interval. No tensor touches
this file; it is unit-tested against hand-written score traces. Every threshold lives in `config.py`
and is stamped into the packet as `thresholds_id`.

### 4.3 Rolling media buffer (`services/perception/ringbuf.py`)

Per camera: a **90 s default** (configurable 30–300 s) ring of GOP-aligned 2 s encoded segments plus a
4 Hz thumbnail ring. Nothing reaches long-term storage until an event commits. On commit the retention
policy exports `[t_start − pre_roll(8 s), t_end + post_roll(4 s)]` intersected with what the ring
still holds; **if the span has aged out, the record carries `truncated:true` with the actual span** —
a short clip is never presented as complete. Synthetic segments are refused at insert. No continuous
recording by default; enabling it is an explicit, logged configuration change.

### 4.4 Retention policy (`services/perception/retention.py`)

`retain[t]` plus a hard bytes-per-camera-hour budget. Ticks are admitted in descending utility until
the budget is met; labelled-event ticks are admitted unconditionally. Realised bytes/camera-hour and
the achieved retention–recall operating point are emitted as telemetry, so the C2 claim is
continuously measured in production rather than asserted once at training time.

### 4.5 Evidence clip record — `EvidenceClip` (this slice owns it)

Additive offered the clip record; **I take it**, because the ring buffer, the retention decision and
the truncation truth all live here. It carries Additive's identity fields verbatim so a print run's
evidence stays traceable:

```jsonc
{ "result_kind": "perception.evidence_clip",   // registered durable kind (§4.8)
  "clip_id": "...", "run_id": "...", "device_id": "...", "trigger_event_id": "...",
  "t_start": <tick>, "t_end": <tick>, "sha256": "...", "retention_class": "...",
  "truncated": false, "media_id": "...", "transport_secure": false,
  "synthetic": false,                       // always false — synthetic never becomes evidence
  "bytes": 2148231, "codec": "h264",
  "established_seq": 40119 }                // returned by mfg_event_append; never minted here
```

`clip_id` is content-addressed (sha256 of the encoded bytes + canonical header) and immutable;
corrections emit a new record with `supersedes`. The row is journaled through `mfg_event_append`
(§4.8), and the fields a UI paints are published wrapped in `FreshValue<T>` with `state:"ok"`,
`ttl_ms:null` and `src:"advisory"` — a committed clip is a frozen fact and does not decay on a wall
clock (review C8). Ring eviction retires the *link*, not the row, and travels on
`mfg.perception_evidence`.

### 4.6 Evidence packet — `aicad.perception_event/1`

Emitted as `mfg.perception_event` and journaled as durable kind `perception.event` (§4.8). The payload
is canonical JSON (sorted keys, fixed float precision) and its **sha256 is the packet identity**.
Amendment is impossible: a correction is a new `event_id` carrying `supersedes`, and the prior row is
retired by a single `mfg.perception.invalidated` — `class:"superseded"` with `superseded_by` when it
is merely no longer current, `class:"falsified"` when its scores were wrong. Every packet carries the
`established_seq` returned by `mfg_event_append`, so invalidation is seq-scoped and no `seq` is ever
minted here. Every painted scalar is a `FreshValue<T>` — no perception-local envelope, no second
vocabulary — and on a committed packet those wrappers are no-decay: `state:"ok"`, `ttl_ms:null` (C8).

```jsonc
{
  "schema": "aicad.perception_event/1",
  "event_id": "<uuidv7>",
  "packet_sha256": "<hex>",
  "supersedes": null,               // on a correction the prior row is retired by mfg.perception.invalidated
  "established_seq": 40122,         // Gateway-stamped; an invalidation applies only when at_seq >= this

  "device_id": "...", "run_id": "...", "job_revision": "...",
  "artifact_sha256": "...", "approval_id": null,     // never bound into an approval

  // Metrology-owned vocabularies, bound verbatim. NEITHER is a constructor parameter here:
  // packet.build() DERIVES both from the source device kind, mirroring Metrology's own
  // device-kind table (camera -> "advisory"). There is no caller-supplied string to bypass,
  // so "construction cannot produce qualified" is structurally true rather than validated.
  "evidence_class": "advisory",     // enum: "qualified" | "advisory" | "simulated" — exactly these three
  "accuracy_tier":  "A2_station",   // ladder: A0_indicative, A1_scaled, A2_station,
                                    //         Q0_unqualified, Q1_repeatable, Q2_qualified,
                                    //         Q3_traceable, S_twin. Perception caps at A2_station.
  "qualification": {                // READ from the station profile, never synthesized
    "id": "...", "kind": "station_profile",
    "established_at": "...", "expires_at": "...",
    "state": "live"                 // "live" | "expired" | "superseded"
  },
  "capability": "monitor",

  "camera_ids": ["cam-a", "cam-b"],
  "interval": { "tick_start": 918102, "tick_apex": 918220, "tick_end": 918342 },
  "kind": "process_deviation | state_change | view_degraded | forecast_divergence",

  "scores": {
    "surprise_q":  { "v": 0.94, "state": "ok", "t_mono_ns": 91834200000000, "seq": 40122,
                     // ttl_ms is null on a committed packet: a durable row is published wrapped but
                     // does not decay on a wall clock (C8). The live per-tick score lane keeps a
                     // real ttl_ms; this is the frozen historical statement.
                     "age_ms": 240, "ttl_ms": null, "src": "advisory",
                     "why": "" },
    "anomaly":     { "v": 0.81, "state": "ok", "...": "...", "src": "advisory" },
    "importance":  { "v": 0.62, "state": "ok", "...": "...", "src": "advisory" },
    "surprise_raw_nll": 12.7,
    "uncertainty": { "predictive_sigma": 0.31, "calibration_id": "cal-2026-08-01-a" }
  },

  "state_observations": [
    { "label": "part_detached", "horizon_ticks": 4,
      "prob": { "v": 0.88, "state": "ok", "src": "advisory", "...": "..." },
      "calibrated": true }
  ],

  "camera_relevance": [
    { "camera_id": "cam-a",
      "relevance": { "v": 0.71, "state": "ok", "src": "advisory", "...": "..." } },
    { "camera_id": "cam-b",
      "relevance": { "v": null, "state": "unknown", "src": "advisory",
                     "why": "No frame received from cam-b for 6 s.",
                     "last_ok": { "v": 0.29, "t_mono_ns": 91831000000000, "seq": 40098 } } }
  ],

  "attribution": [ { "group": "z_snr.accel_band_2", "delta_nll": 4.1 } ],   // masked ablation

  "input_health": [
    { "group": "cam:cam-a", "state": "ok",          "age_ms": 250 },
    { "group": "cam:cam-b", "state": "stale",       "age_ms": 6000,
      "why": "No frame received for 6 s." },
    { "group": "ctl",       "state": "ok",          "age_ms": 0 },
    { "group": "snr",       "state": "unsupported", "why": "No sensor lane on this machine." },
    { "group": "cmd",       "state": "stale",       "age_ms": 15250,
      "why": "Program lookahead is 15 s behind; forecasts are unconditioned." }
  ],

  "evidence": [ { "clip_id": "...", "role": "interval" },
                { "kind": "frame", "role": "apex", "uri": "...", "sha256": "...",
                  "tick": 918220, "synthetic": false } ],

  // MeasurementObservation `id` strings, format "sha256:<hex>", schema aicad.metrology_observation/1.
  // Referenced only — a measured value is never copied into this packet and an offset is never proposed.
  "metrology_refs": ["sha256:9f2c…"],

  "provenance": {
    "model_id": "mldp-v1", "model_sha256": "...", "feature_spec_id": "...",
    "calibration_id": "cal-2026-08-01-a", "thresholds_id": "thr-v1",
    "encoder_id": "mnv3s-owned-v2", "detector_id": "yolox-tiny-ft-3",
    "station_profile_id": "...", "intrinsics_id": "...",
    "media_id": "...", "transport_secure": false,
    "runtime": "onnxruntime-1.22/CUDAExecutionProvider", "gpu": "RTX 2080"
  }
}
```

Companion events: `mfg.perception_frame_health` (§4.1, live telemetry lane),
`mfg.perception.invalidated` (the single invalidation event, `class` + `reason`) and
`mfg.perception_evidence` (routine lifecycle, carrying no invalidation envelope).

### 4.7 Downstream LLM grounding (`services/perception/grounding.py`)

The LLM sees **committed packets only** — never raw scores, never a live tensor, never a frame absent
from some packet's `evidence[]`. `build_brief(run_id, window)` returns a token-budgeted structure with
`facts[]` (each with `event_id`, calibrated probability, `evidence_class:"advisory"`, and input
health), `unknown[]` (explicitly enumerated: which groups were stale/missing/unsupported and for how
long, using the §4.1 sentences), and `evidence_index[]`. Rules:

1. Every factual sentence cites ≥1 `event_id`. `verify_citations(text, brief)` deterministically
   rejects uncited claims; the summary is regenerated or degraded, never shipped.
2. A fact whose `state != "ok"` is rendered as unknown with its `why`. `last_ok` is never narrated as
   current.
3. Perception facts are labelled advisory in the brief text; a perception fact may never be phrased as
   a measurement, and Metrology's `HONESTY_STRING` posture is mirrored, never overridden or re-tiered.
4. The brief carries no action verbs and no perception fact may justify a device command.

MCP surface — **shared family `manufacture`**, all read-risk, registered
`register_perception_tools(_reg.view(mcp, "manufacture"))` exactly as every other domain in the
overhaul (gateway §17, Main's final ruling): `mfg_perception_brief`, `mfg_perception_event_get`,
`mfg_perception_evidence_get`, `mfg_perception_health`. **No mutating tool exists.** The earlier
`perception` family is deleted, not deprecated: review B11 establishes that an unlisted family
defaults **on**, so a private family would have shipped enabled. Per-tool granularity comes from the
existing `get_tool_options` toggles, as it does for every sibling.

### 4.8 Durable-result kinds — submitted for W1.1 (closes review B10 for this slice)

Gateway §24.3(iii) refuses every append of an unregistered kind with `mfg_unregistered_result_kind`,
and §24.11(4) refuses a domain advertising any level above `unsupported` without a
`<domain>.capability` kind (`mfg_capability_requires_durable_kind`). This slice appends four kinds and
advertises `monitor`, so all four are submitted here with **both** §24.1 columns — (a) epoch policy
and (b) runtime invalidation triggers.

| result kind | what it is | (a) epoch policy | (b) runtime invalidation triggers |
|---|---|---|---|
| `perception.event` | committed `aicad.perception_event/1` packet (§4.6) | `epoch_invariant`; `src:"advisory"` permanently | falsify on `event_falsified`, `config_invalid`, `frames_were_corrupt`, `camera_identity_changed`, `calibration_expired` or `qualification_withdrawn` covering its interval; supersede on `refined_event`, `newer_model`, `newer_calibration`; `supersede_with_parent` **false** — a committed packet is a record of what was observed, not a live claim (§24.7, second half) |
| `perception.evidence_clip` | `EvidenceClip` row (§4.5) | `epoch_invariant` | falsify on content-hash mismatch at read-back or on `camera_identity_changed`; supersede on a correction carrying `supersedes`; `supersede_with_parent` **false** (shipped-artifact rule, §24.7). Ring eviction is **not** an invalidation: it emits `mfg.perception_evidence action:"evidence_expired"` |
| `perception.model_export` | the deployed model/calibration binding a packet cites: `model_id`, `model_sha256`, `feature_spec_id`, `calibration_id`, `thresholds_id`, `encoder_id`, `detector_id`, licence refs (§8.2 stages 6-8) | `epoch_invariant` | falsify on parity-gate failure, on `feature_spec_id` mismatch against the live extractor, or on a failed/corrupt load; supersede on a newer export for the same `feature_spec_id`; `supersede_with_parent` **false** |
| `perception.capability` | the `CapabilityReport` this slice's surface paints (§1) | `epoch_invariant`; `supersede_with_parent` **false** (§24.11(2)) | falsify when any cited `derived_from` row is falsified; supersede on re-derivation from newer evidence. `derived_from = [perception.model_export, station-profile ref, MediaSourceRef]` |

All four are `epoch_invariant` and none carries a `falsify` epoch policy. That follows from §24.2's
general rule rather than being a concession: perception holds **no** transport at all — not even
§24.5's read-only slot — so it owns no transport-bound or physically time-bound evidence, and its
durable rows survive a gateway restart intact. Should this slice ever gain a probed transport it must
re-declare every kind, and the contract test fails until it does.

**Capability truth, and why `verified` is unreachable.** Gateway §24.3(i) permits `monitor` to become
`verified` only on fresh `probed` evidence from an open `ITransport`, and never on a
`procedural_fact`, `advisory`, `simulated`, `declared` or `assumed` row. Perception holds no
`ITransport` and every field it produces is `src:"advisory"`, so its `monitor` level has exactly two
reachable truths: **`declared`** — a parity-passed `perception.model_export` row and a `live` station
profile both exist and are cited in `derived_from` — and **`unsupported` with a `why`** ("No
perception model is loaded on this device.") otherwise. `plan`, `generate`, `post`, `parse`,
`simulate`, `transport`, `execute` and `safety_integration` are permanently `unsupported`, each with
its own `why`. This slice registers no `procedural_fact` kind and defines no path that raises
`monitor` above `declared`; registering a durable capability kind changes what is *journaled*, not
what is *claimed*, so the advisory-only monitor posture of §1 and §13 is untouched. `CapabilityTruth`
is composed verbatim from `core/device/capability.h`; this slice defines no second capability shape
and never uses `probed` as a truth value.

**Every painted `seq` is journaled (DoD 10).** Perception mints, guesses, derives and forwards **no**
`seq`. Each of the four kinds obtains its `seq` from `mfg_event_append`, and that returned value *is*
the row's `established_seq` (§4.1 seq-scoping). There is no perception-local durable store, no
ledger, and no file-local ordinal anywhere in this slice. The one non-journaled surface is the live
per-second `mfg.perception_frame_health` lane, which is telemetry stamped by the gateway republisher
and lane-epoch handled; it is deliberately not a durable kind because nothing durable is painted from
it.

**Durable rows are published wrapped, with no wall-clock decay (closes review C8).** Every row above
is journaled **and** published with each painted field wrapped in UX's `FreshValue<T>`: `state:"ok"`,
`ttl_ms:null`, `src:"advisory"`, `seq` = the row's `established_seq`. A committed packet, clip, export
record or capability row is a frozen statement that does not age out on a clock — it changes only by
falsification or supersession, both of which arrive on the one `mfg.perception.invalidated` event.
Only the live lanes (per-tick scores, frame health) carry a real `ttl_ms` and can go `stale` by age.
There is no third option: a durable row is never published unwrapped, and never published with a
wall-clock TTL.

**Publication is a locked product choice, not an open question.** DoD v4 states it as a product
choice, so this slice implements it rather than deferring it: **the MLDP model/training core is
published** — the `training/perception/` model, losses, splits, calibration and evaluation code of
§10.1, the deterministic numpy reference path of §8.3, the feature specification behind
`feature_spec_id`, and the synthetic fixtures (the 20-window smoke corpus of §12, the locked parity
fixture of §8.3, and Kubric/Twin `appearance_domain:"geometry_only"` synthetics of §5.3) — together
with an **exportable benchmark subset** of §5.2's `test_locked` split and the §7 result battery,
*where licensing and consent permit*: a manifest record may enter the published subset only when
§5.1's `license` ∈ {`owned`, `synthetic`}, `consent_ref` is present, `export_restricted` is false and
`redaction_applied` holds. `test_license_manifest.py` extends to that predicate, so an unexportable
record cannot leak into a published benchmark any more than an unlicensed weight can enter the
product tree (§9).

**Commercial-user data and integrations stay private by default.** Recorded production frames,
`EvidenceClip` bytes, controller/sensor logs, programs, per-machine calibrations, thresholds and
trained per-site weights are **local by default**; leaving the site requires an explicit, logged
opt-in export, and no default path ships them. That is the reason §5.1 carries `license_ref`,
`consent_ref`, `operator_id_hash`, `redaction_applied`, `retention_class` and `export_restricted` at
all: they are the gate on the published subset, not decoration.

`perception.model_export` is orthogonal to both halves and unaffected by either: the kind journals
*which* export is deployed on a machine so a painted score cannot outlive its model, and carries no
publication semantics.

---

## 5. Dataset, splits, provenance

### 5.1 Manifest record (`training/perception/manifest.py`, validated schema)

Asset: `clip_id`, `uri`, `sha256`, `bytes`, `codec`, `resolution`, `fps_nominal`, `fps_measured`,
`duration_s`.
Machine/job: `device_id`, `machine_serial`, `controller_kind`, `run_id`, `job_id`, `job_revision`,
`artifact_sha256`, `program_sha256`, `material_id`, `tool_ids[]`, `fixture_id`, `wcs_id`,
`process_family` (`cnc_mill` | `fdm`).
Camera/optics: `media_id`, `camera_id`, `camera_serial`, `station_profile_id`, `intrinsics_id`,
`extrinsics_id`, `calibration_date`, `calibration_valid_until`, `mount_state`
(`fixed` | `moved` | `unknown`), `lighting_id`, `exposure_mode`, `transport_secure`.
Time alignment: `start_wall_utc`, `start_tick`, `clock_source`, `clock_error_ms`, `tick_grid_hz` (4),
`alignment_method`.
Sidecars: `controller_log_uri`+sha256, `sensor_log_uri`+sha256, `program_uri`+sha256.
Rights: `license` (`owned` | `licensed` | `synthetic` | `third_party`), `license_ref`, `consent_ref`,
`operator_id_hash`, `redaction_applied`, `retention_class`, `export_restricted`.
Annotation: `annotation_pass`, `annotator_ids[]`, `double_annotated`, `adjudicated`, `iaa_kappa`,
`label_schema_version`.
Synthetic: `synthetic` (bool), `synthetic_source` (`null`|`kubric`|`twin`), `twin_snapshot_id`,
`twin_frame_hash`, `appearance_domain` (`real`|`geometry_only`).
Split: `split_group`, `split` (`train`|`calib`|`test_locked`), `locked_at`.

### 5.2 Splitting

`split_group = sha1(machine_serial | job_id | calendar_day)`. Grouped split by that key — never by
frame, never by window. The locked test set additionally holds out **entirely** ≥1 camera id, ≥1
material and ≥1 calendar week, so the reported number answers "new day, new camera, new material" —
the only question deployment asks. `calib` is disjoint and used solely for temperature/variance
scaling and threshold selection; touching it during training is a test failure.
`test_split_leakage.py` asserts group-disjointness, that no window crosses a split boundary, that
`synthetic:true` records appear only in `train`, and that `test_locked` is real-only.

### 5.3 Staged data requirements

| Stage | Needs | Buys | Annotation cost |
|---|---|---|---|
| **S0 — bootstrap** | 25–50 h ordinary recorded production video + gateway controller logs + queued programs, 1 machine | C1 forecasting, C2 retention prior, C3 relevance, surprise calibration | **zero** — the future is the label |
| **S1 — events** | 50–100 complete runs with boundaries and observable-state labels; ~50–150 k windows | state heads, event boundaries, supervised retention | boundary + multi-label, ~2 passes |
| **S2 — positives** | 50–100 distinct real deviation episodes (pilot); 100–300 before any product recall claim | anomaly head, lead-time claims, per-mode reporting | episode tagging + mode taxonomy |
| **S3 — detector** | 10–20 k real annotated frames / 50–100 k boxes, +20–80 k synthetic (Kubric) for rare pose/occlusion | `z_det` quality, presence/count claims | box annotation (CVAT self-hosted, MIT core) |
| **S4 — twin (optional)** | `VirtualFrame` depth + per-pixel `label` at exact ticks | free geometry-truth auxiliaries, ground-truth extrinsics for the relevance head, deterministic replay fixtures keyed on `frame_hash` | zero |

**S0 unblocks the core with recorded real frames alone**, which is what keeps this slice off Twin's
critical path. Per Twin's own declaration, `VirtualFrame.gray` is flat-shaded and non-photorealistic:
usable for geometry-truth auxiliary supervision and for exercising the ChArUco path against known
extrinsics, and **barred from appearance-domain training** by manifest assertion
(`appearance_domain == "geometry_only"` records cannot enter the `z_img` encoder job). Twin-derived
frames are `synthetic:true` end to end and can never become evidence.

---

## 6. Resource envelope

Capacity facts: RTX 3080 Ti = 12 GB (primary trainer), RTX 2080 = 8 GB (inference / rendering /
pre-labelling / small independent experiments). **Explicitly not** heterogeneous DDP; explicitly not
full RT-DETR training. Jobs run sequentially; peak VRAM is the max over jobs, not the sum.

### 6.1 Training (3080 Ti, 12 GB)

| Job | Precision / batch | VRAM | Wall time |
|---|---|---|---|
| Feature extraction (frozen encoder, 4 Hz over 25–50 h ⇒ 360–720 k frames) | FP16, B=128, 224 px | 2–3 GB | GPU ≈ 5–15 min; **decode-bound ≈ 1–3 h wall** |
| **MLDP core** (3.1 M params, cached shards, 50–150 k windows) | AMP, B=256 | **1.5–3 GB** | **40–90 s/epoch; 100 epochs < 3 h** |
| Encoder light tune / owned-data SSL (MobileNetV3-S, EffNet-B0) | AMP, B=32–64, 224–320 px | 3–7 GB | 4–12 GPU-h |
| Normality AE (2–8 M) | AMP, B=64 | 3–6 GB | 2–6 GPU-h |
| YOLOX-Tiny fine-tune (416–640 px) | AMP, B=8–16 | 5–8 GB | 6–18 GPU-h |
| RT-DETR-R18 (only if small-object accuracy demands it) | AMP, B=2–4 + grad-accum | 9–12 GB | 12–36 GPU-h |

The architecture's point is visible in that table: the *novel* component is the cheapest thing in it.
Caching frozen-encoder embeddings to `.npy` shards turns MLDP iteration into a minutes-long loop,
which is what makes the §7.3 ablation grid actually runnable on one desktop GPU.

### 6.2 Inference (2080, 8 GB) — the minimum deployment target

Encoder at 4 Hz per camera, detector at 2 Hz, MLDP at 4 Hz, RAFT-Small off by default (~0.99 M params
but ~47.7 GFLOP — ROI-only when enabled).

| Config | VRAM | p95 end-to-end |
|---|---|---|
| 1 camera, encoder + MLDP | 1.0–1.8 GB | < 35 ms |
| 1 camera, + YOLOX-Tiny @2 Hz | 2.0–2.5 GB | < 60 ms |
| 4 cameras, + YOLOX-Tiny @2 Hz | **4.5–6.0 GB** | **< 100 ms** |

Gates: p95 < 100 ms, peak < 6 GB, 24-h soak with no VRAM growth and no throttle-induced deadline miss.
Overrun ⇒ shed detector rate, then camera slots, and **report the shed as degraded health** — never a
silent dropped tick.

---

## 7. Baselines, metrics, ablations

### 7.1 Baselines (beat them or withdraw the claim)

Forecasting: persistence (`ẑ_{t+k}=z_t`), EWMA, linear AR(4), **vision-only MLDP (C1 ablated)**.
Anomaly: frame-difference threshold, motion energy, normality AE, Anomalib PatchCore/PaDiM (dev-only,
out-of-tree). Retention: continuous H.264, fixed-interval sampling, motion-triggered recording,
supervised-event-detector-only (C2 ablated). Relevance: static camera priority, sharpness ranking.

### 7.2 Metrics

**C1** — per-horizon NLL and CRPS on `test_locked`; PIT histogram; variance calibration; ECE. Target:
beat persistence at every horizon, and beat vision-only MLDP at `k ≥ 20` — the horizons where knowing
the commanded future should matter. That comparison *is* the C1 claim and is reported even when it fails.

**C2** — retention–recall curve (bytes retained vs. labelled event intervals retained) and its AUC vs.
all four retention baselines. Operating point: ≥ 95 % event recall, ≤ 1 false event/camera-hour,
≤ 2 s median boundary error, ≥ 90 % byte reduction vs. the site's continuous H.264 baseline while
retaining ≥ 95 % of labelled event intervals.

**C3** — Spearman ρ ≥ 0.7 between predicted relevance and measured LOCO NLL delta; bad-view rejection
recall ≥ 95 %; missing pose renders `unknown` in 100 % of injected cases.

**Supervised heads** — multi-horizon state macro-F1 ≥ 0.90 with Brier/ECE; anomaly AUPRC plus ≥ 90 %
episode recall at a fixed false-alarm/machine-hour budget, with lead-time distribution per failure
mode, material, tool and camera condition; detector mAP50-95 with ≥ 0.90 recall on critical classes;
count accuracy ≥ 98 %; IDF1 ≥ 0.90.

**Geometry** — pose P95 reprojection RMS ≤ 2 px inside the calibrated envelope, using Metrology's
values unmodified; `planarity_cov is None` never scores as in-plane.

**Grounding** — ≥ 95 % coverage of ground-truth run facts; < 1 % unsupported statements; 100 % of
factual sentences carry an `event_id`; 100 % of non-`ok` fields appear in `unknown[]`.

**Deployment** — FP32→FP16 task-metric delta < 0.5 pp; p95 < 100 ms and < 6 GB on the 2080; 24-h soak.

All numbers carry confidence intervals and are stratified by machine, material, camera and failure
mode. A single pooled headline number is not an acceptable report.

### 7.3 Ablations (the publishable grid)

Control conditioning on/off; horizon sets {1}, {1,4}, {1,4,20}, {1,4,20,120}; heteroscedastic vs.
homoscedastic; camera gating vs. mean pooling; retention supervised by residual / labels / both;
mixer depth 3-6-9; window 32-64-128; encoder frozen vs. tuned vs. owned-data self-supervised.

---

## 8. Train / eval / export pipeline

### 8.1 Reusing the existing flywheel

`tools/flywheel/runner.py` already provides exactly what this needs — atomic `O_EXCL` job claim,
`detect_capability()` OS/VRAM/CUDA matching, DVC dataset pull, MLflow logging, provenance record — so
it is reused, not duplicated. One additive dispatch branch in `_run_training()`, alongside the
existing `sft`/`dpo`/`grpo` methods and the `method_early` GRPO guard, routes `method: mldp` to
`training.perception.train.train_job(job, recipe, out_dir) -> metrics`. The existing `_train_fn`
injection seam makes that branch testable without the ML stack installed.

Job front matter reuses the flat-YAML convention of `training/jobs/schema.md` verbatim (same
regex parser, no nesting), with an appended perception section:

```
method_family: perception        # runner dispatch discriminator
feature_spec_id: <sha256>
encoder_ref: <owned-weights id or reviewed checkpoint id>
encoder_license: owned|licensed|reviewed-permissive
manifest: training/corpora/perception/v1/manifest.json
horizons: 1,4,20,120
requires_vram_gb: 4.0
requires_cuda: true
```

`base_model` carries `encoder_ref` and `adapter_out` becomes the checkpoint+ONNX output directory —
no schema fork, no second job runner.

### 8.2 Stages

1. **Ingest & align** — manifest validation, clock alignment, tick-grid resampling, split assignment.
2. **Extract** — frozen encoder + detector + tracker + fiducial/station health → `.npy` feature shards
   keyed by `feature_spec_id`. Idempotent and content-addressed; re-runs are cache hits.
3. **Train** — MLDP on cached shards (AMP, cosine schedule, MSE→NLL warm-up over 2 epochs to prevent
   variance collapse, early stop on val forecast NLL).
4. **Calibrate** — temperature/variance scaling and all thresholds fitted **on `calib` only**, emitting
   `calibration_id` and `thresholds_id`.
5. **Evaluate** — the §7 battery on `test_locked`, written as JSON beside the checkpoint (mirroring the
   existing `training/benches/*/results/*.json` convention).
6. **Export** — `torch.export` → ONNX opset 18, two graphs: `mldp_core.onnx` (static `[1,4,64,200]`
   etc.) and `encoder_proj.onnx`. Detector, tracker, fiducials, eventizer, ring buffer, `FreshValue`
   wrapping and packet construction stay host-side, deliberately.
7. **Parity** — FP32 ONNX vs. PyTorch max-abs-diff ≤ 1e-4 on a locked 256-window fixture; FP16
   task-metric delta < 0.5 pp. Failure blocks the artifact.
8. **Ship** — ONNX Runtime **CUDA EP first** (MIT). TensorRT EP FP16 later with CUDA fallback, fixed
   profiles and **per-GPU engine caches** (Turing and Ampere built separately). Documented pin:
   ORT 1.22 + TensorRT 10.9 + CUDA 12.0–12.8, containerised. INT8 only after 500–1 000 representative
   calibration images and a task-level parity re-run.

### 8.3 Deterministic CPU reference path

`services/perception/reference.py` is a pure-numpy forward pass of the exported graph (the dilated
causal mixer makes this ~200 lines). Rules:

- `test_reference_parity.py` asserts numpy vs. ORT FP32 agreement to 1e-5 on the locked fixture.
- Any perception value rendered adjacent to a Metrology measurement is computed via the reference path
  and carries its `calibration_id`. Its `evidence_class` (`"advisory"`) and `accuracy_tier`
  (`"A2_station"`) are **derived from the source device kind inside `packet.build()`**, mirroring
  Metrology's own device-kind table — they are not constructor parameters, so a `qualified` or
  `Q*`-tier perception packet is unconstructible rather than merely rejected. `qualification` is read
  from the station profile (`{id, kind, established_at, expires_at, state}`) and never synthesized;
  `state != "live"` fires one `mfg.perception.invalidated` with `class:"falsified"`,
  `reason:"qualification_withdrawn"` (§4.1). Metrology's tier and honesty
  string display unchanged; perception never overrides, annotates or re-tiers them.

---

## 9. Licence containment (release gate)

| Component | Licence | Placement |
|---|---|---|
| PyTorch, torchvision (MobileNetV3 / EfficientNet / RAFT-Small **code**) | BSD-3-Clause | **product tree** |
| ONNX Runtime + CUDA EP | MIT | **product tree** |
| OpenCV (ArUco/ChArUco — already an optional dep of `services/vision/fiducial.py`) | Apache-2.0 | **product tree**, unchanged |
| YOLOX (upstream repo) | Apache-2.0 | **product tree**, vendored from upstream — *never* via Ultralytics |
| RT-DETR / RT-DETRv2 (upstream repo) | Apache-2.0 | product tree only if selected over YOLOX; upstream only |
| ByteTrack | MIT | **product tree** |
| Kubric | Apache-2.0 | dev-only tooling, out of product tree; **generated assets still need per-asset review** |
| CVAT Community core | MIT | out-of-tree self-hosted service; optional serverless models excluded |
| Anomalib | Apache-2.0 | **dev-only baseline harness**, out of product tree, never a runtime dep |
| **Ultralytics** | AGPL-3.0 / Enterprise | **EXCLUDED.** No import, no checkpoint, no RT-DETR-via-Ultralytics. ONNX export does not cleanse it. |
| **MVTec AD** | CC BY-NC-SA 4.0, non-commercial | **EXCLUDED** from product and from every reported acceptance number; dev-only sanity fixture at most, never DVC-tracked into the product corpus |
| **BlenderProc** | GPL-3.0 | **EXCLUDED** from the product tree; prefer Kubric. Out-of-process sidecar only if ever used. |
| **ImageNet-1K-derived pretrained weights** | code BSD-3 ≠ **data/weight provenance**; ImageNet DB terms are non-commercial research/education | **Gated.** Default is `encoder_license: owned` — the compact encoder is self-supervised-pretrained on owned footage (S0 alone yields 360–720 k frames, ample for a 128-d projection encoder). ImageNet-derived weights require a recorded legal review keyed by `encoder_ref`. |
| **TensorRT GA binaries** | NVIDIA SDK SLA (the GitHub OSS subset is Apache-2.0; the GA binary is not) | optional accelerator under the SLA's runtime-library terms; **CUDA EP stays the shipped default so TensorRT is never load-bearing** |

Enforced by `training/perception/tests/test_license_manifest.py`: every `encoder_ref` / `detector_id`
in a recipe must resolve to a row in `contracts/licenses.md` with a non-empty `license_ref`, and no
manifest record with `license: third_party` may lack one. An unlicensed weight is a build failure, not
a footnote.

---

## 10. Exact files

### 10.1 New, exclusively owned

**`services/perception/`** — in-package `test_*.py`, matching `services/vision/`. `scripts/gate.sh`
stage 4 derives its unit list by glob over `services/*/`, so a new subpackage is picked up
automatically and the coverage guard makes an enumeration gap fatal:

`__init__.py` · `config.py` (FeatureSpec, cadence, thresholds, ring sizes, `feature_spec_id`) ·
`source.py` (`MediaSourceRef` consumer, rate/byte budget, lane isolation, synthetic quarantine) ·
`features.py` · `encoders.py` · `detect.py` · `geometry.py` (thin adapter over
`services.vision.fiducial`/`frames`/`calibrate`/`station` — **read-only, no signature changes**) ·
`windows.py` · `runtime.py` (ORT session, EP selection, engine cache) · `freshness.py` (UX
`FreshValue<T>` construction only — wraps, defines nothing) · `health.py` (the five-state machine) ·
`eventizer.py` · `retention.py` · `ringbuf.py` · `clips.py` (`EvidenceClip`) · `packet.py` ·
`grounding.py` · `reference.py` (numpy reference) · `service.py` (per-device worker).

**`services/mcp/perception_tools.py`** (new file, exclusively owned) — the module lives beside its
siblings by convention, not inside the owning package: `services/mcp/server.py` imports each module
as `from services.mcp.<module> import register_<module>_tools` and calls it with a family view (see
`cam_tools` / `cam_audit` at `services/mcp/server.py:362-365`). The family view is the **shared
`manufacture` family** per Main's ruling in gateway §17 — `register_perception_tools(_reg.view(mcp,
"manufacture"))` — never a `perception` family. This module is a thin delegation layer over
`services.perception.grounding` / `packet` / `health`; no logic lives in it.

Tests: `test_features.py` · `test_windows.py` · `test_freshness.py` · `test_health.py` ·
`test_eventizer.py` · `test_retention.py` · `test_ringbuf.py` · `test_clips.py` · `test_packet.py` ·
`test_grounding.py` · `test_reference_parity.py` · `test_synthetic_quarantine.py` ·
`test_invalidation_paths.py` · `test_degradation.py`.

**`training/perception/`**: `__init__.py` · `model.py` · `losses.py` · `manifest.py` · `data.py` ·
`splits.py` · `train.py` · `calibrate.py` · `evaluate.py` · `export_onnx.py` · `baselines.py` ·
`tests/test_model_shapes.py` · `tests/test_causality.py` · `tests/test_losses.py` ·
`tests/test_manifest.py` · `tests/test_split_leakage.py` · `tests/test_export_parity.py` ·
`tests/test_license_manifest.py`.

**Configs / jobs / corpora / contract**: `training/recipes/perception_mldp_v1.yaml` ·
`perception_mldp_smoke.yaml` · `perception_encoder_ssl.yaml` · `perception_detector_yolox.yaml` ·
`training/jobs/perception_mldp_smoke.md` · `training/jobs/perception_mldp_v1.md` ·
`training/corpora/perception/v1/spec.json` · `manifest.json` (mirroring `training/corpora/vlm/v1/`) ·
`contracts/manufacturing_perception.md`.

### 10.2 Existing files needing modification — for ExecutionPlanner to serialize

| File | Change | Size |
|---|---|---|
| `tools/flywheel/runner.py` | additive dispatch branch in `_run_training()` for `method: mldp`; dry-run writes a checkpoint stub instead of `adapter_config.json` when `method_family: perception` | ~25 lines, additive |
| `training/jobs/schema.md` | append "Perception job fields" (`method_family`, `feature_spec_id`, `encoder_ref`, `encoder_license`, `manifest`, `horizons`) | append-only |
| `services/mcp/server.py` (family registration site, alongside the `cam` block at :362) | `from services.mcp.perception_tools import register_perception_tools` + `register_perception_tools(_reg.view(mcp, "manufacture"))` | 2 lines |
| `services/mcp/tool_registry.py` | **nothing authored by this slice.** Gateway owns this file for the risk-class change (review B14) and for the `manufacture`-off-by-default edits (B11); perception submits four `read`-risk rows (`mfg_perception_brief` / `_event_get` / `_evidence_get` / `_health`) as data for that owner to append | 0 lines authored |
| `contracts/licenses.md` | append the §9 rows | append-only |
| `contracts/README.md` | index `manufacturing_perception.md` | 1 line |

**Zero changes to `services/vision/frames.py`, `measure.py`, `fiducial.py`, `calib_sheet.py`** from
this slice. `geometry.py` calls `detect_board()`, `load_image()`, `blur_score()` and Metrology's new
`calibrate.py`/`station.py` as they exist. One inbound dependency to sequence: Metrology's
`Detection.planarity_cov` → `Optional[float]` must land **before or with** W2, since `features.py`
treats `None` as fail-closed unknown.

### 10.3 Types exported / expected

**Exported:** `aicad.perception_event/1`; `EvidenceClip` (carrying Additive's identity fields); the
four durable result kinds `perception.event` / `perception.evidence_clip` / `perception.model_export`
/ `perception.capability`, each with both §24.1 columns (§4.8);
`mfg.perception_event` / `mfg.perception_frame_health` / `mfg.perception.invalidated` (the single
invalidation event, `class` + `reason`) / `mfg.perception_evidence` (lifecycle, no invalidation
envelope); `mfg_perception_brief` / `_event_get` / `_evidence_get` / `_health`, all registered into
the shared `manufacture` MCP family; `PerceptionBrief`; the six-state camera health vocabulary of
§4.1.

**Prefix reservation requested (review C1, Gateway §0.10 to land):** `mfg_perception_*`,
`mfg.perception_*` and `mfg.perception.invalidated` for this domain.

**Expected from siblings:** UX — `FreshValue<T>` (consumed verbatim; perception defines no envelope).
Gateway — `ControllerSnapshot` field names and exact state/alarm enum strings, `device_id`/`run_id`/
event identity, the tick source, and the `t_mono_ns`/`seq`/`age_ms`/`ttl_ms` stamping layer.
Metrology — `MeasurementObservation` id type, evidence-class enum, accuracy-tier enum,
`Detection.planarity_cov: Optional[float]`, `station.py` profile id, `calibrate.py` intrinsics id.
Additive — `MediaSourceRef`. Twin (optional, W5) — `chudware::twin::VirtualFrame` /
`VirtualFrameRequest` from `core/twin/vcamera.h`. Host — `CameraFrame::synthetic`
(`host/src/platform/camera.h` v2).

---

## 11. Integration order

- **W0** — land the dirty tree (session-wide prerequisite; not this slice).
- **W1** — Gateway lands `ControllerSnapshot` names + tick source; UX's `FreshValue<T>` lands;
  Metrology's `planarity_cov: Optional` + `station.py`/`calibrate.py` land; Additive's
  `MediaSourceRef` lands. **This slice's only W1.1 deliverable is a contract row set, not code:** the
  four-kind table of §4.8, with both §24.1 columns, appended to the durable-result-kind table in
  `contracts/manufacturing_gateway.md` (review B10). Until it lands, every perception append is
  correctly refused `mfg_unregistered_result_kind` and the capability row is refused
  `mfg_capability_requires_durable_kind`, so W2 has nothing durable to publish. Perception is
  design-frozen against the sibling names above and blocked on nothing else.
- **W2 — the whole deterministic spine, no model.** `contracts/manufacturing_perception.md`, the full
  `services/perception/` package with a **stub scorer that reports `unknown` for every tick**, source
  lane + synthetic quarantine, ring buffer, `EvidenceClip`, eventizer, packet, grounding + citation
  verification, `services/mcp/perception_tools.py`, all fourteen unit suites. This ships something
  honest and integrable before any weights exist: the system correctly says "I don't know". No GPU,
  no dataset, no Twin. **Note:** the two-line `services/mcp/server.py` registration into the shared
  `manufacture` family is serialized by ExecutionPlanner into a later integrator wave (three
  claimants on that file: Gateway, Additive, perception), and `services/mcp/tool_registry.py` is
  Gateway's file entirely (B14), with perception's four `read` rows submitted as data. Gateway's
  `manufacture`-off-by-default edits (B11, W1.1) are a hard predecessor of that integrator wave. That
  is a clean split — the package, the contract and every test land at W2 and are gateable standalone;
  only the surfacing of the four read-only MCP tools waits for the integrator slot.
- **W3 — S0 data + train the core.** Manifest, extraction pass, `training/perception/` model + losses +
  splits, MLDP trained on real recorded frames alone (no annotation), calibration, ONNX export, parity
  gate, ORT CUDA EP wired into `runtime.py`. C1, C2 and C3 are all evaluable at the end of this wave.
- **W4 — supervised heads (S1/S2).** Event boundaries, state, anomaly, importance; the retention
  operating point; per-mode reporting.
- **W5 — detector (S3) and optional twin (S4).** YOLOX-Tiny fine-tune for `z_det`; Kubric synthetics;
  Twin `VirtualFrame` geometry-truth auxiliaries. Both optional, neither blocking.
- **W6 — deployment hardening.** TensorRT EP FP16 with CUDA fallback and per-GPU engine caches; 24-h
  soak; shadow-mode operation with operator review; production telemetry of the retention–recall
  operating point.

W2 is deliberately model-free so this slice's critical path is deterministic code the rest of the
system can integrate against immediately.

---

## 12. Tests and smokes

**Unit (auto-swept by `scripts/gate.sh` stage 4 via the `services/*/` and `training` units):**
mask/age construction and the no-silent-imputation invariant; `planarity_cov is None` ⇒ mask 0 +
`not_calibrated`, never in-plane; `FreshValue` unwrap rule (`v` used only when `state=="ok"`, `last_ok`
never read into a feature, `seq` gap ⇒ unknown); the five distinguishable health states each producing
its own non-empty `why`; calibration-expiry emitting an overlay-invalidation; tick-grid resampling
across gaps; **causality** (perturb ticks after `t0`, assert bitwise-identical output); eventizer
hysteresis/dwell/refractory against hand-written traces; retention budget admission ordering; ring
aged-out ⇒ `truncated:true`; synthetic quarantine (ring refuses insert, packet raises on synthetic
evidence); `EvidenceClip` content-addressing and `supersedes` chain; packet canonicalisation
determinism and sha256 stability under key-order permutation; `evidence_class` and `accuracy_tier`
DERIVED from device kind rather than caller-supplied, so a `qualified` packet is unconstructible;
`qualification` read from the station profile and never synthesized; exactly one invalidation event
name existing, so name-keyed dispatch is impossible, with each retroactive path setting the correct
`class` (`falsified` clears; `superseded` carries `superseded_by` and never alarms), a routine ring
eviction reaching `mfg.perception_evidence` only, and pushes ordered BEFORE the underlying value's
freshness lapses; every durable row's `seq` coming from `mfg_event_append`, so a minted, guessed or
derived `seq` is a test failure; every durable published field carrying `state:"ok"` with
`ttl_ms:null` while the live lanes carry a real TTL; the capability row unable to reach `verified` on
advisory evidence and unable to outlive a falsified `derived_from` row; no code path presenting a
Stage-1 freshness pass as a verdict; grounding rejects an uncited claim and
lists every non-`ok` field in `unknown[]`; OpenCV-absent degradation reusing the
`HAVE_CV2`/`VisionUnavailable` posture of `services/vision/fiducial.py`; manifest validation; split
group-disjointness, no window crossing, synthetic-only-in-train, real-only test; licence-manifest
resolution; model output shapes/dtypes; loss masking; ONNX↔PyTorch↔numpy parity.

**Smokes (run the thing, not a test file):**
1. `perception_mldp_smoke.md` through `tools/flywheel/runner.py` on a 20-window synthetic corpus —
   claim, train, export, parity, provenance record, < 60 s on CPU.
2. Replay a recorded 10-minute clip + controller log through `services/perception/service.py`: ≥1
   committed packet, every `packet_sha256` verifies, every evidence uri resolves, stored bytes < 10 %
   of the source clip.
3. **Starvation smoke** — cut the camera feed mid-replay; assert the emitted fields go
   `ok → stale → unknown` with non-empty `why`, `v` is null throughout the non-`ok` span, `last_ok`
   is populated but never gated on, and **zero** events are scored. This is the fail-closed proof.
4. **Grounding smoke** — committed packets → brief → model; `verify_citations()` passes and every
   non-`ok` fact appears in `unknown[]`.
5. **Synthetic smoke** — feed a `synthetic:true` frame; assert no clip is persisted, no evidence is
   attachable, and every derived field carries `src:"simulated"`.
6. **2080 latency/VRAM smoke** — 4-camera synthetic load; p95 < 100 ms, peak < 6 GB.
7. **Invalidation smoke** — paint an advisory overlay, then expire the station calibration mid-replay;
   assert **one** `mfg.perception.invalidated` is pushed with `class:"falsified"`,
   `reason:"calibration_expired"`, the gateway-computed `affected[]` closure and `at_seq`. Then assert
   the `class` split holds on the same event name: a newer `model_id` pushes `class:"superseded"`,
   `reason:"newer_model"` with `superseded_by` set and renders without alarm, and a routine ring
   eviction emits `mfg.perception_evidence action:"evidence_expired"` and **never** reaches
   `mfg.perception.invalidated` under either `class`.
   Finally assert seq-scoping under the real frame order: deliver the invalidation in the same frame
   as an RPC result established at a higher `seq`, and confirm the newer result survives — this is the
   `st.rpc.drain()`-before-event-tick hazard, and it must hold with no synchronous hold anywhere.

---

## 13. Non-goals (deliberate)

No safety claim of any kind: not guarding, not E-stop, not an interlock, not a protective function,
and never an input to one. No automatic pause/stop/resume; perception proposes nothing a deterministic
arbiter would act on. **No gate participation at either stage**: `src:"advisory"` fails UX's Stage-1
admission by construction, so perception never reaches domain evaluation and this slice therefore
defines no Stage-2 guard — and it never presents a Stage-1 pass as a verdict anywhere. No offset
authority — machine offsets remain probe/calibration evidence under Metrology, and there is no
`advisory → qualified` promotion path; the classes are derived from device kind, so the promotion is
unconstructible rather than merely refused. No `Q*`-tier and no `A1_scaled` claim: perception caps at
`A2_station` even from a fully calibrated fixed station. No 6-DoF pose from the network (geometry does
that, in Metrology's stack, unchanged). No neural video codec claim. No foundation-model training and
no LLM training on these GPUs. No heterogeneous DDP. No INT8 in the first release. No continuous
recording by default. No synthetic or twin-sourced frame ever persisted as a capture or attached as
evidence. No camera discovery (Additive owns it). No printer/controller transport (Gateway and
Additive own it). No Ultralytics, no non-commercial datasets, no GPL/AGPL in the product tree. No
cross-machine or cross-process-family generalisation claim without per-machine calibration and a fresh
locked test set. No hard-real-time guarantee — deadline overrun degrades and reports, it never silently
drops. No restructuring of `services/vision/**`.
No second MCP family (the shared `manufacture` family is the only one this slice registers into) and
no family this slice defaults on. No second invalidation event name, no name-keyed invalidation
dispatch, and no per-trigger event table. No perception-minted `seq`, no perception-local durable
store or ledger, and no durable row published either unwrapped or with a wall-clock TTL. No
`monitor: verified` claim, and no capability shape other than gateway's `CapabilityTruth`.

---

## 14. Review closure — findings owned by this slice

Source: `local://manufacturing-plan-review.md` (`ArchitectureConsistencyReviewer`). Each row states the
review's own required correction and what this document now says. No product scope changed.

| Finding | Sev | Required correction (review) | Applied here | Where |
|---|---|---|---|---|
| **B10** (perception half) | P0 | submit a kind table carrying both §24.1 columns plus a `<domain>.capability` kind before W1.1 closes | four kinds registered with both columns — `perception.event`, `perception.evidence_clip`, `perception.model_export`, `perception.capability` — all `epoch_invariant` with `supersede_with_parent` false and `derived_from` declared; capability truth capped at `declared` because §24.3(i) makes `verified` unreachable without a probed `ITransport`; every painted `seq` journaled via `mfg_event_append`; the table named as this slice's only W1.1 deliverable | §1, §4.8, §10.3, §11 (W1) |
| **B12** | P1 | register through `view(mcp, "manufacture")`, keeping granularity via per-tool `get_tool_options` | the `perception` family is deleted rather than deprecated; the four read-only tools register into the shared `manufacture` family, which is OFF by default under Gateway's B11 mechanism; per-tool toggles retain granularity; `tool_registry.py` rows are submitted as data to Gateway (B14) rather than authored here | §1, §4.7, §10.1, §10.2, §10.3, §11 (W2) |
| **C3** | P2 | one invalidation event per domain carrying the §5 envelope; `class` carries alarm-vs-quiet, `reason` the per-trigger distinction | the three-name split is withdrawn; one `mfg.perception.invalidated` carrying `{class, reason, why, affected, at_seq, superseded_by?}`, `affected[]` being the gateway's computed closure; routine lifecycle stays a lifecycle event with no invalidation envelope; the cry-wolf argument is served by `class` | §4.1, §4.6, §8.3, §10.3, §12, §13 |
| **C8** | P2 | a durable authored row is journaled **and** published wrapped, with `ttl_ms:null` and `state:"ok"` for rows that do not decay | all four durable kinds are journaled and published wrapped with `state:"ok"`, `ttl_ms:null`, `src:"advisory"` and `seq` = `established_seq`; the committed-packet example carries `ttl_ms:null` explicitly; only the live score and frame-health lanes keep a real TTL | §4.5, §4.6, §4.8 |

Preserved unchanged, and asserted here so a later edit cannot quietly regress it: capability level
**`monitor` only**, every learned field `src:"advisory"`, no Stage-1 admission and therefore no
Stage-2 guard, never an offset source, never a safety function (§1, §4.8, §13); and DoD v4's locked
product choice that the **MLDP model/training core is published** — with synthetic fixtures and a
licensing/consent-gated exportable benchmark subset, while commercial-user data and integrations stay
local by default behind an explicit logged opt-in export (§4.8, gated by §5.1's `license` /
`consent_ref` / `export_restricted` / `redaction_applied` fields and §9's licence containment).
`perception.model_export` carries no publication semantics either way.

Findings this slice does not own and does not modify: B1-B9, B11, B13-B16, C2, C4-C7, C9, C10. C1's
prefix reservation is Gateway-owned; perception's request is recorded in §10.3 for that pass.
