## yield — findings

**Audit mode:** read-only source review. No files changed, payloads executed, tests/builds/scanners run, or network requests made. Findings below are confirmed control defects in current source; runtime impact is identified separately where reproduction remains necessary.

### 1. High — Domain-specific file parameters bypass the gateway’s tenant filesystem boundary

**CWE:** CWE-863, CWE-22  
**Impact:** Cross-account disclosure and overwriting files accessible to the shared daemon.

**Reachable chain**
- `Webservice/gateway/engine_bridge.py:215-229` recognizes only a fixed set of path field names. It omits `pump_curve`, `step_path`, `stl_path`, `vtu_path`, `progress_path`, `export_system_path`, and `rates_path`.
- The gateway applies this incomplete inspection at `engine_bridge.py:1072`, then forwards requests at `1209`.
- `core/hostd/server.cpp:1079-1122` forwards native requests; `core/hostd/doc_registry.cpp:431-451` has no tenant filesystem check and permits requests without `doc_id`.

**Concrete sinks**
- **Inexpensive arbitrary first-line disclosure:** `piping_operating_point` dispatches at `core/piping/piping.cpp:265-267`; its `pump_curve` string reaches `core/piping/solve.cpp:2340-2353`, then `load_pump_curve()` at `691-704`. This opens the string as a filename. If its first line is not a valid pump CSV header, **the parsed first line is returned in the error hint** at `735-739`.
- **Overwrite:** FEA accepts `study.export_system_path` at `core/sim/fea_run.cpp:4230-4237`, creates parent directories and opens with truncation at `4280-4284`.
- **Other affected sinks:** `core/sim/vtu_writer.cpp:206`, `core/sim/flow_run.cpp:338-343`, and input reads at `flow_run.cpp:620-623`. Structural custom rates are another omitted field: `core/structural/structural_rpc.cpp:190-193` → `estimate.cpp:157-166`.

**Controls considered:** Ordinary `path`/`doc_path` fields are checked; document capabilities protect recognized document IDs. Neither covers these parameter names or file-valued domain strings. This is not an allegation against authorized desktop file access—it crosses the explicitly designated web tenant boundary.

**Minimal remediation:** Method-specific input schemas declaring all file inputs/outputs; resolve them under authenticated tenant roots before dispatch. Avoid accepting “inline text or filename” in the same unchecked field. Enforce the resulting authorized file handles/paths at the native boundary.

**Safe repro:** In an isolated fixture, create sibling `alice/` and `bob/` directories and `bob/header.txt` containing `AUDIT_FIXTURE_ONLY\n`. Assert that:
```json
{"method":"piping_operating_point","params":{"pump_curve":"<fixture>/bob/header.txt"}}
```
passes `path_violation(..., [PurePosixPath("<fixture>/alice")])`, then invoke only the native pump-import path and observe the marker in `hint`. No simulation, credentials, or real tenant data is needed.

---

### 2. High — SVG schematic export escapes the checked destination through an unchecked sheet identifier

**CWE:** CWE-22  
**Impact:** A tenant can overwrite another tenant’s `.svg` files or other daemon-writable paths ending in `.svg`.

**Reachable chain**
- `core/ecad/session.cpp:632-650` accepts an inline schematic through `params.doc`, avoiding any document-ID prerequisite.
- `plot_opts_from()` accepts attacker-controlled `opts.sheet` at `session.cpp:603-605`.
- `core/ecad/schplot.cpp:469-485` uses sheet IDs verbatim; even an unknown `opts.sheet` becomes a generated page.
- `schplot.cpp:1191-1203` returns that identifier as the output key.
- `session.cpp:550` builds `stem + "-" + key + ".svg"` without sanitizing the SVG key.
- `session.cpp:587-589` joins that name to the checked destination and opens it with truncation.

**Controls considered:** `export_stem()` sanitizes the document name, and Gerber keys have separate separator sanitization. Neither control sanitizes SVG sheet keys. The gateway validates the base `path`, not the composed filename.

**Minimal remediation:** Map sheet IDs to safe leaf filenames and enforce canonical containment of each final output path. Reject separators and traversal components rather than relying on document-name sanitization.

**Safe repro:** Under a temporary root, create:
- `alice/Schematic-x/`
- `bob/proof.svg` containing a marker.

