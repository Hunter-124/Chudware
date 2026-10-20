# Chudware security audit — 2026-10-12

## Executive decision

**Current state does not support a security assurance for an Internet-facing, multi-tenant Chudware deployment. External release is blocked.**

### Verified local remediation — 2026-10-13

The findings and original observations below describe the vulnerable audit baseline,
not an unchanged source tree. The first P0 batch is implemented and locally verified:

| Boundary | Current local disposition | Verification |
|---|---|---|
| RB-01 / browser exposure in RB-02, RB-03, RB-10 | **Contained, not full product closure.** Version-1 `/engine` admits schema-validated `hello`/`ping` only, for all web roles. All CAD, import, agent, worker, global lifecycle, credential and device RPCs are unavailable. Browser modeling is intentionally disabled until sink ownership and isolated workers exist. Desktop transports are unchanged. | Native/worker/lifecycle registered-method negative matrix for both web roles; strict envelope, recovery/event filtering and pending-request bounds regressions; isolated real gateway+hostd smoke. |
| RB-04 / OAuth preregistration and OAuth-only admin | **Fixed locally.** OAuth deletes unverified passwords and invalidates old verification/recovery capabilities without blessing them; verified passwords survive. OAuth-only admin bootstrap removes password login and revokes old sessions. Authentication transitions serialize on the account row. | Real local OAuth state/cookie callbacks and transactional/concurrency regressions; preregistered password remains rejected after the isolated callback. |
| RB-04 / dormant sessions | **Fixed locally.** Suspension/denial revokes sessions in the account-status transaction. Session issuance locks the same row and rejects blocked accounts; supported pending-account sessions remain possible. | Suspend/deny→reinstate without intervening request, rollback and unaffected-account regressions; isolated old bearer returns `401` after reinstatement. |
| RB-04 / billing authority | **Fixed locally.** Billing changes customer/plan/subscription fields, never operator-controlled account status or decision metadata. | Real-store checkout/renewal/downgrade/deletion/failure coverage across all account statuses; isolated genuine-HMAC checkout preserves suspension and updates the plan/subscription. |

Verification: **814 gateway tests passed, 29 skipped** in the provisioned gateway
environment. The separate smoke exercised production-mode actual ASGI routes,
disposable PostgreSQL, a uniquely isolated existing hostd build, a fake ambient
provider sentinel, real local OAuth state/cookie handling with only provider identity
retrieval replaced, and Stripe's real HMAC webhook verification. Exact observations:
[`p0-remediation-evidence.json`](p0-remediation-evidence.json). Original vulnerable
observations remain unchanged in [`runtime-evidence.json`](runtime-evidence.json).

**No production deployment, session invalidation, credential rotation, or customer-impact
investigation was performed.** Previously activated attacker passwords and sessions
from historical vulnerable transitions are not identified or repaired by this source
change; deployed exposure needs log review and operator-directed revocation/recovery.
The following source cutover supersedes the initial containment without changing this
historical P0 evidence. **External release is still blocked**; no clean-build,
immutable-commit, production-deployment or incident-closure attestation is claimed.

### Verified source cutover and isolated acceptance — 2026-10-13

