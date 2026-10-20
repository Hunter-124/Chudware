## yield: findings

Read-only review of the current source. **No tests, builds, scanners, payloads, network requests, or git commands were executed.** Findings below are source-confirmed unless explicitly classified otherwise. Safe reproduction recipes are for the integrator, not claims of observed runtime results.

### 1. High — Git RPC arguments remain Git options, enabling writes outside the workspace and conditional command execution
**CWE-88: Argument Injection**

- **Source → sink:** `services/plm/worker_rpc.py:691-720` forwards client-controlled `ref` and `remote` unchanged. `services/plm/gitscm.py:164-174` appends `ref` to `git log`; `:342-347` appends `remote` to `git fetch --prune`; `:42-59` executes that argument vector.
- `git_log(ref="--output=/outside/workspace/file")` makes a nominal revision argument an output option. `git_fetch(remote="--upload-pack=…")`, with a configured local/file origin, selects a program Git invokes for upload-pack.
- **Reachability:** Registered at `services/plm/worker_rpc.py:1048,1061`; worker registration is `services/worker_main.py:156`. The gateway’s path-key checks do not inspect paths embedded in `ref`/`remote`, and these methods are not blocked by `Webservice/gateway/engine_bridge.py:1029-1072`. Hostd forwards engine-unknown methods at `core/hostd/server.cpp:1123-1182`. Its entitlement check at `:761-772` is not tenant authorization.
- **Prerequisites/impact:** An authenticated, cloud-entitled tenant and an accessible Git repository. Command execution additionally requires a local/file origin or another applicable transport. This is a shared-service compromise path, **not** a claim that an authorized desktop user’s Git commands constitute privilege escalation.
- **Nearby controls:** `shell=False` and a subprocess timeout do not stop Git interpreting its own options. Stage/diff paths already use `--`.
- **Minimal remediation:** Validate revisions and remote names, reject option-like input, separate options from positional arguments correctly, and restrict local Git execution/configuration in the shared web service.
- **Safe repro:** In two temporary directories, create a one-commit repository and call `gitscm.log(repo, ref="--output=<other-temp>/marker.txt")`; check only that marker. For execution confirmation, configure a temporary local origin and use a temporary upload-pack helper that writes one marker and exits; enforce a short subprocess timeout.

### 2. High — Thumbnail RPC is an arbitrary HTTP response proxy into the daemon’s network
**CWE-918: SSRF**

- **Source → sink:** `services/parts/gui_rpc.py:1961-1975` accepts arbitrary `image_url`; `:1909-1918` checks only the `http(s)` prefix; `:1649-1655` uses redirect-following `urllib.request.urlopen`.
- Any successful response body—not just an image—is accepted at `:1728-1730`, cached, and returned as `image_b64` at `:1915-1918`. There is no public-address, DNS, port, redirect-target, or successful-response image validation.
- **Prerequisites/impact:** An authenticated web tenant can directly call `parts_thumbnail`/`parts_thumbnails`. A malicious supplier result can also supply the URL: `services/browser/adapters/parts_provider.py:74-80` carries remote thumbnail metadata into records, and `gui_rpc.py:258` exposes it to the GUI. Internal HTTP content can be returned to the caller; internal GET endpoints can be invoked.
- **Controls considered:** Initial scheme filtering, a 4 MiB read cap, and short timeouts exist. They do not prevent SSRF. Gateway path controls do not cover `image_url`.
- **Related unguarded fetch:** `services/research/datasheet_fetch.py:123-139` follows arbitrary HTTP redirects without the public-URL policy. Unlike explicitly requested local PDF access, a public datasheet URL redirecting to an internal target is an untrusted crossing. Returned text requires a parsable PDF, but the request occurs regardless.
- **Minimal remediation:** Apply connection-bound outbound policy to thumbnails and datasheet fetching, including redirects; validate raster responses before returning them.
- **Safe repro:** A temporary loopback HTTP server returns `b"isolated-marker"` with status 200. Point a unique thumbnail request at it, with the thumbnail cache redirected to a temporary directory. The current handler returns the marker base64. Also test a loopback redirect chain. Never target real metadata services.

### 3. High — Web tenants can overwrite shared supplier credentials and browser session state
**CWE-862 / CWE-653: Missing Authorization / Insufficient Compartmentalization**

