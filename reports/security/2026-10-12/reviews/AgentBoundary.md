## yield — Findings

**Read-only, source-confirmed audit. No tests, payloads, builds, scanners, network calls, or product edits were performed.** Reproduction steps below are proposed for the integrator, not exercised results.

### A1 — Critical: authenticated web clients can execute arbitrary Python through `run_model`
**CWE-94: Code Injection**

**Execution path**
- The gateway blocks `agent_admin_turn` and `doc_file_operations`, but not `run_model`: `Webservice/gateway/engine_bridge.py:1031-1060`.
- Path validation examines selected JSON field names, not Python embedded in `code`: `engine_bridge.py:215-271`.
- Unknown engine methods are forwarded to the services worker: `core/hostd/server.cpp:1123-1180`.
- `run_model` is registered and directly calls `run_code_multi(params["code"], ...)`: `services/worker_main.py:56-65,95-100`.
- `_exec_model` uses unrestricted Python `exec`; no replacement `__builtins__`, privilege reduction, or sandbox is installed: `services/kernel_exec.py:355-369,545-560`.

**Privilege/impact:** Any account allowed to connect to the engine bridge can execute code as the shared services-worker OS user. Neither an AI subscription nor model cooperation is required. This grants access to all files, daemon sockets, credentials and processes accessible to that service account—not root privileges unless the deployment itself grants them. Confidentiality, integrity, availability and potentially connected-device safety are affected.

**Nearby controls:** The subprocess helper is crash isolation, not a security sandbox; this RPC uses the in-process executor anyway. Gateway filesystem checks cannot constrain code strings.

**Minimal remediation:** Deny this method to browser clients; if genuinely required remotely, execute it in a separately authorized, resource-bounded tenant sandbox without daemon/control/credential access.

**Safe reproduction:** In the existing gateway `FakeDaemon` fixture, send `run_model` with `code:"print('AUDIT_FIXTURE_ONLY')"` and confirm it is forwarded. Separately invoke `run_code_multi` in a disposable process with `_build123d_namespace` temporarily replaced by an empty namespace; use code that creates only `<temporary-directory>/marker`. The returned model failure does not undo the marker, demonstrating execution before geometry validation.

---

### A2 — High: `agent_resolve_provider` returns ambient service API keys to ordinary web accounts
**CWE-200 / CWE-522: Sensitive Information Exposure / Insufficiently Protected Credentials**

**Execution path**
- This RPC is not an administrator-only `agent_provider_*` method and is not in the gateway’s credential-gated inference set: `engine_bridge.py:280-283,1036-1060`.
- The native handler returns the complete resolved provider descriptor directly: `core/api/libchudware.cpp:1391-1398`.
- An absent key in the supplied environment map falls back to actual process environment: `core/agent/config.cpp:181-187`.
- OpenAI, Anthropic and Gemini provider descriptors include the resulting raw `api_key`: `config.cpp:461-526`.
- The gateway’s general response handling does not redact provider descriptors: `engine_bridge.py:642-705`.

**Prerequisite/impact:** An authenticated browser user, and a supported provider credential present in the daemon environment. The key can be disclosed without invoking the provider or owning that credential. If no such ambient key exists, this particular request does not manufacture one.

**Nearby controls:** `share_provider_keys=False` protects the gateway’s model-list injection, not this independent native resolver. An injected billing actor is ignored by this handler.

**Minimal remediation:** Remove credential-resolution RPCs from the browser surface. Expose only a sanitized model/provider metadata response, and never return secret-bearing descriptors.

**Safe reproduction:** In a fresh isolated native test process, set `OPENAI_API_KEY=AUDIT_SENTINEL_NOT_A_KEY`, supply `{"name":"openai","cfg":{},"env":{}}` to `agent_resolve_provider`, and assert the sentinel appears in `api_key`. No HTTP is needed. Separately assert gateway forwarding with `FakeDaemon`.

---

### A3 — High: private runtime administration and control-token discovery are exposed through the gateway
**CWE-862: Missing Authorization**