| Boundary | Verified disposition | Evidence |
|---|---|---|
| Workspace/actor ownership and execution | Contract 2 uses explicit rights/epoch admission and a complete 660-method classification. Workspace cells, actor-private workers and nested Ask/Plan readonly sandboxes replace the shared daemon path. Foreign/malformed worker handles refuse as `not_found`; document IDs remain cell-scoped. Managers can stop another actor, not steer/read/close it. | 21 real two-tenant scenarios plus 81 deployment cases pass independently: **102 passed**. Real gateway middleware, disposable PostgreSQL, production cell/worker launchers, built `hostd --cell`, nested bubblewrap, pinned Chud RPC and a local refusing CONNECT broker. |
| Revocation, credentials, continuity and quotas | Silent observer revocation, stale meshes, active process-tree stopping, BYOK rotation, same-part sequential edits and attribution are exercised. Per-actor abuse budgets do not affect other actors; storage accounting charges the workspace owner. A killed or OOM-limited cell leaves its sibling intact. | Acceptance includes real pids `EAGAIN`, isolated allocation OOM, crash/restart, warm reopen with zero executed features, and rotated-key cancel/resume. **Not** a hard-disk-quota or installed-production-cgroup attestation. |
| Persisted native state and hostile files | `cache.provenance` authenticates consumed cache members. Foreign caches restore immediately as unverified and refresh in the background; valid checkpoints stay warm even if another cache entry is skipped. SAT/PLY/STL/SolidWorks bounds, DXF work budgets, KiCad depth and archive inflation limits; private open-file native save publication and exact-case archive lookup. | Controlled old-build repro observed sibling truncation and uppercase metadata admission; patched real C API refuses both and reopens a 24,000 mm³ box warm. Four standalone parser binaries pass Clang ASan/UBSan (miniz dependency not instrumented); native core **135/135** and host **31/31** ctest pass. |
| Release/input authenticity | Linux installation verifies Ed25519 artifact identities. Gateway publication/promotion authenticates exact payload/SBOM/provenance/test bytes from private snapshots. Native stable promotion needs an owner-authorized publisher attestation. Retained real CTest/JUnit/log hashes bind each artifact. Candidate Python inspection uses a separate trusted base, and Chud binary probes run private snapshots. Git replacement refs are forbidden. | Release producer **149 passed, 22 skipped**; runtime/interpreter/CTest regressions **41 passed**; workflow policy reports **0 errors, 0 advisories**. Real disposable-key producer→gateway round trip accepts Linux and refuses unsigned Windows stable promotion. Real Windows/macOS qualification remains unavailable. |
| Staging, identity, commerce and input boundaries | Dedicated staging identity/storage/DSN/signing root/units, production-operation refusal and opt-in ingress. OAuth/status fixes plus passive identity transitions, encrypted outbox contexts, settled/deduplicated credit grants, marketplace injection and race fixes. Central pinned-address bounded HTTP, PLM sync/ZIP/Git safety, SPICE data grammar/name remapping, PDF escaping, bounded capture and manufacturing state. | Final pinned-dependency gateway suite: **1021 passed, 29 skipped**; network **289 passed**, control-language **90 passed** before additional real-netlist regressions; manufacturing Python **110 passed**, full native firmware **43/43** including live Renode, manufacturing native **64/64**. Exact scopes and final regressions are retained in the remediation evidence. |
| Dependencies | Public dependency pins and integrity-bearing site lock upgraded; reachability and remaining conditional advisory exceptions recorded. | Disposable public-package environment `pip check` passes; site install/build succeeds and production npm audit reports zero vulnerabilities. [`dependency-dispositions.md`](dependency-dispositions.md) preserves original advisory inputs. These are not private runtime wheel approvals. |
| Operational source closure | Nightly/pre-deploy dumps stream directly into age; manifests cover configured data/workspace/bucket/cell roots and mount boundaries, with credential separation and environment-scoped retention. Restore is isolated, bounded and path/type-checked. Passive terminal sends revalidate authority and a one-second watcher closes silent revoked endpoints. Entitlement provisioning delivers bounded key bytes via stdin, not a plaintext temporary file. | **33** backup regressions and **49** terminal/provisioning cases pass. Real disposable PostgreSQL+age restore drill preserves source rows and opaque cached-part bytes, restores all four roots, excludes nested credentials and retains no plaintext dump. No live/off-host recovery attestation. |

**Acceptance limitations:** `.venv-mcp` and its installed editable RPC SDK are bound
readonly only in the scratch harness because the authenticated runtime lock is absent.
Production staging still refuses that substitution. Provider network calls are deliberately
refused by the local broker; no live LLM response or pooled/subscription provider broker
is certified. The WASM client builds, but full customer browser/provider workflows and
authorized Windows/macOS installer/verifier fixtures remain release prerequisites.

Exact exercised commands, before/after native observations, acceptance scenarios and
limits: [`release-hardening-remediation-evidence.json`](release-hardening-remediation-evidence.json).

**Operator gates still open:** complete reviewed private runtime/wheelhouse and base-image
identities; production UID/group/sudo and installed unit/drop-in parity; runner defining
workflow/ref restrictions and protected-secret migration; root-provisioned storage quotas;
edge/firewall/SSH/exposure validation; authenticated key rotations/access-log review;
off-host backup and restore objectives. Source/templates/scratch results do not prove these.

**Verification side effect:** an initially mis-isolated browser-profile fixture wrote
metadata into the developer's profile root. The request-root reload bug was fixed;
test-created entries and fixture robots-cache fields were removed. Existing cookie/profile
data were not opened by Chromium or deleted; prior bookkeeping timestamps could not be
reconstructed. This local testing incident is not customer-compromise evidence.

### Original audit baseline


The primary failure is architectural, not cosmetic: an ordinary entitled web account can reach one shared native daemon and services worker. In an isolated production-mode gateway exercise, that account:

1. requested an arbitrary host process through `agent_spawn`, and the supplied `/usr/bin/touch` argv executed;
2. received a fake ambient `OPENAI_API_KEY` from `agent_resolve_provider`;
3. bypassed the gateway path jail with a NUL-containing path and read a marker outside its tenant root.

A second isolated exercise confirmed that `run_model` code receives unrestricted Python builtins. These are direct confidentiality, integrity, and availability failures at the customer boundary. They do not require model cooperation, administrator role, production credentials, or a parser exploit.

