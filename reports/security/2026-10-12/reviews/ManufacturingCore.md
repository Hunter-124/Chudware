## Yield — historical finding revalidation

**All eight assigned IDs accounted for.** These classifications are based on current source, not historical test results. Nothing was edited or executed; no hardware, network service, credentials, or live configuration was accessed.

| Historical ID | Current status | Current-source evidence |
|---|---|---|
| **MFG-LEASE-BEARER** | **Fixed** | `core/hostd/mfg_lane.cpp:664-681` now requires both the correct token and `leases_.held_by(..., caller, ...)`. Nonowners lose the token in device-list responses (`1223-1226`); grant/renew/release events redact it (`1312-1316`, `1337-1341`, `1373-1376`). `core/hostd/server.cpp:774-780` supplies OS-peer identity plus server-assigned connection ID, not request-supplied owner identity. This fixes bearer theft, **not tenant-level authorization to claim devices**; see finding 1. |
| **MFG-ARTIFACT-ARBITRARY-READ** | **Fixed** | `core/hostd/mfg_lane.cpp:1649-1667` accepts `lines` or `content`; the filesystem-open branch is gone. `path` alone is refused. The MCP docstring still advertises `path` (`services/mcp/mfg_tools.py:97-101`), a stale documentation issue rather than a surviving file-read vulnerability. |
| **MFG-GRBL-UNBOUNDED-LINE** | **Fixed** | `core/device/grbl_adapter.cpp:325-362` bounds both byte assembly and complete-line input, terminalizes oversized input, and retains bounded diagnostic text. Default limit derives from RX capacity (`40-46`); retained raw is capped at 256 bytes (`core/device/grbl_adapter.h:53`). |
| **MFG-SERIAL-IDENTITY-PATH-ONLY** | **Partial** | Linux commissioned USB tuples are now checked through sysfs (`services/manufacturing/gateway/transport_serial.cpp:161-196`, `238-268`); generic symlinks are refused (`121-143`); opened descriptors use `O_NOFOLLOW` and must be character devices (`296-313`). **Tupleless commissioning remains valid**, and USB checks run only when the allowlist supplied a tuple. There is also no post-open binding of the validated USB identity to the descriptor’s `st_rdev`. See finding 5 and limits below. |
| **MFG-TCP-DRAIN-UNBOUNDED** | **Fixed** | `services/manufacturing/gateway/transport_tcp.cpp:591-611` checks a byte budget and deadline during draining, and no longer duplicates received data into `raw`. Budget is 256 KiB (`transport_tcp.h:23`). An integration regression concerning already-expired deadlines is noted below, separately from the fixed exhaustion issue. |
| **MFG-DNS-POST-AUTH** | **Partial across assigned lane; fixed in native TCP/WS branch** | Native TCP now requires a commissioned address class for DNS names and checks every resolved address before connecting (`transport_tcp.cpp:138-161`, `423-441`). Native WebSocket uses that TCP implementation. **Printer-class connections bypass this transport and use unrestricted Python HTTP resolution instead** (`core/hostd/mfg_lane.cpp:1386-1414`); see finding 3. |
| **MFG-ATTENTION-UNAUTHENTICATED** | **Partial** | Attention mutations and journal query require the caller’s commander lease (`mfg_lane.cpp:2154`, `2211`, `2228`, `2259`, `2271`); acknowledgment identity comes from `caller.seat` (`2213`). Text/item/delivery bounds exist. Remaining problems are tenant/device-independent access and replayable, insufficiently bounded durable writes; see findings 1 and 4. Observer run/attention/approval views remain unrestricted inside the lane. |
| **MFG-CAPABILITY-LEVELS-DROPPED** | **Fixed** | `core/hostd/mfg_lane.cpp:826-840` normalizes worker level arrays into an object and preserves supplied levels. Physical execute remains forcibly `assumed`, and safety integration `unsupported` (`843-857`). The producer remains array-shaped (`services/manufacturing/printers/model.py:218-253`). |

## Yield — surviving findings