- **Source → sink:** `services/parts/gui_rpc.py:1329-1371` exposes `vendor_creds_set`, which writes the process-wide `paths.find_config()` supplier credentials. `services/parts/embed_rpc.py:107-128` exposes `browser_session_import`, which persists cookies under a caller-selected site and pushes them into the shared CEF session.
- Profiles are keyed by site, not tenant: `services/browser/profiles.py:65-75,295-306`. Page-cache keys likewise contain only site and URL: `services/browser/page_cache.py:130-138`. Vendor queue state is process-global: `services/browser/queue.py:77-79,125-150`.
- These RPCs are registered at `gui_rpc.py:2116` and `embed_rpc.py:143`; the gateway has neither a deny rule nor tenant-specific rewriting for them.
- **Prerequisites/impact:** A normal cloud-entitled account can replace supplier API credentials used by other accounts, inject another vendor login into shared browsing, contaminate acquisitions, and inspect shared download-job metadata. Actual credential values are not directly returned by `vendor_status`, a positive control.
- **Important repro detail:** Omit the optional cookie `path` field; a literal `path:"/"` is caught by the gateway’s generic path check, but the cookie normalizer supplies `/` when absent.
- **Minimal remediation:** Disable desktop-global supplier/session RPCs on the web bridge until credentials, profiles, caches, and queues are tenant-scoped and authorization is enforced.
- **Safe repro:** Replace configuration/profile roots with temporary directories and stub CEF cookie import. Invoke the handlers under two synthetic caller contexts with dummy keys/cookies; demonstrate that the second replaces the first in the same storage. No real credentials or browser profiles.

### 4. High — Parts RPC path aliases bypass the gateway’s filesystem jail
**CWE-863: Incorrect Authorization**

- The gateway recognizes a fixed set of keys at `Webservice/gateway/engine_bridge.py:215-231`. It omits **`cad_path`, `folder`, and `artifact_path`**.
- **Concrete sinks:**
  - `vendor_part_register_local_cad`: `services/parts/vendor_parts.py:256-265` → `:120-138`, resolves and copies caller-selected CAD files into the global library.
  - `vendor_part_scan_library`: `services/parts/parts_library.py:150-152` → `:94-145`, scans an arbitrary directory and returns/registers discovered CAD paths, filenames, and metadata.
  - `parts_import_artifact`: `services/parts/gui_rpc.py:1126-1156`, opens a caller-selected ZIP/CAD path and passes it to native import.
- **Prerequisites/impact:** Authenticated web tenant, knowledge or guess of another directory/file path. This allows unauthorized discovery and copying of other customers’ CAD files into shared library state; arbitrary native parsing/import is also reachable.
- **Scope precision:** Native import returns document identifiers/body counts (`core/parts/place.cpp:134-159`). I did **not** prove full geometry extraction solely through that handler: the gateway’s document-capability rules remain a separate control.
- **Minimal remediation:** Replace heuristic key scanning with per-method typed authorization; authorize resolved source paths and tenant-scope the vendor library.
- **Safe repro:** Create temporary sibling “tenant A” and “tenant B” roots with one harmless CAD-named fixture. Show `path_violation()` accepts requests carrying B’s path under these aliases; stub native import/index rebuilding and confirm the worker receives or copies B’s fixture.

### 5. High — Rendered research fallback defeats the public-destination policy before checking the result
**CWE-918: SSRF**

- **Source → sink:** Public research HTML judged a JS shell triggers rendering at `services/research/fetch_page.py:357-368`.
- `_render_page()` calls `browser_fetch` first and validates `final_url` **after navigation** at `:214-225`.
- `services/browser/session.py:824-830` forwards navigation without a URL/redirect policy. Playwright executes `page.goto` at `services/browser/pw_client.py:69-74`; CEF’s `FetchClient` implements loading/rendering handlers but no request-policy handler at `host/src/ui/widgets/cef/cef_headless.cpp:89-144`.
- **Prerequisites/impact:** An attacker controls a public page the user/agent legitimately requests. The static response can be a JS shell whose rendered version navigates to an internal URL. The internal request has already happened when final-URL validation rejects it. Internal subresource loads are not covered by final-URL validation either.
- **Nearby control:** The static HTTP branch does validate each redirect before requesting it (`fetch_page.py:288-297`); the fallback does not inherit that guarantee.
- **Minimal remediation:** Enforce policy inside the browser network/navigation boundary, including redirects and subresources, or render in an egress-restricted service without internal-network access.
- **Safe repro:** Use temporary localhost fixtures and a fake browser backend first to show ordering: record navigation to a forbidden final URL, then observe rejection. An optional isolated real-browser check should use only a temporary marker listener and profile.