Independent release blockers include OAuth account pre-hijack, missing ownership on shared runs/sessions/device operations, staging-to-production authority crossover, unsafe release-signing/installer trust paths, SSRF and unchecked file aliases, native parser memory-safety/resource defects, desktop filename shell injection, and unsafe firmware-emulator configuration.

No production system, customer file, real credential, provider API, signing key, network target, or physical device was touched. Therefore this audit establishes **vulnerable code paths**, not evidence that a customer was compromised. Production impact remains unknown until operational evidence is collected.

## Audit basis and confidence

| Item | Audit basis |
|---|---|
| Repository | `/home/hunter/Repositories/CAD` |
| Base commit | `92a4ebb71ce307fbf68babc736945c6dfe968cc2` |
| Source state | Current dirty worktree; initial inventory was 140 modified and 8 untracked files. No immutable snapshot was created. Existing changes were treated as user-owned and were not overwritten. |
| Architecture under review | C++ core/host daemon, Python workers/services/MCP, native and WASM host, FastAPI gateway, marketplace/billing, release/deployment, embedded frontend, simulation, firmware, and manufacturing |
| Review model | Sixteen parallel specialist source reviews, parent correlation/deduplication, dependency and history scans, then bounded isolated runtime reproduction |
| Audit-time product changes | None during the original audit. The separately dated P0 remediation above records subsequent source changes and verification. |
| Strongest evidence | Actual production-mode ASGI gateway + disposable PostgreSQL + isolated local hostd/libchudware; actual identity/billing transitions; direct predicate/model executor probes |
| Source-confirmed evidence | Current input-to-sink traces with controls and prerequisites recorded in the linked annexes |
| Unavailable evidence | Live production units, edge policy, firewall/SSH, service privileges, provider revocation, CI runner ACL/reimage, private Chudcode runtime internals, backup restores, customer-impact logs, Windows/macOS runtime behavior |

Evidence labels used here:

- **Observed:** exercised with disposable local fixtures; exact results are in [`runtime-evidence.json`](runtime-evidence.json).
- **Source-confirmed:** complete source path and missing control were traced, but the payload was not run.
- **Conditional:** source defect is established, but exploitability depends on named deployment, platform, feature, or third-party behavior.
- **Unresolved:** required implementation or live operational evidence was unavailable; no safety claim is made.

Reviewer-local counts below are deliberately **not summed into one vulnerability total**. Several reports describe the same shared-daemon root cause at different layers, and a flat sum would overstate the result.

## Immediate release blockers

