## yield: coverage_summary

Read-only audit completed for the assigned manufacturing, perception, firmware-process, MCP manufacturing, and historical-review scope. **Six current findings survive source review. Seven historical IDs are fixed; one is partially addressed with the current external bypass blocked.**

No files were changed. No builds, tests, scanners, payloads, network calls, device operations, mounting, or media writes were performed. Reproduction recipes below are **proposals for the integrator, not observed results**. Deployment-level isolation and web-method exposure remain the other reviewers’ responsibility.

## yield: historical_classification

| Historical ID | Current status | Current-source evidence |
|---|---|---|
| **MFG-PRINTER-BODY-UNBOUNDED** | **Fixed** | `services/manufacturing/printers/http_client.py:27,61-68,107-110` caps successful and HTTP-error bodies at 1 MiB. `printers/base.py:34,99-135` removes per-field raw duplication, attaches raw once, and replaces payloads above 64 KiB with a digest/preview. |
| **MFG-WORKER-NO-ALLOWLIST** | **Partial; no demonstrated current external bypass** | `printers/worker_rpc.py:59-82` now requires matching `base_url`/`expected_base_url` origins, but both values still arrive in the same parameter object; the worker does **not** independently consult commissioning policy. The current trust boundary is effective: `core/hostd/mfg_lane.cpp:240-258` claims every `mfg_` method and reloads the allowlist; `:2333-2334` refuses unknown manufacturing methods. `core/hostd/server.cpp:779-789` routes to that lane and fails closed when unavailable. `mfg_lane.cpp:544-547` supplies the expected endpoint. The regression at `printers/test_worker_rpc.py:94-112` pins prefixes and mismatch refusal, not independent authorization. Treat the remain…
| **MFG-WEBHOOK-SSRF** | **Fixed for the historical unrestricted/redirect/rebinding paths** | `notify/channels.py:258-306` resolves and rejects unspecified, multicast, and—by default—loopback/link-local/reserved/private addresses. `:167-234` connects to the checked sockaddr while preserving HTTPS hostname validation. `:309-328` disables proxies, installs pinned-address handlers and `_NoRedirect`, and reads only one response byte. Explicit `allow_private_networks=True` is a policy opt-in, not a bypass. |
| **MFG-OSASCRIPT-INJECTION** | **Fixed** | `notify/channels.py:134-146` uses a constant AppleScript program with `on run argv`; notification text follows `--` as arguments rather than script source. |
| **MFG-REMOTE-PATH-TRAVERSAL** | **Fixed for the historical OctoPrint path** | `printers/octoprint.py:15-23` rejects absolute paths, percent/backslash/control characters, and empty/`.`/`..` segments. Both upload-response ingestion (`:154`) and start (`:166`) validate before the select URL is constructed (`:167-172`). |
| **CAPTURE-CLIP-INTEGRITY** | **Fixed** | `services/perception/capture/replay.py:39-71` verifies file SHA-256, reconstructs the canonical body ID, and binds that ID to the manifest and filename before decoding frames. These are integrity checks against the manifest, not manifest authentication. |
| **MFG-NOTIFY-SEND-OPTIONS** | **Fixed** | `notify/channels.py:132-133` inserts `--` before title/body. |
| **MFG-PYWS-FRAGMENT-LOOP** | **Fixed** | `printers/websocket.py:20,81-108` limits reception to 256 frames per message, counting control frames too, and rejects an interleaved text frame during fragmentation. Frame/message byte ceilings remain present. |

`docs/manufacturing/23_security_review.md` is historical evidence; its old locations and blanket “being fixed” statement are not current proof.

## yield: findings

### 1. FW-RENODE-MONITOR-INJECTION — Declarative scenario strings become executable monitor commands