### 1. MFG-TENANT-ISOLATION — High
**Manufacturing RPCs and events escape the shared-daemon tenant boundary**

**Confidence:** High, confirmed by source.  
**CWE:** CWE-862, CWE-863, CWE-200.

**Source → missing control → sink**

- Authenticated cloud-capable users reach the shared daemon through `/engine`: `Webservice/gateway/engine_bridge.py:913-969`.
- The request checks restrict document capabilities, selected methods, provider administration, and paths, but do not authorize manufacturing devices, runs, attention items, or journal access: `1019-1055`, `1072-1096`, `1187-1210`.
- Manufacturing reads expose process-global records:
  - `core/hostd/mfg_lane.cpp:2130-2151`: run list/describe.
  - `2200-2208`: attention list.
  - `1856-1873`: approval/proposal inspection.
- Any caller can claim an available, allowlisted device, including the always-present emulator; claim is not account-authorized: `1272-1317`.
- After acquiring its own legitimate lease, a caller can read the entire historical journal (`2270-2284`) and acknowledge/resolve any known attention ID (`2210-2225`, `2258-2268`). No record-owner or tenant check exists.
- Events are broadcast to every connection: `core/hostd/server.cpp:93-95`, `134-140`. Gateway response filtering only recognizes document IDs, so ordinary manufacturing events pass through: `Webservice/gateway/engine_bridge.py:594-610`, `647-703`.

**Prerequisites / impact**

An authenticated cloud-enabled account and an operational shared manufacturing lane suffice for observer disclosure. Acquiring an unoccupied lane lease enables broader historical disclosure and attention mutation. Active leases cannot be stolen—the bearer fix works.

Customer job metadata, artifact hashes, machine addresses, attention text, approvals, and telemetry can cross accounts. A subsequent commander can clear another customer’s attention items.

**Physical consequence:** Physical starts remain blocked. However, if the shared deployment has commissioned printers, an unauthorized tenant can claim an available printer and connect it. Expiring that claim triggers `mfg_printer_adapter_cancel` regardless of whether the lane owns an active run (`mfg_lane.cpp:452-474`); Moonraker implements this as `POST /printer/print/cancel` (`moonraker.py:144-146`). Thus “physical start disabled” does **not** mean this exposed surface cannot interrupt a physical job. Actual commissioned hardware in deployment was not examined.

**Minimal remediation**

At the sole tenant boundary, deny manufacturing RPCs and events unless explicitly supported. If supported, enforce account/device/record authorization and filter events accordingly. A connection-bound commander lease is not a tenant capability.

**Safe isolated reproduction**

Use one temporary `ManufacturingLane`, emulator only, and two distinct raw `LeaseOwner` sessions:

1. A claims the emulator and raises `attention:tenant-a` containing a synthetic marker.
2. B, without a lease, calls `mfg_attention_list`; inspect the marker.
3. A releases; B claims the emulator, queries the journal, and resolves A’s attention ID.
4. Feed the captured synthetic manufacturing event through `_authorize_daemon_response` with empty document capabilities; it is returned rather than dropped.

No approval, physical transport, real account, or network connection is needed.

---

### 2. MFG-ARTIFACT-RETENTION-UNBOUNDED — High
**Repeated bounded artifact registrations permanently grow the shared daemon’s memory**

**Confidence:** High, confirmed by source.  
**CWE:** CWE-400, CWE-770.

**Evidence**

- `core/hostd/mfg_lane.cpp:1649-1683` accepts arbitrary content/line collections, mints a new artifact ID, and retains each complete program in `artifacts_`.
- The owning map is process-lifetime state: `core/hostd/mfg_lane.h:190`.
- All `artifacts_` usages were traced structurally: registration inserts; remaining usages read. Release/device selection do not evict artifacts.
- The emulator is allowlisted without commissioning (`mfg_lane.cpp:279-285`), and registration does not require connection, preflight, or approval.
- Gateway frame-size enforcement (`engine_bridge.py:1008-1016`) limits individual requests, not cumulative retained memory.

**Prerequisites / impact**