| ID | Boundary failure | Evidence | Customer consequence | Required disposition before exposure |
|---|---|---|---|---|
| RB-01 | Browser tenant → shared host execution | **Observed.** `/engine` forwarded `agent_spawn`; supplied argv executed. `run_model` independently receives unrestricted Python builtins. Multiline frames can bypass method inspection. | Read/modify customer data and service credentials; disrupt the shared service; pivot to any device or process available to the service account. | Remove these methods from the browser surface immediately. Replace default-allow forwarding with a versioned default-deny schema. If remote code execution is a product requirement, place it in a separately authorized, per-tenant, resource-bounded sandbox with no ambient daemon/control/credential access. Parse exactly one JSON envelope and reserialize it before forwarding. |
| RB-02 | Tenant path and object ownership are not enforced at sinks | **Observed** NUL path-jail bypass. **Source-confirmed** unchecked document references, domain file aliases, run/session/subagent/DAG/reverse-engineering IDs, SVG sheet paths, `run_id`, and manufacturing/device objects. | Cross-customer CAD/mesh/session disclosure, overwrite/cancellation, and destructive global operations. | Bind every persisted object and handle to tenant/workspace ownership. Authorize at the sink, not by guessing JSON key names. Reject NUL/control characters and canonicalize once. Deny all unowned/global methods until capabilities exist. |
| RB-03 | Shared ambient credentials and mutable global integrations | **Observed** fake ambient provider key returned to a standard tenant. **Source-confirmed** shared provider management, vendor credentials, browser cookies, worker control information, and native-assist ambient credential use. | Provider-key theft/spend, logout/key mutation for other users, supplier-account compromise, and cross-tenant browser state. | Never return secret-bearing provider descriptors. Remove global management aliases from tenant RPC. Isolate credential stores and browser state per tenant. Rotate any real credentials that were present in a deployed vulnerable daemon after access-log review. |
| RB-04 | Identity and account-state transitions cross trust boundaries | **Observed.** A preregistered unverified password became valid after a legitimate OAuth callback. A dormant session survived suspend→reinstate. A valid checkout event reactivated a suspended account. | Account takeover, survival of revoked access, and reversal of an operator security decision. | OAuth proof must not verify a password chosen before inbox control. Revoke sessions transactionally on suspension/denial. Billing may update billing state/plan but must never override operator status. Review OAuth-only admin bootstrap by the same rule. |
| RB-05 | One shared daemon amplifies parser and resource defects | **Source-confirmed.** SAT nonadvancing allocation, binary/ASCII PLY out-of-bounds reads, unchecked PLY indices, KiCad stack exhaustion, DXF/SolidWorks/archive amplification, unbounded native/process outputs, and daemon-killing `agent_spawn` metadata. | One customer can crash or exhaust the process serving other customers; native memory errors may have impact beyond denial of service. | Admit untrusted formats in isolated, quota-constrained workers; add parser depth/count/expanded-byte/work budgets and checked arithmetic/indexing. A shared process may not be the blast-radius boundary for hostile CAD input. Fuzz the corrected parsers under sanitizers before re-enabling remote import. |
| RB-06 | Release/signing/install trust is bypassable | **Source-confirmed.** Windows finalization ignores native failure codes, PR code shares persistent signer state, Linux installer does not verify the advertised artifact signature, and build inputs/private RPC packages can resolve without immutable provenance. | Distribution of attacker-substituted installers or libraries; signing-key use/theft; root execution through package install. | Stop promotion on every failed builder/verifier. Separate disposable PR workers from signing workers. Verify pinned-key signatures in the Linux installer. Use hash-locked/offline sources and `--no-index` for private packages. Require immutable archive hashes and commit IDs. |
| RB-07 | Staging has production data/control authority | **Source-confirmed.** Staging inherits production workspace and daemon; staging admin can trigger production deploy/log operations; provisioning republishes staging. | Production CAD disclosure/modification and production interruption after staging compromise. | Give staging a separate workspace, bucket, daemon, service identity, credentials, and operational routes. Keep it unpublished by default. A staging role must never imply production authority. |
| RB-08 | Server-side fetch controls are inconsistent | **Source-confirmed.** Parts thumbnails, datasheet PDFs, rendered research fallback, and redirects can reach loopback/private destinations; response bodies are often unbounded. | Internal service probing/data return and shared-process memory exhaustion. | Centralize a pinned-address public-destination client: scheme allowlist, DNS classification, connect-to-checked-address, hostname-preserving TLS, redirect refusal/revalidation, proxy refusal where required, and streaming byte/time budgets. |
| RB-09 | Stored content and filenames trigger trusted actions | **Source-confirmed.** Marketplace description attribute injection creates a real `data-buy="credits"` action. Desktop image/gallery filename interpolation reaches `std::system`. | Buyer credits spent by a disguised documentation link; local code execution when a customer clicks a normal project filename. | Build DOM/command arguments without string interpolation. Bind purchase handlers only inside trusted action containers and require explicit confirmation. Replace shell command construction with argv-based process APIs. |
| RB-10 | Firmware/manufacturing boundaries can affect the host or other tenants | **Source-confirmed.** Renode command generation accepts executable monitor text, QEMU enables native semihosting for untrusted ELF, manufacturing RPCs/events are global, and another tenant can claim/cancel shared devices. | Host file/service exposure, falsified simulation evidence, interruption of active machine work, and unsafe control-plane interference. Physical start from the audited path remains gated; cancellation/interruption risk remains. | Sandbox emulators with copied fixtures and no host/network authority; disable native semihosting. Encode/allowlist Renode identifiers. Bind devices, leases, artifacts, jobs, and events to tenant/operator ownership. Keep physical execution disabled until independent safety validation. |
| RB-11 | Historical secrets and recovery posture lack closure evidence | Scanner plus deployment-source evidence. Known historical Cloudflare/R2 material exists in history, but current validity and rotation are unknown. Backup scope misses canonical external customer roots; plaintext rollback/restore paths remain. | Continued credential abuse if not revoked; false confidence in recovery; loss of customer CAD after incident. | Obtain provider-side revocation/rotation records and access-log review. Inventory derived credentials and consumers. Encrypt/retain rollback dumps, include every configured root in backup manifests, separate credential backups, and complete isolated restore drills. |
| RB-12 | Known-vulnerable dependency sets are currently selected | Current `pip-audit`/`npm audit` results. At least one PyJWT advisory reaches configured Access parsing; other advisory applicability varies. | Request failures, memory-safety/DoS risk, and supply-chain exposure depending on feature reachability. | Upgrade to fixed versions, regenerate locks, and verify actual reachable paths. Do not report advisory counts as confirmed application exploits; do not ship known-affected production sets without disposition for every advisory family. |

## Observed verification

The audit intentionally used tiny synthetic markers and fake credentials rather than offensive payloads.

### Gateway and daemon boundary

The successful run used production-mode FastAPI middleware, a disposable Unix-socket-only PostgreSQL cluster, two synthetic active trial accounts, temporary workspace/bucket roots, and an isolated hostd socket. The daemon environment contained only a fake provider sentinel in addition to the minimum process environment.

Observed controls:

