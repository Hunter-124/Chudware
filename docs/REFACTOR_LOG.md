# Refactor Log

Running handoff log for the CAD modular refactor + GUI abstraction. Each node
appends a one-paragraph handoff (files touched, deferred, gotchas). See
`AGENTS.md` and the plan file for context.

## Archive

Entries through 2026-07 are in [`refactor-log/2026-07-and-earlier.md`](refactor-log/2026-07-and-earlier.md) (73 dated July entries plus the
undated P0/refactor-node sections).
Entries from 2026-08-01 through 2026-08-08 are in [`refactor-log/2026-08-01-to-08.md`](refactor-log/2026-08-01-to-08.md) (39 dated entries).
This file carries 2026-08-14 onward.

## 2026-08-14 — the version graph, and an OCCT crash that took the daemon with it

Two efforts. **(1) A real version graph.** The vault has always been a git DAG that
`gitbind.log()` read as a line (`parents[0]` only), so every commit reachable only through
a merge's second parent was invisible in `plm_history`, `plm_overview` and `plm_compare` —
merged-in work looked merged away. Added `gitbind.log_dag()` (Kahn walk over child→parent
edges from every branch tip, HEAD and version ref; newest-first AND topologically
consistent, so a renderer assigns lanes in one forward pass), a `refs/aicad/versions/<name>`
namespace with `services/plm/versions.py` for named versions (the Onshape V1/V2 noun), a
real `merge` entry kind (merges were stamped `checkpoint` and thus indistinguishable),
compare-and-swap on merge commits, branch ahead/behind topology, and `Vault.version_graph()`
decorating nodes with `parents`/`refs`/`release`/`pin`. Three additive RPCs
(`plm_version_graph`, `plm_version_tag`, `plm_version_list`) and `contracts/plm.md`
Appendix C record the surface, since §1 said "linear in v1" and §7 said "do not implement
plm_branch/plm_merge" while `branches.py` and the overview panel had already shipped both.
`plm_history` deliberately keeps its first-parent walk — Appendix B.6's per-item semantics
depend on it. GUI: a new `host/src/ui/plm/versions_tab.cpp` Versions page replacing History
on the rail (`plmDrawHistoryPage` stays for the Files page), deterministic git-graph lanes on
the force-graph's ImDrawList/camera/label-culling patterns, per-kind glyphs, branch and V*n*
chips, and an inspector with tag/restore/checkpoint/branch/merge, "Show changes" and
agent micro-history. Two things beat Onshape: any-two-commit compare (ctrl-click), and AI-VC
agent checkpoints with provenance.