### 6. High — Remote response bodies are buffered without a byte budget
**CWE-400: Uncontrolled Resource Consumption**

- **Sources → sinks:**
  - Research page: `services/research/fetch_page.py:292-301` buffers the full HTTP response before Markdown truncation.
  - Datasheet: `services/research/datasheet_fetch.py:129-139` buffers the full PDF before extraction.
  - Browser downloads: `services/browser/pw_client.py:103-114` obtains the entire body.
  - CEF downloads: `host/src/ui/widgets/cef/cef_headless.cpp:153-156` grows a vector on every chunk without a cap; `:669-679` writes only after completion.
- **Prerequisites/impact:** A malicious public page/download, including an adapter-discovered CAD link, can exhaust worker/helper memory. On shared infrastructure this affects other customers.
- **Controls considered:** Timeouts and output/Markdown truncation are not input-byte budgets. The hardened `download_public_cad()` has a streaming 200 MiB cap, but many browser adapters bypass it; later CAD file-size checks occur after buffering.
- **Minimal remediation:** Stream with explicit decoded-byte limits, bounded parsing, and cancellation; apply the same cap to CEF callbacks and Playwright-backed downloads.
- **Safe repro:** Use a small bounded response, e.g. 1 MiB, with instrumented fake HTTP/browser responses to demonstrate full consumption before truncation. Do not run an actual memory-exhaustion test.

### 7. High — PLM automatically expands all unknown ZIP attachments without limits
**CWE-409: Improper Handling of Highly Compressed Data**

- **Source → sink:** `services/plm/snapshot.py:124-134` reads every non-derived member fully and retains unknown entries in `extras`, with no member-size, total-expanded-size, count, or compression-ratio limit.
- This is reached automatically when a vault first opens: `services/plm/vault.py:305-310,1195-1209`; container reading occurs at `:1313-1323`. Files-page discovery also invokes expansion at `:1693-1695`.
- **Prerequisites/impact:** Attacker supplies a `.step+`/`.ecad+` with a tiny valid document and a highly compressed unknown attachment. Merely discovering/checkpointing the project can consume excessive memory in the shared worker.
- **Nearby controls:** Derived geometry/cache entries are skipped, and project path containment exists. Neither limits unknown preserved members.
- **Minimal remediation:** Enforce member-count and expanded-byte budgets before and during ZIP reads, with bounded handling of preserved attachments.
- **Safe repro:** A temporary ZIP with `document.json={}` and a 1 MiB repeated-byte attachment demonstrates unrestricted expansion/retention. Instrument reads rather than increasing the payload toward OOM.

### 8. High — Box mirroring follows project-file symlinks outside the selected project
**CWE-59 / CWE-200: Link Following / Information Exposure**

- **Source → sink:** `services/plm/box_mirror.py:520-536` skips dotnames but follows file symlinks through `p.stat()`. `:619-640` passes those paths to hashing/upload; `:573` reads their targets and sends them to Box.
- **Prerequisites/impact:** Box mirroring must be enabled. An attacker who can contribute a symlink to a project—for example through an ordinary Git checkout—can cause unrelated local files to be uploaded under a benign visible project filename. A collaborator with access to the configured Box destination can receive the data.
- **Controls considered:** `os.walk` does not follow directory symlinks by default, but file symlinks are sufficient. Size limits and dotname filtering do not constrain their targets. PLM’s `generic_snapshot.contained_path()` already provides a contrasting containment pattern.
- **Minimal remediation:** Reject symlinks/nonregular files or use a race-resistant contained-file open, then hash/upload the same verified file.
- **Safe repro:** Temporary project plus sibling `outside.txt`, linked as `project/manual.pdf`. Stub `_http` and `_access_token`, then capture that the multipart payload contains only the dummy outside marker. No Box requests.

### 9. High — Fetched PLM manifests can silently replace the destination of later syncs
**CWE-610: Externally Controlled Reference to a Resource in Another Sphere**