- anonymous `/api/me` returned `401`;
- anonymous `/healthz` returned only `{"ok": true}`;
- password login, secure/HttpOnly/SameSite cookie flags, own-workspace upload/read, traversal rejection, standard-user admin rejection, same-name tenant isolation, and suspension-time denial behaved as intended.

Observed failures:

- a cross-origin mutation request received `200`; this proves absence of a server Origin check, **not** a standalone browser-CSRF exploit because SameSite/CORS/content-type behavior still matters;
- a standard tenant connected to the shared native daemon;
- `agent_resolve_provider` returned `AUDIT_SENTINEL_NOT_A_KEY` from the daemon environment;
- a NUL-containing authorized-looking path returned a marker outside the tenant root;
- `agent_spawn` accepted `/usr/bin/touch <temporary-marker>`, and the marker appeared.

### Identity and billing

With real local OAuth state/cookie handling and only provider identity resolution replaced by a fixed tuple:

- anonymous preregistration installed an unverified attacker-chosen password;
- password login was initially refused with `403`;
- a legitimate OAuth callback completed with `303`;
- a fresh password login using the preregistered password then returned `200`;
- a previously issued session returned `200` after suspend→reinstate without an intervening request;
- `checkout.session.completed` changed a suspended account to `active` and selected `studio`.

### Restricted execution claims

- `services.dag.verify._eval_predicate` evaluated `abs.__self__.open(<temporary-marker>).read() == 'fixture-only'` as `(True, None)`.
- `services.kernel_exec._exec_model` executed a temporary-file write with unrestricted builtins and no execution error. The build123d namespace was replaced with an empty fixture only to avoid geometry work; the execution primitive itself was real.

Exact structured observations and qualifications: [`runtime-evidence.json`](runtime-evidence.json).

## Complete source-review register

| Surface | Highest severity | Surviving findings | Customer-risk themes | Full annex |
|---|---:|---:|---|---|
| Web identity/security events | High | 9 | OAuth pre-hijack; verification login CSRF; pre-limit gzip expansion; recovery-token invalidation; global telemetry destination; plaintext live links in outbox; lazy session revocation; PyJWT exception; registration timing oracle | [`WebIdentity.md`](reviews/WebIdentity.md) |
| Gateway engine bridge | Critical | 9 including merged daemon administration | Multiline policy bypass; NUL path bypass; `run_id` path escape; shared provider/session/run state; pending-request leak; passive revocation; native-save quota bypass | [`WebEngine.md`](reviews/WebEngine.md) |
| Host daemon IPC/process lifecycle | Critical | 5 | Arbitrary spawn/global administration; daemon termination; permissive local file modes; Windows control-channel starvation; Windows mutex squatting | [`DaemonIPC.md`](reviews/DaemonIPC.md) |
| Agent/worker boundary | Critical | 11 | `run_model` code execution; provider key disclosure; runtime administration; unowned sessions/subagents/runs; delegated credential confusion; builtin/host-tool policy gaps; native assist; firmware path escape; memory/regex/mode defects | [`AgentBoundary.md`](reviews/AgentBoundary.md) |
| Web files/marketplace rendering | High | 4 | Revoked team mesh access; multipart pre-handler spooling; purchase-action injection; quota bypass | [`WebFiles.md`](reviews/WebFiles.md) |
| Commerce/organizations | High | 6 | Event-loop database deadlock; security-status override; unsettled-payment value grant; private metadata IDOR; failed→paid settlement loss; owner-removal race | [`Commerce.md`](reviews/Commerce.md) |
| Deployment/operations | High | 5 findings + 7 control defects | Secrets in argv; staging/production crossover; passive terminal revocation; staging publication; retention, backup, credential, documentation, and restore defects | [`Deployment.md`](reviews/Deployment.md) |
| Native untrusted inputs/persistence | High | 11 | Embedded filesystem references; SAT/PLY memory safety; DXF/SolidWorks amplification; archive-limit bypass; untrusted solved cache; save symlink | [`NativeInputs.md`](reviews/NativeInputs.md) |
| Simulation/ECAD/reverse engineering | High | 11 | Unchecked file aliases and SVG path; unowned sessions/jobs; ngspice control injection; PDF SSRF; STL/KiCad/archive denial of service; capture starvation | [`NumericalServices.md`](reviews/NumericalServices.md) |
| Integrations/PLM/parts/browser | High | 14 | Git option injection; SSRF; shared credentials/cookies; path aliases; unbounded fetch/archive; symlink upload; manifest-controlled remotes; credential persistence; relay origin; Windows download traversal; Dulwich DoS | [`Integrations.md`](reviews/Integrations.md) |
| Embedded desktop frontend | High | 5 | Filename shell injection; sign-out race; ZIP thumbnail expansion; unbounded CEF responses; persistent trace-log growth | [`EmbeddedFrontend.md`](reviews/EmbeddedFrontend.md) |
| Manufacturing core | High | 5 current + 8 historical revalidations | Global tenant/device state; unbounded artifact registry; printer DNS residual; attention replay/storage; serial identity residual | [`ManufacturingCore.md`](reviews/ManufacturingCore.md) |
| Manufacturing/firmware services | High | 6 current + 8 historical revalidations | Renode injection; QEMU native semihosting; shared temporary trust; G-code allocation; scenario expansion; process-output capture | [`ManufacturingServices.md`](reviews/ManufacturingServices.md) |
| Release/signing/update pipeline | High | 4 | Failed-verification continuation; persistent signer runner; unsigned Linux artifact metadata; conditional CMake TLS failure | [`SupplyChain.md`](reviews/SupplyChain.md) |
| Runtime/dependency provenance | High | 2 | Public-index resolution of private RPC package; unhashed/mutable native/bootstrap inputs; private runtime isolation unresolved | [`RuntimeArtifact.md`](reviews/RuntimeArtifact.md) |
| DAG/deliverables/training remainder | High | 4 | Predicate eval escape; unowned DAG controls; ReportLab local-file embedding; optional unauthenticated MLflow binding | [`RemainingSurfaces.md`](reviews/RemainingSurfaces.md) |

