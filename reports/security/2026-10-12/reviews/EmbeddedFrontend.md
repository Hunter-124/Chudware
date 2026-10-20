## yield: findings

**Read-only audit; no files changed, payloads executed, network calls made, or checks run.** Findings below are confirmed by current source, not runtime reproduction.

### H1 — High: Shell injection through ordinary project/image filenames
**CWE-78 — OS command injection**

**Affected locations**
- `host/src/ui/reveng/image_gallery.cpp:208-226`: `revealInFileManager()` interpolates filenames directly into shell command strings.
- `host/src/ui/reveng/image_gallery.cpp:608-611,699-703`: filenames enter from file selection and drag/drop.
- `host/src/ui/reveng/image_gallery.cpp:680-682`: clicking **Show** invokes the sink.
- `host/src/ui/plm/graph_tab.cpp:2536,2904-2915`: graph-node paths are accepted and resolved without shell escaping.
- `host/src/ui/plm/graph_tab.cpp:3077-3135`: both reveal and external-open actions interpolate those paths into `std::system()`.
- `host/src/ui/plm/graph_tab.cpp:4215-4225,4947-4950`: ordinary user actions reach those functions.

**Execution path and prerequisites:** An attacker supplies an image/project asset whose filename contains shell substitution, such as `sample$(printf AUDIT).png`. The user imports/browses it and clicks **Show**, **Show in folder**, or the relevant external-open action. On Linux/macOS, double quotes do not prevent `$()` or backtick expansion. These are valid POSIX filenames; no attacker-controlled executable or existing same-user code execution is required.

**Impact:** Commands run as the desktop user, exposing customer designs and credentials and permitting file modification. This is a hostile-file-to-code boundary violation—not elevation above the desktop user.

**Nearby controls:** Path normalization, file extensions, and user clicks do not escape shell syntax. A separate, correctly quoted implementation already exists in `host/src/ui/file_dialog.cpp:621-639`; the vulnerable modules bypass it.

**Minimal remediation:** Route reveal actions through the existing safe file-manager helper; implement external file launching through argument-vector/native APIs, not a shell.

**Bounded safe repro for integrator:** In a temporary directory, create a harmless image named literally `sample$(printf AUDIT).png`. In an isolated harness for the affected function, substitute temporary `dbus-send`/`xdg-open` executables that only record argv. Invoke the action once. Receiving `sampleAUDIT.png`, rather than the literal filename, proves command execution using only the shell’s harmless `printf`; do not run against an open customer session.

---

### H2 — Medium: An in-flight entitlement result can sign the desktop back in after explicit sign-out
**CWE-613 — Insufficient session expiration**

**Affected locations**
- `host/src/ui/account_panel.cpp:483-489`: `setPending()` stamps a result with the controller’s **current** generation at completion.
- `host/src/ui/account_panel.cpp:533-537`: refresh work starts without capturing its originating generation.
- `host/src/ui/account_panel.cpp:580-615`: delayed valid refresh responses persist policy/subject/enforcement state and enqueue `Fresh`.
- `host/src/ui/account_panel.cpp:836-870`: loopback authorization work has the same completion-stamping problem.
- `host/src/ui/account_panel.cpp:1004-1029`: sign-out increments the generation, clears pending work, erases stored session material, and marks the UI signed out.
- `host/src/ui/account_panel.cpp:909-957`: a matching-generation `Fresh` result restores signed-in state and persists the token.

**Execution path:** Begin a legitimate refresh; sign out while its HTTPS response is outstanding; let the valid response complete. `setPending()` assigns the post-sign-out generation, so the intended stale-result rejection passes. The old account is restored. Worker-side persistence can also recreate material erased by sign-out.

**Impact and prerequisites:** Account isolation fails for a customer who signs out and leaves a shared desktop. A subsequent user can encounter the previous account restored. No forged signature, credential theft, or network compromise is needed; the stale input is a legitimate response from the prior authorization context. This is not evidence of web-tenant crossover.

**Nearby controls:** Token verification and the generation comparison are present, but generation is recorded at the wrong lifecycle boundary. The comments claiming old work is invalidated do not match the implementation.

**Minimal remediation:** Capture the generation when launching work and carry it through completion. Gate all resulting persistence and state changes against that captured generation, synchronized with sign-out.

**Bounded safe repro:** A temporary state-machine harness needs no credentials: launch generation `g=0`; simulate sign-out to `g=1`; perform the current completion assignment `result.generation=g`; evaluate the current comparison. It accepts the stale result. For integration coverage, use a fixture-only delayed response and isolated secret directory, never a real account/keychain.