**Execution path**
- The gateway protects `agent_provider_*` and six legacy subscription methods, but does not protect `chudcode_*`, `memory_*`, or `worker_control_info`: `engine_bridge.py:284-301,1031-1060`.
- Actual runtime auth administration is registered under `chudcode_set_key`, `chudcode_remove_key`, `chudcode_logout`, `chudcode_login`, and `chudcode_auth_state`: `services/agent/worker_rpc.py:2080-2140,3830-3859`.
- These use the shared service client, constructed without tenant credentials: `services/agent/chudcode_host.py:2496-2523,2607-2644`.
- Memory RPCs likewise directly forward caller-selected `root` to that service: `worker_rpc.py:2628-2637`; accepted shapes: `chudcode_host.py:251-263`. `root` is not a gateway `_PATH_KEYS` entry.
- `worker_control_info` returns `server.info()` verbatim: `services/worker_main.py:78-82,99`; that response includes the worker token: `chudcode_control.py:117-125`.

**Privilege/impact:** Ordinary authenticated accounts can manipulate operator runtime authentication, inspect operator account labels, and address shared memory namespaces. They can also obtain a private control credential. POSIX Unix-socket access is still independently restricted; disclosure alone does not make the socket remotely reachable.

**Nearby controls:** Auth responses scrub secret fields and literal submitted keys. That prevents one form of accidental reflection, but does not authorize the operation. The control socket’s random token, 0600 permissions and constant-time comparison are good controls undermined by public token discovery.

**Minimal remediation:** Classify runtime administration as operator-only or unavailable on the web bridge. Keep `worker_control_info` internal. Bind memory requests to a server-derived account/project namespace.

**Safe reproduction:** Use gateway `FakeDaemon` to record forwarding of the named methods with dummy identifiers. Replace `_service_client` with a recording fake, call `chudcode_remove_key` using a fixture provider, and assert it reaches the shared service without actor context. Replace `_CONTROL_SERVER` with an object returning a fixed fake token; assert the registered discovery method returns it. Never invoke real logout/login/set-key operations.

---

### A4 — High: conversation, subagent and active-run controls lack enforced tenant ownership
**CWE-639 / CWE-862: User-Controlled Object Reference / Missing Authorization**

**Execution path**
- Gateway authorization collects document IDs, but not ownership of `session_id`, `subagent_id`, or `run_id`: `engine_bridge.py:595-635,1020-1060`.
- Subagent scope is optional; omitted filters explicitly preserve global behavior: `services/agent/subagent_rpc.py:51-76`.
- `subagent_list({})` enumerates the shared registry; transcript/control operations accept those returned IDs without mandatory account scope: `subagent_rpc.py:94-172,176-280`.
- Registry records disclose owning session/run/project and transcript metadata: `services/agent/subagent_registry.py:344-387`.
- Chudcode maintenance resolves sessions solely by ID: `services/agent/chudcode_host.py:1979-2076`. Close similarly uses only the ID: `1417-1437`.
- Online `session_handoff` returns a document generated from the named session’s authoritative history without owner validation: `services/agent/worker_rpc.py:481-504`; `services/agent/session_cmds.py:340-379`.
- Hostd routes cancel/steer/answer to an active worker using `run_id`, without the requesting client/account: `core/hostd/server.cpp:1029-1037`; `core/hostd/worker_bridge.cpp:614-629,660-692`.

**Privilege/impact:** An authenticated web account can enumerate existing shared subagent records, read their transcripts, and queue steering or controls. Known live conversation/run IDs also permit unauthorized maintenance and control; handoff can disclose conversation content. Main-session attacks require an existing target and its identifier; global subagent enumeration supplies identifiers for applicable registered runs.

**Nearby controls:** Explicit project/session filters correctly refuse mismatches, including blank filters. They are caller-selected filters, not authorization. Credential identity is checked when claiming a session for a **new turn**, but not these management operations.

**Minimal remediation:** Associate sessions/runs/subagents with immutable tenant ownership and check that identity at every access. Gateway must inject mandatory scope, not trust optional user-supplied filters. Do not treat an opaque ID as an authorization capability.

**Safe reproduction:** Under `CHUDWARE_SUBAGENTS_DIR=<tmp>`, create two terminal fixture records for different project/session owners and append harmless distinct transcript markers. Call `subagent_list({})`, then `subagent_transcript` for the other fixture without scope. Compare with the existing correctly refused explicit-scope case. For Chudcode controls, use `FakeFactory` and two fake live sessions; demonstrate management by ID requires no actor. No live agents should be involved.

---

### A5 — High: `/btw` and delegated role turns drop web credential and workspace scope
**CWE-441 / CWE-863: Confused Deputy / Incorrect Authorization**