A caller holding its own emulator lease can repeatedly submit sub-limit requests. Under finding 1, this includes an ordinary cloud tenant. `[INFERENCE]` Sustained registrations can exhaust the shared hostd’s memory and interrupt other customers’ CAD and manufacturing sessions. No hardware is necessary, and no approval bypass is claimed.

**Minimal remediation**

Enforce cumulative retained-byte and object-count budgets, account/device quotas, and a defined artifact lifecycle. Bound line count as well as content bytes; reclaim unreferenced artifacts on an appropriate lifecycle boundary.

**Safe isolated reproduction**

In a disposable lane, claim only the emulator. Register eight approximately 48 KiB synthetic artifacts and collect the eight distinct returned IDs. Release and reclaim the device; confirm the source-backed registry has no eviction path. Stop there—do not attempt OOM. A temporary test-only inspection hook can measure retained bytes without production changes.

---

### 3. MFG-DNS-POST-AUTH — Medium, residual
**Printer HTTP connections bypass the newly added native resolved-address authorization**

**Confidence:** High, confirmed by source.  
**CWE:** CWE-350, CWE-918.

**Evidence**

- Printer-class `mfg_device_connect` goes directly to worker open/probe and never calls native `TcpTransport::open`: `core/hostd/mfg_lane.cpp:1386-1416`.
- `workerParams()` transfers the hostname URL and credentials, but not the commissioned `address_class` or resolved-address authorization: `521-548`.
- Worker validation compares URL origins as strings: `services/manufacturing/printers/worker_rpc.py:59-66`, `79-96`.
- `EndpointConfig` validates URL syntax and credentials-in-URL, not resolved destination: `model.py:119-134`.
- `HttpClient` uses the default HTTP connection resolver and attaches API-key headers before opening the request: `http_client.py:81-89`, `102-108`.
- Probe immediately makes requests, independently of execute approval: `moonraker.py:50-54`.

**Prerequisites / impact**

A commissioned printer hostname whose DNS resolution an attacker can influence, followed by a legitimate or attacker-triggered connect/poll. No ability to change the allowlist is required.

The daemon worker can contact a different destination, including a disallowed address class. With API-key authentication, the configured printer key is sent to the substituted endpoint. The no-redirect control does not prevent DNS substitution.

**Physical consequence:** Spoofed monitoring responses and loss of printer confidentiality/integrity are possible; the lane’s physical-start refusal remains intact.

**Minimal remediation**

Carry commissioned destination policy into the HTTP adapter; validate and pin the actual connection addresses for every request/reconnection. Preserve hostname handling separately from address authorization. Do not treat equality between `base_url` and `expected_base_url` as destination authentication.

**Safe isolated reproduction**

Use a temporary printer entry with synthetic hostname and `address_class=local`, no credentials. In a Python-only fixture, replace `socket.getaddrinfo` with a deterministic disallowed-address answer and replace `socket.socket` with a fake whose `connect()` records the address and raises a sentinel before any OS socket/network operation. Attempt one probe request. The HTTP path reaches the fake connect; the native TCP equivalent rejects an out-of-class answer before connection.

---

### 4. MFG-ATTENTION-UNAUTHENTICATED — Medium, residual resource/replay defect
**Attention bounds do not bound durable history; replays append indefinitely and delivery payloads remain open-ended**

**Confidence:** High, confirmed by source.  
**CWE:** CWE-400, CWE-770.

The original unauthenticated mutation is fixed; this finding requires a valid commander lease.

**Evidence**

- Repeated acknowledgments append even when already acknowledged: `core/hostd/mfg_lane.cpp:2210-2225`.
- Repeated resolves append even when already resolved: `2258-2268`.
- Raising an existing resolved ID resets its occurrence count; raising an existing item replaces its projected delivery history with an empty array: `2172-2197`, `619-634`.
- Delivery validation bounds only named string fields. Other JSON members survive into the journal and memory: `2237-2255`.
- Every accepted operation serializes, writes, and synchronizes another journal record: `core/manufacturing/journal.cpp:271-287`.
- Replay materializes matching history into a vector (`journal.cpp:112-129`, `291-294`); the RPC’s 1,000-result limit is applied **after** replay (`mfg_lane.cpp:2275-2281`).