Export the minimal inline schematic `{"schema":"aicad.sch/1","name":"Schematic"}` with `kind:"svg"`, `path:"<fixture>/alice"`, and `opts.sheet:"x/../../bob/proof"`. The composed path resolves to the sibling fixture’s `proof.svg`. Rendering an empty SVG requires no numerical solve.

---

### 3. High — Reverse-engineering sessions and bundles have no tenant ownership control

**CWE:** CWE-862, CWE-639  
**Impact:** An authenticated web user can enumerate other customers’ reconstruction sessions, retrieve manifests, export their photographs/meshes/research, and invoke session mutations or cancellation.

**Reachable chain**
- `services/reveng/worker_rpc.py:163-205` exposes `list_reveng_sessions`, `get_reveng_session`, reconstruction mutations, splat cancellation, and `export_reveng_bundle`.
- Registered in `services/worker_main.py:148-149`; unknown native methods reach the worker at `core/hostd/server.cpp:1147-1176`.
- All these requests use the singleton general worker: `core/hostd/worker_bridge.cpp:61-98,175-178`.
- The session root is process-wide at `services/mcp/reveng_common.py:119-131`, not tenant-qualified.
- `services/mcp/reveng_session_tools.py:785-799` enumerates every session; `804-813` returns a selected manifest.
- `services/mcp/reveng_build_tools.py:712-732` archives the selected session into caller-selected `dest`.

**Controls considered:** The gateway’s document capability checks do not authorize reconstruction `session_id` values. A destination inside the attacker’s own workspace passes the filesystem check. Random session IDs provide no protection because listing discloses them. Gaussian `_root()` checks syntax/existence, not ownership.

**Minimal remediation:** Bind reconstruction sessions and jobs to a server-authenticated account; filter listing and enforce ownership on every read, mutation, cancellation, and export. Carry identity through the worker transport rather than trusting an input owner field.

**Safe repro:** Reuse `services/reveng/test_worker_rpc.py`’s `CHUDWARE_REVENG_DIR` fixture. Seed one synthetic session with a tiny `images/private.txt`. Using two synthetic gateway identities and an isolated fake daemon/worker fixture, verify B can list A’s ID, fetch its manifest, and export its marker to B’s fixture destination. No reconstruction is necessary.

---

### 4. High — Reconstruction `session_id` is also an unchecked filesystem path

**CWE:** CWE-22  
**Impact:** Read manifests and export directory contents outside the reconstruction store; write-side operations may also escape through session paths.

**Root cause distinct from finding 3:** Even a deployment with only one reconstruction owner does not confine identifiers to the reconstruction root.

**Evidence**
- `services/mcp/reveng_common.py:128-133` directly computes `_reveng_dir() / session_id / "session.json"`.
- `_load_session()` reads that path at `180-186` and accepts any JSON object.
- `get_reveng_session()` exposes it at `services/mcp/reveng_session_tools.py:804-813`.
- `export_reveng_bundle()` recursively archives `_session_path(session_id)` at `services/mcp/reveng_build_tools.py:721-731`.
- The gateway does not classify `session_id` as a file path.

An absolute ID replaces the configured base; `../` escapes it. Export requires the selected directory to contain a readable JSON-object `session.json`, which limits but does not eliminate exploitation.

**Controls considered:** `services/reveng/gaussian_jobs.py:135-142` validates its own session IDs, but `_load_session()` reads the manifest **before** consulting Gaussian status, and a Gaussian refusal does not reject the manifest.

**Minimal remediation:** Centralize strict session-ID validation and root containment in `_session_path`; ensure manifest-internal `session_id` agrees with the authorized requested ID.

**Safe repro:** Set the reconstruction root to `<fixture>/store`; put `{}` in `<fixture>/outside/session.json` and a marker in `outside/proof.txt`. Call the registered bundle handler with `session_id:"<fixture>/outside"` and a ZIP destination elsewhere in the same temporary fixture. Inspect the ZIP for the marker. No real outside directory is needed.

---

### 5. High — Untrusted schematic strings become executable ngspice control-language directives

**CWE:** CWE-74 / CWE-78  
**Impact:** A supplier-provided or otherwise untrusted schematic can execute commands with the simulation worker’s permissions when the user chooses the optional ngspice backend.