- **Source → sink:** Remote manifests include `remotes`; `services/plm/remote.py:794-825` commits this configuration into history. Pull replaces local HEAD with remote history at `:948-959`. The next `sync()` reads `remotes[].url` from that manifest at `:854-885`.
- `_fetch_folder()` uses it at `:379-384`, and an empty new destination triggers `_cas_push()` at `:930-936`; upload is `:478-488`.
- **Prerequisites/impact:** A malicious remote maintainer/collaborator can alter the versioned remote URL. After the victim pulls, a subsequent ordinary “sync origin” can publish private subsequent work/history to the attacker’s destination or use ambient transport authentication there, without a separate local trust decision.
- **Controls considered:** CAS guards concurrent writes, not destination trust. User-initiated configuration of a remote is legitimate; accepting remote-authored changes to that configuration is the crossing.
- **Minimal remediation:** Keep trusted transport destinations local, or require explicit approval for fetched changes before use.
- **Safe repro:** Three temporary local bare repositories: legitimate origin, victim, and empty “attacker” destination. Change the manifest remote in a crafted incoming commit, perform the two sync transitions using only temporary repositories, and verify a dummy victim-only commit reaches the third repository.

### 10. Medium — Git remote URL credentials are committed into shared PLM history
**CWE-312: Cleartext Storage of Sensitive Information**

- `services/plm/remote.py:756-775` accepts credential-bearing SSH/HTTPS URLs; `:794-825` embeds them verbatim in versioned `manifest.json`.
- The credential rejection at `:715-716` applies only to live-server URLs. Removing sibling keys named `token`/`password` at `:813-816` does not sanitize URL userinfo.
- **Prerequisites/impact:** A user supplies an authenticated Git HTTPS URL, an explicitly supported transport convention documented at `remote.py:10-12`. Collaborators/readers of shared history then receive the credential; changing the current URL does not remove historical copies.
- **Minimal remediation:** Reject/remove URL userinfo for all remote types and use a local credential store/helper; rotate already-shared credentials.
- **Safe repro:** Register `https://dummy-user:dummy-marker@example.invalid/repo.git` in a temporary vault without syncing, then inspect its temporary manifest/history for the dummy marker.

### 11. Medium — PLM path containment permits remote overwrites of vault control files
**CWE-73: External Control of File Name or Path**

- `services/plm/generic_snapshot.py:62-73` prevents escape from the project but permits `.aicadvault/config`, `.git/config`, and other reserved control paths.
- A peer-supplied item path reaches `services/plm/remote.py:263-279`, which writes a reconstructed ZIP to that destination. `manifest.loads()` only validates the schema version at `services/plm/manifest.py:84-97`.
- **Prerequisites/impact:** Attacker can publish a malicious PLM commit. A sync can corrupt its own vault/configuration or unrelated project control files, making future repository access fail. I am **not** claiming script execution from the ZIP bytes.
- **Nearby control:** Existing `../` and symlink-escape protections are genuine; this is an in-root reserved-path gap.
- **Minimal remediation:** Reject reserved metadata/control paths and incompatible destination types during whole-manifest preflight.
- **Safe repro:** In a disposable project, materialize a crafted item whose path is `.aicadvault/config`; compare that temporary file’s bytes. Do not use an existing user vault.

### 12. Medium — Tokenless loopback PLM relay accepts cross-origin WebSocket joins
**CWE-1385: Missing Origin Validation in WebSockets**

- `services/plm/server.py:321-333` permits an empty token for loopback listeners.
- `:352-363` calls `websockets.serve()` without an origin restriction; `:410-414` unconditionally authorizes when no token is configured.
- With valid project/item IDs, joining returns the document snapshot at `:562-584`, and proposals are processed at `:435-437`.
- **Prerequisites/impact:** A tokenless local relay, known project/item IDs, and a browser allowing the page’s loopback WebSocket connection. An attacker-controlled page can read/edit that project without being an authorized same-user desktop tool. Browser local-network restrictions may reduce deployability; they are not enforced here.
- **Controls considered:** Non-loopback listeners require a bearer token, comparison is constant-time, item resolution is contained, and message-size limits exist.
- **Minimal remediation:** Require a per-session secret even on loopback and explicitly allow only intended origins.
- **Safe repro:** Use the existing temporary fake-engine setup from `test_live_server.py`, start a fresh ephemeral loopback relay with no token, and connect with `Origin: https://attacker.invalid` and the fixture IDs. No real documents or daemon.