**Execution path**
- `session_btw` is explicitly web-allowable and receives a gateway-validated actor/key: `engine_bridge.py:280-283,812-909,1095-1187`.
- `_session_btw` forwards only messages, question, provider, model, session ID and offline flag. It ignores the injected actor, API key and `options.workspace_dir`: `services/agent/worker_rpc.py:508-534`.
- `_run_one_shot_role` only requires a nonempty session ID; it does not verify a live parent. It creates role options without actor or workspace credentials: `services/agent/session_cmds.py:216-264`.
- `run_role_turn` constructs runtime parameters without `web_actor`, `web_tenant`, `web_credential` or a BYOK scope: `services/agent/chudcode_roles.py:814-836`.
- `_credential_scope` therefore interprets this as desktop execution, and `_make_client` inherits ambient environment/home: `services/agent/chudcode_host.py:155-189,2239-2269`.
- The delegated-role path has the same omission even when `actor=self.chudware_actor` is supplied: `services/agent/mission_dispatch.py:1771-1784`; `chudcode_roles.py:814-836`.

**Prerequisite/impact:** For remote `/btw`, the user must satisfy the gateway’s credential/pool gate, and the ambient runtime must have a usable selected provider for successful inference. The user’s own verified credential is then ignored in favor of operator credentials. Private prompts can be sent under the wrong account; billing/account isolation breaks. If ambient credentials are absent, legitimate BYOK requests instead fail.

**Nearby controls:** The normal `_agent_admin_turn` validates digest proofs and creates credential-scoped clients correctly. The tool-free CAD helper path also explicitly propagates credential scope. Neither protects this role-turn adapter.

**Minimal remediation:** Propagate a trusted credential/workspace context through every role/side-question adapter and fail closed for web callers. A supposedly tool-free side question should use the existing `complete_text` seam with inherited scope, rather than an ordinary general-agent session.

**Safe reproduction:** Reuse `test_chudcode_roles.py`’s recording `_Host` and `online_runtime` fixture. Supply a fixture actor to `run_role_turn` and inspect captured `host_params`: no web credential context is present. For `_session_btw`, replace `btw_answer` with a recorder and provide a fake gateway actor/key/workspace; assert none are forwarded. No real provider or credentials are needed.

---

### A6 — High: builtin execution bypasses host-tool modes and filesystem/credential controls
**CWE-863: Incorrect Authorization**

**Execution path**
- Every ordinary runtime session is launched with `read`, `bash`, `edit`, `write` and `eval`: `services/agent/chudcode_host.py:100-103,2306-2313`.
- Ask/Plan restrictions and dispatch-time policy are enforced only for CAD host bindings: `services/agent/chudcode_tools.py:732-810,1470-1518,1571-1575`.
- `_configure_turn` registers host tools but does not narrow builtin tools by mode: `chudcode_host.py:2399-2493`.
- `/btw` advertises “Any tool call … is discarded without executing,” but implements only prompt text plus `allowed_tools:[]`: `services/agent/session_cmds.py:75-86,244-260`. That empty list still gets host-tool floors and does not remove runtime builtins.
- Host `run_shell` itself only restricts the starting directory; the source explicitly acknowledges absolute paths and `cd` escape. It inherits the worker environment: `services/mcp/fs_tools.py:1158-1201`.
- Alternative host filesystem tools are also not globally jailed: `office_read_csv` directly opens arbitrary paths and `office_edit_csv` writes arbitrary destinations: `services/mcp/office_tools.py:769-812`. The common registry wrapper applies policy/leases, not path containment: `services/mcp/tool_registry.py:602-648`.

**Privilege/impact:** In desktop usage this is **not OS privilege escalation**; it bypasses the human’s restrictive mode/tool choice using the application’s existing same-user privileges. With web `/btw` from A5, an attacker-controlled question reaches an ordinary, ambient runtime with these capabilities. Actual malicious tool selection requires model cooperation; the authorization gap itself is explicit in source.

**Nearby controls:** `fs_*` canonical path and sensitive-directory checks are real for those tools. They are not an enforced sandbox around builtin execution, shell contents, or other tool families. Clean `HOME`/environment in the normal web client is credential selection, not filesystem/process isolation.

**Minimal remediation:** Enforce effective permissions on **every** execution surface, including builtin read/write/bash/eval and nested agents. Use an OS sandbox for remote tenants. Remove all tools from truly tool-free turns. Route host filesystem tools through a shared authorized-root policy.