**Reachable chain**
- Schematic reference text is copied into the netlist at `core/ecad/netlist.cpp:869-893,937`.
- `core/sim/netlist.cpp:1073-1074,1256` preserves it as a circuit device name; `make_dev()` at `354-364` does not escape it.
- `services/sim/spice.py:644-657` inserts device names and node strings directly into line-oriented SPICE.
- `services/installer/ngspice_backend.py:154-171` uses that output and also interpolates `.include` and X-device fields verbatim.
- `_run_ngspice()` writes the deck and executes ngspice at `321-337`, with the worker’s environment.
- The normal tool path is `services/mcp/ecad_sim_tools.py:304-316,401-403` → `services/mcp/ecad_common.py:782-790`.

Newlines can introduce `.control` blocks; ngspice’s command language is not merely circuit data. The `.tf` analysis is another injection input: its `output` receives only nonempty-string validation at `ecad_sim_tools.py:1617-1625`, then is interpolated at `ngspice_backend.py:199`.

**Controls considered:** Numeric values receive finite-value checks; schematic reference validation checks duplicates but does not prohibit embedded newlines (`core/ecad/schema.cpp:421-428`). Argument-vector subprocess launch prevents a shell parsing the argv—it does not prevent the solver interpreting injected deck commands. MNA is the default, so ngspice availability and selection are explicit prerequisites.

**Minimal remediation:** Validate every deck identifier/string against a strict grammar, reject line breaks/control directives, and constrain vendor includes. Run external solver jobs in a genuine OS sandbox with a minimal environment.

**Safe repro:** Do not execute ngspice. Pass `emit_deck()` a one-resistor circuit whose name is:
```text
R1 n 0 1k
.control
echo AUDIT_FIXTURE_ONLY
.endc
*
```
Assert the emitted deck contains a standalone `.control` block. For end-to-end input propagation, use a temporary schematic/netlist with that reference and inspect the generated deck. No shell command is needed.

---

### 6. High — PDF research acquisition bypasses the public-network URL policy

**CWE:** CWE-918  
**Impact:** Authenticated users can cause server-side requests to loopback, private-network services, or redirected internal endpoints. PDF-readable responses can be returned as research content; non-PDF targets still receive the request.

**Reachable chain**
- `reveng_research_source(url=...)` accepts the URL at `services/mcp/reveng_build_tools.py:988-1028`, and is worker-exposed.
- `services/vision/research_ingest.py:307-340` chooses `datasheet_fetch` for a URL whose path ends in `.pdf`.
- `services/research/datasheet_fetch.py:118-135` accepts HTTP or HTTPS and calls `httpx.get(..., follow_redirects=True)` without public-address validation.

**Controls considered:** Offline mode blocks fetching; a PDF extractor must be installed for the public `datasheet_fetch()` path. Neither is network authorization. The sibling HTML fetcher has a public-HTTPS policy and per-redirect checks (`services/research/fetch_page.py:78-95,273-293`), but the PDF path does not reuse them.

**Minimal remediation:** Share one outbound-fetch policy across HTML, PDF, and browser fallback; validate every destination and redirect, enforce connection-time address restrictions, and bound downloaded bytes.

**Safe repro:** Monkeypatch `httpx.get` to a recording fake and the PDF extractor to return a fixture marker. Invoke the PDF research path with `http://127.0.0.1/audit.pdf`; confirm that the fake receives the private URL and `follow_redirects=True`. No socket or actual service should be contacted.

---

### 7. Medium — Malformed ASCII STL advances beyond its terminating NUL

**CWE:** CWE-125  
**Impact:** Out-of-bounds reads in the shared native daemon; process crash is a credible consequence, with exact manifestation dependent on allocation layout.

**Evidence and path**
- `sim_run_flow` accepts an attacker-owned `stl_path` at `core/api/libchudware.cpp:3294-3301`.
- `core/sim/flow_run.cpp:620-623` reads it.
- At `flow_run.cpp:640-643`, after matching `"vertex"`, a failed first `strtof()` advances `p` unconditionally and repeats `strstr()`.
- If the file ends exactly in `vertex`, `p` initially points at the string’s terminating NUL; `++p` moves beyond it, and the next unbounded search reads past the logical buffer.

**Controls considered:** The initial 15-byte minimum, `solid`/`facet` detection, and later finite-coordinate checks do not protect this error branch. The first-coordinate failure is uniquely handled by continuing rather than refusing.