### 13. High on Windows — Site-supplied download filenames can escape the download directory
**CWE-22: Path Traversal**

- **Source → sink:** `services/browser/adapters/grabcad.py:91-100` extracts a raw HTML href, obtains its “filename” by splitting only `/`, and joins it to the download directory.
- A href ending in `..\..\outside.step` retains Windows separators. The retail PDF path repeats the pattern at `services/browser/adapters/_retail.py:387-390`.
- The resulting destination reaches the unrestricted write at `services/browser/pw_client.py:97-114` or `cef_headless.cpp:676-679`.
- **Prerequisites/impact:** Malicious/compromised acquired page content on Windows. The downloaded CAD/PDF bytes can overwrite files outside the acquisition directory, including existing engineering models. This is not arbitrary executable execution; the reviewed discovery patterns constrain suffixes.
- **Minimal remediation:** Generate a local filename or reject both separator types, drives, and dot components; verify destination containment before opening.
- **Safe repro:** Purely static `PureWindowsPath` fixture using a href with backslashes, plus a stubbed `browser_download` that records the destination. For a write check, use only a temporary Windows directory tree.

### 14. High, dependency-backed — Installed desktop Dulwich pack-resolution DoS is reachable through PLM
**CWE-835 / CWE-400**

- **Integrator evidence read:** `scratch/security-audit-2026-10-12/desktop-installed-audit.json`, Dulwich `1.2.7`, advisory **GHSA-35mr-4567-66vg**, fixed in `1.2.9`.
- **Applicable source paths:** `services/plm/remote.py:381-386` fetches attacker-controlled remote packs; `:625` clones bare repositories. Subsequent object reads use `repo[...]` at `remote.py:188-198,255-258` and `services/plm/gitbind.py:192-225`.
- **Impact/prerequisite:** A malicious Git remote/repository can reach the vulnerable pack parser, causing worker CPU exhaustion/crash. This finding applies to the integrator-reported desktop dependency, not the gateway’s separately reported `1.2.12`.
- **Minimal remediation:** Upgrade the affected runtime dependency to a fixed release and verify packaged runtime closure.
- **Safe repro recommendation:** Prefer the dependency’s bounded regression test. Any malformed-pack reproduction must run in a new disposable subprocess with a strict timeout, never the live worker.
- **Other supplied Dulwich advisories:** No calls to `filter_branch`, `porcelain.checkout(paths=…)`, or `stash.pop` were found in the reviewed PLM implementation. Do not label their traversal/RCE scenarios reachable here solely from package presence.

## yield: positive_controls_and_unconfirmed_gaps

**Current positive controls**
- PLM rejects traversal and symlink escapes outside project/tree roots through `generic_snapshot.contained_path`; sync preflights manifest paths before writes. Current tests include `test_sync_rejects_peer_manifest_with_escape_before_overwrite` and the generic snapshot escape tests.
- Research static HTTP fetching requires credential-free public HTTPS, rejects non-global IPv4/IPv6/mapped addresses, checks every DNS answer, and validates redirect targets before following them.
- `download_public_cad()` streams with declared/actual size limits and deletes incomplete downloads.
- CAD ZIP import selects supported members and writes to generated/basenamed destinations; no `extractall()` use was found in the assigned services.
- Browser profile slugs reject `.`/`..`; cookie sidecars are chmodded `0600` and consumed/deleted. Site profile locks and request ceilings exist.
- Printables validates page/download hosts and uses the bounded public downloader.
- Live PLM requires authentication off-loopback, resolves registered items inside the project, and bounds messages/operations.
- `services/intake` has no telemetry upload/network collection path in the reviewed code. `services/agent/config.py:1393-1406` defaults telemetry consent to `off`, rejects unknown tiers, and forces `off` in offline mode. Intake audit fields are local document data, not evidence of external collection.

