# Printer Integration Research

## Executive recommendation

Build a **local edge coordinator with capability-negotiated printer adapters**. The coordinator owns the durable job/event log, retries, reconciliation, notifications, and multi-printer view; each printer adapter remains thin and uses the printer stack's first-party local interface. Keep slicing/NC generation and webcam transport outside the execution adapter.

Recommended delivery order:

1. **Moonraker/Klipper first.** It has the cleanest local control plane: documented upload and print-control REST/JSON-RPC, asynchronous object subscriptions, explicit job/temperature/progress/filament objects, webcam URL discovery, and Apprise notifications. Target Moonraker, not Klipper's Unix socket, and treat Mainsail/Fluidd as UX and conformance references.
2. **OctoPrint 1.11.8 second.** Its REST job/file surface is stable and broad, its bundled virtual printer is excellent for adapter testing, and it reaches many Marlin-class machines. Isolate its explicitly changeable SockJS push interface behind the adapter and retain REST reconciliation.
3. **PrusaLink local third, as a capability-probed family rather than one protocol.** Support only explicitly lab-certified model/firmware combinations. Start with local polling and file/job control. Do not promise camera, runout, push events, or identical authentication across Prusa generations.
4. **Direct Marlin serial last.** It gives broad hardware reach but makes the product responsible for serial framing, line numbers/checksums/resends, command queues, compile-time feature variance, safety semantics, and host-vs-SD print continuity. Prefer OctoPrint in front of Marlin unless direct serial is a validated user requirement.
5. Add **self-hosted Obico** as an optional, separately deployable vision/failure-monitoring service after reliable execution. Use Mainsail/Fluidd/OctoFarm as reference implementations, not execution backends.

## Architecture and capability matrix

Legend: **L** = documented local interface; **P** = partial/optional/plugin/model-dependent; **C** = cloud or unsupported private surface; **—** = absent in that layer.