**Minimal remediation:** Reject a malformed vertex immediately, or parse with explicit begin/end bounds; never increment past the terminator.

**Safe repro:** Only in a short-lived sanitizer-enabled fixture process, call `load_stl_soup()` on a temporary file consisting of `"solid facet " + 12 spaces + "vertex"`—30 bytes, no trailing newline. If allocation slack masks the access, use the same bounded pattern at lengths 60 and 120. Call the loader only, not a flow solve. A sanitizer failure has **not** been exercised here.

---

### 8. High — Deeply nested KiCad input can exhaust the shared daemon’s native stack

**CWE:** CWE-674  
**Impact:** One malicious uploaded/imported KiCad file can terminate the shared daemon and interrupt other customers’ active work.

**Reachable chain**
- `ecad_open` dispatch: `core/api/libchudware.cpp:4247-4248`.
- Extension-based import: `core/ecad/session.cpp:744-766`.
- `core/ecad/kicad.cpp:916-929` parses the entire file before checking its root tag.
- `sexpr_parse()` at `900-908` invokes recursive `build()`.
- `build()` at `115-123` recursively handles every `(` without a depth limit.

**Controls considered:** Empty, unmatched, and trailing-token checks exist. C++ exception handlers around import do not recover a stack-overflow signal. A compressed file or numerical solve is unnecessary.

**Minimal remediation:** Enforce a modest parse-depth and node-count ceiling, preferably with an iterative parser.

**Safe repro:** In an isolated child process with a small stack limit, no core dumps, a memory limit, and a short wall-clock timeout, call `sexpr_parse()` on a temporary string containing at most 4,096 nested parentheses. Assert controlled rejection rather than process death. Never point this at a running daemon.

---

### 9. High — `.ecad+` loading eagerly decompresses unbounded archive entries

**CWE:** CWE-409, CWE-400  
**Impact:** Small uploaded archives can force large memory allocations/decompression in the shared daemon, denying service to all tenants.

**Evidence**
- `core/ecad/container.cpp:201-208` calls `mz_zip_reader_extract_to_heap()` without checking an entry’s advertised uncompressed size.
- `meta.json` and `document.json` are extracted before their semantic checks.
- `container.cpp:250-260` eagerly extracts **every unknown entry**, retaining it in `result.extra`.
- `core/ecad/session.cpp:744-766` invokes this importer from `ecad_open`.

**Controls considered:** ZIP validity, container version, and JSON parsing are checked, but there is no per-entry, cumulative-uncompressed-size, or entry-count quota. Upload byte limits would constrain compressed bytes, not expansion. No disk extraction occurs, so this is not a ZIP-slip allegation.

**Minimal remediation:** Preflight archive metadata with strict per-entry/cumulative/count ceilings and enforce those limits during decompression; avoid preserving arbitrary unknown payloads without a budget.

**Safe repro:** Build a temporary archive with ordinary metadata/document fixtures plus a 1 MiB zero-filled unknown entry compressed with DEFLATE. Call the container reader in an isolated child and inspect `extra` to demonstrate eager retention. This tiny fixture proves the missing budget path without constructing an OOM bomb. Resource-exhaustion impact remains unexecuted.

---

### 10. Medium — Native simulation cancellation is authorized solely by a caller-selected job ID

**CWE:** CWE-862, CWE-639  
**Impact:** A web tenant who knows another tenant’s simulation ID can cancel or supersede that run.

**Evidence**
- Caller-supplied IDs enter flow jobs at `core/api/libchudware.cpp:3318` and `core/sim/flow_run.cpp:320`.
- `core/sim/sim_cancel.cpp:15-26` stores tokens in one global map keyed only by that string. Re-registering a name cancels its previous token.
- `sim_cancel()` at `sim_cancel.cpp:36-48` sets the token without an owner check.
- `core/api/libchudware.cpp:3414-3418` exposes cancellation directly.
- Gateway checks protect recognized document IDs and paths, not simulation-job ownership.

**Prerequisite:** Knowledge of the victim ID; I did not establish a general native-job enumeration endpoint. This is not a claim that high-entropy IDs are readily guessable.

**Minimal remediation:** Server-minted job capabilities bound to authenticated accounts, checked on registration, status, and cancellation. Do not let one tenant’s registration replace another tenant’s token.