- **Severity:** High
- **Confidence:** High for command injection; arbitrary host-code execution is a consequence of Renode’s executable script/include surface, not exercised here.
- **CWE:** CWE-94, CWE-116
- **Attacker prerequisite:** Supply a scenario consumed by a user’s firmware simulation, or control an ELF filename selected for that simulation. This does not require the user to intentionally request arbitrary Renode script execution.
- **Customer impact:** Scenario data can alter the generated simulation program, falsify evidence, and reach the emulator’s host-side scripting surface with the emulator process’s privileges.

**Source → missing control → sink**

1. `core/firmware/firmware_rpc.cpp:185-215` accepts scenario objects/JSON/files, validates them, and passes them to simulation.
2. `core/firmware/scenario.cpp:208-218,268-276` validates CAN event shapes, times, IDs, payload bytes and drive-state values, but leaves endpoint strings unrestricted.
3. `scenario.cpp:816-822` interpolates `track.endpoint` and `drive_events[].endpoint` inside quoted Renode commands without escaping quotes/newlines.
4. `scenario.cpp:864-888` emits those strings verbatim.
5. `core/firmware/emulator.cpp:385-398` writes and launches the generated `.resc`.

Related manifestations of the same root cause:
- `scenario.cpp:758-760`: legacy SPI-stub name interpolation.
- `emulator.cpp:1071-1073`: platform/ELF paths interpolated into monitor source without monitor-language quoting. `image_error()` at `:158-187` checks the referenced file’s ELF header, not whether its filename is safe to embed in a script.

**Nearby controls considered:** `bus_models.cpp:84-85` constrains explicit bus-link peripheral names; this does not constrain CAN endpoint strings. Hex encoding protects UART payloads at `scenario.cpp:798-809`, but is not applied to the vulnerable fields.

**Minimal remediation:** Validate identifier-valued fields against a closed grammar, including membership in declared endpoints. Centralize correct Renode argument/path encoding; reject CR/LF and other unsupported characters rather than concatenating them into source.

**Safe isolated reproduction proposal:** Call only `validate_scenario()` and `build_scenario_plan()`—never Renode—with an otherwise valid CAN scenario whose `drive_events[0].endpoint` is the literal string:
```text
peer" 0
echo "AUDIT_MARK"
#
```
Use synthetic capabilities `family=stm32f4, can=true` and platform text containing `can1:`. Assert that validation succeeds and a timeline string contains a separate `echo "AUDIT_MARK"` line. Stop at inspecting the plan.

---

### 2. FW-QEMU-NATIVE-SEMIHOSTING — Simulated firmware receives unsandboxed native-host services

- **Severity:** High
- **Confidence:** High for unsafe configuration and absent in-code isolation
- **CWE:** CWE-250, CWE-749
- **Attacker prerequisite:** Persuade a user to simulate an attacker-supplied ARM ELF on the QEMU backend.
- **Customer impact:** Native semihosting exposes host filesystem services to guest firmware; host-command services also exist in the semihosting interface. Consequently, treating an ELF as merely simulated device code does not protect host files or credentials. Actual deployment confinement was not established.

**Evidence**
- `core/firmware/emulator.cpp:468-473` launches:
  ```text
  -kernel <elf> -semihosting -semihosting-config enable=on,target=native
  ```
- `emulator.cpp:1125-1133,1188-1209` admits valid ARM ELF input and selects/falls back to QEMU.
- `core/firmware/subprocess.cpp:193-205` directly `fork`/`execvp`s the emulator, without filesystem isolation or privilege reduction.

**Nearby controls considered:** ELF-header validation, backend capability checks and a process timeout do not restrict semihosting operations. This finding is distinct from intentionally executing a caller-provided `.resc` through `fw_emulate_renode`.

**Minimal remediation:** Disable native semihosting for untrusted simulation inputs, or execute the emulator in a dedicated sandbox exposing only copied input/output fixtures. If semihosting is needed for console output, do not implicitly grant its full host-service surface.

**Safe isolated reproduction proposal:** First replace QEMU with an argv-recording fake in a private test PATH and confirm these flags, using the existing fake-backend testing pattern. To verify third-party behavior, the integrator may use an isolated disposable sandbox with no host mounts/network and a synthetic ELF that reads a single synthetic sentinel via semihosting and prints its bytes. No real files, credentials, shell payloads or devices are needed.

