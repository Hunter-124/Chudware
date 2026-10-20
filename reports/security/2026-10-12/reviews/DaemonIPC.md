## Yield — findings

**Five substantiated findings.** All are confirmed by current source inspection, **not exercised**. No files changed, commands/tests run, network calls made, or live daemon contacted.

### 1. Critical — web tenants can invoke unrestricted daemon process execution and global administration

**CWE:** CWE-862, CWE-749  
**Boundary:** Authenticated web account → shared daemon OS account. This is **not** a finding against authorized same-user desktop IPC.

**Source → sink**

- `Webservice/gateway/engine_bridge.py:1018-1055` rejects unauthorized document IDs and a small set of methods, but not `agent_spawn`, `agent_stop`, `agent_list`, `daemon_drain`, or `recovery_*`.
- Its path filter recognizes named dictionary keys, not strings inside `argv`: `engine_bridge.py:215-232`.
- Agent sanitization retains attacker-supplied `argv` and `env`; it primarily replaces actor/provider metadata: `engine_bridge.py:812-875`.
- The remaining request is forwarded to the shared daemon at `engine_bridge.py:1208-1209`.
- `core/hostd/server.cpp:1007-1012` routes child-supervision methods without a tenant/administrator check.
- `core/hostd/child_supervisor.cpp:297-315` accepts arbitrary executable arguments and merges arbitrary environment overrides with the daemon’s inherited environment.
- Execution occurs at `child_supervisor.cpp:368` (`execvpe`), or `:375` on the Darwin branch. Windows has the corresponding arbitrary `CreateProcessW` path at `child_supervisor_win.cpp:349-391,450-451`.

**Impact**

A cloud-enabled, non-admin account can execute commands with the shared daemon’s filesystem and environment access, defeating every tenant/document restriction enforced only by the gateway. Customer confidentiality, integrity, and availability are affected. No root privilege is required or claimed.

**Other manifestations of this same unrestricted administrative surface**

- `daemon_drain` is globally effective: `server.cpp:831-847`.
- `agent_stop` accepts another caller’s `child_id`; `spawnerClient` is attribution, not authorization: `child_supervisor.cpp:226-250`.
- `recovery_list` exposes the shared recovery inventory; `recovery_discard {"all":true}` deletes all tenants’ recovery artifacts:
  - `server.cpp:875-889`
  - `autosave.cpp:464-480`
- `recovery_open {"all":true}` consumes shared recovery artifacts and attaches the requesting connection as a writer: `server.cpp:891-936`. Do **not** assume the gateway grants subsequent document capabilities here—it does not classify `recovery_open` as a mint—but unauthorized consumption/deletion is already established.

**Nearby controls considered**

Gateway authentication, cloud-plan checks, document capabilities, path checks, and actor rewriting exist. None authorizes executable arguments or these daemon-administration methods. Installed-payload/runtime checks before spawning validate the Chudware installation, not `argv[0]` or the caller’s authority. Process groups/PDEATHSIG control lifecycle, not tenant access.

**Minimal remediation**

Use a default-deny web RPC allowlist with per-method parameter and ownership rules. Keep process execution, control-endpoint discovery, daemon lifecycle, and global recovery administration off tenant connections. Any permitted recovery operation needs tenant-owned recovery identifiers.

**Safe isolated reproduction**

Use only a scratch gateway/test account fixture connected to a **new scratch daemon**. Give that daemon temporary `HOME`, `XDG_DATA_HOME`, and private runtime directory; disable network access and provide no credentials.

Through the fixture’s non-admin WebSocket, send:

```json
{"id":1,"method":"agent_spawn","params":{"argv":["/usr/bin/touch","/ABSOLUTE/SCRATCH/ONLY/executed"]}}
```

Use an existing harmless executable appropriate to the platform. The expected source-derived result is a child ID and creation of the scratch marker despite it not being an authorized web workspace operation. Never run this against the open daemon.