**Safe repro:** In a tiny native fixture, register a token named `fixture-job`; call `sim_cancel("fixture-job")` through a second synthetic principal’s gateway request path and observe the original token change. No solver or real process cancellation is required.

---

### 11. Medium — Optional phone-capture listener permits unauthenticated connection starvation

**CWE:** CWE-400  
**Impact:** A network peer can block the capture service and its shutdown path without knowing the capture token.

**Evidence**
- `services/vision/capture_server.py:275-281` creates a single-threaded `HTTPServer` bound to `0.0.0.0`, without accepted-socket/header-read deadlines.
- Token checks only occur inside `do_GET`/`do_POST`, after the base HTTP handler has read the request (`capture_server.py:165-176,205-218`).
- `stop_session()` calls blocking `shutdown()` at `capture_server.py:288-292`.

One accepted connection sending an incomplete request line/header holds the sole request-processing thread before authentication. The comment “only LAN-reachable” is not an enforced network restriction.

**Controls considered:** Tokens are random and compared with `hmac.compare_digest`; upload filenames are reconstructed from an allowed suffix. Those are good controls but do not address pre-authentication blocking. Authenticated multipart bodies also lack an explicit size cap at `capture_server.py:138-142`.

**Minimal remediation:** Accepted-connection and header/body deadlines, bounded concurrent handling, upload byte/count limits, and an explicit listener exposure policy.

**Safe repro:** Instantiate the same handler on **loopback only**, ephemeral port, in an isolated fixture subprocess. Send an incomplete header on one connection; attempt a second token-authenticated GET with a 200 ms client deadline. Close the first socket in `finally` so the fixture recovers before shutdown. No external interface or real capture session is needed.

## yield — hypotheses and policy gaps, not additional confirmed exploits

- **CAD-Recode’s “sandbox” is only a subprocess.** `services/vision/cad_recode.py:520-531,613` launches the regular interpreter and executes generated Python with full builtins, ordinary filesystem/network access, and inherited environment. I did **not** prove a reliable attacker-controlled mesh → chosen generated code path, so this is a confirmed isolation gap, **not a demonstrated remote-code-execution finding**.
- **Provisioning descriptors:** `services/vision/reveng_provision.py:110-176` includes mutable model URLs and pending hashes, while Brush has version/hash metadata. I did not trace the provisioning downloader or verify installed artifact provenance, so no supply-chain compromise finding is asserted.
- **Simulation job quotas:** `services/sim/jobs.py:859-904` launches each accepted job without a central concurrency admission limit. Native lattice memory governors exist. I did not establish unrestricted direct web invocation of this Python job-start API, so this remains an availability-policy gap rather than a separate remote DoS claim.
- Python simulation polling rejects unknown registry IDs before reading their artifacts in the reviewed public tools. I therefore did **not** report a traversal merely because the internal `job_dir()` helper concatenates a string.

## yield — positive controls observed

- LandXML disables entity resolution/network/DTD loading and explicitly refuses documents with a doctype: `services/civil/landxml.py:394-401`.
- Project-schedule XML rejects DTD/entity declarations before parsing: `services/civil/planning.py:614-619`.
- GeoPackage import uses SQLite read-only URI mode and quotes externally supplied table identifiers: `services/civil/gis_io.py:400-424`; geometry decoding bounds point counts against remaining bytes.
- OpenFOAM shell construction quotes case paths and joins argv using `shlex`; subprocess pumping checks exit status, fatal solver output, and timeouts: `services/sim/foam_runner.py:94-145,233-245,350-365`.
- CalculiX jobs restrict job names, use temporary directories and argv execution, and apply timeouts: `services/mcp/fea_advanced.py:194-215`.
- Simulation jobs use a closed stage/method list, parameterized SQL, generated IDs, process-start identity checks, and refuse to promote failed/cancelled results to converged success.
- Gaussian jobs validate session-ID shape, serialize mutations with locks, verify process identities before signalling, and use a cross-process training slot.
- Native simulation cache reads check file-size ceilings, engine/version/input identity, solved status, finite results, and checksums: `core/sim/sim_cache.cpp:230-279`.
- Native flow/pipe-flow implement memory governors; binary STL uses 64-bit expected-size arithmetic and rejects nonfinite vertices.
- Civil persistence drops results when the model changes (`services/mcp/civil_common.py:189-200`); EM worker results undergo finite-result validation.
- No production `pickle.load`, `allow_pickle=True`, or archive-extraction-to-filesystem sink surfaced in the searched target Python directories.