**Safe reproduction:** Using `FakeFactory`, compare session launch arguments for Agent, Ask and Plan; each contains the same execution builtins. Compare with the existing host-binding test that refuses `add_features` in Ask. For the path discrepancy, register `fs_read` and `office_read_csv` in a capture registry; set root to temporary directory A and place harmless CSV in sibling B. `fs_read` refuses B, while `office_read_csv` reads it. No shell or real model is necessary to demonstrate the missing controls.

---

### A7 — High: native assist routes ignore web actors and resolve operator credentials
**CWE-441: Confused Deputy**

**Execution path**
- Gateway handling specially meters/validates `agent_ask_v2` and `agent_clarify_v1`, but not their native aliases `agent_answer_question` and `agent_clarify`: `engine_bridge.py:280-283`.
- Native aliases converge on the same handlers: `core/api/libchudware.cpp:1115-1120,2409-2474`.
- `resolve_assist_provider` loads ambient config and resolves credentials without consulting `_chudware_actor` or the gateway-injected top-level API key: `libchudware.cpp:454-464`.
- Credential lookup uses config/environment, followed by actual provider execution: `core/agent/config.cpp:461-526`; `libchudware.cpp:2430-2435,2467-2473`.

**Prerequisite/impact:** An ordinary authenticated browser account and a usable ambient configured provider. Direct native aliases bypass the gateway’s inference credential/credit reservation branch and spend operator credentials. Even the checked aliases do not consume the injected tenant credential at this sink.

**Minimal remediation:** Remove native provider helpers from the public RPC surface, or route every alias through the same actor-scoped inference service. Canonicalize aliases before applying gateway policy.

**Safe reproduction:** In gateway `FakeDaemon`, compare forwarding and actor/credit handling of the checked and native aliases. In a native isolated fixture, replace the HTTP transport with a recorder and use a temporary config containing `AUDIT_OPERATOR_SENTINEL`; show the assist resolver chooses it instead of a distinct dummy top-level tenant key. Do not contact a provider.

---

### A8 — High: firmware source operations can escape their project root
**CWE-22 / CWE-59: Path Traversal / Improper Link Resolution**

**Execution paths**
1. Plain `add` values bypass Python path normalization: `services/mcp/firmware_tools.py:388-396`. The native sink concatenates `project.root_dir() / rel`, creates parent directories and creates a file without checking containment: `core/firmware/firmware_rpc.cpp:105-140`.
2. The Python `write` path rejects lexical `..`, but resolves containment only for absolute paths. Relative paths through an existing symlink pass: `firmware_tools.py:196-213`, then `root / rel` is written at `425-435`.

**Privilege/impact:** A direct authenticated web `fw_sources` request can create an empty file outside the firmware/workspace root using a string in `add`; the gateway does not inspect string-array values under that key. Existing files are not overwritten by the native stub path. The Python symlink variant can overwrite content outside the project when the attacker can supply a symlink in that firmware tree and invoke the MCP authoring tool. Neither requires firmware flashing.

**Nearby controls:** Absolute-path and `..` tests cover the Python `write` branch only. The native layer does not enforce the same invariant.

**Minimal remediation:** Canonicalize and authorize every source path at the native boundary, including plain `add`; use safe root-relative opens that cannot follow escaping symlinks. Apply the same guard to Python writes.

**Safe reproduction:** In an isolated engine process with `CHUDWARE_ENGINE_DAEMON=0` and `CHUDWARE_FIRMWARE_DIR=<tmp>/firmware`, create one fixture project. Add `../../audit-empty.txt` and inspect only the temporary ancestor. For the Python branch, create `project/link -> <tmp>/outside`, then write `link/audit.txt` containing a harmless marker. Never build or flash the fixture.

---

### A9 — Medium: advertised result caps do not bound producer memory
**CWE-400 / CWE-770: Uncontrolled Resource Consumption**

**Sources/sinks**
- `fs_read(limit=1)` still reads and decodes the whole file and splits all lines before paging: `services/mcp/fs_tools.py:762-783`.
- `run_shell` buffers the complete combined output with `communicate()`, and only afterwards truncates to 96 KiB: `fs_tools.py:1196-1227`.
- Image attachment processing reads and base64-encodes an entire caller-named file without a byte/type check: `services/agent/chudcode_host.py:3387-3434`.
- Result-image loading reads all bytes before enforcing encoded-image limits: `services/agent/tool_images.py:248-272`.