| Stack / interface | Upload + start + pause/resume/cancel | Webcam | Telemetry | Filament/runout | Failure detection | Notifications | Multi-printer | Offline / simulator |
|---|---|---|---|---|---|---|---|---|
| **Klipper v0.13.0 + Moonraker** | **L**: multipart upload, optional SHA-256 and print-after-upload; REST or JSON-RPC start/pause/resume/cancel | **P**: Moonraker stores stream/snapshot URLs; crowsnest or another app actually operates the camera | **L**: query plus WebSocket/Unix-socket `printer.objects.subscribe`; `print_stats`, `virtual_sdcard`, temps, state | **L/P**: explicit switch and motion-sensor objects when configured | **P**: firmware/job `error` and shutdown states, not visual print-quality detection | **L**: Apprise notifiers for started/complete/error/cancelled/paused/resumed and macro calls | **P**: one Moonraker/Klippy endpoint per printer; coordinator can connect many. Mainsail and Fluidd provide farm UX | No internet required. Loss of the browser/coordinator link does not itself stop Klippy, but loss of the Klipper host is fatal. Developer simulation via batch mode and `simulavr`, not a turnkey Moonraker appliance emulator |
| **Mainsail v2.18.2 / Fluidd v1.37.3** | Via Moonraker | Via Moonraker URLs | Via Moonraker | Via Moonraker | Job history/status only unless another service is added | Via Moonraker/plugins | **L at UI level**: Mainsail advertises printer-farm support; Fluidd can manage multiple printers from one install | Local web clients. **Not independent printer APIs**; Mainsail explicitly says it would not be possible without Moonraker |
| **OctoPrint 1.11.8** | **L**: multipart upload/select/print and `POST /api/job` start/cancel/restart/pause-resume | **P**: exposes configured stream/snapshot URLs; actual capture/streamer is external or plugin-driven | **L/P**: REST state plus real-time SockJS `current`, `history`, `event`, plugin messages; push interface carries an explicit change warning | **P**: firmware/plugin-specific. Core `FilamentChange` is inferred from M600/M701/M702 sent through OctoPrint and is not emitted for printer-SD printing | **P**: connection/firmware `Error` and `PrintFailed`; no core visual spaghetti/warping detector | **P**: rich event bus and plugins/system subscriptions, but no Moonraker-like built-in multi-channel notifier abstraction | **P**: official deployment tooling creates one or more OctoPrint instances; use one endpoint/instance per printer. OctoFarm can aggregate them | Local/LAN. A host-streamed print survives loss of the browser but not loss of the OctoPrint host; printer-SD printing can continue. **Strong simulator:** bundled virtual-printer plugin can inject checksums, resends, temperature models, errors, pauses, and disconnects |
| **PrusaLink v1 family**: Python PrusaLink 0.8.2 for older i3 + embedded Buddy firmware | **L/P**: draft OpenAPI documents status/job, binary upload with `Print-After-Upload`, file start, job pause/resume/delete. Embedded Buddy v6.6.3 source implements this core subset | **P by generation**: draft spec has snapshot routes; embedded Buddy v6.6.3 v1 route selector has no camera branch. Buddy3D instead documents separate LAN RTSP, after Connect/app pairing | **L/P**: polling `GET /api/v1/status` and `/job`; no documented subscription/push channel | **P/Gap**: schema has filament-use metadata and MMU capability, but no standardized local runout event/status object | **P**: polled printer/job `ERROR`/`ATTENTION`; no visual detection and limited standardized error detail in the status schema | **— locally** in PrusaLink; Prusa Connect documents cloud notification integrations | **P**: Python PrusaLink documents multiple instances on one Pi; embedded printers are separate endpoints | Prusa officially says local Link supports monitoring, upload, and control without internet. Execution continuity differs by generation: embedded storage should be printer-resident; Raspberry-Pi Link depends more on the Pi. No supported turnkey API emulator |
| **Prusa Connect** | **C** for product integration: user-facing control is cloud | **C**, except separately documented camera RTSP on LAN | Public printer SDK sends telemetry to Connect at least once/sec; this is printer-to-cloud, not a supported end-user monitoring API | Cloud/model-specific | Cloud/model-specific | **C**: Connect integrations | **C**: Connect officially manages farms/statistics | Internet/cloud service. Do **not** scrape the web UI or infer a consumer REST API from the printer SDK |
| **Marlin 2.1.2.8 host protocol** | **P**: serial G-code; M28/M29 upload to SD, M23/M24 start, M25 pause, M24 resume, M524 abort. Every feature is compile-time/capability-dependent | **—** | **P**: M105/M155 temps, M27 SD progress, free-form/host-action output | **P**: M412 and runout G-code when compiled; host handling requires `HOST_ACTION_COMMANDS` | **P**: firmware thermal/errors only, often textual; no visual detector | **—** in firmware; host must implement | **—** in firmware; coordinator opens one serial session per printer | Printer-SD jobs can continue without the host; host-streamed jobs cannot. Marlin includes a desktop simulator, but it is a firmware developer tool rather than a full product conformance appliance |
| **OctoFarm 1.7.3** | Proxies/coordinates OctoPrint instances; its own package describes control, management, and upload | Inherits OctoPrint | Inherits OctoPrint | Inherits OctoPrint/plugins | Inherits OctoPrint/plugins | Inherits OctoPrint ecosystem | **L at aggregator level** | Useful architecture/UX reference; avoid making the product depend on it |
| **Obico self-hosted, release branch** | Not recommended as the authoritative executor; clients attach to OctoPrint or Klipper | Camera input is central | Receives client telemetry | Depends on attached stack | **L for optional visual monitoring**: open self-host server includes a deep-learning failure detector | Product feature, but keep separate from control core | Self-host server can handle multiple printers; GPU increases capacity | Can be fully self-hosted on a LAN-routable server. It is a heavier Docker/ML subsystem, not a printer protocol simulator |