---

### H3 — Medium: Selecting a hostile CAD package can trigger unbounded thumbnail ZIP expansion
**CWE-409 — Improper handling of highly compressed data**

**Affected locations**
- `host/src/ui/plm/graph_tab.cpp:2962-2979`: `readZipEntry()` calls `mz_zip_reader_extract_to_heap()` without checking entry size or an expansion budget, then copies the full result into `std::string`.
- `host/src/ui/plm/graph_tab.cpp:3028-3037`: `thumbnail.png` is extracted before image validation.
- `host/src/ui/plm/graph_tab.cpp:3954-3967`: selecting a graph node automatically attempts the packaged-thumbnail fallback when the worker-rendered texture is unavailable.

**Execution path and prerequisites:** An attacker supplies a `.step+` package with an extremely compressible `thumbnail.png` entry. A customer places it in the project and selects the corresponding node while no cached texture is available. Full decompression and a second full allocation happen before PNG decoding can reject the contents.

**Impact:** Memory exhaustion or an unresponsive/crashed desktop, potentially losing unsaved work. The entry need not contain a valid PNG to reach the expensive sink.

**Nearby controls:** The 64-entry thumbnail cache and extension checks do not bound the size of one entry or its temporary decoding allocations.

**Minimal remediation:** Inspect ZIP entry metadata before extraction, impose a small thumbnail-byte limit, enforce the limit while extracting, and bound decoded image dimensions/pixels before full decoding.

**Bounded safe repro:** Create a temporary ZIP with a `thumbnail.png` entry containing exactly **1 MiB of zeros**, compressed with DEFLATE. Exercise only `readZipEntry()` in an isolated CPU harness and record the returned size/allocation count. This demonstrates expansion and the second copy without creating a memory-exhaustion payload. A rejection regression can use a deliberately small fixture limit such as 64 KiB.

---

### H4 — Medium: CEF download responses are buffered without a byte limit
**CWE-400 — Uncontrolled resource consumption**

**Affected locations**
- `host/src/ui/widgets/cef/cef_daemon.cpp:67-73`: download requests route to `headless_download()`.
- `host/src/ui/widgets/cef/cef_headless.cpp:148-168`: every `OnDownloadData()` chunk is appended to an unbounded vector.
- `host/src/ui/widgets/cef/cef_headless.cpp:654-681`: HTTP status is checked and the destination opened only after response buffering finishes.

**Execution path and prerequisites:** A customer or authorized agent requests a download from an attacker-controlled supplier/resource URL. The response server supplies a large body, including a large error response. The CEF daemon allocates for all received bytes before accepting/rejecting the response.

**Impact:** Exhaustion of the native browser daemon’s memory and disruption of parts/download workflows. Collateral host memory pressure is possible. A tenant-wide impact depends on the separately reviewed browser-service deployment and is not established here.

**Nearby controls:** There is an approximately 30-second wait budget and a post-download status check, but neither bounds received bytes. HTTP authentication prompts are refused; that does not constrain response size.

**Minimal remediation:** Enforce a byte limit in the callback, abort oversized responses, and stream accepted downloads into a bounded temporary file rather than retaining the entire body.

**Bounded safe repro:** Directly exercise `DownloadClient::OnDownloadData()` in a temporary harness with sixteen 64-KiB chunks. Confirm the vector grows to 1 MiB with no size rejection. Do not generate a large response or contact any live daemon. The browser-services reviewer should correlate this sink with externally reachable `browser_download` callers.

---

### H5 — Medium: Any embedded webpage can append unbounded data to a persistent native trace log
**CWE-400 — Uncontrolled resource consumption**

**Affected locations**
- `host/src/ui/widgets/cef/cef_web_view.cpp:574-584`: page console messages starting with `[RC]` are passed directly to `trace_file_line()`, without source/origin validation, truncation, or rate limiting.
- `host/src/ui/widgets/cef/cef_trace_file.h:16-35`: logging is enabled by default and appends complete messages to a persistent file without a size cap or rotation.
- `host/src/ui/widgets/cef/cef_web_view.cpp:507-514`: page-triggered popups navigate the embedded main frame.
- `host/src/ui/widgets/cef/cef_app.cpp:75-87`: injected diagnostic scripts share the page environment; their marker is not an authenticated provenance signal.

**Execution path and prerequisites:** A malicious page loaded in the embedded browser emits arbitrary console strings with the `[RC]` prefix. The native console handler treats these as diagnostics and appends them to disk. Ordinary webpage JavaScript suffices; no Chromium exploit or native RPC access is required.