**Prerequisites / impact**

A commander, including a cloud tenant able to claim the emulator under finding 1. Small replays defeat the apparent per-item bounds and can cause durable disk growth and increasingly expensive replay/startup. Arbitrary extra delivery members permit much larger records than the documented field bounds suggest. No physical device or approval is required.

**Minimal remediation**

Make repeated ack/resolve idempotent, validate a closed bounded delivery schema, retain appropriate occurrence/delivery accounting across re-raises, and bound journal ingestion/replay independently of response count.

**Safe isolated reproduction**

Create one temporary attention item. Acknowledge it three times and resolve it three times; observe distinct journal sequence increments despite identical logical operations. Separately submit one delivery with a harmless 1 KiB extra member and confirm persistence. To examine the delivery-counter reset, use the existing 64-record test fixture, re-raise the same ID, then submit one additional delivery. Do not grow the journal beyond these bounded examples.

---

### 5. MFG-SERIAL-IDENTITY-PATH-ONLY — Low, conditional residual
**Tupleless serial commissioning still authorizes replacement hardware by pathname**

**Confidence:** High for the configuration-dependent behavior.  
**CWE:** CWE-290, CWE-346.

**Evidence**

- A serial identity with zero USB fields remains valid: `services/manufacturing/gateway/device_allowlist.cpp:269-278`.
- `SerialTransport::open` performs USB identity checks only when the commissioned entry has a nonempty VID: `transport_serial.cpp:238-268`.
- Subsequent checks establish a character-device descriptor and exclusivity, not the identity of a tupleless device: `296-329`.

**Prerequisites / impact**

An installation commissioned by a reusable path such as `/dev/ttyUSB0`, plus physical ability to replace the peripheral so that the path is reused. This is **not** a remote privilege-escalation claim. Actual commissioning choices were not inspected.

The replacement endpoint receives identification traffic and can supply false telemetry. Current physical execution remains disabled. Linux tuple-bearing entries materially reduce this risk; they are not broken by this finding.

**Minimal remediation**

Require an appropriate stable identity for physical USB devices, or explicitly label and restrict tupleless commissioning as a weaker mode. Bind post-open descriptor identity to the identity validated before opening.

**Safe isolated reproduction**

Use two PTYs and a temporary configured by-id directory. Commission a tupleless temporary alias, open/close it against PTY A, repoint the alias to PTY B, and reopen. Both are accepted. Contrast with the existing fake-sysfs USB-tuple fixture, which refuses a changed serial. No real `/dev/serial` entry or hardware should be touched.

## Yield — positive controls and safety limits

### Controls confirmed by source

- **Physical start is explicitly refused**, even after connection: `mfg_lane.cpp:1896-1912`.
- All raw IPC peers are classified as nonhuman (`server.cpp:774-780`); approval creation rejects them (`mfg_lane.cpp:1798-1803`). Request-supplied initiator/approver identity is rejected.
- Arbiter rechecks freshness, approval bindings, expiry, policy, and gates before one-shot durable nonce burn: `core/manufacturing/arbiter.cpp:123-194`, `241-365`; `approval.cpp:13- sixty-four` **[location clarification: `approval.cpp:13-64`]** and `104-125`.
- Journal sequence is reserved before append; append is synchronized before returning. Torn-tail repair and exclusive writer locking exist: `journal.cpp:132-156`, `195-217`, `271-287`.
- Leases are exclusive, caller-bound, and TTL-limited to 60 seconds: `lease.h:24-25`; `lease.cpp:33-120`.
- `mfg_run_resume` is refusal-only: `mfg_lane.cpp:2059-2104`.
- All `mfg_` names are intercepted; unknown names are refused rather than reaching the generic worker fallback: `mfg_lane.cpp:240-252`, `2331-2332`.
- Native WebSocket checks masking, RSV/opcodes, canonical lengths, fragmentation, UTF-8, handshake acceptance, and configured frame/message limits.
- The MCP `manufacture` family defaults off: `services/agent/config.py:638`, `1060-1102`; it is registered under that family in `services/mcp/server.py:415-416`. **This is not a raw IPC/web gateway authorization control.**