**Integrator coordination:** This finding crosses the gateway assignment; merge with its unrestricted-RPC finding rather than count these method variants separately.

---

### 2. High — malformed `agent_spawn` metadata escapes the supervisor thread and terminates the shared daemon

**CWE:** CWE-248  
**Boundary:** Web-supplied RPC parameter → uncaught exception in daemon-lifetime thread.

**Evidence**

- POSIX `ChildSupervisor::reaperLoop` invokes `doSpawn(*req)` without exception containment: `core/hostd/child_supervisor.cpp:129-139`.
- After successfully launching the child, `doSpawn` reads strings using typed accessors:
  - `child_supervisor.cpp:402`: `params.value("mission_id", "")`
  - `child_supervisor.cpp:403`: `params.value("doc_id", "")`
- An integer, array, or object at these fields throws a JSON type exception.
- Windows has the same root cause:
  - `child_supervisor_win.cpp:186-195`
  - `child_supervisor_win.cpp:480-481`
- The request is handed to the supervisor thread through a promise/future at `child_supervisor.cpp:253-271`. The exception does not propagate back through that future; it escapes the thread entry and invokes `std::terminate`.
- The per-request catches in `server.cpp:649-662` cannot catch an exception thrown on this other thread.

**Prerequisites and impact**

On a serving installation where child spawning is permitted, an authenticated cloud account can reach this via finding 1. One malformed request terminates the shared daemon, interrupts all tenants, and loses changes newer than the latest autosave. The same crash can be caused accidentally by a local client; same-user authority alone is not the claimed escalation.

**Minimal remediation**

Validate all spawn parameters before launching a child, and contain exceptions at the supervisor request/thread boundary so the promise always receives a structured error. Preserve cleanup if an exception happens after child creation.

**Safe isolated reproduction**

Start a separate scratch daemon with no user documents, secrets, or network access. Send:

```json
{"id":1,"method":"agent_spawn","params":{"argv":["/usr/bin/true"],"mission_id":0}}
```

Observe only that spawned daemon’s exit and connection closure. Use a parent timeout and disable core dumps **for this child only**. A fixed implementation should return a parameter error and still answer `ping`.

This is a separate root cause from finding 1: restricting web methods does not repair supervisor exception handling.

---

### 3. Medium — autosaves and daemon logs inherit permissive filesystem modes, exposing private CAD data to other local users

**CWE:** CWE-732  
**Boundary:** Private customer document → cross-OS-user-readable application state.

**Evidence**

- State directory discovery returns `$XDG_DATA_HOME/Chudware` or `$HOME/.local/share/Chudware` without making it private: `services/ipc/ipc_discovery.cpp:84-89`.
- Daemon startup creates the log hierarchy first using default permissions:
  - `core/hostd/main.cpp:530`
  - `core/hostd/hostd_log.cpp:24-35`
- Autosave directories are created with default permissions:
  - `core/hostd/autosave.cpp:162,185,278,365`
- Every dirty, nonempty, non-internal document is serialized to this hierarchy:
  - `autosave.cpp:250-283`
- Name metadata is also written with ordinary `ofstream` creation:
  - `autosave.cpp:291-292`
- The `.step+` writer creates a file through ordinary miniz file initialization and publishes it without private permissions:
  - `core/doc/stepplus.cpp:301-308,329-330,381-383`
- Agent stdout/stderr logs explicitly use mode `0644`: `core/hostd/child_supervisor.cpp:359-365`.

**Prerequisites and impact**

With a normal `022` umask and traversable home/XDG-data ancestors, newly created application directories are `0755`, and autosaves/name sidecars/logs are normally `0644`. Another local OS account can read unsaved CAD designs even when their original workspace is private.

This is **configuration-dependent exposure**, not a claim that the current workstation’s home or deployed data directory is readable. Ancestor `0700` permissions or a restrictive service umask mitigate it; neither is imposed by these paths.