The separate [`OriginCheckSupplement.md`](reviews/OriginCheckSupplement.md) preserves the browser/server-origin analysis. Its identity text duplicates `WebIdentity.md`; it is not an additional set of nine findings. The tested `200` cross-origin server response is defense-in-depth evidence only; the review did not establish that an unrelated website can deliver an authenticated JSON mutation through current SameSite/CORS controls.

## Historical finding closure

### Manufacturing review

All 16 named historical manufacturing findings were revalidated against current source.

| Status | Count | IDs |
|---|---:|---|
| Fixed | 12 | `MFG-LEASE-BEARER`, `MFG-ARTIFACT-ARBITRARY-READ`, `MFG-GRBL-UNBOUNDED-LINE`, `MFG-PRINTER-BODY-UNBOUNDED`, `MFG-WEBHOOK-SSRF`, `MFG-OSASCRIPT-INJECTION`, `MFG-TCP-DRAIN-UNBOUNDED`, `MFG-REMOTE-PATH-TRAVERSAL`, `CAPTURE-CLIP-INTEGRITY`, `MFG-NOTIFY-SEND-OPTIONS`, `MFG-PYWS-FRAGMENT-LOOP`, `MFG-CAPABILITY-LEVELS-DROPPED` |
| Partial/residual | 4 | `MFG-WORKER-NO-ALLOWLIST`, `MFG-SERIAL-IDENTITY-PATH-ONLY`, `MFG-DNS-POST-AUTH`, `MFG-ATTENTION-UNAUTHENTICATED` |

“Fixed” means the named historical mechanism is absent in current reviewed source. It is not a certification of the whole manufacturing subsystem; current findings in the two manufacturing annexes remain open.

### Public rollout rows

| Historical row | Current classification |
|---|---|
| Stream-proxy `urljoin` SSRF | Surface deleted |
| Shared workspace/bucket | Route-level tenant roots present; environment isolation remains open through staging |
| Unrestricted shared-daemon bridge | **Open and release-blocking**; runtime-confirmed execution/credential/path failures |
| Host keys copied into session containers | Container surface deleted; other ambient credential paths remain open |
| Admin terminal shell | Partially hardened; passive revocation and intentionally persistent authority remain |
| Forwarded identity headers to stream upstream | Surface deleted |
| Missing CSP | Fixed in reviewed gateway source |
| Unbounded uploads | Destination streaming caps exist; framework multipart spooling still precedes them |
| Unpinned dependencies | Direct pins present; provenance gaps and current advisories remain |
| YAML allowlist/admin emails | File-backed authorization removed |
| 48-bit session IDs | Original manager deleted; current terminal IDs use 128 random bits |
| `/healthzFOO` public prefix | Fixed with exact/slash-boundary matching |
| Anonymous health telemetry | Fixed; isolated run returned only `{ok:true}` |
| Admin static assets for standard users | Fixed |
| Chat `?email=` identity fallback | Fixed |
| Arbitrary admin project paths | Resolved-root checks present |
| No rate limits/quotas | Controls present but incomplete across native/remote writers and combined storage |
| Raw journal tokens | Recognized-shape redaction present; staging still selects production journal |
| Release filename header injection | Fixed by basename restrictions |

Operational obligations are not closed by source alone: plan/storage enforcement and pooled credits are partial; the shared daemon is open; host privilege separation, firewall/SSH hardening, update state, historical credential rotation, and recoverable encrypted backups require live proof.

## Dependency and secret scanning

### Python