---

### 3. FW-SHARED-TEMP-CACHE — Predictable firmware directories trust foreign ownership and symlinks

- **Severity:** High on shared-user hosts; deployment-dependent
- **Confidence:** High
- **CWE:** CWE-377, CWE-59
- **Attacker prerequisite:** A different local OS user can precreate the predictable temporary directory before the victim uses firmware features. A private service `TMPDIR` would mitigate this; its deployment use was not verified.
- **Customer impact:** Victim-user file overwrite through symlinks; firmware project/platform tampering. This is a distinct-user attack, not authorized desktop same-user execution.

**Evidence**
- `core/firmware/platform_db.cpp:118-143` uses `<temp>/chudware_renode_platforms`, accepts existing directories, follows `is_regular_file()`/ifstream symlinks, and truncates differing files with `ofstream`.
- `core/firmware/project.cpp:91-102` defaults, without the environment override/APPDATA, to `<temp>/Chudware/firmware`, again without ownership or permission validation.
- `project.cpp:182-187` creates project trees and writes executable build inputs beneath that root.
- `core/firmware/toolchains.cpp:98-130` subsequently configures/builds those inputs.

**Nearby controls considered:** Rewriting platform files when content differs is not protection: the comparison and rewrite both follow symlinks. Random project IDs do not protect a project root whose parent directory is attacker-owned.

**Minimal remediation:** Store persistent firmware projects in a private per-user application-data directory. Create temporary/cache directories securely with ownership checks and restrictive permissions; reject symlink components and use atomic, no-follow file creation.

**Safe isolated reproduction proposal:** Set `TMPDIR` to a fresh temporary fixture. Create `chudware_renode_platforms` there and a `stm32f4_discovery.repl` symlink pointing to a synthetic sentinel elsewhere **inside the same fixture**. Call `get_platform_file("stm32f4")` only. Observe whether the sentinel is replaced by embedded platform text. Do not launch Renode or touch the system temporary directory.

---

### 4. MFG-GCODE-WIDTH-ALLOCATION — Tiny G-code can request an enormous morphology kernel

- **Severity:** Medium
- **Confidence:** High
- **CWE:** CWE-789, CWE-400
- **Attacker prerequisite:** Supply a G-code file that the user or agent asks the normal verification tool to inspect.
- **Customer impact:** Python-worker memory exhaustion or extended CPU consumption before verification can return a safety verdict.

**Source → sink**
- `services/mcp/print_tools.py:1175-1205` exposes verification of a supplied file.
- `services/manufacturing/slicer/gcode_check.py:135-137` accepts arbitrary floating-point `;WIDTH:` values.
- `:244-250` retains that width in extrusion segments.
- `:303-312` derives a dilation radius from the width and constructs `x*x + y*y`, allocating a square kernel.
- A second equivalent kernel path exists at `:448-454`.
- `:743` invokes geometry checking even when earlier checks have already failed.

**Nearby controls considered:** `:378-385` caps the XY raster dimensions, but those dimensions depend on toolpath endpoints, not the attacker-controlled width. A short path inside the bed therefore passes this allocation guard while requesting an enormous kernel.

**Minimal remediation:** Reject nonfinite, nonpositive and unreasonable extrusion widths before retaining segments; enforce an explicit kernel/sample allocation budget before NumPy allocation. Refuse oversized analysis rather than reporting it as verified.

**Safe isolated reproduction proposal:** Use the existing tiny `_START + _layer(.2) + _END` fixture, replace `;WIDTH:0.45` with `;WIDTH:1000`, and substitute a guard object for the module’s `np.ogrid`. Have its `__getitem__` record the requested dimensions and raise a test-only exception **before allocating** when the width exceeds 100 cells. Verify that the oversized request is reached despite small XY bounds. Do not execute the unguarded allocation.