**Impact:** Persistent log growth can fill the customer’s data volume, interrupting saving and other application operations. Attacker-controlled newlines also permit log-entry forgery.

**Nearby controls:** `CHUDWARE_CEF_NETLOG` is opt-in, but this sink does not require it. `CHUDWARE_CEF_TRACE=0` disables the file sink, but the shipping default is on.

**Minimal remediation:** Disable persistent diagnostics by default; independently cap message length, rate, and total retained log size. Do not use a page-controlled string prefix as trusted diagnostic provenance.

**Bounded safe repro:** In a throwaway CEF profile with `HOME`/`XDG_DATA_HOME` redirected to a temporary directory, load a local fixture that emits exactly three short `console.log("[RC] AUDIT_ONLY")` messages. Confirm only those temporary log entries. No loops, large strings, external URLs, or customer profiles are needed.

## yield: policy gaps and conditional defects

These are **not included as proven standalone exploitation findings**.

1. **CEF renderer isolation is explicitly disabled.**  
   `cef_platform_linux.cpp:47-53` and `cef_platform_windows.cpp:49-53` set `no_sandbox=1`; `cef_app.cpp:140-155` defaults to `single-process`. Hostile web content therefore executes within an unsandboxed, normally in-process renderer. **CWE-250 defense-in-depth gap:** an additional renderer vulnerability would have desktop-user impact without a sandbox escape. I did not establish such a renderer exploit. An isolated configuration/process-topology inspection is sufficient to validate the missing control; no exploit is appropriate.

2. **WASM AICM count narrowing can invalidate parser bounds.**  
   `aicm_loader.cpp:71-80` accepts `size_t` counts, while `:128-135,202-205,243-255` supplies `3ull *` attacker-described counts. With 32-bit `size_t`, `3 * 0x55555556` narrows to `2`; the subsequent byte-availability check sees the truncated count. BODY indices are checked against declared `n_verts`, not actual decoded vector length. `id_pass.cpp:219-233` subsequently writes `faceIds[base[k]]` unchecked.  
   A tiny fixture can demonstrate the parser inconsistency: one BODY with `n_verts=0x55555556`, two position floats, two normal floats, one triangle, plus one FACE. **Upstream attacker control remains unclosed:** normal host paths obtain AICM from daemon events/HTTP (`engine_events.cpp:434-442`; `aicm_loader.cpp:409-445`). The integrator should correlate imported cached-model handling with this defect before classifying customer exploitability. Safe reproduction should stop after parsing and inspect vector lengths; do not invoke the out-of-bounds consumer.

3. **WASM shared-memory envelope uses overflowing addition.**  
   `aicm_loader.cpp:439-445` checks `8ull + sz > bytes.size()`, which wraps for `sz > UINT64_MAX-8`, then narrows `sz` to `size_t`. Validate `sz <= bytes.size()-8` instead. As above, attacker reachability through the actual gateway/daemon producer was outside this scope. A tiny in-memory envelope is enough for a bounded arithmetic rejection test.

4. **Credential-storage behavior is weaker than the OS-keychain description implies.**  
   `secret_store.cpp:258-299` selects macOS/Linux keychains only for an empty `dataDir`; ordinary account persistence supplies `appdataAppDir()` (`account_panel.cpp:188-197,301-320`). These normal paths therefore use the protected file fallback even when a keychain exists. The file permissions are substantially hardened; this is an at-rest protection/deployment-policy issue, **not a demonstrated other-user credential read**.

5. **Session export is broader than the selected vendor.**  
   `cef_web_view.cpp:943-976` uses `VisitAllCookies()` on the shared interactive profile and sends the complete jar through `cef_rpc_bridge.cpp:34-55`. This includes HttpOnly cookies and potentially unrelated vendor/xAI sessions. Cookie domain scoping prevents a direct “vendor receives every cookie” conclusion. The separate services/browser review must establish filtering, persistence, and who can read the imported jar.

6. **Legacy URL launcher remains outside the safe opener.**  
   `ui/copilot/settings.cpp:866-884` shells out using the xAI status-provided verification URL. The active general Settings launcher uses `openExternalHttpUrl()` instead. The unsafe sink is present, but I did not establish a realistic untrusted producer for this provider-status value, so it is not an additional confirmed injection finding.

## yield: positive controls / rejected candidates