Verifying against a REAL vault rather than the code found seven defects the compiling page
hid: the two-commit release pair drew twice (the pin is now folded into its release,
inheriting its decorations and re-parenting its children); "Show changes" reported "that item
is not present at one of those points" on a good commit because `plm_compare` is per-ITEM
(now unioned over the project's items, not-present treated as untouched); "AI activity" was
permanently empty because it sent `plm_timeline` a `commit` it does not accept and read keys
it never returns; a tip chipped "main main"; the legend advertised a Release-candidate state
this tree can never produce; left-drag panned AND selected; and 400 nodes drew every frame.
Two were NOT in the new code at all: `summarize.py` emitted a Unicode arrow the UI font
renders as `?`, and the rebrand from `aicad-plm-*` to `chudware-plm-*` renamed the trailer
READERS, so on any pre-rebrand vault kind/scope silently fell back to defaults (a release
read as `auto`, a planning commit polluted the design timeline) and every message leaked its
raw trailers — 6 of 6 messages on the dogfood vault, and 1 of 6 mis-scoped. Readers now take
both spellings. Also: the first-run onboarding wizard opened over every `CHUDWARE_SHOT`
capture, so headless shots were verifying the sign-in step instead of the surface under test.

**(2) T-BL61 — a fillet SIGSEGV killed hostd.** OCCT's corner resolver does not throw; it
dereferences into `Geom2dAdaptor_Curve::D0` and segfaults, so `blend_drop_failing`'s
`catch (Standard_Failure&)` was structurally unable to contain it. Blend builds now run in a
bounded forked child (`core/util/process_isolation.*`) that serializes its result with
`BinTools`; a signal, non-zero exit, unreadable payload or timeout becomes the same
"unbuildable edge set" the bisect already handled, so `n_dropped` and "round what you can"
are unchanged. A timeout is a failure, not a retry, because `fork()` in a multithreaded
daemon can deadlock the child on a malloc lock. Both the explicit-edge and the whole-body
overloads are isolated. Cost: +0.6 ms on a successful 12-edge fillet (3.88 → 4.47 ms median),
+0.4 ms on the 113 ms partial-drop path; one child per build attempt, never per edge.
**Deferred, honestly:** a 94-case convergent-blend sweep (`scratch/fillet_fuzz.cpp`) produced
ZERO signals, so the card's first DoD box stays unchecked and T-BL61 is `review`, not `done` —
containment is proved by fault injection (`CHUDWARE_TEST_BLEND_SIGSEGV`), which the card
permits. Closing it needs the real captured document, i.e. agent bracket builds re-run with
the run journal on. Also noticed and NOT touched: this repo's own tracked `.aicadvault/`
(2463 files, committed at e4a1a607) accumulates local object churn from any run that treats
the repo root as a project — that is a repo-hygiene decision for Hunter, not a code fix.

## 2026-08-15 — a pixel audit of the workspaces nobody had looked at

Drove all 25 `CHUDWARE_SHOT` scenarios headless and read the captures instead of trusting exit
codes. The exit codes were lying: **`ecad`, `parts` and `aiws` all exited 0 while photographing
the Model workspace.** Startup doc restore calls `activateTab`, which pulls any workspace that
cannot host a 3D part back to Model, and that race had been hand-patched three separate times
(parts, then reveng, then PLM) without anyone generalising it. One per-frame pin
(`AppState::shotWorkspacePin`) replaces the special cases; it ADOPTS any non-Model workspace the
run selects and only restores it when something yanks back to Model, because `fea`/`flow`/
`simdisc_*` reach their workspace mid-run — a blunter pin broke them the other way and a
subagent correctly refused to verify against the wrong surface. On top of that the `ecad` and
`drawing` scenarios only ever set their ribbon tab, never their workspace.

With honest captures, the surfaces confessed. **ECAD** had never been seen with a board on it:
its scenario drove `ecad_design`, a provider-backed AI tool, as its first call, got an error
envelope and aborted, leaving an empty grid crossed by what looked like stray geometry. The
default scenario now mints a provider-free `.ecad+` fixture and applies deterministic ops (48x32
outline, three footprints, GND/SIG, three routes over both layers, a via, one deliberate 0.1 mm
width breach), which exposed two more defects: **net row selection was a dead control** (set
`mgrSel`, changed no pixels — now highlights the net's copper and dims the rest, 20,294 pixels
different) and the "stray line" was the **y=0 grid axis** drawn solid and in front, landing
exactly where an Edge.Cuts line would be. Also worth recording: the first fixture's 6 DRC errors
and 3 warnings were HONEST (routes terminating on opposite-net pads); the fixture was fixed, not
the rules. And the native ECAD op vocabulary cannot create a net — an agent-facing API gap.

**Drawing** had an unreadable title block: row heights scale with sheet zoom while text does
not, and the guard was a `std::min` of the two — exactly backwards, so at 149 % zoom every label
collapsed onto its value. Now a `max` that grows the block upward, verified at 149 % and 211 %.
View labels also fell under the title block and under their own dimensions (dims paint later
into the same band), and the sheet hint read as two fragments because it ellipsised to the full
content width while the floating doc-tab strip covered the middle.

**Simulation Manager** was clipping its own text through three `PushTextWrapPos` calls handed
SCREEN-space X (the call takes window-local), and its body sliced a row in half under the pinned
Run button. **Seven icons** rendered as magenta placeholders in every scenario, and the icons
lint could not see them because its regex missed namespace-qualified `ui::getIcon` — fixed and
proved by reintroducing one bad name. **Mass Properties** mixed unit systems (IPS volume and
mass beside a hard-coded g/cm3 density) and printed `X: 4.449e-17in` for a symmetric body. The
**orientation triad** drew on top of the Simulation Manager during its dodge ease. The **RevEng**
busy mark cleared the tesseract's legibility floor by inflating the radius it passed, rendering
as a broken glyph box.

Two crashes, both latent, both mine to own in different senses. `nlohmann::json::value()` throws
on an existing-but-null key and evaluates its default eagerly, so `value("when", value("time",
""))` threw on plm_timeline's null fields and abandoned the checkpoint list mid-parse in three
separate panels — the copilot rollback popover had been silently truncating history. And the
T-BL61 fork isolation **hung the GUI**: fork gives the child a snapshot of every lock, and the
desktop app links libchudware beside its worker and RPC threads, so children wedged and each
blend burned its 30 s budget (`CHUDWARE_SHOT=fea`: 90 s scenario → hard hang). A timeout now
means "fork is unusable in this process", not "this geometry is bad": retry in-process, disable
isolation for the process, keep signal containment intact. Budget 30 s → 5 s. A new test runs an
isolated fillet against four allocator-hammering threads (7.4 ms, no drop) so the daemon's
containment is measured rather than assumed, and fails the build if it drifts toward the budget.

Verification: all 25 scenarios exit 0 with zero `unknown icon` lines at 1280x800;
`scripts/gui_lint.py` clean; `python3 scripts/check.py` **all 9 gates PASS** — it had been red
before this session on a committed `.kimi-code/skills` symlink that a Windows checkout turns
into a text file, now untracked.

All three items first listed as deferred were then fixed in the same session. The "New study"
picker opened on top of the panel it was launched from (ImGui parks a popup at the cursor) and
now flies out beside the manager column — which also proved the earlier "clipped prose" reading
of that panel was occlusion, not clipping. ECAD zoom-to-fit measured the FULL canvas region
while the pinned Copilot column floats over its right side, so the board's right Edge.Cuts edge
sat under the column; fit now measures the visible strip and offsets the pan to centre the board
in what you can actually see. And the agent DAG's "drag to pan" hint was painted onto the canvas
with no backing, overprinting whatever node card sat in the corner; it gets a washed plate.
Re-verified after all three: 25/25 scenarios exit 0 with zero icon misses, gui_lint clean,
check.py 9/9, test_ops 3702 assertions green.

## 2026-08-15 — Polyhedral-FEA second mesher backend

Polyhedral-FEA (`polymesh`, BSD-3) landed as a second FEA mesher behind the existing
`chudware::sim::MeshOut` contract, with shared sizing in `sim/mesh_size_band.h`, shared quality
accounting through `detail::fill_quality`, and a clean `algorithm3d` → `mesher_backend`
cutover. Touched: `core/cmake/ProvisionPolymesh.cmake`,
`core/sim/polymesh_backend.{h,cpp}`, `core/sim/mesh_size_band.h`,
`core/sim/mesher.{h,cpp}`, `core/sim/fea_run.cpp`, `core/sim/fea_fatigue.cpp`,
`core/api/libchudware.cpp`, `core/tests/test_polymesh_backend.cpp`, and
`core/CMakeLists.txt`. Gotchas for the next contributor: the dependency is pinned by SHA in
`ProvisionPolymesh.cmake` and is `add_subdirectory`-only with no install/export; its concrete
libraries are `polymesh_*` with `polymesh::` aliases; it MUST share the host Eigen target
because Eigen types cross its API and linking two Eigen versions is an ODR violation. The
tet10 mid-edge slot permutation between the two node orderings is a silent-wrong-answer trap
guarded only by the cross-backend solve test. Polymesh emits tet4/tet10 only; its hybrid/VEM
element families cannot fit `MeshOut`'s fixed-stride connectivity. Verification: core ctest
101/101; cantilever analytic tip deflection 0.2000 mm, Netgen 0.19270 mm, polymesh
0.19068 mm.

Then the adaptive solver, which is the reason the backend was worth having.
`sim/fea_adaptive.{h,cpp}` + the `sim_run_adaptive` RPC mesh, solve, score per-element
Zienkiewicz-Zhu error, refine and repeat to an error target, reusing `fea_run_static`
verbatim each pass by handing it an inline mesh — so BC resolution, bonded ties,
cancellation and the result payload cannot drift from an ordinary study. Surfaced as the
Simulation panel's adaptive controls plus convergence readout, and the `run_fea_adaptive`
MCP tool (registered in `advisor/triggers.{yaml,py}`, `sim_engineer.yaml`, the simulate
skill, `advisor/stages.py`, and BOTH `agent_turn.py` fabricated-success sets — miss those
and a model that ran only an adaptive study is told it ran none). Measured on a
120x60x8 plate with a 16 mm hole: eta 0.157 → 0.100 over three passes, 41.5k → 187.5k
elements, peak 170.0 MPa against a 56.8 MPa net-section nominal, i.e. SCF 2.99 vs the
classical Kirsch 3.

Two traps for whoever touches this next. First, `polymesh`'s graded-tet fill is a
three-level lattice (h, h/2, h/4): a size field alone only decides WHERE the fine cells
go, and on a part that is mostly boundary skin the fill is already saturated, so the
field changes nothing — measured, an identical 41,515 elements whether it asked for
4.87 mm or 1.96 mm. The background size is therefore the lever that guarantees progress;
the error field rides along as a bias. Second, and more embarrassing: this integration
first shipped the claim that polymesh "succeeds on geometry Netgen rejects" — in a GUI
tooltip, the sim role card, the simulate skill and an advisor finding — on no evidence.
Measuring five awkward solids found no case where polymesh meshed a part Netgen refused,
and one where the reverse held (two boxes fused along a single edge: Netgen meshes it,
polymesh's own watertightness guard refuses). Every one of those texts now describes
polymesh as an INDEPENDENT second discretization, which is what the measurement supports,
and `test_polymesh_backend.cpp` pins the edge-touch case so the claim cannot drift back.

**Public site (2026-08-16): tesseract brand hero + homepage restructure.** The
homepage hero is now the live brand mark: `src/scripts/tesseract.ts` renders the
tesseract in WebGL2 with the exact math of `host/src/ui/tesseract.cpp`
(DoubleRotation style, kSpin, kCamYaw/Pitch, fitStyle's d4/d3/depth/scale —
computed offline by the same sweeps and baked as constants, then verified over a
60k-sample trajectory sweep: pole margin 0.891, worst screen radius 0.9652 with
full camera wobble). 32 edges + 3x96 ring segments are instanced glow ribbons
(one-pass analytic bloom, no FBO), 16 vertex dots + the logo's 8-point spark are
billboard sprites, 240 dust points; two programs, three instanced draws, zero
dependencies, adaptive pixel budget like hero.ts. Two gotchas worth keeping:
(1) additive blending into a premultiplied canvas MUST write alpha =
accumulated intensity, not 1.0 — a constant alpha composites as opaque black
wherever the glow falls off (this rendered as black quads at every vertex);
(2) the part-builder panel needed its positioning UNLAYERED because
global.css's motion layer gives `[data-lift]` `position: relative` and later
layers beat earlier ones. The old SDF part hero moved down-page as a framed
showcase (`HeroCanvas.astro`, still the same renderer + `centered` option in
hero.ts that re-frames camera distance/uShift for a short box). index.astro:
hero copy slots into `TesseractHero.astro`; global.css: glass header, gradient
display text, gradient primary buttons, ambient body washes. Fallbacks kept:
prefers-reduced-motion parks the exact brand-pose frame (t=0), no-WebGL2 shows
the static mark; verified headless Chrome desktop/mobile/reduced-motion/
no-WebGL, zero console errors.

**Web (2026-08-19): the account system is the /admin door, and provider login
works in the browser.** Two owner-reported faults, one root cause each.
(1) `AuthMiddleware._authenticate` computed `cf_present` from the mere PRESENCE
of a `Cf-Access-Jwt-Assertion` header or `CF_Authorization` cookie and, when
present, skipped the `chudware_sid` branch entirely — so every browser that had
ever passed Cloudflare Access for the zone had its live session ignored on
EVERY request and was hard-401'd ("invalid access token") once that CF cookie
expired or stopped matching `cf_aud`, while holding a perfectly valid admin
session. `revalidate_websocket_user` had always preferred the session, so the
two disagreed. The session cookie is now tried first, unconditionally; Access
survives only as a fallback for a caller with no session. Proven by restoring
HEAD's `auth.py` under the new `tests/test_admin_account_door.py`: 4 of 6 cases
fail 401 there and pass now. The edge half is `cf_provision.py`, which no longer
provisions the `app.chudware.com/admin*` Access application or emits its AUD;
`verify_public.sh` now asserts the gateway's own `303 -> /signin?next=%2Fadmin`.
**The leftover Access application must be deleted by hand in Zero Trust** —
Access runs before the origin, so until then Cloudflare still answers /admin.
(2) Provider login in the WASM build had no working path at all. Every
credential surface funnelled into `drawOmpProviderManager`'s `agent_provider_*`
RPCs, which the gateway refuses for non-admins and which write into the ONE
shared daemon's bridge AuthStorage anyway; `-32003` is not `unknown_method`, so
`s_omp.available` stayed true and the legacy degrade section never drew. The
sound mechanism — the gateway's account-scoped `/api/providers/keys` (validate,
AES-256-GCM seal, resolve server-side for that account's turns) — had exactly
one client in the repo: the portal's own JS page. New `ui/web_provider_keys.{h,cpp}`
is an `emscripten_fetch` client for it (no-op off `__EMSCRIPTEN__`), Settings ->
Providers routes to `drawWebAccountProviderManager` on web, `providerUsable` /
`cloudCompatKeyPresent` / `moonshotKeyPresent` count an account key so a BYOK
user's models are selectable, and `drawValidatedCredentialHint` draws the form
instead of linking a locked door. Supported names are the six the gateway can
validate, and each IS the wire provider a turn carries — that identity is what
makes the stored key resolvable, so do not "tidy" it into a display name.
Subscription sign-in is now honestly closed on web: `subscription_login/logout/
switch/status/accounts/usage` are admin-only on the bridge
(`_OPERATOR_SUBSCRIPTION_METHODS`) because two of five providers use a fixed
server-loopback OAuth redirect a remote browser can never answer, and the status
RPCs leak the operator identity `providers.py` deliberately scrubs; the client
stops polling them under `webBuild()`. Verified in a real browser against a live
daemon: pasting a key into Settings -> Providers surfaced OpenAI's own 401
through the full WASM -> fetch -> gateway -> provider round trip.
Third fault, same session: the WASM client would not load on an iPhone in
Lockdown Mode (it worked on Android). Lockdown Mode disables WebAssembly in
Safari, and `host/wasm/shell.html` had NO capability preflight — the browser
downloaded ~12 MB and then either sat on "Loading Chudware…" or printed a raw
`Exception: …` from `window.onerror`, with `Module.onAbort`/`setStatus`
overwriting whatever was on screen. There is nothing to "fix" about Apple's
policy, but Lockdown Mode is excludable per site, so the honest failure IS the
deliverable. `shell.html` now preflights the three hard requirements —
WebAssembly, SharedArrayBuffer + cross-origin isolation (the build links
`-pthread -sPTHREAD_POOL_SIZE=8`), WebGL2 — names the missing one in a real
full-screen panel with selectable text, and on an Apple UA leads with the
per-site Lockdown exclusion steps. `setStatus` becomes a no-op once the panel is
up so the module's own churn cannot bury the diagnosis, and
`Module.instantiateWasm` is stubbed so the 11.6 MB `Chudware.wasm` is never
fetched at all. Verified headless with `WebAssembly` deleted before page scripts,
under both a desktop and an iPhone UA/viewport: panel renders, canvas hidden,
only `Chudware.js` and `Chudware.data` on the wire.

Suite note (pre-existing, fixed here because it hid the above): several gateway
fixtures did `config.settings = config.load_settings()`, replacing the singleton
that `auth`/`accounts`/`limits` had already bound `from config import settings`.
Whether a test saw the live object depended on pytest's file order, and the
symptoms were order-dependent rather than honest — `test_chat`'s WS broadcast
blocked FOREVER (`AuthMiddleware.__init__`'s no-strong-auth fail-fast firing
inside the message loop, so no frame was ever sent) and `test_auth_accounts_gate`
failed "no strong auth configured" only when `test_accounts` ran first. Both
reproduce on pristine HEAD from a two-file selection. `tests/conftest.py` now
offers `reload_settings`, which re-reads Settings from the environment and
monkeypatches it field-by-field ONTO the existing object; `test_accounts` and
`test_chat` use it and no longer pop `config` out of `sys.modules`. Do not
reintroduce a `config.settings = …` rebind in a fixture.

### 2026-08-19 — automatic release pipeline + dashboard-first /admin
Reworked /admin into a tabbed dashboard-first app (Dashboard/Releases/Accounts/
Terminal/System) and stood up the automatic release path end-to-end:
`.github/workflows/nightly-release.yml` (nightly-if-changed + dispatch) builds
Linux AppImage + Windows Inno on the self-hosted runners at a pinned
`CHUDWARE_VERSION_OVERRIDE`, runs ctest + a headless GUI smoke, signs with a
CI-held Ed25519 key (`release-ci-v1`; private seed in GitHub secrets, public
half in `core/security/release_keys.h`, the gateway keyring, and
`packaging/publish.py`), and publishes over the gateway LOOPBACK only
(`POST /api/releases/ci/publish`, bearer token in gateway.env, artifact
re-digested from disk before any trust). Beta rows carry a paired stable
signature; /admin's Releases tab promotes beta→stable in one click
(`POST /api/admin/releases/promote`, requires the full evidence bundle).
Updater got `requestCheckNow()` + a Settings "Check now" button, and
empty-channel 404 now reads "up to date" instead of HTTP noise. Gotchas:
`project(VERSION)` rejects prerelease — the override keeps MAJOR/MINOR/PATCH
numeric for cmake and puts the full string only in the version header;
`releases_version_canonical_check` (0011) forbade prerelease, so 0014 replaces
it allowing `-suffix` on beta only; `services/mcp/private_hostd.py` now sets
`CHUDWARE_MFG_STATE_DIR` and `start_new_session=True` for the spawned daemon
(a shared journal flock aborted it when any other hostd ran, and sharing the
test runner's process group delivered phantom SIGINT mid-suite).

### 2026-08-21 — first published release, and the defects it exposed
`0.1.0-beta.22` (linux/x86_64) is published, promoted to stable `0.1.0`, and
proven installable: `test_updater --live` drove the production check-and-stage
path against app.chudware.com and verified 340,724,216 bytes (size, SHA-256,
Ed25519, inner payload manifest) before the atomic stage. Getting there took
four fixes, each invisible until a real artifact existed:
(1) appimagetool REWRITES the AppDir's desktop entry while packing
(`X-AppImage-Version=$VERSION`, inserted after the group's last key), so the
shipped payload no longer matched the manifest signed before packing —
`stage_desktop_entry` now writes that key itself (a proven fixed point) and
finalize re-verifies the manifest AFTER packing;
(2) `local artifact="$1" request="$artifact…"` never works: the shell expands
every word before the builtin assigns any of them, so `$artifact` was unset and
`set -u` killed both finalizers at their last step;
(3) both finalizers delete the staged tree that held the only copy of
`payload-manifest.v1.{json,sig}`, so `finalize_release complete` aborted with
"found 0" and the published beta carried no evidence bundle (unpromotable) —
the pair is now retained beside the artifact;
(4) the updater could not consume its own manifest: the gateway omitted
`platform`/`arch` (which `manifestFromJson` requires), the artifact GET did not
send the entitlement the gateway's own artifact route demands (403), and the
inner-payload inspector execs the artifact while a streamed download lands 0644
("could not be safely inspected"). The credential now goes back only to the
origin that issued the manifest, and a credentialed download does not follow
redirects (libcurl and WinHTTP both replay custom headers across hops).
Also: each leg's failure reporter no longer depends on the helper the capability
step writes, so a leg that dies there stops reading as "queued" forever in
/admin.

### 2026-08-21 — all three platforms publish, and what stood in the way
`0.1.1-beta.32` published for linux, windows and macos x86_64 in one nightly and
was promoted to stable `0.1.1` for all three; the portal serves each. Every
blocker after the Linux one was platform-specific, and none of it was visible
before a real artifact existed:
(1) Windows never compiled: `core/util/platform_paths.cpp` defined
`WIN32_LEAN_AND_MEAN` while the CEF helper target already defines it on the
command line and compiles `/W4 /WX` (C4005 -> C2220);
(2) Nuitka refuses onefile mode on Windows without Dependency Walker and asks
interactively -- CI answers "no" -- so `build_sidecars.ps1` passes
`--assume-yes-for-downloads`;
(3) `os.stat()` and `os.fstat()` disagree on Windows for the SAME file:
st_dev/st_ino come from different APIs, st_ctime is a creation time NTFS tunnels
onto a replaced name (measured 14 minutes apart), and st_mode's executable bits
are a PATH heuristic os.fstat cannot apply. The payload TOCTOU guard compared
all of them and rejected every payload file; those three are now excluded from
PATH-vs-HANDLE comparisons on Windows only, handle-vs-handle stays strict, and
every mismatch message names the fields -- which is what turned each round trip
into a measurement instead of a guess;
(4) the Windows step signed a preimage name only the shell builders write;
(5) uploads died on **413**: the edge in front of the public origin caps bodies
at 100 MB, so the gateway now accepts `?offset=`/`&complete=1` chunks and both
off-LAN legs push 64 MiB at a time (proven against the live origin with a 220 MB
probe before it was wired in);
(6) macOS `installedPayloadRoot` looped forever: `for (q = p.parent_path();
!q.empty(); q = q.parent_path())` converges on "/" and "/" is never empty, so
ANY executable outside a .app bundle spun in place -- a shipped-app startup
hang, found because host test_security burned a whole ctest timeout;
(7) `chudware_read_version` used GNU-only `\+` in sed, so BSD sed read no
version and every macOS packaging step reported a missing header;
(8) onnxruntime-node 1.26.0 ships no darwin/x64 prebuild at all, so Intel macOS
leaves it `--external` (allow-unresolved matches module specifiers, not the
relative path that package computes);
(9) mesher_advanced's 90s wall ceiling is real on a 2017 iMac (114s) --
CHUDWARE_TEST_WALL_SCALE relaxes it on that runner only.
ctest now runs with `--timeout 900` everywhere: an unbounded stall used to burn
the job's whole 6-hour budget and, with cancel-in-progress, every nightly behind
it. macos/arm64 stays unproven only because `macbook-air-m2` is asleep.

### 2026-08-22 — organizations, teams, and customer file destinations
The web gateway now models organizations, organization memberships and invitations,
teams, and team memberships in PostgreSQL (`migrations/0015_organizations.sql`,
`orgs.py`), with owner-funded shared storage, opaque tenant authorization, and
single-use invite links. `bucket.py` keeps personal files private while adding
explicit team-scoped roots; the portal and bucket assets were rebuilt as
content-hashed, accessible destination surfaces with real organization/team
management and upload/rename/delete flows. Full gateway coverage is green (615
passed, 4 skipped), including cross-team isolation and UI asset contracts. Billing
remains deliberately account-owned: an organization derives shared-storage
entitlement and quota from its active owner rather than introducing a second
Stripe customer model.

### 2026-08-25 — T-DP14 design-pipeline history wiring

The four typed design-pipeline exits now call the existing PLM bridge after
their ledger exit event is durable, producing one deterministic Project History
narrative from each stage-local `feature`, `geometry_spec`, `linkage`, or
`build_node` slice. Multi-part boundaries carry an item-to-buildgraph-node map
through immediate and queued checkpoints; the vault writes the mapped node into
each item's AI metadata so per-item timelines retain the producing participant.
Touched `services/agent/{pipeline/phases.py,plm_bridge.py}`,
`services/plm/{client.py,vault.py}`, their focused tests, and the additive
design-pipeline/PLM contract notes. The bridge remains best-effort and managed
mode only; a vault failure queues metadata without blocking mission progress.

### 2026-08-26 — portable SolidWorks parts/assemblies into native FEA workflow

Modern SolidWorks 2015+ `.SLDPRT`/`.SLDASM` import no longer requires a Windows
SolidWorks installation: `core/doc/solidworks_tessellation.*` decodes saved-display
streams, reconstructs the exact triangle-strip topology, composes nested assembly
instance transforms, and shares distinct component meshes before `formats.cpp` sews
them into baked native B-rep import bodies. `session.cpp` still prefers COM→STEP for
exact analytic surfaces, persists flattened component paths plus explicit
`saved_display_tessellation` fidelity in `.step+`, and the MCP surface falls through
to this portable lane when feature-tree COM extraction is unavailable. The Python
format oracle mirrors the codec. Focused synthetic tests cover source-independent
STEP+ reopen, two-instance assembly placement, and Polyhedral-FEA meshing; the real
Crimson Comet corpus parsed 35/35 native files (4 assemblies), real `Tread.SLDPRT`
and `Wheel.SLDASM` imported through `libchudware`, and an imported
`Axle Rod.SLDPRT` completed a 1 kN PolyMesh static solve.

## 2026-08-26 — Box.com mirror integration (`aicad.box_mirror/1`)

Optional cloud MIRROR of saved project files on Box.com; live edits stay local.
New `services/plm/box_mirror.py` (stdlib-only Box REST client, durable per-project
spool under appdata `box_mirror/`, sha1 change detection, version-preserving
uploads, OAuth refresh-token rotation persisted under `config_lock`, loopback
OAuth connect at `http://127.0.0.1:53682/box`). Hooks: post-`Vault.save` commit
(`services/plm/vault.py`), verified `_project_save` + bounded terminal-sweep drain
(`services/mcp/cad_common.py` — the fresh-per-turn agent worker must drain
blocking; the spool survives kills). Tools `box_sync`/`box_status` registered in
the plm family (`services/mcp/plm_tools.py`, pack in `tool_categories.py`,
`box_sync` risk-overridden EXTERNAL in `tool_registry.py`). GUI: worker RPCs
`box_status/box_sync/box_notify_save/box_oauth_login` (`services/plm/worker_rpc.py`),
Settings → General → "Box.com mirror" section, `boxMirrorNotifySave()` beside every
`plmAutoRecordSave` call plus the async `doSaveDoc` success callback (host/src).
Contract: `contracts/box_mirror.md`. Config ownership gotcha: the GUI must
nested-overlay `box`/`box.auth` — Python owns the ROTATING `access_token`/
`refresh_token`/`access_token_expires_at` (Box refresh tokens are single-use; a
clobber revokes the grant). Tests: `services/agent/test_box_mirror.py` (fake
transport, 16 tests). Verified live against a real Box account end-to-end
(upload, version update, dot-dir exclusion, loopback OAuth connect).

### 2026-08-26 — agent harness integrity and cost-control cutover

The universal in-app agent lane now preserves an absent desktop billing actor as
JSON null instead of manufacturing `{}`, classifies reached-worker failures as
`worker_error`, hoists actionable terminal detail, and preserves partial/refine-cap
outcomes. `agent_turn` now applies HARD finish guards to synthetic handoffs,
requires fresh post-mutation verification evidence, fails closed on guard errors,
keeps sketch-role products intentional, distinguishes attempted from completed
delegate work, and enforces aggregate token grants. Vendored OMP is the sole
empty-completion retry owner; failed OMP responses are metered, failed delegates
accrue spend, provider tiers reach live schema rendering, and delegate schemas
render once. Dead `adopt_role`/`request_role_change` prompt, config, dispatcher,
and Settings surfaces were removed in favor of exact catalog role IDs through
`delegate`; unknown roles fail before allocating a child. Terminal document saves
now run once and report failures as partial delivery. Regression coverage spans
actor forwarding, worker classification, token ceilings, retry disposition,
guard ordering, persistence, canonical roles, and terminal honesty; generated
tool-panel tables were refreshed.

### 2026-08-26 — manufacturing drawing and model-delivery quality cutover

Press-project forensics traced unusable shop output to four connected seams: render
tools were reduced to image-path text before the model's next turn, feature-target
diameter picks used ambiguous concentric centres, vector drawing export auto-cropped
away the requested paper/title-block semantics, and `set_material` changed appearance
while native documents silently retained default 6061. `services/agent/agent_turn.py`
now feeds real result images back to vision providers and requires a current final
drawing review before export. Drawing MCP tools now pair pixels with exact layout and
material diagnostics, support free-text notes plus in-place annotation edit/delete,
and select the requested concentric ring. Core drawing layout reserves a title-block
band, compresses unused grid columns, renders/reviews the full sheet, and exports fixed
ISO paper with a border and framed title block. Named material assignment now stores
the engineering registry identity alongside appearance; role/playbook/tool guidance
requires mission routing for multi-part shop work, explicit material, domain-specific
verification, final reviewed drawings, native `.step+` plus neutral `.step` delivery.
Regression coverage pins image feedback, export review freshness, concentric diameters,
annotation repair, material consistency, occupied-grid layout, fixed A3 MediaBox, and
title-block vector cells. A representative stepped-punch workflow produced clean
9.8/20 mm diameter callouts, 95 mm OAL, zero review warnings, A3 PDF, native `.step+`,
and neutral STEP. Existing `/home/hunter/Desktop/press` artifacts remain historical and
must not be sent to a shop without regeneration through the corrected workflow.

## 2026-08-26 — agent harness: refusals are data, not failures

Triggered by a live card reading `tool 'research_web_search' exceeded
max_calls_per_turn=3 (agent.tool_options)` — a red "failed" tool card that named a
config key where a next action belonged. A four-slice read-only audit found the same
shape everywhere: **the harness refuses a call, then reports the refusal as if the
tool had crashed.** Clean-HEAD `services/agent` went 37 failures → 1 (the remaining
one is a deliberate open decision, below).

**One taxonomy, end to end.** `agent_turn._classify_tool_outcome` returns
`(outcome_kind, executed)` over `TOOL_OUTCOME_KINDS` = ok / policy_refusal /
guard_block / validation_error / timeout_unknown / internal_error / tool_error.
`agent_events.tool_call_event` carries both (omitted when unclassified, so older
consumers keep ok-only behaviour), and `host/src/ui/copilot/{feed,activity_timeline}.cpp`
render each class distinctly via the new `tool_outcome.h` — deleting the `"loop guard:"`
prose match that used to be the only way the GUI could tell a refusal from a crash.
`executed` answers exactly one question: did the tool BODY run, so could state have
changed? A timeout reports `executed=True` because the abandoned thread is still
running.

**Consequences the callers depend on.** A refusal no longer: spends the turn's
execution budget (`tool_calls_made` is charged only inside the `_blocked is None`
branch, with a separate bounded `_refusal_ceiling` so a wedged guard still cannot
spin); files a bug report; or halts queued directives — that halt is now gated on
`executed`, because a call that never reached the tool body invalidated no state, and
halting used to delete the exact remedy the refusal had just named. Allowlist denials
also now EMIT a tool event; they previously bypassed `tool_call_event` entirely, so
the user watched the model stall with no card at all.

**The per-tool cap specifically.** `config.effective_tool_call_cap` is now the single
resolver shared by enforcement (`_dispatch_tool`) and ADVERTISEMENT
(`tool_schemas.render_tools_block` renders `Per-turn limit: at most N call(s)`), so
the model can plan around a cap it can finally see. The counter is charged AFTER the
call and only when it reached the tool body, so malformed calls can no longer retire a
tool the model never used once; the call that spends the last unit carries a heads-up
note; and the refusal itself is a `call_cap_reached` payload carrying the tool's own
curated `prompt_note` as the remedy. `research_web_search` stays at 3 — a sweep of
every role card confirmed no role's prose mandates more than three searches.
Settings-disabled tools are dropped from the rendered schema entirely rather than
advertised and then refused.

**Two dispatch paths, one policy.** `copilot_mode_tools.apply_meta_tool_floor` is now
applied by `agent_turn`, `mission_dispatch` AND `node_runner` before schema rendering.
Path B never applied it, so every compiled DAG node ran confined to its 14 card tools:
`enable_tools` was callable but INVISIBLE. Same for budget —
`tool_budget.completion_cap_for_subagent` gives both lanes the same 8192 headroom (B
silently took `agent_turn`'s 2048 default when a node had no tier), and
`tool_budget.ROLE_MIN_CALLS` raises the soft convergence floor to each role's audited
mandated chain (ecad_engineer 35, model3d 24, feature_modeler/spec_writer 18 — all
were being told to "converge now" at 16, before their own card's chain could finish).
`role_models.clamp_provider_effort` is applied to the FINAL provider on every path out
of selection in both lanes; the session-default branch discarded the clamped config and
reused the ambient provider, leaking `max` effort past the `high` ceiling.

**Honest terminals.** A twice-truncated reply is `reply_truncated` (retryable in
`node_runner._RETRY_REASONS`), not `no_code` — it is a provider output cap, not model
inaction, and `no_code` both blamed the model and blocked retry. A done-gate that
cannot RUN returns `GUARD_INTERNAL_PREFIX` and routes to `guardrail_internal_error`
instead of handing the model our ImportError forever as `guardrail_blocked`. The WI-10
refine cap is enforced BEFORE the rework dispatches, so the revision the turn keeps is
genuinely the last settled one; it used to fire after the mutator had already dirtied
the geometry while reporting "the verified build is kept". `_PRODUCTIVE_TOOLS` now
unions `_DELIVERED_PRODUCT_TOOLS` (a cam_programmer that generated a real toolpath
counted as having written nothing), and a geometry-producer `_ARTIFACT_PRODUCT_GATES`
entry stops a `feature_modeler` being synthesised as `done` after only verifying.
Every artifact gate now also checks the lane can actually CALL the product tool it
demands — that check is what keeps these gates satisfiable.

**Recovered work.** `_drop_oldest_tool_results` REPLACES trimmed results with a
placeholder instead of deleting them: deletion left orphaned assistant messages, and
strict-alternation providers answered the overflow retry with a 400 that
`classify_provider_error` reported as `provider_auth` — sending users to check
credentials that were fine. A 404 "no endpoints" is now non-retryable (it burned every
DAG retry). `_attach_arg_notes` wraps a non-envelope result instead of returning it
unchanged and dropping the note. The delta coalescer is flushed in `run_turn`'s
`finally`, so the last thing a model said before a provider error still reaches the
transcript. `enable_tools` is transactional — schemas render and registry policy
applies against a CANDIDATE set before `allow` is widened, so a green card can no
longer be followed by "not allowed". Sub-agent termination is read and acted on in its
own guarded block: a registry write that threw after the flag was set used to skip the
return and keep dispatching tools while Stop looked accepted.

**Also found and fixed, same session.** `providers/token_usage.record_usage`
incremented `usage_event_count` only when a worker-RPC sink was bound, so on every
normal desktop run `agent_turn` classified each completion as unreported, wrote a zero
row, and the GUI's context/token gauges were SUPPRESSED ENTIRELY. `geometry_helpers`
hard-required `libchudware.so` and raised, so all process gating and provenance died
on an unprovisioned box — it now mirrors the six authoritative DfM profile rows
(verified 6/6 equal to live `dfm_get_profile`) and the bundled material registry.
`sim_engineer.yaml` carried 15 tools against the hard cap of 14, silently losing its
trailing entry. `test_cheatsheet_examples` leaked open engine documents into every
later test in the process. Loop-guard exemptions now cover every real status poller
(the third identical `sim_status`/`fw_status`/`scrape_queue_status` was refused) and
`_canonicalize` no longer strips `coordinate_claim`'s genuine `intent` argument.
`guardrails.checkpoint_verdict` is shared by both checkpoint paths so the capability-A
aggressive path can actually reach its retry escape. The manufacturing guidance family
is authored (15 entries) and the Tools-panel tables regenerated; a new gate pins
snapshot guidance CONTENT, not just tool names — seven entries had drifted into
materially false advice.

**Deferred, deliberately.** `Webservice/gateway/provider_accounts.py` genuinely
violates `test_prohibited_auth.py::test_no_consumer_oauth_token_reuse`: it exposes
`https://claude.ai/oauth/authorize` and posts grants to the consumer token endpoint for
OTHER tenants' accounts. That gate is red at HEAD and stays red — removing the Claude
Pro/Max subscription sign-in is a 212-line product and billing decision, not a harness
fix, so it is surfaced rather than taken. The failure message now names both source
lines and the two accepted remediations. Also unresolved and environmental: the
`services/mcp` ECAD/FEA/CFD/hostd suites need a live engine and daemon.

## 2026-08-26 — drawing overhaul: from nominal sheets to machinable packages

Trigger: the press package. Six one-page PDFs with bare nominal dimensions, a
`4X DIA 5` where `4X M6×1.0 ⌵14` belonged, every tolerance/finish/weld exiled
to a Markdown disclaimer, and a review gate that called it "manufacturing_
ready" because the LAYOUT was clean. The audit found three strata and all
three were repaired: the renderer lacked primitives (text-only angular dims,
leaderless notes, no tables/multi-sheet), the engine had capability no tool
exposed (smart tapped callouts, balloons, BOM rows, weld symbols, datum
targets), and the pipeline never demanded completeness (auto-dimension
preferred, coverage never required, release gated on pixels).

Engine: multi-sheet drawings with export-all (multi-page PDF, side-by-side
DXF); first-class BOM/revision/hole/notes/cut-list tables feeding raster and
vector alike; real angular arcs, note leaders, chamfer callouts, ordinate
sets, full AWS weld symbols, material-aware hatch; title block extended with
checked/approved, drawing standard, projection glyph, general tolerance,
mass; jogged multi-plane sections refuse rather than silently approximating.
Tools: hole callouts auto-populated from Hole features, tolerances at
dimension creation, composite GD&T, sheets/tables, and audit_drawing — a
spec-to-annotation coverage matrix plus a standards category (title
completeness, GD&T datum resolution, thread grammar, dangling annotations);
release_ready requires full critical coverage AND zero standards failures.
review_drawing no longer claims manufacturing readiness from layout.
Pipeline: drawing_detailer hands off a coverage matrix; spec_writer owns the
machined-part checklist; missions expand a manufacturing package into part
drawings + assembly drawing + BOM/cut list + work instruction + weldment
traveler. Formats: standalone editable .draw+ (aicad.draw+/1, embedded
source, honest missing-source state), SVG exports with real text and layer
groups, PDF companions=[dxf,svg], ODT/ODP/ODS + legacy PPT/XLS reading,
office_convert, structural DOCX edits. Deliverables: work_instruction and
weldment_traveler templates (provenance-backed, signoff blocks).

Proved live in-process: box + bore + M6 tap → drawing → tapped callout
"M6x1.0 ⌵ 14" → audit passes coverage, BLOCKS release on genuinely missing
projection/standard/general-tolerance title fields → PDF/DXF/SVG all carry
the thread text.

## 2026-08-26 — manufacturing planner, machining reference, standards floor

The agents now THINK about manufacturing before they model. `mfg_plan` is the
fifth typed ledger kind: process with resolved DfM profile snapshot, stock,
datum/setup strategy, ordered operations, inspection plan, and CRITICAL
DIMENSIONS derived from the part's function — every numeric row carries
required provenance and is audit-native, so `mfg_plan_get(view="audit_spec")`
feeds `audit_drawing` byte-for-byte and `view="work_instructions"` projects
straight into `make_work_instruction`. `mfg_plan_check` is three-state; an
empty evaluation can never read as pass (the same fix landed in dfm_check,
which also gained min-feature, hole-aspect, and thread-strategy checks). A
new manufacturing_planner role runs between SPECIFY and BUILD for machined/
fabricated deliverables; spec_writer, feature_modeler, drawing_detailer,
cam_programmer and fixer all read `mfg_plans` and follow the plan → model →
check → audit → instruction chain.

`machining_ref_search/get` is the Machinery's-Handbook-class reference: a
curated dataset where every row cites its governing standard (ISO 2768
general tolerances, IT-by-operation capability, ream/bore allowances,
finish-by-process, drill series, counterbores per B18.3, thread engagement,
keyways, press-fit and heat-treat/grind stock guidance) and queries about
facts that already have canonical calculators DELEGATE to them rather than
duplicating tables. `machining_ref_index_pdf/search_pdf` index the user's own
handbook PDF locally (per-page FTS5, page-cited snippets, nothing
redistributed) — drop a PDF in and cite it by page. Fixing the reference
layer also fixed a real defect: the fit catalog's own H7/m6 bearing-seat
recommendation was uncomputable because ISO 286 shaft `m` was missing; every
recommendation row is now pinned computable.

## 2026-08-26 — electrical schematic subsystem: connectivity truth, deliverables, editor

A four-scout audit of the ECAD schematic stack found the engine computing the
wrong answer and the surfaces around it unable to reach it. Connectivity keyed the
union-find on a bare `Point{x,y}`, so two hierarchical sheets whose geometry happened
to coincide were electrically shorted; a pin landing on a wire's *interior* floated
(only exact vertex coincidence united); local labels were emitted under their bare
text, merging same-named nets across sheets (the code warned they were
"shorted-by-name"); buses were parse-only; and multi-unit parts emitted one component
per unit, so U1A/U1B became two BOM lines and two footprints. ERC compounded it:
`passive` counted as a driver so `no_driver` almost never fired, ANY power port
counted as a power source so a plain GND symbol silently satisfied
`power_not_driven`, and `no_connect_connected` required a second pin so an NC flag on
a wired stub went unreported. All of that is fixed: the key is `(sheet, snapped
point)`, endpoints attach to segment interiors (explicit junctions still required for
true crossings), classes carry stable ids with `@sheet` disambiguation, buses/bus
entries carry real member connectivity, units group into one component, and ERC uses
KiCad drive levels with `PWR_FLAG` as the only power source. `netlist.cpp` now emits
`net_info`, `buses` and `by_object` (entity id → net) — the last is what finally lets
the canvas highlight a net without re-deriving connectivity.

The subsystem could also produce **nothing**: `ecad_export` rejected every kind that
was not `gerber|drill|bom|cpl` and each required a pcb doc, so a schematic had no
netlist, no BOM, no SPICE deck, no KiCad output and no printable sheet. New
`core/ecad/schexport.cpp` (netlist s-expr / BOM / SPICE) and `core/ecad/schplot.cpp`
(SVG + a self-contained vector PDF sharing ONE `Painter` so the two can never drift)
close that, and `ecad_export` now honors `path` and returns `paths` — it previously
ignored `path`, wrote nothing, and the GUI toasted "Exported" unconditionally.
`core/ecad/symlib.cpp` adds the native symbol library the C++ core never had (78
symbols); before it, `sch_add_symbol` fabricated a generic 2-pin body for every libid.
The op vocabulary went 42 → 62 names so every entity the schema can hold can also be
edited and deleted, `sch_import_kicad` stopped being a registered no-op, and
`ecad_design` — which the GUI called on every AI Design click and which **nothing
served** — is now a native compiler from `recipes.h` to a real, wired, ERC-checked
schematic.

Two defects only showed up under verification, and both are worth remembering.
`sch_connect` routed pin-to-pin straight through the source symbol's body; with the
old netlist that was invisible, but once pins attach to wire interiors it became a
genuine short (wiring U1.3 to C1.1 across a QFP48 also caught pin 34 at the same y).
The router now escapes each pin along its own direction and rejects any candidate path
that touches a pin it was not asked to connect. And the new on-canvas sheet frame was
anchored at the origin extending into −y while content lives in +y, so zoom-to-fit
unioned two disjoint regions and crushed the schematic into an unreadable clump — the
page is now anchored around the content bbox exactly as the plotter does it.

Deferred, deliberately: `sim_run`/`sim_cancel{run_id}` in `contracts/ecad/ecad_rpc.md`
still describe an isolated circuit-sim worker that does not exist (the shipped path is
synchronous `ecad_simulate` + mechanical `sim_cancel{job_id}`); that divergence is now
recorded in the contract rather than papered over. `ecad_to_3d` still ignores
`into_doc_id` and returns no `part_doc_id` — board→3D, out of scope for this pass.

## 2026-08-27 — hostd exits when the app does; one autosave authority
Symptom: a tray icon reading `Chudware hostd — draining (1 client)` that never went away.
Root cause: every exit path (idle-exit AND drain) waited on `status_.clients`, so ONE
connected-but-silent socket vetoed shutdown forever — measured live at 5.5 h, the "client"
being a parked tooling connection with no attachments and no traffic. `Server::idleExpired`
and the drain now gate on `Server::pinningClients(idleWindowSecs)`: a `gui` client always
pins, any other client pins only while it has an engine call in flight, an attached doc, or
an RPC inside the idle window (`ClientConn::lastRpcMs`, `DocRegistry::hasAttachments`).
`Server::beginDrain(deadlineSecs)` replaces the bare `status_.draining = true`: explicit
stops (`daemon_drain`, `--stop`, tray Quit, SIGTERM) are HARD and carry a deadline
(`CHUDWARE_HOSTD_DRAIN_GRACE_S`, default 20 s; tray Quit 5 s) after which stragglers are
`shutdown()` and the daemon exits; the stale-binary self-drain stays soft (deadline 0). The
doc-GC grace no longer pins idle-exit at all — with the 1800 s agent floor it kept hostd and
its tray icon alive for half an hour after the GUI closed, protecting a doc nothing could
open (the check is only reached when nothing is attached). Clean exit still discards; a
grace expiring while the daemon serves is still retired by `tick()`. Files:
`core/hostd/{server.h,server.cpp,doc_registry.h,doc_registry.cpp,main.cpp}`. Also removed
`Autosave::stop`'s dead `finalSave` parameter (its body already `(void)`-ed it).

CEF fetch daemon: 193 orphaned `chudware --cef-daemon` processes were alive 34 h after
every spawner died, sockets already unlinked. Its only self-termination was an idle check
inside the single thread that also drives `CefDoMessageLoopWork` — and `initialize()` can
block on Chromium's ProcessSingleton before that loop is ever reached, so a wedged instance
lived forever while each later turn spawned another. `cef_daemon.cpp` now arms
`start_idle_watchdog()` BEFORE `initialize()` (own thread, `_Exit(0)` after `idle_secs` of
no `mark_activity()`), and `services/browser/cef_client.py::_spawn_daemon_detached` is
single-flight (flock + re-probe under the lock) so a fresh per-turn worker adopts an
existing daemon instead of minting a rival.

Autosave de-duplication: `cad_common._autosave()` was writing an MCP-side snapshot on ~7
per-tool call sites with no daemon-mode guard, while its two aggregate callers each carried
their own copy of that guard. In daemon mode that write repoints the live doc's `path`
outside hostd's recovery tier and clears `dirty`, defeating `gcCloseDoc`'s
retire-if-unsaved gate — an abandoned agent doc could be closed without ever reaching the
orphan tier. The guard now sits at the single write; the duplicate in `_autosave_all_open`
is reduced to a cheap `doc_list` skip. Deleted `prune_autosave_debris` (an opt-in
maintenance sweep with no tool entry point — its own docstring said "never called by any
tool"), its `cad_tools` re-export and its test.

Tests: `services/mcp/test_hostd_registry.py` gains `test_parked_client_does_not_pin_the_daemon`,
its control `test_attached_client_still_pins_the_daemon`, and
`test_drain_exits_on_its_deadline_despite_a_stuck_client`. The two bug-defending ones were
A/B'd against a HEAD-pinned hostd built out-of-tree (`/tmp/headhostd`) and fail there.
Gotcha for the next pass: `test_inprocess_internal_agent_docs_are_registered_but_silent`
fails on HEAD too — it is the concurrent ECAD session's in-progress `core/api/libchudware.cpp`,
not this work.

## 2026-08-27 — ECAD: the gaps the schematic pass left open

Four scouts re-audited the ECAD surface after the schematic overhaul and found the
remaining defects were mostly *reachability*, not missing capability — working engine
code that nothing could call, and one predicate that swallowed calls it had no
implementation for.

The worst was in `services/worker/worker_client.cpp`. `routeEcadToCpp` claimed a call
for the C++ core whenever the method began with `ecad_` and the doc id began with
`ecadcpp`, with **no check that the core implements that method** — unlike the sibling
`kDocless[]` branch, which is properly allowlisted. Since `cppEcadFlag()` now
unconditionally returns true, every board is C++-backed, so every MCP-only `ecad_*`
tool carrying a doc id was captured and died on the core's `unknown method`
fallthrough. That envelope carries `error`, not `code`, so the caller's
`result.contains("code")` test missed it and the call hard-failed *before* reaching the
generic Python/MCP bridge. The GUI's "Send board to thermal FEA" button therefore
failed 100% of the time on every board on every platform, with the real implementation
sitting in `services/mcp/ecad_thermal.py` the whole time. 40 doc-bound MCP tools were
in that blast radius. The comment justifying the over-claim ("there is no Python ECAD
worker left to fall back to") confused the deleted Python ECAD *worker* with the very
much alive Python MCP *tool surface*. Both lists are now explicit and their disjoint
union is exactly the 28-method dispatch table.

`core/ecad/drc.cpp` had the same shape of bug the schematic ERC had: checks that pass
because their input never arrives. `track_shape()` returned false for any track whose
`kind` was not `"segment"`, so every arc track vanished from `copper_shapes()` — and
because `copper_shapes()` is also the autorouter's obstacle source and zone-fill's
keepout source, the router would happily lay a track across an arc of another net and a
pour would flood over it. DRC could not merely miss a short; the tools could *create*
one it then could not see. Arcs now tessellate into capsule chains with a sagitta
margin so the swept copper can never be under-approximated. Filled zone copper was
absent from the DRC model entirely (fill-time clearance goes stale the moment a
footprint moves), and net classes were honored for clearance but not for `track_width`
or via sizing, so a track thinner than its own class minimum passed clean. Nine of the
eleven new `test_drc_geometry` cases fail against the old engine; the two that pass are
the deliberate false-positive guards.

`services/mcp/ecad_sch_tools.py`'s `_pcb_status` swallowed a DRC compile exception and
reported `0 DRC error(s)` — the exact defect fixed for schematic ERC one commit
earlier, still live in the PCB twin, on the call agents are told is "THE situational-
awareness call".

The SPICE engine turned out to be real and substantial, and completely uncancellable:
`sim_cancel {job_id}` was a working registry whose only consumer was the LBM wind
tunnel, so cancelling a circuit sim was a guaranteed no-op returning
`cancelled:false`. The work is genuinely unbounded (transients reach ~4M iterations;
Monte-Carlo allows 100k full re-analyses) and the GUI called it synchronously from the
frame loop, so a heavy study froze the whole application. The solver now polls a token
at every unbounded loop head and the GUI runs it off-thread with a Cancel button. A
300-run Monte-Carlo study that would have taken ~9 minutes now stops **35 ms** after
the cancel, returning `ok:false, cancelled:true` with its partial run count.

Also closed: `ecad_to_3d` honors `into_doc_id` and returns `part_doc_id` (it mints the
part doc via the already-shipped `doc_import_file`, so a re-export updates the existing
3D tab instead of spawning another anonymous one, and a bad id degrades to a warning
without erasing a successful STEP); `led_driver` is ported to `core/ecad/recipes.cpp`,
so native `ecad_design` can finally build one and the orchestrator stops bypassing the
RPC; `ecad_route_diff_pair` and `ecad_load_symbol_library` — both working, both with
zero callers — are now exposed as MCP tools; the PCB canvas paints persistent DRC
markers like the schematic already did; and the fictional `sim_run`/`sim_cancel{run_id}`
declaration is deleted from the contract in favour of what ships. `all_recipes()` was
dead in both languages and is gone.

## 2026-08-27 — PLM team sharing and advisory-lock completion (T-R7)

Completed the existing contract-first remote stack in `services/plm/remote.py`, `vault.py`, worker RPC, and MCP tools: fixed plain-language transport failures with raw detail restricted to logs, best-effort live lock status, expiry, explicit one-confirm force override, reserved account identity, and forward-compatible snapshot-sync/advisory-lock capabilities. The Project Hub now has a real Team & Sync destination, folder/server share dialog, per-remote sync results, live lock rows, and an active-document lock chip/action path. Frozen `contracts/plm.md` §6.4 remains authoritative: divergent edits are automatically preserved as copy-as-new-item with no merge-choice UI; Box mirror remains separate. Verified with 388 PLM tests, isolated/UI compilation, scoped GUI lints, an integrated host build, and a real remote-profile Team headless capture. Concurrent ECAD/public-site/hostd/browser work was excluded.

## 2026-08-27 — self-hosted PLM live collaboration (T-R8)

Added the frozen `aicad.plm-live/1` authoritative relay after an OSS survey rejected CRDT/OT duplication of Chudware's semantic engine order. `services/plm/server.py` and `live_client.py` provide authenticated total-order batches, canonical snapshot catch-up, bounded presence, reconnect idempotency, local-hostd atomic application, and one participant-attributed final checkpoint; worker/MCP live methods and `plm-server` profiles select the mode without changing T-R7 dumb-remotes. `WorkerClient` transparently reroutes joined documents' ordinary `doc_apply_ops` to the live proposal seam, never falling back locally after refusal. Project Hub/quickbar render live-server join/leave, LIVE/reconnecting, and participant state while retaining advisory locks for folder/SSH/HTTPS profiles. Proof: actual clients observed sequences 1/2 and equal final hashes, one checkpoint named both committers, core replay passed `eval_compare`, 413 PLM tests passed, native and WASM builds passed, all 103 core CTests passed, the 9,627-test full gate returned PASS, scoped GUI lints were clean, and a live-server Team headless capture wrote 1340x838. Concurrent marketplace/ECAD/public-site/hostd/browser work was excluded.

## 2026-08-27 — agent harness: limits calibrated against what they bound

Triggered by a live card reading `tool 'research_web_search' exceeded
max_calls_per_turn=3 (agent.tool_options)` mid-research, with the model's own next
thought being "I could fetch more technical references". The 2026-08-26 session had
already fixed that message's PROSE; this session found the defect class underneath it:
**a ceiling calibrated without reference to what the wrapped mechanism actually takes.**
Four read-only audits (refusal surface, turn budgets, guardrail satisfiability,
timeouts/retries) plus four repair slices. `services/agent` + `services/dag`: 3809 pass,
2 fail, both pre-existing (below).

**The unit mismatch.** `max_calls_per_turn` is enforced against a counter created once per
`run_turn`, i.e. per whole USER REQUEST, not per model round-trip. `research_web_search`'s
3 was reasoned per-round-trip ("three searches is enough to triangulate"), so it meant
three web searches for an entire engineering task. Now 24, with the RUN scope stated at the
entry and the arithmetic given (~9% of `MAIN_CHAT_RUNAWAY_TOOL_CALL_BACKSTOP` = 256, an
order of magnitude above any observed legitimate chain, still bounding a rephrase spin at
24 paid searches instead of 250). Identical repeated queries are the loop guard's job; this
counter counts by tool NAME and cannot tell a new subtopic from a retry.

**Refusals: bounded, not free.** `_refusals_made` claimed in its own stop text to be
"consecutive … without a single accepted call" while being a lifetime total that was never
reset, so twelve single-shot guard corrections scattered across a 256-call run forced a
stop. It is now genuinely consecutive (reset by every accepted dispatch) and DISPATCH-time
refusals (per-turn cap, loop guard, Settings kill-switch) are charged to it — at HEAD only
PRE-dispatch blocks were, so an identical-call spin had no bound short of the runaway
ceiling. Measured by neutering the fix: **256 paid provider round-trips**, reported as
`main_chat_runaway_backstop`; with it, ~14 and an honest `refusal_ceiling`. The ceiling is
checked at the TOP of the loop so the refusal that trips it still emits its card — the stop
text promises "the last refusal text names what it demanded", and breaking at the increment
discarded exactly that. A REFUND of `tool_calls_made` was tried and reverted: that counter
is simultaneously the runaway bound and the reported `tool_calls`, so refunding made
refusals free AND unbounded (41 round-trips against a nominal 20-call ceiling, honest stop
replaced by `no_code`). A tighter bound is the fix; a free retry is not.

**Remedies must name tools the lane holds.** `_empty_product_handoff_block` OR-ed an
actor-NAME substring (`"ecad" in actor_l`) as an alternative to its tool-availability check,
so `ecad_engineer` — 14/14 tools, none of them board tools, no widening path — was told to
finish through `board_from_schematic` → `ecad_run_drc` and could only earn allowlist
denials. Both name-based branches now gate purely on the GRANT, with a schematic-only lane
that ends at `ecad_netlist`/`ecad_bom`/`ecad_plot`, the artifacts its own card mandates;
those three also joined `_DELIVERED_PRODUCT_TOOLS`, which had made a legitimate ECAD finish
read as zero delivered product.

**Budgets vs mandated chains.** `ROLE_MIN_CALLS` gained `reviewer: 24` (AC1-AC9 grading plus
its card's standard battery; it holds no `PRODUCER_TOOLS` and its real phase `deliver` misses
`BUILD_PHASES`, so all three producer signals missed the one role that decides PASS/FAIL —
and `reviewer_exec` treats `not_checked` as first-class, so early convergence yields
honest-looking NOT_CHECKED rows rather than a loud failure), `drawing_detailer: 24`,
`cam_programmer: 22`; `PRODUCER_MIN_CALLS` 16 → 20. `MAX_TOOLS_PER_ROLE` 14 → 16: it carried
no rationale at all and 13 of 32 cards sat exactly on it, so it had stopped bounding cards
and started designing them — `ecad_engineer` was ordered by UNIVERSAL RULE 3 to
`ledger_note` every artifact while holding no `ledger_note` seat. Verified zero cards were
losing tools to truncation before raising, so no existing grant changed.
`render_schematic_ascii` left that card: it is a GSL script function, never a registered
tool, and the loader reported it unresolvable.

**Two lanes, one policy.** `delegate` never read the `result['retryable']` flag the DAG lane
has always used, so a transient 429/5xx/truncation killed a delegated child while an
identical DAG node recovered for free; both lanes now share one predicate and a bounded
single retry. `agent.subagent_tool_call_budget` was applied only by `mission_dispatch`, so
the Settings slider silently governed half the dispatch surface; both lanes now call one
shared helper (currently `mission_dispatch.py:217-268`; `configured_subagent_tool_budget`
would sit more naturally in `tool_budget.py`, deferred).

**Layered clocks with no margin.** `ratelimit.BACKOFF_MAX_S` is 900 s and `wait()` slept it
synchronously, while the largest harness ceiling anywhere is 600 s — so a site punished
earlier in the same mission guaranteed abandonment (`research_datasheet_fetch`, 180 s, 5x
over). `wait()` now takes `max_wait_s`, and the three agent-reachable session paths
short-circuit to a `cooldown_active` teaching refusal with `retry_after_s`: **35.1 ms
instead of a 900 s sleep**. Budget 20 s, sitting strictly below `BACKOFF_BASE_S`=30 (so every
rung of the ladder short-circuits) and strictly above `DEFAULT_MIN_INTERVAL_S`=3 and every
crawl-delay (so ordinary pacing still sleeps), with a test pinning that inequality. The
reveng harvest returns partial results plus a skip count instead of losing the whole gallery,
capped at 24 items. FEA: outer 300 s == inner `run_ccx` 300 s, and the outer clock starts
first, so CalculiX's own "try a coarser mesh" was unreachable; outer is now 420 s with a
derived 300 s inner (`run_fea_adaptive` had NO `timeout_s` at all). `fetch_page`'s redirect
walk was `(10+1) x 30 = 330 s` against a 120 s ceiling; a whole-walk budget brings the worst
case to 90 s, so its ceiling stays 120.

**Test-order pollution, pre-existing.** `test_arg_reconciliation.py` drives the real MCP
ToolManager and minted live documents it never discarded, so `save_touched_docs()` at each
linear-turn terminal found the orphan and failed turns that had created no document, with
`persistence_failed` / `no_project_save_dir`. It cost three unrelated linear-turn tests and
was invisible whenever either file ran alone. An autouse fixture discards minted docs AND
restores `cad_common._TOUCHED_DOCS` — restoring the list is as load-bearing as closing the
doc, since `save_touched_docs` reads it directly. Same class as the `test_cheatsheet_examples`
leak.

**OMP fork.** Hunter's internal fork lives as UNCOMMITTED local patches in the gitignored
`scratch/omp-src` (upstream `d1239fd3` + 76 lines adding `isSqliteCorruptError` and
SQLite-corruption quarantine). Its `packages/ai` half is now vendored, but its only consumer
was `packages/coding-agent`, which CAD does not vendor — so the improvement was inert here.
CAD's own equivalent now lives in `vendor/omp/bridge/src/auth-context.ts` (OUR file, survives
re-vendor): a corrupt `omp-bridge-auth.db` is quarantined with its `-wal`/`-shm` companions
and recreated once, warning to stderr only. Verified end to end — before, every auth RPC
failed for the process lifetime with "file is not a database". `OMP_VENDOR.lock` now records
`dirty` + `dirty_files` so it stops claiming the vendored bytes are reproducible from an
upstream sha they are not, and `--verify` no longer reports its own gitignored install
artifacts as drift (it was permanently red, which trains people to ignore it).

**Deferred / known-red.** (1) `test_mega11_legs::test_ecad_engineer_role_has_domain_tools_and_delegate`
still fails, as it did at HEAD: it demands `ecad_add_power`/`board_from_schematic`/
`ecad_run_erc`/`ecad_run_drc`/`ecad_simulate`/`ecad_antenna_design`, which together with the
schematic build loop need ~22 tools. `board_from_schematic` is reachable only through
`spec_writer`'s `family: ecad`, and ERC/DRC/power/sim/antenna are held by NO role at all —
the PCB half of the ECAD pipeline is unreachable. That needs a deliberate role split
(schematic vs PCB), not more seats. (2) `test_prohibited_auth::test_no_consumer_oauth_token_reuse`
remains a deliberate open product decision. (3) The shared budget helper's home, above.

## 2026-08-27 — OMP fork: port what applied, and close the cowork-mode document gap

Audited Hunter's oh-my-pi fork (`Hunter-124/oh-my-pi`, 10 branches, checkout at
`~/Repositories/omp-inspection/oh-my-pi`) against CAD and ported what actually applies.
Its `REQUIREMENTS.md` is the binding mission statement and names CAD as a consumer;
its clauses (transparency over cleverness, failure local not machine-wide, fewer
better defaults without removing capability) drove every fix below.

**Vendored (commit `34354df3`).** `cad/oauth-fix-on-vendored-base` is cherry-picked onto
CAD's exact pin, so adopting it was a no-version-jump change. CAD genuinely serves that
page (`callback-server.ts:14` imports `oauth.html` as text; Anthropic/Codex/Google/Devin/
GitLab-Duo/Z.ai all terminate there), and its `window.close()` is a no-op for a tab the
script did not open, so the button was dead AND the promised auto-close never fired.
Also fixed `vendor_omp.py` silently deleting `vendor/omp/bun.lock` on every re-vendor —
the C++ core installs `node_modules` from that committed lock with `--frozen-lockfile`,
so re-vendoring broke provider auth at the next clean build. Now preserved like `bridge/`;
`scripts/test_vendor_omp.py` guards it (neuter-verified).
**Deliberately NOT ported:** `feat/catalog-cursor-effort-and-release-date`. Its data lives
in a 2 MB generated `models.json` and CAD's pinned copy has ZERO date fields, so porting
the predicate ships dead code. Regenerating that blob is version-jump-class work.

**Cost honesty.** 1646 of 3886 catalog models carry no price (`cursor`, `devin`,
`google-gemini-cli`, `xai-oauth`, `ollama-cloud` have zero priced models; nanogpt 539).
All were rendered as measured $0 and folded into a total whose tooltip asserted
"OMP-observed spend". `models.ts` now emits `priced` per model plus
`pricedModels`/`totalModels`/`unpricedModels[]` on `cost_summary`; both C++ aggregation
sites (`settings.cpp drawSessionCostStrip`, `ai/metrics_view.cpp` — fixing one leaves the
other lying) now show a floor with an "N of M priced" provenance, and unpriced rows read
"no price data" rather than "free". Genuinely-local runtimes keep "free" (hosted
`ollama-cloud` deliberately excluded).

**Auth truth + failure locality.** `computeAuthState`'s `state:"error"` branch was
structurally dead: it filtered for `disabledCause !== null` over rows from
`listAuthCredentials`, whose SQL is hard-coded `WHERE disabled_cause IS NULL`. A revoked
credential therefore reported `signed_out`, so users re-signed-in blindly forever, and
`listDisabledCredentials` had zero call sites. Now reachable with cause + a removable
`disabled[]`, with user-initiated causes excluded (Hunter's real db holds 4 rows all
caused "deleted by user" — without that filter his panel would have shown false errors).
Separately, a non-corrupt auth-db open failure (CANTOPEN/IOERR) cached its rejected
promise at module scope and killed every provider RPC for the bridge's whole life; the
memo is now cleared with a 2 s cooldown (strace: 12 calls → 2 `openat`).

**Settings reachability.** The canonical Generation block (reasoning effort, max tokens,
context window, with clamping) was doubly unreachable — the sole caller passed
`ProviderModel`, and that call sat inside `if (!s_omp.available)`. Reasoning effort had no
Settings control at all while the search index offered an entry that went nowhere. Now a
Generation card on Providers, search entries retargeted, dead `drawProviderModelPicker`
and the `All` enum value removed, and the degrade-path call now persists its edits (it
discarded the save-me return).

**Cowork mode** (external MCP client driving CAD). `save_touched_docs()` — the only thing
that turns an AI-created part into a real file — was wired ONLY into the two internal
turn boundaries, so an external client's work was never durably saved, and the orphan
watchdog's `os._exit(0)` skipped even atexit. `server.py` now has a bounded terminal sweep
on EOF/signal/atexit and the watchdog saves before its hard exit. `task_list` is now on the
shared registry; the design ledger moved into `lite` (the profile the installer recommends
for exactly the long external sessions that need it). `_doc()` no longer adopts a sole
engine-global document minted by another AI client (`doc_list` exposes only `origin_kind`,
no client id — that is the finest distinction the wire allows today), with legacy mode
gated unconditionally so single-client callers never regress.

**Left deliberately.** `delegate` stays OUT of the shared registry. It reads as a parity
gap, but two tests pin it as a mission-scoped pseudo-tool so `_unresolved_tool_error` can
name mission scope precisely — both written after a live incident where a fabricated cause
sent an engineer to the wrong subsystem. Real external delegation needs an external-facing
mission entry point binding a run_id + ledger + tree, which is design work, not a
registration line. Also open: composer folder-attach, `agent.model_assignment` has no GUI
consumer so Auto-mode roles never show their resolved model, and no basic/advanced
settings split yet.

**Also found en route:** a small PDF passed the composer's binary sniff, so its raw bytes
were inlined and invalid UTF-8 crashed `json::dump()` mid-send (`type_error.316`) — attached
documents now route through the existing `office_extract` RPC instead; and
`setup_mcp_clients.py --detect/--wizard` died with `NameError` on every run (two regex
globals that exist nowhere), i.e. the script the README tells users to run to register CAD
with Claude Code was broken.

Baseline unchanged: `services/agent`+`services/dag` 3809 pass / 2 pre-existing fail;
`services/mcp` 2891 pass / 1 pre-existing fail (proven at HEAD). NOTE for future runs:
this host's `TMPDIR` is `~/.cache/tmp`, which makes `AF_UNIX` paths too long and fails two
hostd tests that pass under `TMPDIR=/tmp`. But `/tmp` here is a 2 GB tmpfs shared with
other sessions, and a full `services/mcp` run needs roughly a gigabyte of scratch. When it
runs short it fails in bulk with `OSError: [Errno 28] No space left on device` — 65 such
failures in one 17-minute run, every one of which then passed 169/169 on re-run with space
free. Errno 28 in this suite is an environment limit, never a code regression: check
`df -h /tmp` before believing a red run.

Also: never select a subset by passing test *files* as pytest arguments. That puts
`services/` on `sys.path`, so CAD's own `services/mcp` package shadows the installed MCP
SDK (`cannot import name 'Image' from 'mcp.server.fastmcp'`) and every reveng module fails
to import — 132 phantom failures. Pass the `services/mcp` directory and exclude the
complement with `--ignore`; `~/.cache/mcp_chunk.sh` and `~/.cache/mcp_keep.sh` do this.
Subsets can still hang in `daemon_bridge`/`core_bridge` on cross-file daemon fixtures, so a
subset that hangs is likewise not evidence about HEAD. The full suite exceeds the 600 s
bash ceiling; run it under `hub op:"start"` instead.

---

## 2026-08-27 — Settings progressive disclosure: 155 AI controls down to 9 by default

**Why.** Owner report: "the current setup for all ai settings is like over 1 hour if you
read everything." The OMP fork's own mission statement names this exact defect — "the
surface has grown far past useful. Fewer, better defaults; everything still reachable via
config file or an explicit 'advanced' mode. **Removing a setting must never remove a
capability.**" A grep for `showAdvanced` / `advanced mode` across the modal returned **zero
hits**: there was no progressive disclosure of any kind, so a tool-call-budget slider
carried the same visual weight as "should the assistant plan before building".

**Measured first, then cut.** A full read-only inventory of the AI tab classified **155
interactive controls: 10 EVERYDAY / 145 ADVANCED / 0 auto-derivable**, per section —
Harness 9/27, Roles 0/59, Prompts 0/8, Skills 0/6, Rules 0/16, Benchmarks 0/5, Tools 1/22,
Dev Preview 0/2. Roles being entirely advanced is the load-bearing finding: per-role model
pinning is a power surface, and the everyday "which model do I use" choice already lives on
the Providers tab. So Basic showing Harness alone is what the data supports.

**Mechanism.** New `Settings::showAdvanced` (top-level `show_advanced` in config.json,
omitted while false), surfaced as a right-aligned `Advanced: On/Off` item in the tab strip
itself (`ImGuiTabItemFlags_Trailing`) so it reads as a property of the modal, not of a page.
`kAiSubAdvanced[8]` marks which sub-sections are power surfaces; the rail filters through a
`visIdx` map so the eight section bodies keep switching on their STABLE index and needed no
edits. With one visible section the rail is suppressed entirely and the pane gets the width.
Within Harness, the all-advanced preference combos, orchestration flags, Chat-display block
and budget sliders are gated (the last via early return, since that table ends the
function); Memory & Learning early-branches to its two non-duplicate everyday controls.

**Three guarantees that hiding never removes a capability.** (1) The switch. (2) config.json
stays authoritative and is read whether or not a control is painted. (3) Settings search
still indexes all ~130 entries, and a jump that lands in a hidden section **turns disclosure
on and persists it** — which also keeps the composer's `/rule` route (Rules is advanced)
working. Plus: Tools' single everyday control ("Repair / install runtime") is re-homed onto
Harness in Basic via an extracted `drawRuntimeMaintenanceSection`, rendered in exactly one
place at a time, so a broken runtime is always fixable without discovering the switch.

**Duplicates removed** (no capability cost, both were rendered on ONE screen): the second
`settings.memoryEnabled` checkbox and the second `settings.memoryTokenBudget` slider, which
Memory & Learning repeated from the session grid directly above it. Separately,
`ai_workspace.cpp` had a hand-rolled max-tokens/context-window pair lacking the canonical
widget's clamping, tooltips and "0 = automatic" hint; it now calls
`drawAiSettingsSection(...Generation)` and threads real `busy` state instead of `false`.

**Verified by A/B pixel capture** at 1340x838, tab 2. Basic (config key genuinely absent, so
this is the shipped default): 9 controls, one screen, no scroll, no rail. Advanced: full
8-item rail, all 4 preference combos, every orchestration flag, the complete Chat-display
block and all 3 budget sliders return. `./build.sh` green; `gui_lint` theme/color/glyph/dupe
clean (the 10 `scale` findings are pre-existing in `ecad/ecad_canvas.cpp`, untouched here);
`check.py` all 9 gates pass; vendor lock verifies. Because the session grid is shared, the
AI workspace Options view simplifies from the same flag.

---

## 2026-08-27 — CAD now pins the clean private OMP backbone

**Problem.** CAD still vendored six packages from public `can1357/oh-my-pi` at detached
`d1239fd3`, then carried five uncommitted patches in a gitignored clone. Meanwhile the actual
product platform, private `Hunter-124/omp`, had moved to 18.0.8 and already contained the
universal fixes: OAuth's manual-close degrade and a better shared SQLite corruption classifier
in `pi-utils` (`SQLITE_CORRUPT*` + `SQLITE_NOTADB`), re-exported by `pi-ai`. Three remaining
patches touched `coding-agent`, which CAD never vendors. Keeping the public pin therefore
duplicated fixes, hid 614 models and 8 providers, and made the private backbone irrelevant to
the product it was built for.

**Cutover.** `vendor/omp/OMP_VENDOR.lock` now pins clean private commit `54f0e24f09b5`
from `https://github.com/Hunter-124/omp.git`: 805 content-hashed runtime files, six packages,
zero dirty files, zero local patches. `scripts/vendor_omp.py` defaults to the private URL so a
future refresh cannot silently regress CAD onto public upstream. The private repo is the
universal platform; CAD's preserved `bridge/` is explicitly a thin domain/native adapter.

**Compatibility seam.** Private OMP renamed CAD's local `isSqliteCorruptError` to the shared
`isSqliteCorruptionError`; the adapter follows the platform name. Bridge deps moved from stale
`pi-utils`/`pi-natives` 17.1.5 to workspace `pi-utils` and ABI-matching `pi-natives` 18.0.8,
with a regenerated committed `bun.lock`.

**Upgrade-only defect found by a clean install.** Bun does not reliably delete an obsolete
nested workspace package. An upgraded checkout retained
`bridge/node_modules/@oh-my-pi/pi-utils@17.1.5`, which shadowed workspace 18.0.8 and failed
with a nonexistent Handlebars dependency, while a clean checkout worked. The C++ provisioner
now removes both generated `node_modules` trees under its cross-process install lock whenever
the lock fingerprint changes, then performs the frozen install. End-to-end proof seeded the
exact stale nested 17.1.5 package and a stale stamp; the real C++ provider path deleted it,
ran frozen install, started private OMP, and rendered Providers.

**Measured result.** Full read-only surface smoke: 12/12 methods succeed; 4,500 models
(was 3,886), zero missing `priced`; 78 providers (was 70), zero missing `disabled`. Private
OMP also cuts cold bridge spawn→first ping from ~305 ms to **157 ms** and module import from
~279 ms to **154 ms**. Warm `models_list` is 5.4 ms for the larger 1.63 MB/4,500-row payload.
Providers pixel-captured at 1340×838 through rebuilt `libchudware.so`.

**Platform boundary.** Universal harness behavior moves into private OMP incrementally;
CAD retains geometry/document/daemon tools, product roles/prompts/policy, and the native C++
fast path where measured latency warrants it. A concurrent OMP session owns the standalone
platform UI, so CAD exposes and consumes stable contracts rather than duplicating that work.

---

## 2026-08-27 — First harness extraction: shake now lives once in private OMP

**Duplicate removed.** `services/agent/compaction.py` carried a 319-line Python port of
OMP's shake algorithm: recent-token protection, tool-call pairing, skill-read protection,
fenced/XML scanning, min-savings gating, mutation ordering, and placeholder accounting.
Private `pi-agent-core` already owned the canonical selector/mutator and tests. Keeping both
guaranteed drift — the two had already diverged on aggressive-tail and artifact-read policy.

**Platform contribution.** Private OMP commit `3a81ac1f7e` adds
`shakeWireMessages`: a product-neutral JSON/OpenAI-style adapter inside `pi-agent-core`.
It deep-copies input, preserves unknown fields/content blocks, pairs top-level OpenAI tool
calls, calls the existing `collectShakeRegions` and `applyShakeRegions`, and returns
JSON-safe original regions. Ten focused private-platform tests and package typecheck pass.
No standalone OMP UI file was touched.

**CAD cutover.** CAD now vendors `pi-agent-core` as its seventh private OMP package.
The bridge's `shake` RPC performs runtime JSON validation and maps the platform result to
CAD's snake-case wire shape. Python retains only two product adapters: RPC lifecycle and
image-block stripping (OMP shake deliberately selects textual context regions only).

**Honesty improvement.** The old port emitted `artifact://shake` even though no artifact
store had written that ID. The universal contract now emits `[shaken ~N tokens]` and returns
the original region to a caller that genuinely wants to persist it. A recovery link is never
invented.

**Proof.** Real bridge RPC aggressively shook a 9,750-token tool result to one placeholder,
saved ~9,734 tokens, and emitted no fake artifact URL. Existing CAD compaction/session-command
tests pass unchanged apart from reversing the obsolete fake-link assertion: 25/25.

**Bridge YAGNI sweep.** `warm_cache`, `cache_policy`, and the second `compact` engine had
zero callers across services/core/host/scripts. Deleted both handler modules and the inline
1-token warmer. The real `snap_render`, `shake`, memory, provider/auth/catalog, STT, and logo
surfaces remain. A cache scheduler or second compaction contract returns only when a consumer
and observable behavior ship with it.

---

## 2026-08-27 — OpenRouter transport is private-OMP-only

`config.get_provider("openrouter")` returned `OmpBridgeProvider` from the shared bridge-ID
branch before reaching CAD's direct `OpenRouterProvider` branch, making the latter and its
module production-unreachable. The only remaining direct consumer was a conformance test.

Deleted the Python adapter and unreachable resolver branch. Conformance now pins the real
contract — provider/model/key fields on `OmpBridgeProvider` — and tier prose names the private
OMP bridge. Private `pi-ai` is the single owner of OpenRouter wire format, service tier,
reasoning effort, retries, usage, and catalog semantics. 110 provider/tier tests pass.

---

## 2026-08-27 — Provider routing is generated from private OMP

CAD copied the same 54 OMP bridge provider IDs into Python and C++, with a comment telling
maintainers to re-copy the list after every catalog update. Private OMP 18.0.8 had already
grown beyond it, silently leaving seven supported providers unreachable.

`scripts/vendor_omp.py` now owns the only authored routing policy: six exact OMP IDs served by
native CAD transports (`openai`, `google`, `xai`, `xai-oauth`, `moonshot`, `cursor`) and the
auth-only bridge alias `anthropic-sub`. It derives the remaining sorted set from private
`models.json`, generating a Python frozenset and C++ initializer include. `verify()` fails if
either output is missing or changed.

Result: 61 bridge routes, adding `aiand`, `alibaba-token-plan`, `bedrock-mantle`, `deepinfra`,
`gmi-cloud`, `meta`, and `yolo-auto`. Resolution remains static — no runtime catalog file read
and no Bun spawn. Vendor tests pin generation, native exclusion, deterministic order, missing/
changed drift, and malformed catalog failure.

---

## 2026-08-27 — Release uploads are writable inside the hardened gateway

Production journal showed `/api/releases/ci/upload/*` failing with `EROFS`: Unix ownership was
correct, but `ProtectSystem=strict` + `ProtectHome=read-only` omitted
`~/chudware-releases` from `ReadWritePaths`. A multi-hour nightly could build successfully and
still never stage or publish its installer.

The tracked production unit now grants only that explicit release root alongside the existing
data/bucket roots. A deployment-unit regression test pins it. 158 deploy/release tests pass on
the production-matching gateway venv.

## 2026-08-27 — A vendor's refusal is not an outage, and a decline is not a crash

`~/Desktop/pharm-press-2` was handed over as a real test project: a machine-shop pellet
press plus a jack-driven bulk powder press, with toleranced shop prints. The folder held a
PLM vault, an AI-VC chain and **zero deliverables**. Two runs explained why.
`run-153290332111741` died `status=failed stop_reason=provider_error
error_detail="anthropic: BridgeUnavailable: omp-bridge anthropic: Refusal (bio)"` — the
sidecar, the credential and the endpoint were all healthy, three other credentialed
providers sat idle in the same config, and the user was told the *bridge* was unavailable.
`run-153641706610580` was cancelled by hand.

A content refusal is now its own fact end to end. `vendor/omp/bridge/src/bridge.ts` (ours,
preserved by `vendor_omp.py`) forwards the vendor's `stopDetails` on a completion and
`ProviderResponseError.kind` on an RPC error frame — the latter matters because Google and
the OpenAI Responses path report a safety block by THROWING, so without it those two
vendors were indistinguishable from a dead bridge. `omp_bridge.ProviderRefused` subclasses
`BridgeUnavailable` (existing `project_memory`/`compaction` fall-backs keep working) and
carries `provider` + `category`; `omp_bridge_provider` prefers the structured field and
keeps a deliberately narrow prose fallback for `openai-completions`' `content_filter`
(narrow because `"connection refused"` must never read as a policy decision — pinned both
ways). A refusal is never salvaged from a partial stream. `classify_provider_error` returns
the new non-retryable `provider_refused` BEFORE the auth bucket, because Gemini answers a
safety block with HTTP 400 and that used to send the user to re-check working credentials.
`run_turn` then re-issues the identical prompt to the next enabled vendor FAMILY
(`role_models.refusal_failover_candidates`, capability-ordered, one shot per family,
`anthropic`/`anthropic-sub` collapsed since they share a classifier) and rebinds `provider`
so the rest of the turn runs there. The refusing vendor is NOT marked process-unavailable —
it is healthy for every other request. **Measured on the real stack:** injecting the live
refusal envelope into the real `anthropic` provider moved the turn onto Moonshot (8 real
HTTP 200s, 67k prompt tokens billed there, `new_part` → `add_features` ×2 →
`measure_part`); before, the identical turn produced zero tool calls and died. C++ mirror:
`providers.h::kProviderRefusedMarker` / `is_provider_refusal`, stamped from the bridge
branch using the forwarded `stopDetails`, humanized ahead of the transport branch (which
matches the bare substring `refused`), and `machine.cpp` fails a refused stage on the FIRST
attempt like the auth case instead of paying to re-ask. Its five hand-spelled
`provider_error||provider_auth` verdict chains are now one `is_terminal_provider_verdict`.

Re-running the verbatim project prompt exposed a second, separate defect. Anthropic
accepted it, researched, then the MODEL declined through the harness's own handoff —
`status:"blocked"`, a paragraph of reasoning, three concrete alternatives. `run.cpp` mapped
only `partial` and `guardrail_blocked`, so that landed on `status=failed` with
`error_detail: null`; `jobs.cpp` captured the explanation into `job->agentSummary` and
`app.cpp` rendered it only on the converged path. The user got *"I stopped before fully
finishing (blocked). Whatever was built so far is in the FeatureManager tree"* — wrong
label, false claim, and not one word of the reason. The mapping is now
`run_stop.cpp::general_lane_status` / `general_lane_error_detail` behind `run.h`
declarations (same testability pattern as `classify_worker_stop`), a blocked/escalated
handoff reads `blocked`, the summary backfills `error_detail` clipped to the contract's
~300 chars on a UTF-8 boundary, and `app.cpp` prints the general lane's own account when it
stops. `partial` still outranks a blocked handoff, and a genuine fault still reads `failed`.

Deliberately NOT done: nothing here routes around a model's considered decision. The
failover recovers from a *provider classifier* refusal in a multi-vendor config — an
infrastructure-shaped failure that was reported dishonestly. A model that declines through
the handoff envelope is reported accurately and the turn ends.

Also: `build.sh`'s engine-staleness gate now prunes `core/tests` alongside `core/hostd`.
Test sources compile into their own doctest executables and nothing outside `core/tests`
includes a test header, so adding one `TEST_CASE` made `./build.sh core` print *"ERROR: a
consumed engine is STILL stale after building"* and refuse a completely correct build — the
"gate that cries wolf" the surrounding comment already warns about. The real staleness check
still fires (it caught `core/agent/run.h` mid-session).

Chudcode side (`Hunter-124/chudcode` @ `537a653415`, `docs/fork/CHUDWARE-CONSUMER-FINDINGS.md`):
four findings filed there rather than patched here, because `vendor_omp.py` clobbers
`vendor/omp/packages/` on every re-vendor. The headline is that a refusal has FOUR
incompatible shapes in `packages/ai` — `Flag.ContentBlocked` and
`ProviderResponseErrorKind "content-blocked"` already exist but Anthropic puts it on the
message, Google and OpenAI Responses throw it, `openai-completions` returns English prose,
and OpenAI `refusal` content parts are folded into ordinary text by `finalizeMessageText`
with `stopReason` left at `"stop"`, which is **undetectable downstream and has no
workaround here**. Also filed: `stopDetails` is an undocumented consumer contract,
`error/flags.ts` has zero refusal handling, and `gui/src/app/app.cpp:627-634` sets
`io.IniFilename` from a `std::string` declared AFTER `ImGuiLifetime`, so every clean exit
saves the ini through a freed pointer — eleven junk-named 96-byte files in that repo root,
one per exit, and the window layout never persists.

Tests: `services/agent/test_provider_refusal.py` (18, covering both the message and throw
refusal paths, the transport-fault converse, family exclusion, real failover, and
exhaustion) and two `core/tests/test_agent.cpp` cases. `test_agent` 103/103, 1075
assertions. `services/agent` + `services/dag`: 3828 passed, 2 pre-existing failures
(`test_mega11_legs::test_ecad_engineer_role_has_domain_tools_and_delegate` reproduces at
pristine HEAD in an out-of-tree archive; `test_prohibited_auth::test_no_consumer_oauth_token_reuse`
names `Webservice/gateway/provider_accounts.py:127`, landed 2026-08-19 in `c7b27bd7b`, and
neither file was touched here).

## 2026-08-27 — a clean workspace, and the ECAD lane that no role could walk

Hunter asked for the repo and its agent memory to be cleaned before further work. The
preceding entry's provider-refusal change was sitting uncommitted in the tree; it built,
its 18 Python tests and both `test_agent` cases passed, so it landed first (`7b7ed75b8`)
rather than being swept up in a cleanup diff.

**Deleted, each reference-checked first.** `tools/flywheel/bench/gym/reports/runs/` (391
raw per-seed `ledger/mission/graded` files): `leaderboard.discover_cells` rejects every one
of them for having no `cell.json`, `repomix.config.json` already excludes the tree, and its
colon-bearing directory names are what `scripts/portable_paths_audit.py`'s docstring blames
for a Windows checkout failure. `.gitignore`'s `reports/runs/` rule was supposed to stop
exactly this and never could: a mid-pattern slash anchors a pattern to the repo root, so it
only ever matched a top-level `reports/runs/`. Added `**/reports/runs/` beside it. Also
gone: 14 one-off `tools/probe_*|verify_round*|repro_*` scripts (every one loads a Windows
`dist/Chudware/libchudware.dll` by absolute path — a dev-box session artifact, zero inbound
references), `core/Testing/` ctest scratch, `assets/icons/batches/` + `icon_prompts.json`
(its `output_dir` names `host/assets/icons/src/`, a layout this repo has not had for
months), 8 dated `diag-*.json` archives where only `diag-latest.json` is read, the
self-documented `_invalid/` quarantine, and one captured console log. Tracked paths:
9188 → 5075.

**`.aicadvault/` is untracked now.** It is not source — it is the PLM vault
(`services/plm/gitbind.py` `VAULT_DIRNAME`) that any run treating the repo root as a project
creates, and it had grown from the 2463 files the 2026-08-14 entry noticed to 3684. No test
depends on the repo-root one (they all build vaults under `tmp_path`). `git rm --cached` +
`/.aicadvault/`; the bytes stay on disk and in history.

**`OMP_INTEGRATION_PLAN.md` is gone, but its contract is not.** The root file was a one-sprint
status board whose "Landed" half duplicates `docs/omp-integration/` and whose "Remaining"
half this log already records as done. Its one unique artifact — the frozen twelve-name
`agent_provider_*` → bridge RPC table — is now a section in `01-DECISIONS.md`, and all five
referrers (`core/api/libchudware.cpp` ×2, `host/src/ui/settings_modal.cpp`,
`services/agent/omp_bridge.py`, `services/agent/project_memory.py`, `docs/README.md`) point
at it, so nothing dangles.

**Archive splits, not rewrites.** `REFACTOR_LOG.md` 2906 → 1294 lines, with 2026-08-03..08
moved verbatim to `docs/refactor-log/2026-08-01-to-08.md` on an entry boundary;
`host/GUI_POLISH_TRACKER.md` 189 → 28 and `host/WIRE_REQUESTS.md` 293 → 27, resolved sections
verbatim into `host/archive/`. The live files keep their names because
`docs/plans/GUI_OVERHAUL_SPEC.md`, `docs/reference/EM_SIMULATION.md`,
`host/src/ui/ai/ai_workspace.cpp` and `work/tasks/T-SIM11.md` link to them.

**Board and skill truth.** T-BL10/T-BL15 were self-declared superseded by the finished
workstream R and are retired; T-BL45 is done. Every card citing `engine_oracle/`,
`ui/imgui/` or the nonexistent `docs/HANDOFF_GUI_OVERHAUL.md` was retargeted at what exists,
including T-WIN1, which still asserted "host/ stays deleted" from the reversed T-G11.
`.claude/skills/chudware-dev/SKILL.md` had the mirror-image defect: its ⚠️ warning said
AGENTS.md was stale on GUI ownership when AGENTS.md is now correct and the warning was not,
and its repo map named `docs/PLAN.md`, `docs/CPP_MIGRATION.md` and `engine_oracle/`, all
deleted. Off-index: 7 stashes (6–8 weeks old) and two `~/.cache` worktrees removed —
recoverable from `refs/archive/stash-0..6` and `~/Backups/CAD-stashes-2026-08-28/` — plus
three branches. `ci/provisioning-repair` looked like unmerged work but every one of its 8
real Windows-CI fixes is already on master (ASCII policy gate, `PYTHONUTF8`,
`ErrorActionPreference`, python-build-standalone provisioning); its only unique content was
a stale `mcp_server/server.py` path in a comment. Archived to
`refs/archive/ci-provisioning-repair`.

**The ECAD role split (the long-deferred known-red).** `test_mega11_legs::…ecad_engineer…`
demanded 22 tools from a card capped at `MAX_TOOLS_PER_ROLE = 16` — unsatisfiable by
construction, because `ecad_run_erc`/`ecad_run_drc`/`ecad_add_power`/`ecad_simulate`/
`ecad_antenna_design`/`ecad_make_board`/`ecad_autoroute` were held by NO role and
`board_from_schematic` only by `spec_writer`'s family expansion: the PCB half of the pipeline
was unreachable. `ecad_engineer` is now explicitly schematic capture (16 seats, ending at
netlist/BOM/plot, which is what `agent_turn._DELIVERED_PRODUCT_TOOLS` already treated as a
legitimate ECAD finish), and a new `pcb_engineer` (16 seats) owns
`board_from_schematic` → `ecad_autoroute` → `ecad_fill_zones` → `ecad_run_drc` →
`ecad_export`, plus antenna/RF. `ecad_simulate` is deliberately NOT a permanent seat: the card
spends one seat on `enable_tools` and its prompt, mission job and cheatsheet all call
`enable_tools(categories=['ecad'])` before SPICE — a regression pins that `ecad_simulate` is
really in that category, so the path is reachable rather than merely asserted. Registered in
the family map, catalog, department map, `routing.yaml`, `tool_budget` (28 calls, justified
like its neighbour's 35) and `mission_template._resolve_build_role`, which now sends
schematic/SPICE work to `ecad_engineer`, board/antenna/DRC/Gerber work to `pcb_engineer`, and
starts a mixed deliverable at the schematic with an explicit delegate hand-off.
`_empty_product_handoff_block` had to split with it: `_ECAD_PRODUCT_TOOLS & allow` now
matches `pcb_engineer`, and the old combined remedy would have told it to "start with
`ecad_new_schematic`" — a tool it cannot hold, which is precisely the unsatisfiable roadmap
that function's own docstring was written to prevent. The PCB branch starts at `ledger_get`
and says to delegate the capture if no schematic exists;
`test_empty_product_handoff_block_pcb_lane_is_walkable` loads the real card and asserts the
remedy names only tools in its grant.

**Memory.** The project's Mnemopi bank held 41 entries, 11 of them raw session transcripts
(258 KB, up to 69 KB each). Each was condensed to a dense fact paragraph (74 KB total,
preserving SHAs, paths, measured numbers and stated decisions) and the raw rows invalidated
with `replacement_id` set, so history stays inspectable. Worth knowing for whoever looks:
`~/.omp/agent/memories/mnemopi/banks/CAD-…/banks/` has accumulated ~60 sub-banks belonging to
OTHER projects (CloudCastle, Stimlab, …) nested under CAD's bank directory. Left alone —
that is other projects' memory, not this one's — but the nesting looks like a bank-path
resolution bug worth a look.

Verification: `scripts/check.py` all 9 gates PASS; `./build.sh all` green; `services/agent` +
`services/dag` 3801 passed, 42 skipped, **1 failed** —
`test_prohibited_auth::test_no_consumer_oauth_token_reuse`, unchanged and untouched here. It
is the owner's open product decision: `contracts/providers.md` §4.4 already authorizes this
exact flow for the DESKTOP as opt-in use of the user's own paid subscription, and the
gateway's `personal` scope matches that intent, but its `shared` scope (an operator-owned
account backing a managed pool) is pooled reuse that §4.4 does not cover. Remedy is one of
two lines — remove the flow, or record an owner-approved exception in
`contracts/providers.md` and add a narrow ALLOWLIST entry. Do not fabricate the approval.

## 2026-08-28 — a deduplication and performance pass, and the browser cutover that isn't one

Eight parallel read-only audits (browser migration, agent/copilot duplication, host frame loop,
core kernel, parts latency, build flags, services complexity, repo hygiene) fed one implementation
wave. Everything below is measured or reference-checked; the negative results are recorded with
the same weight as the changes.

**The Chudcode browser capability cannot replace CEF yet, and that is an upstream fact, not a
packaging gap.** Chudcode's `browser`/`computer` tools live in `packages/coding-agent` and are
reachable only from inside a live agent turn — no CLI subcommand, no RPC, no importable service —
and `scripts/vendor_omp.py` deliberately does not vendor that package. Even reachable, they are
the wrong shape twice over: every non-cmux browser mode is CDP, and CAD moved to CEF precisely
because the CDP `Runtime.enable` handshake is what vendor bot walls fingerprint
(`services/browser/cef_client.py`'s own header says so); and the three GUI consumers of
`ui::IWebViewBackend` (`parts_browser_panel.cpp` vendor sign-in, `oauth_capture.cpp`, `web_panel.cpp`)
need an embeddable off-screen frame source, which `computer`'s private Xvfb stage is not. The
decision and the exact upstream prerequisite — a callable service API plus either a non-CDP vendor
transport or a texture-handoff surface — are recorded in `docs/omp-integration/01-DECISIONS.md`
and `services/browser/README.md` so this is not re-attempted blind.

**Copy amplification on the hottest RPC.** `apply_ops_locked` (`core/doc/session.cpp`) took a
speculative rollback snapshot on EVERY edit: a second full copy of the document tree plus deep
copies of both undo and redo stacks (each budgeted to 256 MB), then pushed undo BEFORE knowing
whether compile would accept the batch. The pre-edit tree is now moved, not copied, the undo push
is deferred until compile succeeds (a new explicit-document helper; `push_undo` is unchanged for
its other callers), and failure restores by move — so the history vectors are never copied at all.
`stepplus_doc_hash` similarly copied the whole tree just to erase one root key before
`canonical_doc_json` copied it again to round floats; the rounding traversal now takes an
optional root-only key to omit, halving that work on the doc_save/doc_open cache-validation path.
Both are byte-identical in output, pinned by `document`, `import_roundtrip` and `sketch3d_doc`.

**The GUI was re-rendering the picking buffer for nothing.** `viewport_panel.cpp` ran a full
second offscreen geometry pass into the ID framebuffer every woken frame a mesh was loaded —
including every frame spent typing in chat — while the `glReadPixels` consumers were correctly
gated on hover. `IdPass` now owns its own freshness: camera matrices, target size, topology
uploads, body visibility and section clipping invalidate the cached frame, readback refuses when
no valid frame exists, and the viewport forces a refresh immediately before a real pick or a
scripted one. Parts results stopped demanding thumbnails for all 100 hits on first paint (grid
and list are clipped to the visible range plus a screen of prefetch, dedup untouched), and warm
`vendor_results.sqlite` hits no longer queue behind the three-wide CEF scrape semaphore — the
slot is now taken only around the real `_search`, with the cancellation recheck moved inside it.
`scan_library_folder` did N+1 full-catalog FTS rebuilds and N whole-file registry rewrites for N
files; it now registers in bulk, persists once and rebuilds once. `SimManager`'s EM and DC-sweep
blocker ladders existed twice each (dispatch and draw), one of them issuing a `stat()` per frame;
they are single const helpers with the STL-existence result cached by path — the DC pair's two
observably different strings are preserved deliberately.

**Dead code, with the callers checked in both languages.** Gone: `drawPlanCard` (zero call sites;
`chat_log.cpp` already inlines the same normalize+draw), `mission_dispatch.route_request` (the
live GUI lane calls `run_linear_role_turn` directly from `worker_rpc._agent_admin_turn`; only its
own tests and stale prose referenced it), `services/worker/worker_router.{h,cpp}` (superseded by
`worker_client.cpp`'s own routing flags at T-RF21, still compiled into the host binary), and the
Settings "Load built-in prompt" button with its `get_prompts` RPC — it loaded `loop.py`'s
SYSTEM_PROMPT, which still described the DELETED build123d two-fenced-block contract, straight
into a live override on the tool-calling lane. Three duplicated platform runs-dir resolvers in
`services/dag` now import `executor.default_runs_dir`, and `close_part` finally evicts
`_LAST_VOLUME`/`_PROCESS_DOCS` like its sibling caches (a refused close still evicts nothing).

**Build flags: the LTO option that existed and was never used.** `CHUDWARE_LTO` has been defined,
`check_ipo_supported`-guarded and documented as release-only in `core/CMakeLists.txt` for months,
and no packaging script or workflow ever set it; `host/` had no equivalent at all. Both now have
the same option, all five production presets and every native release/nightly host configure pass
`-DCHUDWARE_LTO=ON`, and Release builds compile with `-ffunction-sections -fdata-sections` (core
directory-wide so the static libraries feeding the engine are sectioned too) and link with
`--gc-sections` / `-dead_strip`, preserving `--export-dynamic` for the sampling profiler's
`dladdr()`. Developer and gate builds are untouched — they never invoke the presets. Measured on
this box: a production host configured with LTO links clean and is 14.49 MB against 15.29 MB
without. `chudware-hostd` picks up `tcmalloc_minimal` in Release when the library is present
(absent here; the configure line says so rather than failing).

**A production binary that printed a refusal and then hung forever.** Verifying the LTO build
surfaced it: `chudware --version` on a build with `CHUDWARE_DEVELOPER_BUILD=OFF` and no payload
manifest printed `installed payload integrity failed` and never exited — 600 s and still alive.
The integrity refusal, the `--verify-payload-manifest` result (both branches) and the Windows
update-recovery failure all used `return`, which deadlocks in global destruction on the detached
`g_agentCtl` worker parked in `cv.wait` — exactly the trap `--version` documents thirty lines
below and solves with `std::fflush(nullptr); std::_Exit(n)`. All four now take that tail. After:
the same invocation exits 78 immediately. This only ever affected shipped builds, which is why
nobody had seen it; the installer's own verify step would have hung the same way.

**Housekeeping.** Five tracked artifacts the 2026-08-27 pass missed are gone and now ignored:
`preflight_events.jsonl` (the repo-root default sink of `advisor/preflight.py`), the gym leftovers
`bushing_flanged_20mm`, `hydraulic_manifold_block.cwpart.step+` (133 KB) and `runs/*.buildgraph.json`
(the real sidecar dir is `~/.local/share/Chudware/runs`), and `.cursor/mcp.json.disabled`, which
leaked an absolute home path past the ignore rule for its non-suffixed sibling.

Verification: `scripts/check.py` 9/9 PASS; `./build.sh all` green; core `ctest` **103/103**;
`services/agent` + `services/dag` 3801 passed, 42 skipped (the standing
`test_prohibited_auth::test_no_consumer_oauth_token_reuse` red is deselected, unchanged and
untouched — still the owner's `contracts/providers.md` §4.4 decision); `services/mcp` 2868 passed
with four failures, all reproduced from PRISTINE HEAD sources against the same daemon
(`test_fs_read_not_found_in_unreadable_parent_still_clean`,
`test_inprocess_internal_agent_docs_are_registered_but_silent`) or caused by a long `TMPDIR`
(`test_config_path_is_not_the_users`, `test_instance_key_parity_with_cpp`); five `CHUDWARE_SHOT`
scenarios (m1, parts, workspaces, simdisc_em, sketch) exit 0. Deferred, honestly: T-PERF6b's
out-of-line `embedded_brep` blob store is still the dominant import-heavy doc_open cost and needs
its own migration-aware task; `libchudware.cpp`'s linear if/else RPC dispatch was measured as
noise next to millisecond compile work and deliberately left alone; the MCP envelope-helper
duplicates (~120 lines across nine files, four incompatible conventions) were scoped and not
taken this pass.

## 2026-08-28 — second cycle: the quadratic ratsnest, the immediate-mode datums, and a prompt cache nobody called

A second audit wave covered what the first did not (CAM/sim/ECAD/solver, the agent turn's own
overhead, the scene renderer, and the PLM vault), then landed six disjoint units. Everything
below is measured against the code it replaced, not estimated.

**ECAD DRC was quadratic in the wrong variable, twice.** `net_class_of` linear-scanned
`board["nets"]` on every lookup, and the clearance rule calls it twice per shape PAIR — so the
O(shapes²) pass was really O(shapes²·nets), with the same extra factor on zone clearance, track
width and via checks. A `NetRules` index built once per invocation removes it. Worse,
`ratsnest()` — which runs on **every interactive board compile**, not just batch DRC — rebuilt
and re-sorted its Prim frontier every iteration and recomputed the full in-tree × remaining
cross product each round: O(V²logV) sorting on top of ~O(k·n²) distance work. It now keeps a
per-remaining-component best-edge cache relaxed only against the component just added. The
tie-break was the whole risk: equal distances resolve by row-major first pair, and mirrored
layouts hit that for real, so the change ships with a reference implementation in
`test_drc_geometry.cpp` and a 406-board differential corpus (400 randomized + 6 deliberately
symmetric) — 4433 airwires and 8951 violations, zero mismatches — plus five mutations, each
caught. Measured: ratsnest 100/200/400 components 17.95→0.43 ms, 143.13→1.11 ms,
1007.45→2.56 ms (394×); `run_drc` on 1000 nets × 300 pads 547→9.55 ms.

**The renderer was drawing reference datums one primitive at a time.** Every dash, cone
triangle, octahedron face and tail segment got its own `glBufferSubData` + `glDrawArrays` into a
six-vertex scratch buffer, every frame — 161 draws and 161 uploads for the *default* scene
(origin marker plus three axes). The pure geometry now lives in a GL-free
`host/src/render/ref_datum_geom.{h,cpp}`, is built once into a single buffer with merged
same-state ranges, and is rebuilt only when the datum set, hidden set, selection, PM-edit target,
scene extent or palette changes: 13 draws and 0 steady-state uploads. The rich 13-datum case goes
248 draws + 248 uploads → 24 + 1. Reference-plane quads are cached the same way (6 uploads → 0),
all 75 per-frame `glGetUniformLocation` sites are resolved once at link like `overlays.cpp`
already did, and seven `glDisable(GL_CULL_FACE)` calls that could not change state are gone.
Equivalence is a committed ctest (`host/tests/test_ref_datum_geom.cpp`) that replays the old
per-primitive path and diffs the primitive streams — mode, colour, width and vertices — across
every datum kind and selection state.

**The PLM vault re-walked and re-exploded everything on every write.** `_project_containers`
`rglob`'d the entire project — including the vault's own `.aicadvault/objects` fan-out — on every
save, checkpoint, release and even manifest-only edits; a pruned `os.walk` (the pattern
`_tree_working_stamp` already used) gives the identical, identically ordered list at 6.231 →
0.090 ms on a 12-container project with 478 vault objects. `_explode_project_into_shadow` then
re-read and re-canonicalized every tracked `.step+`/`.ecad+` regardless of whether it changed;
it now stamps `(mtime_ns, ctime_ns, size, ino)` before the read and reuses the previous explode
on a hit — 12 explodes per save → 1 when one container changed, 0 when nothing did. And
`history(limit=N)` walked the entire commit chain and decoded every commit twice before applying
the limit; a lazy `iter_log()` generator plus scope filtering inside the walk makes
`history(limit=5)` decode 5 commits instead of 82+ on a 41-commit vault. Equivalence for the
explode cache is an end-to-end replay of save/checkpoint/add_item/release/rev/restore with and
without the cache: every commit id, blob sha, manifest and history variant byte-identical.

**The prompt cache existed, was warmed at worker start, and the live chat lane never called it.**
`run_linear_role_turn` recomposed the role prose, allowlist and the full §9.1 tool-schema block
every turn — per-tool `inspect.signature`/`getdoc` plus a config lookup for each of 22+ tools —
while `prompt_cache.compose()` did exactly that with fingerprinted memory+disk caching for the
GUI meters and tests only. Two things stopped a naive swap and are worth knowing: the cache's
resolver renders `ask_user` and `delegate` differently from the mission dispatcher's, and
`render_tools_block` is budget-coupled (tier surplus is spread across the whole allowlist), so
splicing cached entries around live ones would change bytes. The slot is therefore rendered with
the turn's own resolver and memoized on (fingerprint, tier budget slot, allowlist, per-tool
resolved identity); the cached prose is used as a witness, not blindly, because Harness knobs
read through `config` accessors make a stale-but-fingerprint-valid entry possible — trusting it
re-armed `delegate` for a session with sub-agents OFF, which the existing truncation test caught
and a new test now pins. Measured on the general_agent floor at frontier tier: composition
16.1 → 7.6 ms, `inspect.signature` 21 → 0, `config.load_config` 45 → 5, `os.stat` 47 → 7, and
three consecutive turns render the tools block once instead of three times.

**Two smaller ones.** `core/agent`'s `agent_call` and reviewer stage called `build_context`
(which already assembles the prompt) and then re-read the prompt file a second time with the
WRONG default flags purely to recover a telemetry path — for a modeler that meant re-reading the
31 KB shared ops cheatsheet it wasn't even using. The built context now carries the source path
its own load resolved, which also removes a real hazard: the reported path could previously come
from a different read than the prompt actually sent. And the AI Docs panel stat'd the selected
file every frame plus every visible row every frame; both now use a one-second cached existence
check (720 stats → 12 over 60 frames of 12 rows), with deletion still surfacing within a second.
Nine MCP tool modules carried four mutually incompatible envelope conventions duplicated
pairwise; `services/mcp/_envelopes.py` now owns each convention explicitly (they are NOT merged —
`reason`-keyed dict, `error`-keyed string, compact and sorted variants stay distinct) and the
147 duplicated lines are gone, with the numpy/callable JSON-default differences expressed as
arguments and byte-identity checked against the pre-edit helpers.

Verification: `scripts/check.py` 9/9 PASS; `./build.sh all` green; core `ctest` **103/103**
(including the new `drc_geometry` differential cases) and host `ctest` 20/20 with the new
`test_ref_datum_geom`; `services/agent` + `services/dag` 3807 passed, 42 skipped; `services/plm`
+ prompt-cache + truncation 456 passed; `services/mcp` 2869 passed with the same three
environment/pre-existing reds as the previous entry; seven `CHUDWARE_SHOT` scenarios (m1, parts,
workspaces, ecad, sketch, asm, drawing) exit 0 and the m1 capture is pixel-unchanged against the
pre-change shot. Deliberately deferred with reasons recorded in the audits: DRC's remaining
unindexed pairwise scans (clearance, zone-island fixed point, hole-to-hole) want a real spatial
index and their own measurement; `node_runner.py` and `delegation.py` cannot reuse the prompt
cache until it can key on a resolved allowlist rather than role+capability; `zonefill.cpp` keeps
its own net-class scan.


## 2026-08-30 — profile control, path containment, and bounded failure cleanup

Completed T-X5's `browser_profiles` list/refresh/delete seam across
`services/browser/{profiles,session}.py`, `services/mcp/parts_tools.py`, and the sourcing
tool catalog: list performs fresh redirect-aware status probes through the existing login
classifier, refresh reuses the headed login flow, and delete is lock-aware and removes the
profile, metadata, and transient cookie sidecar. The same pass closed two path-containment
holes (`.`/`..` browser slugs and caller-supplied DAG run IDs across executor, journal,
status, worker-RPC, and replan paths), bounded the external DWG converter, guaranteed CEF
thumbnail temp cleanup, and preserved updater OS error codes across cleanup on Linux and
Windows. Verification: `scripts/check.py` passed all 9 gates; the isolated final-tree
browser/MCP/DAG/thumbnail sweep passed 300/300, and the isolated DWG suite passed
6 / skipped 1 (including preferred ODA and fallback LibreDWG timeout regressions).
Isolated `test_updater` + `test_updater_https`
passed 2/2. The full gate
proved core and host builds before its fixed 600-second deadline expired in the unrelated
WASM step. The GUI profile panel remains deliberately deferred by T-X5; the board's `done`
transition is also human-only, so the card records completed DoD while retaining its
machine-enforced pre-merge status.

## 2026-08-30 — Polyhedral-FEA sole-mesher cutover

Polyhedral-FEA is now the only in-process FEA volume mesher. The cutover removed Netgen
provisioning/runtime staging, the custom structured-hex generator, backend selection,
Netgen-only mesh controls, cross-backend tests, component-cache machinery, and stale agent/UI
guidance. `sim/mesher.{h,cpp}` is the narrow graded-tet/tet10 adapter; sizing and quality live
in focused `mesh_size_band.cpp` and `mesh_quality.cpp` units, while Chudware keeps its existing
static/modal/thermal/fatigue solvers and overlay contract. Coarse/draft meshes skip the extra
feature-grading pass to remain interactive; medium/fine retain it, and adaptive studies still
drive the Polyhedral-FEA size field and ZZ recovery. The build now enables the pinned BSD-3
source by default with OCCT, rejects removed wire keys instead of silently aliasing them, and
cleans stale Netgen cache variables and packaged libraries. Verification: both consumed core
trees and the desktop host build; `mesh`, `mesh_quality`, `mesher_advanced`, and `sim_cache`
ctests pass; focused MCP simulation tests pass 9/9, argument reconciliation 83/83, and the
live Simulation mesh panel shows only Polyhedral-FEA fineness/order/size/adaptive controls.

## 2026-08-31 — Chudcode RPC became the sole online agent runtime

**Superseding boundary.** Chudcode now owns every online top-level and nested provider call,
provider auth and models, persistent sessions and branching, memory, compaction/retry/cache,
browser/computer, and STT. Chudware retains CAD tools and schemas, documents/workspaces,
roles, councils, mission/DAG policy, delegation governance, approvals, guardrails, PLM,
event presentation, and the explicit offline-safe native local-modeling fallback. Every
earlier entry in this log that describes the OMP adapter, vendored packages, or a private
Python/C++ online provider loop is dated implementation history and is superseded by this
entry; none is an operational runbook.

**Clean cutover.** The vendored OMP source and its lock/updater, the Bun adapter and both
private clients, and the duplicate online provider implementations were deleted rather than
retained as fallback aliases. The operational skill is now
`.claude/skills/cad-chudcode-integration/SKILL.md`; the retired skill name/path has no alias.
The frozen research and RPC text under `docs/omp-integration/` remains only as explicit
provenance, with its 2026-08-31 section naming the replacement authority.

**Runtime and control topology.** Online `core/agent/run.cpp` calls the persistent services
worker's `agent_admin_turn`; `services/agent/worker_rpc.py` assembles CAD role/governance
context and run-scoped host tools, and process-global `ChudcodeSessionHost` owns one
protocol-v2 `chudcode_rpc.RpcClient` process per Chudware conversation. Nested provider work
stays inside that Chudcode session. The serialized worker stdio lane carries turns and
streamed events; authenticated `worker_control_info` exposes a separate private Unix-socket
or random-loopback-TCP lane so cancel/steer/status remain available while a turn blocks.
Worker shutdown closes control first, then every saved-session and transient client.

**Hard-won session/event/tool rules.** A persisted session file seeds a live conversation
once and cannot change in place; LRU capacity evicts idle sessions only; close is idempotent,
aborts active work, and preserves history. A turn requires terminal `agent_end`, streamed
deltas are the only visible assistant text, and final message envelopes are lifecycle-only.
Tool start/progress is status; terminal result/cancel becomes one correlated
`agent.tool_call`; unknown frames remain bounded deterministic rows. Host-tool schemas and
dispatch derive from the same effective CAD registry/allowlist, skip unbound tools, preserve
Chudcode builtin ownership, and still cross CAD policy, cancellation, scaffold, guardrail,
and loop-accounting chokepoints. Tool exceptions remain errors, never fabricated success.

**Distribution and UI contract.** `vendor/chudcode/CHUDCODE_ARTIFACT.lock` is the sole
compiled-runtime identity and pins product version, repository commit, RPC protocol, and
immutable target digest/URL; null pins fail packaging closed, and staging is offline.
`assets/ui-shared-manifest.json` uses `chud-ui-shared-v1` and is byte-identical to Chudcode's
copy. It shares palette, tone/status names, usage thresholds, font provenance, and small icon
vocabulary—not widgets or runtime authority—so both native products keep independent
implementations and Chudware stays authoritative for CAD state.

Documentation/skill-only verification was deliberately limited to repository reference
audits, per this coordinated wave's instruction to skip gates and formatters.

## Cold-start latency: payload verification was the whole bill (2026-09-02)

**Symptom.** On a packaged install the tray icon took ~20 s to appear and the app another
~6 s to connect. Reproduced against `/opt/chudware` (0.1.2-beta.41): window at 3.0 s,
`hostd-<key>.sock` at 19.9 s, tray at 20.0 s, `[engine] daemon mode: connected` at 25.9 s,
with `eu-stack` showing the GUI main thread parked in `__pthread_once_slow` — i.e. inside
`services/worker/engine_backend.cpp`'s `initOnce` — for the whole 3–20 s window.

**Cause.** `verifyInstalledManifestEntry` / `verifyInstalledManifestHeld` asked for
`VerifyMode::Startup`, which hashes every executable/library/worker/policy row plus 1-in-16
resources — 835 MB of the 922 MB manifest — merely to authenticate the manifest before
checking ONE named row. `resolveHostdBin()` (`services/ipc/ipc_discovery.cpp:113`) calls it
on every invocation, and `EngineClient::connectInner`/`connectOrSpawn` call *that* four times
per connect; `diskBuildId()` then execs `chudware-hostd --version`, whose `main()` ran the
same 835 MB pass before argv was even parsed. One launch paid the full-payload hash ~8 times
at ~2.7 s each. Scalar SHA-256 at ~310 MB/s, single-threaded, with `libcef.so` alone at
437 MB.

**Fix.** Four independent changes, all in the verifier and its callers:
`VerifyMode::ManifestOnly` (signature + schema + path-safety, hashes nothing) for the
single-row verifiers; argv dispatch moved ahead of the payload gate in `core/hostd/main.cpp`
and `host/src/main.cpp` (`--version`/`--status`/`--stop` load no payload — the gate still runs
unconditionally for every path that starts the app or the daemon); the per-file hashing fanned
out across cores with verdicts written by index so the reported first failure stays
manifest-ordered; and `Sha256` given a bulk-block `append` plus an x86-64 SHA-NI compression
path behind `__builtin_cpu_supports`, with the portable scalar path retained.

**Measured** against the real signed payload: `Startup` 2700 ms -> 296 ms (9.1x),
`verifyInstalledManifestEntry` 2700 ms -> 6 ms (450x), `ManifestOnly` 3.6 ms. All 411 signed
rows still verify; tamper detection and deterministic first-failure ordering re-checked by
byte-flipping one and then two library rows in a copied payload. Repo (developer) build headed
launch for comparison: window 0.32 s, socket 0.95 s, tray 1.26 s, main thread never blocked.

**Also.** `services/dag/worker_rpc.py` imported `services.dag.executor` at module scope for
one symbol, dragging langgraph -> langchain_core -> langsmith -> pydantic_core into the
services worker: 352 ms of its 457 ms import, paid behind `WorkerBridge`'s single
process-wide mutex on the first engine-unknown RPC after the GUI connects. Now imported
inside `_runs_dir()`; worker import 457 ms -> 86 ms, and a cold worker-routed `dag_status`
round-trip through a real daemon is 0.47 s.

**Gotchas / deferred.** `VerifyMode::ProtectedOperation` also hashes 817 MB and runs before
every protected child spawn (`worker_client.cpp:1204`, `worker_bridge.cpp:471`,
`child_supervisor.cpp:279`, `core/agent/run.cpp:667`); it is now ~300 ms rather than ~2.6 s,
but it is still paid per spawn — deliberately, since caching a verdict across spawns is
exactly the tampering window it exists to close. Untouched and still true: `initOnce` and
`waitEngineReady` (300 s deadline, 100 ms poll) run synchronously on the GL thread with no
event pump, so a genuinely slow engine load still freezes the window rather than showing
progress; and `Server::Server` -> `mfg::tryLane()` replays `journal.jsonl` twice before
`trayStart`/`accept` (357 KB, ~20 ms today, but append-only with no compaction). Pre-existing
and unrelated: the `test_aicm_cpp` target does not compile (`host/src/render/ref_datum_geom.h`
includes a `vec_math.h` that does not exist, from 369d56676).

## 2026-09-03 — the classification layer is gone; the main chat is one agent with a visible catalog

**What changed.** Every chat send is now ONE Chudcode `general_agent` turn and the model decides
everything: the GUI composer (`host/src/ui/copilot/composer.cpp::submitTurn`) no longer keyword-
classifies the request into `agent_ask_v2` (a native provider Q&A path), the `agent_simulate_v1`
CFD hijack, an auto-mission, an Ask→Agent promotion, or an edit-in-place scope; it never creates
or replaces a document (`launchDesignRun` used to mint a fresh part whenever the open one had
features — the agent now chooses `new_part` vs. editing the active doc); and it no longer inlines
a `[conversation so far]` transcript into every request (the persistent Chudcode session IS the
conversation; `options.history` still seeds a fresh session once). Modes shrank to a user-picked
Agent | Plan | Ask (`CopilotMode` 0/1/2; `normalizeCopilotMode` migrates persisted Swarm/Edit/
Eng.Firm to Agent and legacy Ask=4 to Ask=2), and `jobs.cpp` no longer clobbers `options.mode`.
The composer sends `options.scope = {doc_id, feature_ids, selection}` in EVERY mode while a doc is
open; `worker_rpc._editor_context_block` renders it as an ACTIVE DOCUMENT block at the tail of the
user message (Copilot's "current file"). The Routing card (rowKind 10), the `task_route` /
`agent.classification` / `complexity` feed handling, the session `scopeClass` rail chip,
`agent_events.classification_event`, and the `direct_agent_full_tools` / `auto_mission_routing`
settings (C++ + Python) are deleted. On the Python side the main chat's surface is
`tool_categories.MAIN_CHAT_CORE_TOOLS` (~62 tools, ~27k tokens of schema: documents, add_features,
sketch checks, measure/inspect/render, mates, drawing, PLM, fs, research, ask_user/task_list/
delegate/mission_start) plus a name-level MORE TOOLS menu of every other registered tool
(`render_tool_menu`, ~2.3k tokens, grouped by `_MENU_GROUPS` over the live registry families)
appended by `mission_dispatch._apply_session_preferences`. `enable_tools` takes exact names
(`tools=[...]`) or menu groups (`categories=[...]`), resolves through `resolve_tool_requests`
(group → legacy pack → `family:` → registry name, uncapped), and reports `unknown`; the single
refresh callback lives in `chudcode_roles._register_category_refresh`. `chudcode_tools._effective_names`
unions on-demand loads BEFORE the mode policy so Ask/Plan stay read-only whatever the model loads;
Plan narrows the agent set (not the contract's broad PLAN_TOOLS) and now also drops
`fs_write`/`fs_edit`/`run_shell`. `roles.RoleCard.apply_tool_policy` opens the dispatch gate for
the main-chat harness role unconditionally. `general_agent.yaml` was rewritten for this surface.

**Gotchas / deferred.** The full registry is ~460 tools / ~150k tokens of schema — the menu exists
because advertising everything is not viable; do not "fix" a missing tool by adding it to the
core, add it only if ordinary part work needs it every turn. `run_shell` deliberately stays off
the core (security hardening 3aaee658) and is on the menu. The `contracts/copilot_modes.json`
ask list is unchanged (parity matrix pinned in two languages), so Ask mode has no `render_part`;
append there if that matters. The native offline pipeline in `core/agent/run.cpp` /
`micro.cpp` still classifies complexity/domain for ITS OWN stage structure and still emits
`agent.classification` — nothing in the GUI reads it any more. `prompt_preview` renders the main
chat's tools uncapped (`budget_tokens=1_000_000, trim=False`) to mirror the native Chudcode tool
definitions; the tiered text slot only applies to the offline loop. `onboarding.cpp` still uses
`agent_ask_v2` as its provider connectivity probe — deliberate, it is not a chat path.

## 2026-09-04 — ME490 harness: project-keyed sessions, evidence-grade FEA, assembly-merge primitives

**Why.** A live ME-490 mission (Comet wheels onto the Crankers frame) hand-edited STEP text and
placed the wheels off the frame, the chat "did not save" (it had — the Sessions list filtered it
out), imported SolidWorks parts rendered as flat bricks, and no sim result carried mesh quality,
reactions, provenance or a VTU.

**What changed.** (1) `Conversation.projectDir` is stamped at creation from the same value the
composer sends as `workspace_dir`; `index.json` v3 carries `active_by_project`; the Sessions
panel filters by that stored key (an "All projects" toggle shows everything) and the GUI adopts
headless Chudcode `*.jsonl` sessions it finds in the Sessions dir. (2) Display quality tiers
(`geom::DisplayQuality` draft/standard/fine, default fine, `CHUDWARE_DISPLAY_QUALITY`, RPC
`doc_set_display_quality`, `render_part(display_quality=)`, Settings → Document). (3) SolidWorks
saved-display imports: shells are re-oriented outward, smooth per-vertex normals are computed
(area-weighted, 40° crease) and attached, the engine renderer is Gouraud, and the viewpoint
rasterizer mirrors it. (4) Every `sim_run_static|adaptive|modal|thermal` result carries
`mesh_quality`, `reactions` (static/adaptive), `provenance` and honours `study.vtu_path`;
`sim_cache` schema bumped to /3. New tools `run_fea_convergence`, `sim_evidence_packet`,
`polymesh_cli` (model_units default mm → `--scale 0.001`), `fit_cylinder_axis`
(surface-sampled, works on tessellated bores), `sweep_envelope`, `steering_turning_radius`,
`motion_fea_handoff_sweep`; `simulate_motion` reports per-step `collisions`. (5) Import features
accept `keep_bodies`/`exclude_bodies`; `import_part` surfaces feature failures; `describe_part`
rosters bodies; `asm_add_component`/`list_world_bodies`/`export_part`/`save_part` carry
`placement_source` and a `placement_unverified` warning until `check_interference` runs; new
builtin skill `assembly-merge`. (6) `core/sim/mesher.cpp` retries a refused fill once with feature
refinement and once at Polyhedral-FEA's own `reduce -h to <=` hint (fixed the fin25 thermal leg
at fineness 0.3). (7) Build: `CHUDWARE_POLYMESH_ROOT=<checkout> ./build.sh core` builds against a
local Polyhedral-FEA tree; the `polymesh` CLI is built and staged beside `chudware-hostd` (also
into `services/dist`). Polyhedral-FEA-private gained `--scale` and `docs/cli.md`, and its Linux
ONNX Runtime digest is pinned. `scripts/run_mission.py` drives a headless `agent_admin_turn`.

**Gotchas / deferred.** `placement_source` is MCP-process state, not persisted on the component.
The polymesh CLI's direct solver is hours at h ≤ 8 mm on a 100 mm part — use it as the coarse
cross-check lane, not the primary solve. Display tiers are inert on mesh-derived imports (facet
density is baked by SolidWorks). Buckling/contact/steady-thermal still run on CalculiX box
proxies, not Polyhedral-FEA. The default role model `claude-fable-5-1` is rejected by the
installed chud (needs Claude Code ≥ 2.1.251); missions must pass `--model claude-opus-5`.
`.venv-mcp` was missing `chudcode-rpc`, `python-docx`, `python-pptx`, `openpyxl`, `reportlab`,
`pypdf` — installed by hand; provisioning should own that.

## 2026-09-04 — CAD's skill packs are reachable online (`cad_skill`, catalog in every role prompt)

**Why.** `services/agent/skills/**` was wired only into the offline loop
(`agent_turn.run_turn` via `prompt_cache.static_segments`). Online — the only lane that
runs today — role cards said "full procedure: skill://assembly-merge" to a model whose
only `skill://` resolver is Chudcode's own foreign registry: live
mission-18d24905b774203f answered `Unknown skill: assembly-merge` and the model said it
would fall back to the role card's summary. `simulate` reached the model at all only
because its SKILL.md had been pasted into `sim_engineer.yaml` by hand.

**What changed.** (1) `mission_dispatch.linear_role_prompt` — the single composition point
for the main chat (`worker_rpc._agent_admin_turn`) and delegated roles
(`chudcode_roles._assemble_role_runtime`) — appends `skills.catalog_block()` (≤4 kB,
metadata only). `prompt_cache` is still validated against the pre-catalog witness, so tool-
schema cache hits survive; `agent_turn`'s own catalog emission now skips a copy already in
the prompt. (2) New host tool `cad_skill(name, path=None)` (`cad_doc_tools`, family `cad`,
`_READ_EXACT`) returns a pack body or one file inside it via
`skills.skill_reference_text`; it is in `MAIN_CHAT_CORE_TOOLS` and in
`copilot_mode_tools.META_TOOL_FLOOR`, because the catalog now rides in EVERY role prompt
and a capped role must be able to see the verb it is told to call. (3) The catalog, the
`format_skill_body` footer, `tool_guidance`, both role cards and every SKILL.md cross-link
now say `cad_skill("<name>")`; the catalog explicitly forbids `read skill://`. The
hand-duplicated release-evidence block in `sim_engineer.yaml` is one pointer line.
(4) Trigger auto-injection reached the online lane: `chudcode_tools` prefixes a
trigger-matched skill body (`triggers.tools`) to that tool's FIRST result text per session,
bounded by `SKILL_BODY_CHAR_LIMIT`, leaving `details` untouched. (5) `compaction` protects a
`cad_skill` result the same way it protected `read skill://`.

**Gotchas / deferred.** The catalog is unconditional per turn, not once-per-session: the
online lane has no first-turn flag (Chudcode owns history), and 3.4 kB of metadata is
cheaper than a wasted turn. Chudcode's own skill discovery is still unconfigured — CAD does
not publish anything into it, and `extensions`-style discovery paths on the spawn remain
unused; `cad_skill` is deliberately the only bridge.