**Privilege/impact:** A malicious or unexpectedly large workspace input/tool output can exhaust a worker’s memory. Small output envelopes and time limits do not cap peak allocations. No claim is made that production lacks an external cgroup limit; that deployment evidence was unavailable.

**Minimal remediation:** Bound input/output bytes during acquisition, use streaming reads, reject oversized attachments before allocation, and enforce per-worker memory limits. Continue draining shell output without accumulating it beyond the cap.

**Safe reproduction:** Use only a 1–2 MiB temporary text fixture. Spy on `Path.read_bytes` to record full bytes consumed by `fs_read(limit=1)`. For shell output, use a fake `Popen.communicate()` returning a fixed 1 MiB buffer and observe post-collection truncation. Do not attempt an OOM.

---

### A10 — Medium: `fs_grep` accepts catastrophic-backtracking regexes without an interruptible bound
**CWE-1333: Inefficient Regular Expression Complexity**

**Execution path:** Model/caller-selected pattern is compiled with Python `re`, then applied synchronously to every input line: `services/mcp/fs_tools.py:1040-1080`. The 8 MiB file cap and 500-match cap do not bound one `rx.search` call. The tool has no cancellation or timeout parameter; cancellation is checked before host dispatch: `services/agent/chudcode_tools.py:1554-1563`.

**Privilege/impact:** A crafted search pattern and short matching-prefix text can monopolize the worker. This is independently reachable in read-only tool modes; no mutation or shell permission is needed.

**Minimal remediation:** Use a linear-time regex engine, or execute searches with a reliably enforceable deadline outside the worker’s interpreter. Keep traversal and byte limits as well.

**Safe reproduction:** Only in a spawned disposable process with a two-second parent timeout, search a fixture line of approximately 18–22 `a` characters followed by `!` using `^(a+)+$`. Compare bounded input sizes; never increase until the shared worker hangs.

---

### A11 — Medium: native Ask/Plan requests can take mutating drawing/CAM fast paths
**CWE-863: Incorrect Authorization**

**Execution path**
- `agent_run` normalizes mode/`plan_only`: `core/api/libchudware.cpp:1990-2011`.
- Its deterministic drawing fast path calls `drawing_make` without checking those restrictions: `libchudware.cpp:2069-2096`.
- The adjacent CAM path similarly calls `cam_program_from_part`: `libchudware.cpp:2120-2145`.
- Mode-sensitive design routing occurs only later: `libchudware.cpp:2183-2202`.

**Privilege/impact:** A user explicitly choosing read-only Ask/Plan can nevertheless get a new drawing or CAM job. This is a mode-integrity failure, not tenant privilege escalation or proof that hardware executes the generated program.

**Nearby controls:** The native entry rejects online providers and honors document authorization at the gateway. Neither checks whether a permitted document may be mutated in this mode.

**Minimal remediation:** Apply mode authorization before deterministic fast paths, not just the subsequent agent pipeline.

**Safe reproduction:** In a private in-process engine fixture with a tiny part and deterministic mock provider, call `agent_run` with `options={"offline":true,"mode":"plan","plan_only":true}` and a drawing request. Assert whether a drawing document is created. Do not use a live daemon or manufacturing hardware.

---

## yield — Reviewed paths and coverage

1. **Online ingress, actor/credential propagation, session lifetime:**  
   `services/agent/worker_rpc.py`, `online_turn.py`, `chudcode_host.py`, `chudcode_roles.py`, `session_cmds.py`, relevant `mission_dispatch.py` delegation sections, `providers/chudcode_provider.py`.
2. **Host tool advertisement, dispatch and mode controls:**  
   `chudcode_tools.py`, `copilot_mode_tools.py`, `preferences.py`, `tool_categories.py`, `contracts/copilot_modes.json`, relevant guardrail and role-source searches.
3. **Filesystem and dangerous sinks:**  
   `services/mcp/fs_tools.py`, `office_tools.py`, `firmware_tools.py`, `tool_registry.py`, `services/kernel_exec.py`; sink searches across MCP tool modules and agent Python modules.
4. **Control and object scope:**  
   `chudcode_control.py`, `subagent_rpc.py`, `subagent_registry.py`, `raw_log.py`; supporting `services/worker_main.py`, `core/hostd/server.cpp`, `worker_bridge.cpp`.