Primary API evidence: [Moonraker architecture](https://github.com/Arksine/moonraker/blob/d5ee17128bb88434aacdab90c2e9e990e2b64e4a/README.md), [Moonraker printer API](https://github.com/Arksine/moonraker/blob/d5ee17128bb88434aacdab90c2e9e990e2b64e4a/docs/external_api/printer.md), [OctoPrint 1.11.8 API](https://docs.octoprint.org/en/1.11.8/api/index.html), [PrusaLink draft OpenAPI](https://github.com/prusa3d/Prusa-Link-Web/blob/a67f306bc878009be0e27864a01a1837e5c4cf06/spec/openapi.yaml), [Prusa local/cloud explanation](https://help.prusa3d.com/article/prusa-connect-and-prusalink-explained_302608), and [Marlin G-code reference](https://marlinfw.org/meta/gcode/).

## Proposed local-first control plane

Use a normalized adapter contract with explicit capability discovery rather than a least-common-denominator interface:

- `discover()` returns protocol/version/auth/capabilities and immutable printer identity.
- `upload(artifact, sha256, remote_name, print_after_upload=false)` returns a durable remote artifact identity. Prefer content-hash staging names and verify size/checksum where supported.
- `start(remote_artifact_id)`, `pause(job_id)`, `resume(job_id)`, and `cancel(job_id)` are **commands**, not assumed state transitions. Persist a command ID, issue once, then confirm via observed state. Never blindly retry `start` or `cancel` after a timeout.
- `snapshot()` supplies the current authoritative state. `subscribe()` is optional; when push is available, still reconcile periodically with snapshots and after reconnect.
- Use a common state model such as `OFFLINE`, `UNKNOWN`, `READY`, `UPLOADING`, `PRINTING`, `PAUSED`, `ATTENTION`, `ERROR`, `COMPLETE`, `CANCELLED`, but retain vendor raw state/error payloads alongside normalized fields.
- Put camera discovery/ingestion behind a separate `MediaSource` abstraction. Moonraker and OctoPrint mostly provide URLs, Prusa cameras vary by generation, and Marlin has no camera concept.
- Put notification delivery behind the coordinator. Moonraker's Apprise can remain an optional native path, but consistent product notifications should be driven from the coordinator's durable normalized event log.
- Keep slicing/NC generation separate. These interfaces generally consume G-code; OctoPrint can expose plugin slicing progress, but slicing is not part of the portable execution contract.
- One local edge agent should maintain LAN connections and a durable SQLite/event queue, so cloud/mobile reachability is optional. Cloud should relay intent to the local agent, never be required for an already-started job.

## Safety and failure semantics

- **Pause/cancel are not emergency stop.** They may be queued behind firmware work and may execute macros that move axes or heat/cool. Klipper has a separate `emergency_stop`/shutdown endpoint. Marlin M112 reliability depends on the emergency parser/build and the health of the serial path. Preserve a physical/local stop path and never market remote cancel as a safety interlock.
- Distinguish **transport offline**, **controller/host offline**, **firmware error**, **material attention/runout**, and **visual quality anomaly**. They require different UX and retry policies.
- Core controller errors detect thermal/connection/job failures, not spaghetti, detachment, warping, or collision. Vision should initially default to notify/recommend-pause, with user-configurable thresholds; automatic intervention needs false-positive testing and a safe-state specification.
- Rate-limit cameras independently from control traffic. OctoPrint's own setup page warns that webcam bandwidth on underpowered hosts can negatively affect print quality.
- LAN-local is not automatically secure. Require per-printer credentials, least-privilege OctoPrint keys, Moonraker authorization, isolated printer VLANs, and no direct internet exposure. Prusa Buddy3D explicitly calls local RTSP unencrypted.

## Prusa-specific product gaps

1. **No single PrusaLink implementation.** Older MK2.5/MK3 uses Python PrusaLink on a Pi; newer MINI/MK3.5/MK3.9/MK4/XL/CORE families embed Link in firmware; SLA is another family. Support must be a tested model/firmware matrix.
2. **The only public OpenAPI is `1.0.0-draft`.** It declares Digest auth, while embedded Buddy v6.6.3 source also accepts `X-Api-Key`. Capability-probe `/api/version`, auth schemes, routes, and response shapes instead of branching only on a product name.
3. **Polling, not documented push.** The draft exposes status/job GETs but no subscription channel. Budget polling load and reconnect latency.
4. **Camera surface diverges.** The draft has `/api/v1/cameras/*/snap`; embedded Buddy's v6.6.3 v1 selector implements storage/info/job/status/transfer/files but not camera routes. Buddy3D offers unencrypted local RTSP and snapshots through Connect/app workflows. Ship camera support as separately probed.
5. **Runout/MMU/error semantics are incomplete for a normalized monitor.** Filament-use metadata and MMU capability exist, but there is no documented local runout event object comparable to Klipper's filament sensors. Preserve raw state and treat runout support as unverified until physical tests.
6. **Prusa Connect is not a supported local adapter.** The public SDK is for a printer agent to send telemetry and receive commands in telemetry responses; it does not document a consumer farm/job API. Keep Connect optional/cloud-only unless Prusa supplies a supported partner API and terms.
7. **Licensing is mixed.** Prusa-Link-Web says AGPLv3; embedded Buddy firmware is GPLv3 with graphics/design under CC BY-NC-SA; Python PrusaLink and the Connect printer SDK label themselves `Freeware`, so public source does not equal OSI open source.

## License and API-status summary

| Project | Investigated license | Interface status / implication |
|---|---|---|
| Klipper v0.13.0 | GPL-3.0 | Open documented Unix-socket API; prefer Moonraker for web integration |
| Moonraker @ `d5ee171…` | GPL-3.0 | Open documented REST/JSON-RPC/WebSocket API; current docs are master/commit based rather than a meaningful recent semver release |
| Mainsail v2.18.2 | GPL-3.0 | Open frontend, not a separate printer API |
| Fluidd v1.37.3 | GPL-3.0 | Open frontend, not a separate printer API |
| OctoPrint 1.11.8 | AGPL-3.0 | Open documented REST; documented but explicitly changeable SockJS push surface |
| Prusa-Link-Web OpenAPI `1.0.0-draft` | AGPLv3 declared in spec | Public documented draft; implementations vary |
| Python PrusaLink 0.8.2 | `Freeware` package metadata | Source-available but not demonstrated OSI-open; use the network protocol, do not copy/vend code without legal review |
| Prusa Buddy firmware v6.6.3 | GPL-3.0 firmware; graphics/design CC BY-NC-SA 4.0 | Source confirms an embedded API subset; do not copy UI assets |
| Prusa Connect SDK for Printer @ `24f7f5f…` | `Freeware` | Public printer-to-cloud SDK, not a supported user-facing Connect API |
| Marlin 2.1.2.8 | GPL-3.0 | Open documented G-code host protocol; capabilities compile-time dependent |
| OctoFarm 1.7.3 | ISC | Open multi-OctoPrint aggregator/reference |
| Obico Server @ release `49c0bc7…` | AGPL-3.0 | Open self-hosted monitor/ML server; network-source obligations matter if modified and deployed |

Implementing an independent network client from public documentation is ordinarily architecturally separate from these servers, but that is a legal inference, not legal advice. Do not copy implementation code or bundle/modify GPL/AGPL components until counsel reviews distribution and AGPL network-source obligations.

## Open decisions

1. **Protocol scope:** approve Moonraker → OctoPrint → capability-probed PrusaLink for V1, with direct Marlin deferred unless a concrete user need requires it.
2. **Offline guarantee:** define whether “offline” means only “no cloud/internet” or also survival of edge-host failure. The latter is impossible for Klipper and host-streamed OctoPrint/Marlin jobs; it requires printer-resident execution where available.
3. **Prusa posture:** choose a small certified model/firmware matrix and local PrusaLink only, or pursue a formally supported Prusa partner API. Do not base a product promise on Connect web endpoints.
4. **Safety authority:** decide whether remote pause/cancel is convenience-only and vision is advisory by default (recommended), plus the physical-stop and human-confirmation requirements.
5. **OSS deployment model:** choose between clean-room protocol clients only, optional user-installed OSS services, or product-bundled/modified servers; obtain license review before the latter two, especially for AGPL and Prusa `Freeware` components.