**Confirmed control gaps, not independently demonstrated exploits**
- **DNS connection binding:** `_validate_public_url()` resolves addresses, then HTTPX resolves/connects independently. This occurs in both static research and public CAD download. There is no IP pinning/peer-address verification. A practical HTTPS rebinding exploit also needs the private destination to satisfy TLS for the requested hostname; do not claim generic metadata theft from this gap alone.
- **CEF account-context isolation:** `cef_app.cpp:140-153` defaults to single-process mode; `cef_headless.cpp:40-48` documents that CEF ignores later browser request contexts in that mode. This conflicts with the separate-account intent in `services/browser/usage_scrape.py:82-100,571-606`. The exact cross-account behavior needs a temporary two-profile cookie-isolation reproduction before making a separate runtime finding.
- **Consent scope:** Box is explicitly opt-in (`box_mirror.py:121,732-734`). Its project-wide file collection and research provider requests are not telemetry uploads. I did not establish that the telemetry toggle promises to disable those separately requested integrations.
- **Secret sidecar creation:** Cookie/config writers chmod after writing rather than creating with private permissions. Exposure depends on parent-directory permissions/umask and was not promoted to a standalone vulnerability without deployment evidence.

## yield: reviewed_paths

1. **PLM file/Git/remote boundaries:**  
   `remote.py`, `gitbind.py`, `gitscm.py`, `manifest.py`, `generic_snapshot.py`, `snapshot.py`, relevant sections of `vault.py`, `worker_rpc.py`, `server.py`, `box_mirror.py`; targeted searches of `ai_vc.py`, `branches.py`, `cad_graph.py`, `graph_config.py`, `graph_links.py`, `live_client.py`.
2. **Browser acquisition/session boundaries:**  
   `session.py`, `profiles.py`, `pw_client.py`, `cef_client.py`, `queue.py`, `page_cache.py`, relevant `embed.py`, `driver.py`, `stealth.py`, `usage_scrape.py`; adapter download-sink search across the adapter inventory; detailed review of `adapters/__init__.py`, `_retail.py`, `grabcad.py`, `printables.py`, `parts_provider.py`.
3. **Parts file/GUI/RPC boundaries:**  
   `gui_rpc.py`, `worker_rpc.py`, `embed_rpc.py`, `vendor_parts.py`, `parts_library.py`, `product_media.py`; targeted sink searches of `source.py`, `hub.py`, `index.py`, `bolts_loader.py`.
4. **Research:**  
   `fetch_page.py`, `datasheet_fetch.py`, `providers.py`, `common.py`, relevant `firecrawl_pool.py`; targeted searches of `cache.py` and `material_web_lookup.py`.
5. **Intake/consent:**  
   Relevant sections and I/O/sink search of `intake.py`, `test_telemetry_consent.py`, and `services/agent/config.py:1387-1407`.
6. **Reachability/nearby controls:**  
   `Webservice/gateway/engine_bridge.py`, `services/worker_main.py`, `core/hostd/server.cpp`, `worker_bridge.cpp`, `core/parts/place.cpp`, `host/src/ui/widgets/cef/{cef_daemon.cpp,cef_headless.cpp,cef_app.cpp}`.
7. **Existing test evidence read/search:**  
   `services/plm/{test_remote.py,test_remote_transports.py,test_plm_generic.py,test_gitscm.py,test_live_server.py}`, `services/research/test_research.py`, browser profile/hardening/Playwright tests, parts worker/thumbnail test inventories. Tests were **not executed**.

## yield: verification_recommendations_and_limits

**Suggested existing checks for the integrator**
- `services/research/test_research.py::test_fetch_page_rejects_private_targets_before_fetch`
- `services/research/test_research.py::test_http_get_revalidates_private_redirect_before_next_connection`
- `services/plm/test_remote.py::test_sync_rejects_peer_manifest_with_escape_before_overwrite`
- `services/plm/test_plm_generic.py::test_forged_manifest_cannot_restore_outside_project`
- `services/plm/test_plm_generic.py::test_tree_symlink_members_cannot_read_or_restore_outside_project`
- `services/plm/test_live_server.py::test_non_loopback_and_authentication_fail_closed`
- `services/browser/test_pw_client.py::test_download_uses_context_cookies_and_writes_bytes`
- `services/intake/test_telemetry_consent.py`

These are existing controls/regressions, **not coverage of all new findings**. Avoid indiscriminately running `test_gitscm.py`: its GitHub test can inspect ambient CLI authentication.

**Limits:** No deployment/firewall/browser-runtime claims were verified; no actual secrets/configuration or customer data were read. Windows traversal and CEF profile isolation were source-reviewed, not executed. This was a boundary-focused review across the assigned five service areas, not a line-by-line correctness review of every engineering formula or every vendor parser. Native CAD-parser memory safety, complete telemetry upload infrastructure, and general gateway authorization outside the traced methods remain with their respective audit owners.