5. **Prompt/image/result/log egress:**  
   `tool_images.py`, relevant `chudcode_events.py` mapping sections, result-envelope/spill logic, attachment handling, role-call secret scrubbing, `providers/logging_provider.py`.
6. **Native agent boundary:**  
   Relevant `core/agent/run.cpp`, `config.cpp`, `copilot_modes.cpp`, `paths.cpp`, `ask.cpp`, `clarify.cpp`, `http.cpp`; targeted auth/provider searches; supporting native RPC and firmware sink sections.
7. **Gateway reachability:**  
   `Webservice/gateway/engine_bridge.py`, including request gates, actor injection, path/document authorization and response filtering.
8. **Existing test source inspected:**  
   `test_chudcode_host.py`, `test_chudcode_tools.py`, `test_chudcode_roles.py`, `test_chudcode_control.py`, `test_worker_rpc_chudcode.py`, `test_session_cmds.py`, `test_subagent_registry.py`, `test_mode_tools.py`, `test_web_subscription_turn.py`, MCP filesystem/firmware tests and gateway engine/path-jail tests.

## yield — Positive controls, rejected candidates and limits

**Positive controls**
- Normal web `_agent_admin_turn` verifies credential proofs, refuses missing/unsupported credentials and rejects live-session credential switching.
- Credential-scoped runtime clients blank inherited environment credentials and use private temporary homes and 0700 session directories.
- Tool-free CAD helper completions explicitly carry owning credentials and have bounded cleanup.
- `fs_*` resolve paths, reject ordinary outside-root symlinks and sensitive paths, and check recursive descendants.
- Host-tool mode/policy failures fail closed; model-supplied helper runtime/cancellation callbacks cannot replace host-owned capabilities.
- Control transport has strong random tokens, constant-time checks, private POSIX sockets, bounded frames/connections and sanitized handler failures.
- Transcript export resolves and contains the output within the session’s project.
- Explicit subagent scope checks correctly reject foreign/blank scope.
- MCP’s shipped entrypoint is stdio, not an independently exposed unauthenticated HTTP server.
- Provider sidecar body logging requires full consent; raw transcript files are initially created with 0600 permissions.

**Important rejected/qualified claims**
- **Do not claim ordinary `agent_run_v2` currently exposes online Chudcode turns:** native `agent_run` rejects online providers, while the gateway blocks `agent_admin_turn`. The concrete alternate online route found here is `session_btw`.
- Arbitrary text in a tool result was **not** shown to become a trusted image reference automatically. `image_refs` traverses mappings/lists, not arbitrary nested text; no standalone image-reference injection finding survives.
- Prompt injection by itself is not treated as a sandbox escape. The reported defects are missing authorization/enforcement at identifiable execution boundaries.
- Authorized desktop same-user tools are not reported as remote OS privilege escalation.
- Native online handoff code also drops some mode fields, but its practical reachable behavior is more constrained by the native provider gate; A11 uses the definite deterministic path instead.

**Limits**
- Current source was reviewed, not deployed process configuration or external isolation.
- Runtime binary/provisioning and provider advisories belong to the separate assignment. No runtime internals were executed or assumed verified.
- Individual geometry/simulation/manufacturing implementations were not exhaustively audited here; this review covered their agent registration/dispatch boundary and the named high-risk ingress/egress paths.
- Actual secrets, production data and live customer/device sessions were not accessed.
- Source is concurrently edited; locations describe the reads made during this audit.

## Suggested existing checks for the integrator

Run only in isolated fixtures:
- `services/agent/test_chudcode_host.py`
- `services/agent/test_chudcode_tools.py`
- `services/agent/test_chudcode_roles.py`
- `services/agent/test_worker_rpc_chudcode.py`
- `services/agent/test_chudcode_control.py`
- `services/agent/test_session_cmds.py`
- `services/agent/test_subagent_registry.py`
- `services/mcp/test_fs_tools.py`
- `services/mcp/test_firmware_tools.py` — retain its private-root/in-process-engine isolation
- `Webservice/gateway/tests/test_engine_bridge.py`
- `Webservice/gateway/tests/test_engine_path_jail.py`

These existing checks establish useful neighboring controls, but do not establish tenant authorization for the newly identified RPC routes. Add the bounded fixture cases above before treating the audit findings as closed.