**Nearby controls considered**

The strict `0700` owner checks in `ipc_discovery.cpp:75-80` protect the **runtime/socket directory**, not the separate persistent state/autosave hierarchy. Secret-store file hardening is likewise file-specific, not parent-state-directory hardening.

**Minimal remediation**

Create and validate owner-private state/autosave/log directories; create confidential files with owner-only access from the outset. Do not depend solely on inherited umask or the home directory’s permissions.

**Safe isolated reproduction**

Use a temporary, traversable fixture hierarchy as `XDG_DATA_HOME`, a separate `0700` runtime directory, and a scratch daemon launched with umask `022`. Create one harmless box using the fixture pattern in `test_hostd_edgecases.py`, allow one short autosave interval, and inspect modes/read access on that scratch `.step+` and name sidecar. A second throwaway OS account can demonstrate reading the marker design; never use real documents or change actual home permissions.

---

### 4. Medium — unauthenticated local users can monopolize the Windows worker control channel with four slow connections

**CWE:** CWE-400, CWE-770  
**Boundary:** Other local OS user → authenticated worker cancellation/steering availability.

**Evidence**

- `services/worker_main.py:284-290` starts `ControlServer` for agent-capable worker roles.
- Windows binds random loopback TCP without an OS-user ACL:
  - `services/agent/chudcode_control.py:70-82`
- There are four shared connection slots: `chudcode_control.py:24,49-52`.
- Slots are acquired **before authentication**; additional connections are immediately closed: `:213-215`.
- The timeout is applied to each socket receive, not to the whole request: `:230,252-278`.
- Authentication happens only after a complete newline-terminated request: `:282-295`.

Four unauthenticated clients can send one byte just before each receive timeout expires. Each connection retains its slot until the 64-KiB request limit is reached; at one byte/second, that is roughly 18 hours. The auth-failure accounting is never reached during this period.

**Impact and scope**

Other local Windows users can prevent legitimate Stop/cancel, steer, answer, status, and release requests from reaching a worker. Discovering a local listening port does not require its token.

This affects the **Windows desktop/standalone worker path**. It is not claimed against Windows hostd’s Python bridge, which currently returns “worker bridge unavailable on Windows” (`core/hostd/worker_bridge.cpp:282-310`). POSIX’s `0600` Unix socket normally excludes other OS users.

**Minimal remediation**

Apply a monotonic absolute deadline to receiving/authenticating each control request, not a fresh timeout per byte. Prefer a same-user authenticated OS transport on Windows, or additionally isolate unauthenticated connection capacity.

**Safe tiny isolated reproduction**

Instantiate only:

```python
ControlServer(lambda action, params: {"fixture": True},
              temporary_directory, platform="nt", client_timeout=0.2)
```

This uses the real loopback implementation even on a POSIX test host. Open four fixture sockets and send one non-newline byte every `0.05` seconds for **one second only**. After `0.4` seconds, attempt a fifth valid `status` request using the fixture token held only in memory. Expect closure/refusal while the four clients remain admitted. Close all sockets and stop the fixture in `finally`; do not print the token.

---

### 5. Medium — predictable Windows singleton mutex can be pre-created by another user to block daemon startup

**CWE:** CWE-923, CWE-400  
**Boundary:** Other local OS user → victim daemon availability.

**Evidence**

- The mutex name is predictable from victim SID and executable-path hash:
  - `services/ipc/ipc_discovery_win.cpp:100-119,141-145`
- `SingleInstance::acquire` creates/opens that machine-global name:
  - `ipc_discovery_win.cpp:189-201`
- Both an access-denied failure and `ERROR_ALREADY_EXISTS` cause refusal, with no proof that an existing object belongs to a legitimate daemon.
- Startup retries for approximately five seconds and exits without serving:
  - `core/hostd/main.cpp:537-565`.