---

### 5. FW-SCENARIO-EXPANSION — Quadrature count has no work budget before in-process expansion

- **Severity:** Medium
- **Confidence:** High
- **CWE:** CWE-400, CWE-789
- **Attacker prerequisite:** Supply a declarative quadrature scenario to a firmware simulation using a supporting platform.
- **Customer impact:** A small input causes very large allocations and CPU work in the native process before the emulator subprocess timeout begins.

**Evidence**
- `core/firmware/scenario.cpp:145-157` checks only that `steps` is an integer ≥1 and `step_ms` is positive.
- `:381-400` converts the count to `int` and materializes one edge per step.
- `:646-650` then creates another collection of monitor-command strings from those edges.
- `core/firmware/emulator.cpp:345-354` builds the plan before checking its requested virtual horizon; the process deadline is not applied until `:398`.

**Nearby controls considered:** The independent sensor-profile route has sample/device ceilings in `sim_sensor_bridge.cpp:195-196,265-266`; these do not constrain directly supplied quadrature tracks. Extremely large integer narrowing is another consequence of the missing upper bound.

**Minimal remediation:** Validate bounded integer counts and a cumulative scenario-event/command-byte budget before expansion. Perform horizon/work-budget admission before materializing the plan.

**Safe isolated reproduction proposal:** Call **validation only** with `steps=100000000` and otherwise valid quadrature parameters; confirm the count is admitted without expanding it. Separately expand 8 and 16 steps with synthetic GPIO platform text to demonstrate linear materialization. Never expand the large value.

---

### 6. FW-PROCESS-OUTPUT-UNBOUNDED — Firmware output is capped only after full capture

- **Severity:** Medium
- **Confidence:** High
- **CWE:** CWE-770, CWE-400
- **Attacker prerequisite:** Run firmware that emits excessive console/semihosting output, or build an imported project generating excessive diagnostics.
- **Customer impact:** Memory exhaustion in the native host process despite a bounded returned log.

**Evidence**
- `core/firmware/subprocess.cpp:243-245,253-257` appends all stdout/stderr bytes without a ceiling.
- Windows capture likewise appends without a ceiling at `:152-156`.
- `core/firmware/emulator.cpp:409-415,481-486` copies/combines output and only then applies `cap()`.
- `core/firmware/toolchains.cpp:77-94` similarly combines complete captured build output before truncation.

**Nearby controls considered:** A wall-clock timeout limits duration, not output volume. Renode’s custom bus-event caps do not cover arbitrary firmware output or compiler diagnostics.

**Minimal remediation:** Enforce stdout/stderr and aggregate capture limits in `run_capture`, terminate the process group on overflow, and return an explicit incomplete-output/refusal status.

**Safe isolated reproduction proposal:** Invoke the capture helper with a temporary fake executable that writes exactly 128 KiB and exits. Confirm that the helper retains the entire output even though consumers advertise a smaller returned-log cap. A finite small fixture demonstrates the missing capture limit; no flood or real emulator is necessary.

## yield: hypotheses_and_policy_gaps

These are **not additional confirmed remote vulnerabilities**:

1. **Stock PrusaSlicer `post_process` configuration.** `print_tools.py:952-978`, `prusaslicer.py:240-249,270-296,317-323`, and `vendor_bundle.py:314-326` admit known preset/override keys and serialize them into the loaded INI without rejecting executable post-processing settings. The repository-side propagation is established, but the installed PrusaSlicer version’s execution semantics were not inspected or exercised. Integrator should verify with a harmless temporary marker-only script in an isolated slicer fixture before promoting this to a command-execution finding.
2. **Subprocess deadline after EOF.** `slicer/sidecar.py:330` calls unbounded `process.wait()` after both pipes close; `core/firmware/subprocess.cpp:265` has the analogous `waitpid`. Both can overrun the nominal deadline with a child that closes output then continues. I did not establish an independent attacker-data path forcing a trusted stock CLI to do this. A finite fake child that closes descriptors and sleeps 200 ms is a safe regression recipe.
3. **Perception replay resource admission.** `capture/replay.py:194-220` materializes every tick between minimum and maximum frame timestamps; model dimensions and loaded clip/session sizes also lack an aggregate replay budget. No production untrusted-session import boundary was established. This remains a library hardening gap, not a reported remotely reachable DoS. Clip hashing does not impose resource bounds or authenticate the manifest.
4. **OPC UA transport assumptions.** `monitor/opcua/client.py` intentionally supports anonymous `SecurityPolicy None`; it is not authenticated/confidential telemetry. Per-recv socket timeouts also do not impose an overall exchange deadline (`:473-487`). Deployment must restrict this monitor to an appropriately isolated network.
5. **Firmware hardware APIs are privileged by design.** `firmware_rpc.cpp:438-467,542-558,664-713` reaches flashing, reset/halt and serial operations without manufacturing-lease checks. Serial handles are process-global integers (`hardware.cpp:156-166,395-439`). These are authorized desktop capabilities, not automatically remote escalation; the web reviewer must ensure they are not exposed across tenants.
6. **Native firmware project path confinement differs from the MCP wrapper.** `project.cpp:121-135` concatenates caller-provided project IDs, and `firmware_rpc.cpp:121-133` materializes caller-provided source paths. The MCP wrapper has `_safe_relpath()` checks (`services/mcp/firmware_tools.py:195-213`). Native/raw exposure and tenant authorization need correlation with the gateway review.
7. **Print-ready packet authenticity.** `print_media.py:398-447` checks schema, `print_ready` and matching G-code hash, not cryptographic provenance of the readiness decision. Appropriate for a trusted local workflow; not an independent authorization boundary for tenant-supplied manifests.

## yield: reviewed_paths_and_positive_controls

1. **Printer adapters:** `services/manufacturing/printers/{http_client,websocket,worker_rpc,model,base,moonraker,octoprint,prusalink}.py`; relevant adapter/worker tests. Response ceilings, raw deduplication, credential redaction, no redirects, probe-before-use, and OctoPrint path validation were checked.
2. **Notifications:** `notify/{channels,service,store,journal}.py` and `test_notify.py`. Pinned DNS resolution, TLS hostname verification, proxy/redirect refusal, constant AppleScript, argument termination, payload allowlisting and delivery rate limits are positive controls. Delivery throttling is not a storage quota.
3. **Slicing:** `slicer/{sidecar,prusaslicer,vendor_bundle,contract,compliance,gcode_check}.py`; relevant tests and `print_tools.py` consumer sections. Sidecar input containment, no-follow access, staged binary hashing, scrubbed environment, process-group termination, bounded capture and canonical-input budgets were reviewed. Stock PrusaSlicer follows a separate invocation path; sidecar hardening should not be assumed to cover it automatically.
4. **Print media:** `print_media.py` admission, inventory, journal validation/recovery, staging, archive, swap and commit sections; `test_print_media.py`. Positive controls include explicit idle confirmation, whole-inventory token, per-drive lock, relative-path validation, symlink refusal, source/readback hashes, journal path scoping, rollback and verified non-printable archives.
5. **Robot:** `robot/{urdf,trajectory,safety_boundary,collision,kinematics}.py` and relevant tests. XML byte/element ceilings, DTD/entity refusal, referenced-path restrictions, trajectory/collision budgets, and permanently absent execution/transport surfaces were checked. Results remain advisory.
6. **PnP:** `pnp/{openpnp,simulator}.py` and relevant tests. Referenced board files remain beneath the job directory; DTD/entity/symlink refusal and simulation outcome limits are present. No physical driver is exposed.
7. **Metrology:** `metrology/{evidence,tiers,offsets,probe,inspection,registration,transform}.py` and test coverage. Exact nominal qualified-observation checks, Q2 locality restrictions, guardbanding, provenance, bounded collections and absence of controller-write APIs were reviewed.
8. **Monitoring:** `monitor/{mtconnect,normalize,replay}.py` relevant parsing/sink sections; `monitor/opcua/{client,codec,protocol,machine_tools}.py` and test sections. XML declarations are refused, frame/message/chunk/array/depth limits exist, service decoding is allowlisted, sequence/request/channel bindings are checked, and emergency-stop values require Boolean scalars.
9. **Perception:** `services/perception/{packet,reference,config}.py` relevant sections; `capture/{storage,replay,session,buffer}.py` and capture/packet tests. Clip integrity is verified, redaction precedes persistence, the retained ring is bounded, NumPy loading disables pickle, and packets explicitly cannot become gate/offset/safety authority.
10. **Firmware:** `core/firmware/{firmware_rpc,subprocess,hardware,project,toolchains,provision,platform_db,emulator,scenario,bus_models,sim_sensor_bridge,sensor_models}.{cpp,h}` relevant process/input/materialization sections; sink review of `renode/*.cs`; `core/tests/test_firmware.cpp` relevant cases. Process execution uses argv rather than a shell; explicit ELF/setup validation and bounded sensor generation are present.
11. **Assigned interface/history:** `services/mcp/mfg_tools.py`, `docs/manufacturing/23_security_review.md`.
12. **Narrow cross-boundary reads solely for closure/taint tracing:** `core/hostd/{mfg_lane,server}.cpp`, `services/mcp/{print_tools,firmware_tools}.py`, and worker-registration references.