## yield — reviewed paths and coverage

1. **`core/sim/`:** directory-wide sensitive-sink search; focused reading of `flow_run.cpp`, `pipe_flow.cpp`, `fea_run.cpp`, `sim_cancel.cpp`, `sim_cache.cpp`, `vtu_writer.cpp`, `netlist.cpp`, and topology input/job-ID handling.
2. **`core/ecad/`:** directory-wide sensitive-sink search; focused reading of `container.cpp`, `session.cpp`, `kicad.cpp`, `schema.cpp/.h`, `netlist.cpp`, and `schplot.cpp`.
3. **`services/sim/`:** job validation/submission/status/results/cancellation; OpenFOAM invocation; SPICE serialization/backend routing; multiphysics registration; EM adapters and output validation. Reviewed relevant portions of `jobs.py`, `foam_runner.py`, `spice.py`, `multiphysics_rpc.py`, and `em/{worker_rpc,adapters,openems_adapter,antenna_bridge}.py`.
4. **`services/civil/`:** directory-wide IO/deserialization search; LandXML, schedule XML, GeoPackage SQL/WKB, EPANET/SWMM IO references, DEM import, and shared model/result persistence.
5. **`services/vision/`:** directory-wide execution/deserialization/network/IO search; capture server, PDF/research ingest, CAD-Recode execution, frame import bounds, reconstruction/provisioning routes.
6. **Additional dependency coverage:** `services/reveng/{worker_rpc,gaussian_jobs}.py`; reconstruction MCP session/build/common modules; `services/installer/ngspice_backend.py`; ECAD simulation MCP modules; `services/piping/{worker_rpc,cfd_handoff}.py`; `core/piping/{piping,solve}.cpp`; structural RPC/file-loading and Python preliminary-check interfaces; `services/research/{fetch_page,datasheet_fetch}.py`; worker registration and selected hostd/gateway enforcement paths.
7. **Existing test evidence inspected:** portions of `core/tests/{test_sim,test_kicad_import,test_sch_engine}.cpp`, `services/sim/{test_jobs,test_ngspice_backend}.py`, `services/reveng/test_worker_rpc.py`, `services/civil/test_landxml.py`, `services/vision/test_vision.py`, and research redirect tests.

**Limits / omissions:** This was sink-driven security review, not line-by-line mathematical validation of every solver. I did not audit third-party parser/solver internals, installed binary versions, GPU kernels in depth, deployment firewall/sandbox policies, provisioning downloader enforcement, the entire agent/tool authorization layer, or live infrastructure. Actual dependency advisories and deployment evidence remain integrator work. No claim of full numerical/physical-safety certification is made.

## yield — suggested existing checks for the integrator

**None were run.** Existing focused checks relevant to preserving controls:

- `services/sim/test_jobs.py::test_malformed_job_is_rejected_before_registry_insert`
- `services/sim/test_jobs.py::test_stage_ok_false_is_a_failed_job_not_done`
- `services/sim/test_jobs.py::test_cancel_requested_job_cannot_adopt_late_success`
- `services/sim/test_jobs.py::test_cancel_kills_surviving_process_group_after_leader_exits`
- `services/sim/test_jobs.py::test_progress_token_resolves_into_the_jobs_own_spool`
- `services/reveng/test_worker_rpc.py::test_start_session_then_list_and_get`
- `services/research/test_research.py::test_http_get_revalidates_private_redirect_before_next_connection`
- Existing native `kicad_import` and `sch_engine` test targets.
- Native `test_sim` case **“sim_cancel registry: running job cancels, unknown/finished reports cleanly”**.

These existing checks do not establish security closure for the new findings. The bounded fixture reproductions above are intended to test the missing controls without expensive simulations or customer data.

**Coverage summary:** All five assigned directory scopes were inventoried and searched for the requested security-sensitive operations; concrete paths were followed into the gateway, shared daemon, worker registry, piping, structural, reverse-engineering, research, and optional ngspice dependencies. **Eleven substantiated defects** survive review, plus explicitly separated isolation/supply-chain/availability gaps.