A different local account can pre-create this mutex—or a conflicting named kernel object—and keep its handle alive before the victim starts. The SID embedded in its name is naming, not namespace access control.

**Nearby controls considered**

Named pipes have a same-user/SYSTEM DACL, reject remote clients, and verify server SID. Those controls prevent pipe impersonation but do not protect the earlier singleton mutex acquisition.

Windows’ `SeCreateGlobalPrivilege` restriction for global file-mapping/symbolic-link creation should not be confused with mutex creation; the comment at `ipc_discovery_win.cpp:150-153` is not a sufficient defense.

**Minimal remediation**

Use a protected private namespace or an owner-protected lock file. Adding a DACL only when creating a predictable public mutex does not stop pre-creation.

**Safe isolated reproduction**

On a throwaway Windows VM with two ordinary fixture users:

1. Choose a unique developer-build `CHUDWARE_INSTANCE_KEY` and private scratch daemon/state paths.
2. Have fixture user B create and hold `Global\chudware-hostd-<fixture-user-A-SID>-<scratch-key>`.
3. Start only user A’s scratch daemon and observe startup refusal.
4. Release B’s handle and retry the scratch daemon.

Use only the fixture name; do not squat or probe the real application’s mutex. This Windows behavior was not executed here.

---

## Yield — positive controls and non-findings

- **POSIX socket privacy:** runtime directory is checked with `lstat` for owner, directory type, and no group/other access; socket is chmod’d `0600` (`ipc_discovery.cpp:64-80`; `ipc_transport.cpp:187-229`).
- **Windows pipe identity:** current-user/SYSTEM DACL, `PIPE_REJECT_REMOTE_CLIENTS`, first-instance protection, and client-side server-token SID verification (`ipc_transport_win.cpp:226-307,426-455`).
- **Identity semantics:** Linux authority identity derives from `SO_PEERCRED`; `hello.user` is document attribution. The daemon is intentionally an OS-account boundary, **not a tenant boundary** (`server.cpp:47-62,723-743`).
- **Document controls:** read-only attachments, edit holds, revision/CAS handling, writer groups, and close-to-detach protection exist. They are collaboration controls, not tenant-owner authorization.
- **Framing:** POSIX IPC and hostd worker responses have 64-MiB line bounds (`ipc_transport.cpp:97-106`; `worker_bridge.cpp:1019-1025`). Native worker control has bounded requests/responses and absolute client deadlines.
- **Worker control authentication:** random 256-bit token, constant-time comparison, action allowlist, bounded response size, POSIX socket permissions, and bounded concurrent handlers.
- **Supply chain:** production library loading verifies manifest-owned files; Linux loads the verified descriptor via `/proc/self/fd`; Windows holds a restricted-sharing verified handle during load. Production daemon spawn also verifies/pins its binary. Developer environment-based overrides are intentional same-user capabilities, not remote escalation by themselves.
- **Child containment:** supervised POSIX children use process groups/PDEATHSIG and TERM→KILL escalation; Windows children are assigned to a kill-on-close job before resuming.
- **Process-isolation temporary file:** `mkstemp` creates a private randomized temporary file. No cross-user temporary-file attack was established.

## Yield — hypotheses / policy gaps, not counted as findings

- `core/util/process_isolation.cpp:137-154` responds to a wait failure/timeout by disabling isolation process-wide and executing the same callback in-process. This removes the protection for later OCCT calls. **No concrete attacker geometry demonstrating a timeout followed by a fatal operation was established**, so this is not reported as an exploitable crash finding.
- `WorkerBridge::stop/reap` still has blocking writes and `waitpid(...,0)` after SIGTERM without a guaranteed SIGKILL escalation (`worker_bridge.cpp:690-791`). A separately demonstrated unresponsive worker would be needed to substantiate a remote shutdown-DoS path independent of finding 1.
- Windows pipe framing and direct `WorkerClient::readLine` lack the POSIX hostd frame cap. Their directly controlled peers are same-user daemon/worker processes; no independent cross-user exploit was established.
- `DocRegistry::isReadOnlyMethod` includes broad `agent_`/`sim_` prefixes and is reused by entitlement containment (`doc_registry.cpp:54-65`; `server.cpp:764-772`). This warrants containment-policy review, but it is not tenant authorization and was not promoted to a customer-boundary finding here.