The manifest audit covered 54 exact package/version pairs split into two sets because the repository selects conflicting Dulwich versions. Five selected packages produced **81 distinct package-advisory pairs**: 77 in the 52-entry primary set and 4 for Dulwich in the secondary set. This is not “81 exploitable Chudware vulnerabilities”; advisories overlap by package and require reachability analysis.

Local environment snapshots were also audited:

| Snapshot | Entries | Affected packages | Package-advisory pairs | Qualification |
|---|---:|---:|---:|---|
| Gateway environment | 85 | 5 | 20 | Local environment, not proof of deployed production bytes |
| Desktop environment | 179 | 11 | 91 | Local environment; private `chudcode-rpc==0.3.1` could not be resolved by the public advisory service |

The PyJWT recursive-payload advisory (`GHSA-42vr-xj54-vc7v`) has a traced gateway path when Cloudflare Access authentication is configured; observed impact is an uncaught request-level exception, not authentication bypass or process compromise. Other advisories must be dispositioned from their actual call paths. Full data: [`dependency-audit.json`](dependency-audit.json).

### JavaScript/site toolchain

`npm audit --package-lock-only --ignore-scripts` reported 11 affected dependency nodes across 495 dependencies: 1 critical, 9 high, and 1 moderate. The critical Astro advisory is [`GHSA-26w7-cxv4-gfx2`](https://github.com/advisories/GHSA-26w7-cxv4-gfx2), involving AVIF optimization below the fixed release. A live customer-controlled image-optimization route was not established, so it remains an upgrade/reachability blocker rather than a claimed production RCE. Full data: [`npm-audit.json`](npm-audit.json).

### Repository history

Gitleaks 8.30.1 scanned 4,717 commits / 191,316,928 bytes and returned 145 detector hits across fixtures, documentation, vendor material, and history. These are **candidates, duplicates, and false positives—not 145 active secrets**. Three detections correspond to the documented historical `Webservice/Cloudflare-info.txt` exposure. Current validity was not tested and no secret values were opened.

The tracked working diff was 700,024 bytes with zero detections. Untracked and ignored local files were intentionally not scanned, so this is not a claim that the working directory contains no secrets. Sanitized locations: [`secret-scan-locations.json`](secret-scan-locations.json).

Required incident closure for the known historical exposure:

1. provider-side revocation records for the Cloudflare API token and R2 credentials;
2. replacement scope and custody records without secret values;
3. revocation of derived tunnel credentials if obtainable from the exposed token;
4. inventory of updated scripts, CI, object-storage clients, and deployed consumers;
5. provider/access-log review and customer-impact determination;
6. removal/disposition of remaining literals, without mistaking deletion or history rewriting for revocation.

## Controls that worked

The audit also found meaningful controls worth preserving:

- Argon2id password storage, dummy verification for unknown-login timing, high-entropy hashed sessions, purpose-bound single-use email tokens, secure cookie flags, OAuth state/cookie binding, provider token validation, and account-derived roles.
- Exact public-route matching, minimal anonymous health response, CSP/HSTS/Permissions-Policy/COOP, administrator checks, and route-level workspace/bucket containment.
- POSIX daemon runtime directory ownership/mode checks and `0600` socket; Windows named-pipe user/SYSTEM DACL and server identity checks.
- Manufacturing lease bearer/connection binding, bounded GRBL and HTTP/WebSocket paths, notification DNS pinning/redirect refusal, OctoPrint path validation, capture hashes, print-media staging/readback/journaling, and metrology/robot/PnP authority separation.
- Canonical release payload signatures, path validation, safe handoff extraction, updater artifact identity checks, offline signing requests, and macOS signing/notarization checks. These do not cure the separate pipeline/installer bypasses.
- Strong Chudcode artifact digest checks before execution and ONNX/Polyhedral-FEA immutable pins. The private runtime’s internal tool sandbox remains unavailable for audit.

## Remediation sequence and acceptance gates

### P0 — Contain active trust-boundary failures before any external exposure

1. Disable `/engine` for non-administrative web users, or reduce it to an explicit minimal read-only allowlist while the tenant capability model is built.
2. Block `agent_spawn`, `agent_stop/list`, daemon lifecycle/recovery, `run_model`, provider resolution/management, DAG/session/run/subagent global methods, native assist, manufacturing, firmware, and every unknown method by default.
3. Make the gateway accept exactly one JSON object per frame; reject parse failures, duplicate/trailing content, noninteger IDs if the daemon requires integers, and unsupported parameter shapes.
4. Separate staging resources and production operations.
5. Fix OAuth preregistration merging, suspension session revocation, and billing status ownership.
6. Pause affected signing/promotion paths and Linux bootstrap release until signature and runner isolation defects are closed.

**Acceptance:** a standard tenant cannot execute any process/code, recover a fake ambient secret, address another tenant object/path, enumerate global state, or affect production/staging/device state. Negative tests must exercise every registered RPC, not a hand-picked denylist.

### P1 — Establish real tenant and execution boundaries

1. Define owner/workspace fields for documents, sessions, runs, subagents, DAGs, simulations, reverse-engineering sessions, artifacts, jobs, devices, browser state, provider state, and passive subscriptions.
2. Authorize reads, mutations, cancellations, and event delivery independently at the sink. Revocation must close passive outbound streams.
3. Replace path-key heuristics with method schemas and capability-bearing path/object references. Reject NUL/control characters and enforce no-follow operations where collaboration/local-user races matter.
4. Move untrusted code, importers, emulators, and heavy parsers into disposable per-tenant workers with CPU, memory, task, file, network, and wall-clock ceilings.
5. Remove shared ambient provider/vendor credentials from tenant-reachable processes.

**Acceptance:** two-tenant adversarial integration runs demonstrate isolation for same-name paths, absolute/traversal/NUL paths, stale grants, IDs discovered through list APIs, silent observers, cancellation/control, credentials, crashes, and quota accounting.

### P2 — Close high-risk product and delivery paths

1. Correct native parser checked arithmetic, index bounds, recursion/depth/count, aggregate decompression, and work budgets; run sanitizer/fuzzer campaigns on corrected code.
2. Centralize SSRF-safe HTTP and bounded response handling.
3. Replace shell/HTML/monitor/control-language string construction with typed APIs or closed encoders.
4. Disable QEMU native semihosting and sandbox all firmware emulators.
5. Fix release fail-fast behavior, runner separation, Linux signature verification, private-package provenance, and every unhashed bootstrap input.
6. Upgrade advisory-affected dependencies and record reachability/exception decisions.

**Acceptance:** release evidence is cryptographically independent of mutable serving metadata; PR code cannot persist into secret-bearing jobs; all downloaded/executed build inputs have immutable identities; hostile-file regression fixtures fail within fixed resource ceilings.

### P3 — Prove operations and incident readiness

1. Verify installed unit/drop-in parity, service UID/group/sudo/user-manager authority, listeners, firewall zones, SSH policy, edge/proxy trust, terminal/provider-sharing settings, and staging publication.
2. Close historical credential rotation and inspect access logs for the vulnerable RPC/identity paths.
3. Back up every configured database/workspace/bucket/institutional root with explicit manifests; keep credentials separately protected.
4. Perform isolated restoration drills, including schema compatibility and source rollback limitations.
5. Confirm retention/update/backup timers for production and staging remain concurrently active.

**Acceptance:** signed evidence identifies installed configuration, credential rotations, monitored exposure windows, successful off-host backups, and completed restore objectives. Source templates and prose are not accepted as live proof.

## Customer protection and incident-response decision

There is no evidence in this audit that a customer was actually accessed or harmed. There is also no basis to claim that did not happen. Before external release or a public security assurance:

- determine whether the vulnerable `/engine` methods were ever Internet-reachable with real tenants and ambient credentials;
- preserve and review gateway/daemon/provider logs for `agent_spawn`, `run_model`, provider-resolution/management, recovery, unowned run/session/DAG/manufacturing calls, unusual file paths, and staging-origin operations;
- determine the exposure window for OAuth preregistration and account-status overrides;
- rotate credentials according to exposure, not merely code changes;
- notify affected customers if log/provider evidence establishes unauthorized access, modification, spend, or device interference, following applicable contractual and legal requirements.

Do not promise “no impact” from absence of logs. Do not claim a fix until the end-to-end tenant boundary and operational deployment are both verified.

## Audit limitations

- No production access, customer data, actual credentials, provider calls, hardware, print media, firmware flashing, signing, publishing, or destructive/exhaustion payloads.
- No complete audit of the private Chudcode runtime; builtin shell/eval/filesystem confinement is unresolved.
- No runtime verification on Windows or macOS; platform-specific findings are source-confirmed/conditional.
- No live CI policy, branch protection, runner reimage, key custody, bucket/CDN, Cloudflare, firewall, SSH, sudo, or backup evidence.
- No exhaustive cryptographic primitive review, third-party source audit, vendored parser audit, or numerical/physical correctness certification.
- Dependency advisories are point-in-time scanner results and require recurring refresh.
- Runtime hostd evidence used an existing dirty local binary; it proves the observed path, not a reproducible clean-build attestation.

## Deliverables

- Main decision record: this file
- Runtime observations: [`runtime-evidence.json`](runtime-evidence.json)
- Python dependency evidence: [`dependency-audit.json`](dependency-audit.json)
- JavaScript dependency evidence: [`npm-audit.json`](npm-audit.json)
- Redacted history scan locations: [`secret-scan-locations.json`](secret-scan-locations.json)
- Full source reviews: [`reviews/`](reviews/)