### Non-finding limitations / integration issues

- Lease expiry is serviced when another manufacturing request enters `handle`, not by an autonomous timer (`mfg_lane.cpp:240-252`, `452-495`). Do not describe it as an independent physical watchdog.
- Native TCP’s new expired-deadline behavior conflicts with `GrblAdapter::service`, which polls using `steady_clock::now()` (`grbl_adapter.cpp:107`). TCP checks that already-expired deadline before receiving (`transport_tcp.cpp:593-596`). This is a source-visible integration regression, not evidence that the old unbounded-drain vulnerability survives.
- Serial post-open identity TOCTOU hardening is incomplete: no `st_rdev`/USB correspondence check. I did **not** establish a separate unprivileged race exploit against a correctly configured tuple-bearing device.
- Native DNS classes are coarse: `local` includes both private and link-local addresses (`transport_tcp.cpp:100-118`). Cross-class rejection is not exact machine authentication.
- Physical-start blocks make several deeper execution-quality concerns unreachable from present raw IPC callers. No claim of an end-to-end physical-start or approval-replay bypass is made.

## Yield — reviewed paths, checks, and coverage

### Reviewed paths/subareas

1. `docs/manufacturing/23_security_review.md`: historical findings and coverage/limits; all eight assigned IDs revalidated.
2. `core/hostd/mfg_lane.{cpp,h}`: routing, claims, lifecycle, artifacts, preflight, approvals, starts/holds/aborts/resume refusal, attention, journal queries, events, worker boundary.
3. `core/manufacturing/`: implementation files for approval, arbiter, lease, journal, gates, tier0, policy, invalidation, and result registry; relevant approval/lease/freshness declarations.
4. `core/device/`: Grbl adapter/protocol, numeric parsing, emulator transport, identity, capability, controller state transitions, and transport interfaces.
5. `services/manufacturing/gateway/`: allowlist, serial, TCP, and WebSocket implementations and transport configuration headers.
6. Dependency excerpts: `core/hostd/server.cpp`; `Webservice/gateway/engine_bridge.py`; printer `worker_rpc.py`, `http_client.py`, `model.py`, `moonraker.py`; MCP `mfg_tools.py` and registration; agent manufacture-family configuration.
7. Existing tests read: `test_mfg_lane.cpp`, `test_device_lane.cpp`, `test_transport_serial.cpp`, `test_transport_net.cpp`, plus relevant authorization/foundation/protocol/MCP test sections.

### Exact existing checks for integrator consideration — not run

Native CTest names are registered in `core/CMakeLists.txt:2553-2596`:

- `test_mfg_lane`
- `test_mfg_authorization`
- `test_mfg_foundation`
- `test_device_lane`
- `test_grbl_protocol`
- `test_transport_serial`
- `test_transport_net`

Particularly relevant existing cases include:

- “lease bearer value is caller-bound and redacted from observers”
- “artifact registration rejects filesystem path sources”
- “terminator-free Grbl input terminalizes at the RX line ceiling”
- “POSIX by-id serial open verifies the commissioned sysfs USB tuple”
- “TCP flood is capped per poll, carries no raw copy, and honors an expired deadline”
- “TCP refuses hostile DNS resolution outside the commissioned address class”
- “physical printer worker cannot promote execute authority”
- “attention mutations enforce text and delivery history bounds”

Python default-off check:  
`services/mcp/test_mfg_tools.py::test_default_off_family_denies_every_tool_without_calling_daemon`

**Coverage summary:** Five source-supported surviving findings/residuals; five historical IDs fixed and three partially addressed. No builds, tests, scanners, payloads, git commands, network calls, or hardware probes were performed. Deployment commissioning, filesystem ownership/ACLs, live entitlements, enabled tool-family settings, and actual hardware availability remain unavailable evidence. The concurrent working tree was read without claiming an atomic repository snapshot.