**Limits:** This is a security-boundary review, not exhaustive numerical/physical-model verification. Gateway C++ transport auditing, tenant-routing proof, installed third-party emulator/slicer implementations, deployment permissions/sandboxing, release provenance and dependency advisories belong to the integrator/other assigned reviews. No claim is made that every helper or mathematical branch in the listed directories was line-by-line audited.

## yield: suggested_existing_checks

Not run. Use temporary fixtures only; do not run an unrestricted firmware suite that may discover tools/devices.

- `services/manufacturing/printers/test_worker_rpc.py::test_worker_methods_stay_behind_mfg_lane_and_open_pins_expected_endpoint`
- `services/manufacturing/printers/test_worker_rpc.py::test_snapshot_raw_is_attached_once_and_capped_before_journaling`
- `services/manufacturing/printers/test_adapters.py::test_octoprint_rejects_vendor_supplied_path_traversal`
- `services/manufacturing/printers/test_adapters.py::test_websocket_rejects_interleaved_data_and_excessive_continuations`
- `services/manufacturing/notify/test_notify.py::{test_webhook_refuses_non_public_resolved_addresses_by_name,test_webhook_redirect_is_refused_instead_of_followed,test_macos_notification_passes_hostile_text_only_as_argv,test_linux_notification_terminates_options_before_hostile_text}`
- `services/perception/capture/test_capture.py::{test_replay_refuses_clip_whose_bytes_miss_manifest_sha256,test_replay_refuses_clip_whose_canonical_body_misses_clip_id}`
- Hermetic `services/manufacturing/test_print_media.py`, `pnp/test_openpnp.py`, `robot/test_offline_run.py`, `metrology/test_metrology.py`, and `monitor/opcua/test_codec.py`.
- Firmware named cases: **“scenario: validation + fixtures + plan (pure)”**, **“emulator rejects invalid setup before probing a backend”**, **“sensor scenario setup rejects meaningless or unbounded inputs”**. Run only in an isolated process with private `TMPDIR` and `CHUDWARE_FIRMWARE_DIR`; the nominally pure scenario test materializes platform files.

**Coverage conclusion:** All eight assigned historical IDs were revalidated against current code, and every named previously unaudited service subarea received boundary/sink review. The strongest new issues are executable Renode source generation, QEMU native semihosting, shared temporary-directory trust, and preflight/input-driven resource amplification.

[Some lines truncated to 768 chars. Use agent://ManufacturingServices:raw:12-12 to read line 12 whole]