## Yield — reviewed scope and limits

**Reviewed paths/subareas**

1. `core/hostd/server.cpp`: peer identity, hello, dispatch, recovery, document policies, drain, worker/control routing, request exception handling, watchdog.
2. `core/hostd/doc_registry.cpp`: attachments, writer groups, holds, CAS, read-only classification, subscriptions and document attribution.
3. `core/hostd/main.cpp`: startup, instance locking, control commands, socket binding, lifecycle.
4. `core/hostd/worker_bridge.cpp`: lane/pool routing, control authentication, spawn environment, framing, termination.
5. `core/hostd/child_supervisor.cpp`, `child_supervisor_win.cpp`: execution, inherited environment, identity/ownership handling, process lifecycle.
6. `core/hostd/autosave.cpp`, `hostd_log.cpp`, `engine_watchdog.h`.
7. `services/ipc/ipc_transport.cpp`, `ipc_transport_win.cpp`, `ipc_transport_wasm.cpp`.
8. `services/ipc/ipc_discovery.cpp`, `ipc_discovery_win.cpp`, `ipc_client.cpp`.
9. `services/worker/cpp_core.cpp`, targeted sections of `worker_client.cpp` and `engine_backend.cpp`.
10. `services/worker_main.py`.
11. `core/util/process_isolation.cpp`, `.h`.
12. Necessary boundary dependencies: `services/agent/chudcode_control.py`, gateway engine forwarding/actor sections, STEP container creation, and secret-store permission logic.
13. Existing focused tests for hostd edge cases, registry, supervision, worker bridge, and control channel.

**Limits**

- Manufacturing `mfg_lane` excluded as assigned.
- No complete audit of every Python worker method implementation, engine geometry/parser, gateway route, dependency binary, or deployment policy.
- Windows/macOS paths were source-reviewed only.
- Actual umask, filesystem ACLs, installed artifact integrity, service containment, cloud deployment configuration, and customer infrastructure were not inspected.
- Current source was the authority; historical reviews and existing tests were not treated as proof of present behavior.

## Suggested existing checks for the integrator — not run

- `services/mcp/test_hostd_supervision.py::test_bad_argv_clean_error_no_zombie`
- `services/mcp/test_hostd_supervision.py::test_agent_stop_grace_kills_term_ignorer`
- `services/mcp/test_hostd_supervision.py::test_child_stdio_lands_in_log`
- `services/mcp/test_hostd_registry.py::test_ro_attach_rejects_mutation`
- `services/mcp/test_hostd_registry.py::test_edit_hold_blocks_then_expires`
- `services/mcp/test_hostd_edgecases.py`
- `services/mcp/test_hostd_worker_bridge.py`
- `services/agent/test_chudcode_control.py::test_posix_endpoint_is_private_and_authenticated`
- `services/agent/test_chudcode_control.py::test_invalid_auth_attempts_are_rate_limited_without_blocking_parent`
- `services/agent/test_chudcode_control.py::test_mocked_windows_endpoint_uses_random_loopback_tcp`
- `services/agent/test_chudcode_control.py::test_malformed_and_oversized_input_are_bounded`
- `host/tests/test_worker_control.cpp`
- Gateway tenant-isolation tests, augmented with the harmless privileged-RPC fixture above.

**Coverage summary:** The assigned IPC/daemon/worker boundaries were reviewed read-only. The principal risk is that the expected same-user daemon authority is exposed to web tenants; independent defects also remain in supervisor exception containment, persistent-file privacy, Windows control admission, and Windows singleton ownership.