- **No generic webpage-to-native privileged JS bridge found.** Searches across `host/src` for CEF message routers, `OnQuery`, native V8 handlers, registered extensions, and process-message receivers returned no matches. `cef_rpc_bridge.cpp` is a native AppState helper, not a web-exposed RPC binding.
- Native chat/prose rendering inspected uses ImGui text/read-only inputs, not HTML execution.
- WASM workspace filenames and errors use `textContent`; observed `innerHTML` use clears containers or inserts fixed preflight markup.
- `ui/web_capabilities.cpp:54-78,89-144` restricts external links to HTTP(S), rejects control characters and userinfo, uses argument-vector/native launching, and uses `noopener,noreferrer` on WASM.
- Desktop account callbacks bind to loopback, generate 32 random state bytes, compare state, and verify entitlement signatures: `account_panel.cpp:639-690,843-870`. Signed security policy is additionally verified before activation at `:918-925`.
- `secret_store.cpp:61-128` protects POSIX temporary/final files with `O_NOFOLLOW`, descriptor checks, `fchmod(0600)`, and durable writes. Windows uses user-bound DPAPI. I did **not** retain a stale-world-readable-temp-file finding.
- Native AICM parsing now enforces chunk boundaries, checks available bytes before allocation, validates BODY indices and FACE/EDGE ranges, and validates appearance regions. The WASM-specific narrowing caveat above prevents treating this as an unconditional all-platform closure.
- Windows clipboard DIB handling checks header and actual pixel-buffer bounds before traversal: `clipboard_image.cpp:231-265`.
- The WASM development server binds to `127.0.0.1`, not all interfaces: `host/wasm/coop_server.py:32`.
- Updater paths inspected verify signed artifact identity before application and reject non-newer versions at startup. This was targeted inspection, not a complete updater-helper audit.

## yield: reviewed paths and coverage

1. **CEF/native browser:**  
   `ui/widgets/cef/{cef_app.cpp,cef_web_view.cpp,cef_rpc_bridge.cpp,cef_headless.cpp,cef_daemon.cpp,cef_platform_linux.cpp,cef_platform_windows.cpp,cef_trace_file.h}` — navigation, exposed bindings, cookies, renderer isolation, downloads, console logging, clipboard bridge.
2. **Accounts/openers:**  
   `ui/{account_panel.cpp,oauth_capture.cpp,web_capabilities.cpp,file_dialog.cpp,settings_modal.cpp,chudcode_descriptors.cpp}` and `ui/copilot/settings.cpp` — callback validation, logout lifecycle, storage callers, URL dispatch.
3. **Untrusted display/file actions:**  
   `ui/copilot/{activity_timeline.cpp,image_lightbox.cpp}`, relevant chat/feed searches; `ui/plm/{graph_tab.cpp,graph_thumbs.cpp}`; `ui/reveng/{image_gallery.cpp,img_tex.cpp}`; `ui/clipboard_image.cpp`.
4. **Mesh/image intake:**  
   `render/{aicm_loader.cpp,aicm_loader.h,id_pass.cpp,doc_thumbnail.cpp}`, relevant `core/{app_state.cpp,engine_events.cpp}` sections, and image-decoder callsite searches across `host/src`.
5. **WASM:**  
   All four files in `host/wasm`; `core/{web_file_pick.cpp,web_fetch.cpp}` and bridge discovery in `app.cpp`/`fs_backend.cpp`.
6. **Requested security utilities/tests:**  
   `core/util/{secret_store.cpp,entitlement.cpp}`, `host/tests/test_security.cpp`; related test registrations in `host/CMakeLists.txt`.
7. **Supply-chain orientation only:**  
   Relevant `update/updater.cpp` verification/application sections and targeted searches of platform update helpers.

**Explicit limits:** This was focused trust-boundary review, not line-by-line review of every rendering/UI feature, camera driver, bootstrap path, or update-helper implementation in the large `host/src` tree. FastAPI tenant authorization, Python browser filtering, imported-model cache provenance, native IPC access controls, installed dependency advisories, and production filesystem permissions require correlation with the other assigned reviews. No actual credential files or deployment state were read. No LSP tool was exposed to this subagent.

**Suggested existing checks for the integrator, not exercised:** `test_security`, `test_updater` **without `--live`**, `test_entitlement_commands`, and `test_clipboard_image`. The reviewed `test_security.cpp` covers integrity, device identity, signed policies/enforcement, and event persistence; it does not establish closure of H1–H5 or the logout race. Use the bounded temporary-fixture reproductions above for those findings.

**Coverage summary:** Five customer-impact findings survived source tracing. Renderer sandboxing and broader cookie export are explicitly separated from proven exploit chains; WASM AICM arithmetic defects are identified with their remaining upstream reachability dependency.
