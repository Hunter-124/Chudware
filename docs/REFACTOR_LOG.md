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

## 2026-09-05 — a static solve refuses a mechanism, and says when its answer is implausible

**Why.** Live (ME490 front spine tube, Ø60 x 5.16 mm wall, tet10 at h = 16 mm ≈ 3x the
wall): `fea_static` returned `ok` with max displacement 9.6e6 mm and
`equilibrium_error_pct` 0.00042. Nothing flagged it — the agent had to notice a nonsense
number by eye. Equilibrium structurally CANNOT catch this: a mechanism carries no load, so
the residual is tiny precisely BECAUSE the model is broken. The old connectivity guard only
looked when the mesh already had more than one component, and no result said anything about
the scale of its own displacement.

**What changed.** (1) `detail::fill_quality` (`core/sim/mesh_quality.cpp`) folds a
node-adjacency union-find into the pass that already validates every index, so
`MeshOut::connected_components` rides WITH the mesh (cache included). (2) `core/sim/mesher.cpp`
adds `MeshOut::thin_wall_ratio` — min over boundary elements of (polymesh
`geom::estimate_local_thickness` at the nearest tessellation vertex / local element size),
paired through an h-sized centroid hash, O(V+E), once per mesh generation; under 1 it also
appends a mesh note. (3) `core/sim/fea_run.cpp`'s static route now computes
`unfixed_components` for EVERY mesh (the old check was gated on `roots.size() > 1`) and
REFUSES with `mesh_quality` attached rather than solving; the bond-gap guidance survives as
a tail on that message. After a solve it reports `plausibility:{max_disp_over_bbox_diag,
bbox_diag_mm, flag}` and, when the peak displacement exceeds the bounding-box diagonal, a
top-level `warnings:["displacement_implausible: …"]` — it does NOT refuse, since a real
large deflection is worth diagnosing. (4) `fea_adaptive` copies the final mesh's
`thin_wall_ratio` onto the result (its passes solve inline meshes, which have no
tessellation to raycast) — same correction shape as the existing `provenance.solver` patch.
(5) `services/mcp/cad_sim_tools.py` forwards `plausibility`/`warnings` as evidence keys, and
`_convergence_row` marks an implausible leg `implausible:true`, gives it no
`delta_pct_vs_prev`, never lets it seed `prev`, and forces `converged:false` with
`implausible_legs` + a remedy note. New ctest `mesh_mechanism`
(`core/tests/test_mesh_mechanism.cpp`): two disjoint boxes in one compound refuse with
`unfixed_components == 1`; one box solves with `connected_components == 1` and
`plausibility.flag == "ok"`; a soft-modulus inline cube is flagged implausible without being
refused.

**Gotchas / deferred.** `thin_wall_ratio` is sampled at tessellation VERTICES, so a coarsely
tessellated planar slab is measured only at its corners, where the vertex normal runs
diagonally and overshoots (a 20 mm cube at h = 5 mm reads ~12.7, not 4); curved geometry —
the regime the metric exists for — is densely tessellated and reads true (an Ø60 x 5.16 mm
tube whose wall carries 4 elements reads 4.1). `unfixed_components` is counted AFTER explicit
bonded gap ties, so a legitimately bonded assembly still reports 0 while
`connected_components` stays >1. A study with no fixtures at all now refuses here instead of
reaching the solver's singular-matrix error; the MCP layer already rejected `fixtures=[]`
upstream, so no caller depended on that path.

## 2026-09-05 — the static solver stops paying for memory it never needed

**Why.** A 200k-element tet10 study was refused at an estimated 12.5 GB (O-04/O-21), and
`ctest tet10_thin_wall` (204600 tet10 elements) refused at "needs an estimated 23.2 GB but
this machine's budget is 15.4 GB". Two causes, both in `core/sim/fea_static.cpp`: the
assembly staged a triplet list of `(3*npe)^2 * 16` bytes PER ELEMENT — 14.4 KB for one tet10,
held resident all the way through the factorization — and tet4 was pinned to Eigen SparseLU
"for byte-identity with the Python parity oracle", a harness that no longer exists in the
tree, whose fill on a bulky 3D solid dominated the tet4 curve.

**What changed.** Assembly is triplet-free: every fixed node pins all three of its DOFs, so
the reduced system is a 3x3-BLOCK pattern over free-node adjacency (`BlockPattern`), built
once from the element cliques plus every bond tie and every contact CANDIDATE, then filled by
scattering element matrices straight into the compressed value array — 4.1 KB/element
measured on tet10. Element matrices are computed in parallel one bounded chunk (~32 MB) at a
time and scattered SERIALLY in element order, so a solve is bit-reproducible whatever the
core count; stress recovery got the same chunking (8192 elements) instead of one
`ElemStress` per element for the whole mesh. Both element orders now share ONE factorization
ladder — CHOLMOD supernodal LLT, then `SimplicialLDLT(AMD)`, then SparseLU only for a
non-positive pivot — and the chosen one is reported: `FeaStaticResult::solver` becomes the
`solver: <label>` note and `provenance.solver` = `chudware/fea_static [CholmodSupernodalLLT]`.
Because the contact pattern is iteration-invariant, the active-set loop now pays for ONE
symbolic analysis and each iteration is a numeric refactorization (an inactive pair holds the
value 0.0). A process-wide gate serializes solves past 20000 free DOFs — the governor only
GRADES a job, it never serialized one — and the queueing time is reported as
`solve_wait_ms` / a `resources:` note. The factor cache now sizes entries from the factor
itself and trims to the governor's LIVE budget on every lookup.

**Measured** (100 mm box, `CHUDWARE_SIM_THREADS=2`, one process per point, peak RSS, before
-> after): tet4 20250 el 0.78 s/0.203 GB -> 0.14 s/0.102 GB; tet4 93750 el 19.9 s/1.555 GB ->
0.98 s/0.471 GB; tet10 20250 el 2.82 s/1.229 GB -> 2.68 s/0.989 GB; tet10 48000 and 93750 el
did not fit a 3 GB slice at all before and now run at 2.79 GB and 6.72 GB. Displacement and
von Mises agree to all 9 printed digits across every solver variant. The governor's measured
calibration was re-fitted on the same element counts (`core/util/resource_governor.cpp`,
`kSparseLU`/`kCholesky` -> `kTet4`/`kTet10`): tet4 is 5.4x-7.4x cheaper than the old table,
tet10 1.35x-1.65x, and `tet10_thin_wall` now PASSES in 139 s.

**Gotchas / deferred.** Do NOT re-order the ladder on the "LDLT is lighter than an LU" rule
of thumb: it is true against SparseLU (tet4 93750 el: 36.5 s/0.60 GB vs 20.5 s/1.37 GB) and
irrelevant next to the supernodal factorization (0.9 s/0.47 GB), and on tet10 a scalar
simplicial Cholesky is 53x SLOWER and heavier (160.8 s/1.52 GB vs 3.0 s/0.99 GB). The
measured table lives beside the ladder; `CHUDWARE_FEA_SOLVER=ldlt|lu|cholmod` reproduces it.
`mesh_size_band.cpp`'s `kTetPackingFactor = 6.0` predicted `6*V/h^3`, but polymesh's graded
counts on that box scale as `N = 1.44e6 * h^-1.51` (surface-driven: `N*h^3/V` runs 122 at
h = 20 mm down to 9 at h = 3.5 mm), so the prediction is now
`max(6.0*V/h^3, 105*A/h^2)` with A the bbox area and the coarsening inverted with sqrt or
cbrt depending on which term binds — both constants are measured, re-measure rather than
scale them. CHOLMOD's factor size is an ESTIMATE (Eigen exposes no accessor), which is only
used for cache accounting. METIS is absent on this box, so CHOLMOD's `metis`/`nesdis`
ordering requests fall back to AMD; installing METIS should improve the tet10 curve further.

## `heal` grows an escalation ladder, a material budget, and a meshability guard (2026-09-05)

**Why.** The recorded `heal` feature was ShapeFix and nothing else, and on the live CD-100
Stage 5 frame spine (body 47, `bad_orientation_of_subshape`) ShapeFix provably does nothing:
144 of its 344 edges are traversed the SAME way by both adjacent faces, and a five-point
tolerance ladder (1e-7 … 0.1 mm) either changed zero bytes or, at 0.1 mm, invented three new
defect kinds. **Files:** `core/doc/document.cpp` (helpers `heal_area`, `heal_bad_edge_count`,
`heal_flood_orientation`, `heal_rebuild_solid`, `heal_narrowest_face`; the `heal` op),
`core/tests/test_document.cpp` (new ladder TEST_CASE beside the existing one),
`services/mcp/cad_eval_tools.py::heal_body`, `services/agent/tool_guidance/cad.py` (+
regenerated `host/src/ui/tool_panel_tables.*`), `contracts/feature_params.md` (appended).

**What it does now.** Four opt-in, recorded rungs after ShapeFix — `sew_tolerance_mm`,
`fix_orientation` (flood-fill the face-orientation graph; reports `faces_flipped` and
`conflicts`), `rebuild_solid` (BOPAlgo_MakerVolume from the faces; reports lumps, kept and
discarded volume) with `rebuild_fuzzy_mm`, and `unify_faces`. `conflicts > 0` is the machine-
checkable statement "no flag repair can fix this body", and it is what routes body 47 to the
rebuild. Body 47: `heal_body(rebuild_solid=True, rebuild_fuzzy_mm=0.05)` → `IsValid()` true,
26 faces, 966555.83 mm^3 (-0.124% of the orientation-consistent pre-heal reading), envelope
unmoved to 18 microns on an 887 mm part, and it MESHES at h = 8 mm (48032 nodes, 190386 tets,
one component, mesh/CAD volume error 0.0013).

**Two things worth keeping in mind.** (1) The volume budget is judged on ORIENTATION-
NORMALIZED volume: a signed integral over a shell whose faces disagree is not a volume (one
reversed face on a 24000 mm^3 box reads 16000), so each side carrying an orientation defect is
re-measured after flood-orienting a COPY, and the budget applies to that. The previous code
simply skipped the volume check when the input reading was untrustworthy — i.e. it was unarmed
for the exact class of body the op exists for; body 47's raw -0.93% drift was unguarded.
(2) A BRep-valid rebuild can still be unusable. At OCCT's own intersection tolerance the
rebuild gives every pair of near-coincident input faces its own intersection line, and body
47's came out valid carrying two 0.011 mm crescent faces; polymesh then refused the whole fill
at h=8 mm and again at 4.8 (its "reduce -h" advice is a mechanical halving — no element size
resolves an 11 micron strip). `rebuild.narrowest_face_mm` is now reported and a face narrower
than 1e-4 of the body's diagonal raises a warning naming `rebuild_fuzzy_mm`; the fuzzy value
also widens the envelope-movement budget to 10x itself, since the caller authorized merging at
that distance. **Deferred:** nothing removes a razor strip after the fact — `ShapeFix_
FixSmallFace` deletes it but leaves the solid invalid, so prevention (fuzzy) is the only path.

## A mechanism can hide inside a connected mesh, so the mesher now dissolves it (2026-09-05)

**How it surfaced.** After the solver ladder moved tet4 off SparseLU onto the SPD ladder,
CHOLMOD started printing "matrix not positive definite" on the real ME-490 WHEEL-RADIAL leg
(316466 tet4, 203163 DOF). The reduced stiffness of a well-posed linear-elastic model is SPD
by construction, so that verdict is a statement about the MODEL — but SparseLU pivots and
never comments on definiteness, so the defect had been invisible, not absent. An independent
triplet+SparseLU re-assembly of the same exported system (`study.export_system_path`)
confirmed the assembly was innocent: symmetric to 1.86e-9, every diagonal positive, one
connected component containing the fixture, zero unreferenced nodes — and lambda_max/lambda_min
= 2.25e7 / 5.6e-27, condition number 4e33, with the soft mode localized on three nodes that
each touched a single tet. That independent SparseLU returned max_disp 5.2e10 mm where the
kernel's LDLT rung returned a believable 0.032 mm: on a singular system, the pivoting solver
is the one that silently produces garbage.

**What the defect is, and what it is not.** It is NOT "a node used by one element" — a tet
apex nobody else uses is fully constrained by its own element (hold the other three corners
and the apex can only move by straining it). That mesh has 51 such tips and 45 of them are
ordinary; refusing on raw valence-1 would have blocked a healthy mesh. The defect is a
FRAGMENT — elements glued to each other face-to-face — touching the rest of the mesh at two
corners or fewer, which can rotate about that node or edge at zero strain energy. Every
connectivity check passes, because the fragment genuinely does reach a fixture, through the
one node it hangs from. Fragment-level is load-bearing: dissolving single hanging ELEMENTS
cleared the exact rigid mode (lambda_min 5.6e-27 -> 1.1e-13) and left a two-element flap that
was still singular in double precision.

**What changed.** `detail::repair_lonely_nodes` (core/sim/mesh_quality.cpp) groups elements by
shared face, computes each fragment's corners shared with the outside, and drops fragments at
or below two; it iterates, because dropping one exposes the next. The mesher calls it after the
tet10 fold repair and before `fill_quality`, so the reported metrics describe the mesh that
will be solved. Node numbering is deliberately NOT compacted — the adaptive driver requires
`disp_mm.size() == nodal.nodes.size()*3` — so the repair leaves unreferenced nodes behind, and
`fea_static` eliminates those (their rows are exactly zero, nothing couples them, so it is
exact); a LOAD on such a node REFUSES rather than being dropped. `mesh_quality` carries
`lonely_nodes_repaired` / `lonely_nodes_remaining` / `hanging_elems_dropped` /
`single_element_nodes`, and `lonely_nodes_remaining > 0` refuses the solve in the same class as
an unfixtured component — a warning would only be as good as someone reading it. The solver
still reports `conditioning_warning` (first line of `solver_notes`) when a strict Cholesky
refuses a matrix anyway, since an inline mesh is counted but never edited.

**The number this corrected.** WHEEL-RADIAL h=12 now solves on CholmodSupernodalLLT with no
warning and an equilibrium residual of 6.2e-11, and `max_disp` falls from 0.0322016 mm to
0.00313464 mm — a factor of 10.3 — while max von Mises is UNCHANGED at 1.294 MPa. That is the
signature of the mechanism: the fragment contributed displacement at zero strain. Repair cost
on that mesh: 34 elements, 22.577 mm^3 = 0.0023% of the 973763 mm^3 solid, 88 mechanism nodes
cleared, 0 remaining, 70 unreferenced nodes eliminated by the solver. Any released packet
quoting a WHEEL-RADIAL displacement (or a stiffness derived from one) must be re-run;
stress-based numbers appear unaffected. Green after the change: sim, tet10, fea_contact,
sim_cache, mesh_mechanism, governor, mesh, mesh_quality, tet10_thin_wall.

**Gotchas / deferred.** The fragment rule treats "the fragment IS the whole mesh" as a domain,
not a defect, so a single-element mesh still solves. Near-degenerate slivers that share three
or more corners are NOT dropped — they are legitimate topology and only ill-conditioned, so
they remain the conditioning warning's job. And the min tet volume on that repaired mesh is
still 0.0057 mm^3 against a 2.005 mm^3 median, which is worth a separate look at the mesher's
sliver production rather than at the solver.

## 2026-09-05 — tet10 nodal stress recovery: Gauss-point extrapolation (StressRecovery)

**What was wrong.** `core/sim/fea_static.cpp` recovered a tet10's stress by AVERAGING its 4
Gauss-point tensors into one element constant and then scattering that constant to all 10
nodes — the tet4 recipe applied to a quadratic element. A tet10's stress varies over the
element, so this smoothed the field twice and clipped every gradient. It made tet10 read
LOWER than tet4 at a stress concentration: on the V&V plate-with-a-hole, rim SCF 1.47 / 1.76
/ 2.44 where CalculiX C3D10, handed the IDENTICAL mesh and loads, recovered 2.23 / 2.58 /
3.44. tet4 already matched C3D4 exactly (1.6153 both), which is what localised the defect to
the quadratic path.

**Fix.** The 4 Gauss-point tensors are extrapolated to the corner nodes with the standard
map for this rule — `corner_i = sqrt(5) * (g[GP_AT_CORNER[i]] - b*sum(g))`, derived beside
`TetGauss4` — and each mid-edge node takes the mean of its two corners. That is exactly
CalculiX's C3D10 recovery, and after it Chudware reproduces ccx's own nodal stress to the
6-significant-digit floor of ccx's ASCII output on every plate-hole mesh: 2.22806/2.57926/
3.44426/3.16604 against 2.2280/2.5792/3.4442/3.1660. The plate-hole tet10 SCF row now PASSES
(3.08792 against Kirsch 3.00 / Howland 3.03, 2.9% / 1.9%) and its error against the
finite-width reference falls at every refinement. tet4 and hex8 branches are untouched and
bit-identical; `ElemStress` grew from one 7-tuple to one PER NODE, so the chunk size is now
`(4 MiB)/sizeof(ElemStress)` instead of a fixed 8192 — the buffer stays bounded and the
serial in-order scatter (thread-count independence) is unchanged.

**Files.** `core/sim/fea_static.cpp` (recovery + the extrapolation constants),
`core/tests/test_sim.cpp` (new case: tet10 extreme-fibre nodal sigma_xx = M c/I; reads 301.4
against 300, where the element-constant recovery read 247.7), `scripts/vv_fea_crosscheck.py`
(the two PLATE-HOLE corroborated findings re-scoped and re-measured), `docs/vv/FEA_VV_REPORT.md`
+ `docs/vv/fea_vv_results.json` (PLATE-HOLE re-run spliced in; `reruns` records exactly what
was replaced).

**Gotchas / deferred.** Only PLATE-HOLE was re-run, so a tet10 nodal-STRESS column elsewhere
in that report (Tier-2 `peak vM Chudware`, Tier 2b) still reflects the old recovery and will
move UP on the next `--all` — expected movement, not a regression; displacements and reactions
do not move at all. The re-run's finest tet10 leg is a different mesh from the old run's
(81928 vs 73072 elements) because the memory envelope differed (`CHUDWARE_MEM_FRAC` 0.30 then
0.50) and the old leg was governor-coarsened; the identical-mesh before/after/ccx triple for
that leg was measured separately and is in the report. Mid-edge nodes are recovered as the
mean of their corners (ccx's choice); a genuinely quadratic mid-edge recovery would need the
element's full quadratic stress field and is not what the reference code does.

## An element-size budget may not coarsen a mesh into debris (2026-09-05)

**How it surfaced.** `ctest mesher_advanced`'s hole-array bracket (100 x 50 x 52 mm bbox over
a 12 mm base plate with four Ø8 bores plus a 12 mm upstand) started failing at
`check_static_ok`: `fea_static: mesh has 6 connected component(s) with no fixture`, with
`connected_components 7, aspect_max 4063, jacobian_min 1.9e-4` and the note "auto-coarsened
for interactivity: estimated 174k elements exceeds 31k budget". The new element-count model
(`max(6·V/h³, 105·A/h²)`) predicted 174k elements at the honest h = 3.93 mm where polymesh
actually delivers **21,764** — a 8.0x over-estimate on a plate-like part — and the geometry
floor could not catch it because the floor is four elements across the smallest BBOX SIDE,
which on this part is 50 mm (floor 12.5 mm, inert) while the governing section is the 12 mm
plate. The hanging-fragment dissolve was innocent: `hanging_elems_dropped == 0` on that mesh.

**What the coarsening actually bought.** Nothing, measured: h = 9.25 mm → 27,690 elements in
7 components (aspect_max 4063); h = 6.00 mm → 76,562 in 38 components; h = 4.66 → 94,322 in
1; the honest h = 3.93 → 21,764 in 1 (aspect_max 36.8). A fill asked for an element size its
features cannot accept spends its budget on feature seeds instead ("232 feature, 240 seed" in
polymesh's own note), so coarsening past the governing section is both more expensive and not
a mesh. And polymesh SAYS SO: at 9.25 mm it refused with "feature unresolved at h=9.254", and
it was Chudware's own recovery ladder that forced the size through by switching the
feature-refinement pass on, producing the shredded mesh.

**What changed.** `SizeBand::geometry_base` (`core/sim/mesh_size_band.{h,cpp}`) now records the
size the geometry and the study's own controls asked for, before either element-count budget
touched it. In `core/sim/mesher.cpp` the fill + recovery ladder became
`fill_with_recovery(loaded, options, h_start, out)` so it can be re-run from a different
starting size, and it grew a new FIRST recovery rung: when polymesh refuses the
budget-coarsened size as unresolvable, the coarsening is WITHDRAWN (re-fill at
`geometry_base`) before spending a feature-refinement pass on a size the fill already
rejected. `mesh_loaded_model` is now a wrapper that also verifies the RESULT: a mesh that came
back in more connected components than the shape has solids, on a study whose band was
coarsened, is rebuilt once at `geometry_base` and the rebuild kept only if it is no more
fragmented. Bracket: mesh 16.3 s / 21,764 elements / 1 component, mesh+solve 34.2 s against
the test's 90 s ceiling (it was 50.7 s before the regression, 81.4 s with the verify-only
version of this fix, and the solve now reads max_disp 0.001977 mm, max vM 41.2 MPa, FoS 6.07).

**The dissolve got a guard of its own** (`core/sim/mesh_quality.cpp`): `repair_lonely_nodes`
refuses a repair that would leave the mesh EMPTY or in more element-bearing components than it
had, restoring the element list byte-for-byte and leaving `lonely_nodes_remaining > 0` so the
solve refuses NAMING the mechanism. The empty case is reachable with real geometry — two
bodies fused at a single NODE are two face-connected fragments, each hanging off the other by
exactly one corner, and the "the fragment IS the mesh" carve-out spares neither, so the
unguarded rule deleted the whole mesh. Pinned by `test_mesh_quality.cpp`'s "the
hanging-fragment dissolve refuses to delete the whole mesh" (7/7 assertions fail without the
guard). `element_component_count` counts components over element-REFERENCED nodes only,
because the repair deliberately leaves unreferenced nodes behind.

**Gotchas / deferred.** A geometry probe was tried FIRST and rejected on evidence: the
smallest opposed-planar section reads 12 mm on the bracket (correct) but 1.6 mm on the fin25
sensor board — its FR4 core, whose opposed 50 x 40 faces are both real — so a section-based
floor of 0.4 mm sits below that study's 0.5 mm base size, withdraws the 0.5 → 0.65 mm
coarsening the board depends on, and polymesh then refuses the fill outright ("mesh element
ceiling 589824 exceeded"). A laminate layer cannot be told from a load-bearing web by geometry
alone, which is why the coarsening is verified on the result instead. The element-count
constants themselves are still fitted on a 100 mm box (`N·h²/A` measured 13-18 on these
plate-like parts against the model's 105) — a real recalibration is a measurement campaign,
not a scale factor. Green after the change: mesher_advanced (47.7 s), mesh, mesh_quality,
mesh_mechanism, thermal, sim, tet10_thin_wall (126 s), and the fin25 golden demo run the
gate's way (12 pass / 0 fail, board max 134.9014 °C — bit-identical to before).

**Follow-up the same day: the rigid-body gate had to learn the same thing.**
`fea_run.cpp`'s mechanism gate counted EVERY node in `points`, so the nodes the dissolve
deliberately leaves unreferenced (node numbering stays 1:1 with the polymesh mesh the adaptive
route refines through) each read as their own "connected component with no fixture" — up to
ten per dissolved tet10 — and any mesh the repair touched was refused instead of solved, even
though `fea_static` already eliminates those rows exactly (`fea_static.cpp` ~1340, zero rows,
and a LOAD on one still refuses). There is now ONE `referenced` mask, built right after the
component union-find, used by both `mesh_quality.connected_components` and the floating-
component gate, plus a `solver_notes` line naming how many nodes were excluded. Bond and
contact springs cannot rescue such a node — both are built from the mesh's own boundary faces,
so every endpoint is an element node by construction. Pinned by `test_mesh_mechanism.cpp`'s
"nodes left behind by mesh repair are not unfixtured components" (a 6-tet inline cube plus two
unreferenced nodes): pre-fix it refuses with "mesh has 2 connected component(s) with no
fixture" and `connected_components 3`; post-fix it solves at 1 / 0. That case does NOT assert
`max_von_mises_mpa` — on a 6-tet cube every node touches the fixture ring the stress summary
excludes, so the reported peak is legitimately 0. Green: mesh_mechanism, fea_contact, sim,
mesh_quality.

## An evidence packet is never overwritten, and it is checkable before it is cited (2026-09-06)

**What went wrong.** ME 490 phase 5B re-ran FRAME-PEDAL-BRAKE into
`Engineering/Simulation_Output/FRAME-PEDAL-BRAKE/`, the folder that already held the
released Phase-4 bonded packet, and `result.json` / `inputs.json` / `provenance.json` from
that run were gone. The never-overwrite rule lived only in the project's Simulation
Runbook, so the only thing enforcing it was the agent's memory — a tool that truncates a
released artifact on a plain second call will eventually be called a second time.

**Archive-then-write.** `cad_common.archive_before_write(targets, anchor=, hash_source=)`
MOVES every existing target into `<anchor>/superseded_<YYYY-MM-DD>_<short-hash>/` before a
writer runs (a rename — free even for a 27 MB `.vtu`), named by the sha256 of the file that
identifies the old set, so two sets archived the same day never collide and re-archiving
identical content is idempotent; it never deletes and never replaces a byte (a name clash
inside an archive is suffixed). `sim_evidence_packet` archives the whole packet AS A SET
(inputs/result/provenance/convergence/checklist/annotated_render — a folder mixing two runs'
files is a collage, not evidence), records what it displaced in the new provenance.json's
`superseded_from` (old result sha256, engine build, when it was written, plus `chained_from`
so the lineage is walkable), and returns `archived_to`. Its new `overwrite=False` default is
the only way to replace in place, and it still writes the `superseded_from` note. Solver
`.vtu` outputs get the same protection through `cad_sim_tools._archive_existing_vtu`, called
beside the STEP export in `_build_static_study` / `_build_modal_study` / the transient-thermal
branch — i.e. only after every setup validation has passed, because a run about to be refused
must not move a file. `_norm_vtu_path` is now pure validation for exactly that reason.

**verify_packet(folder).** The read-back an agent needs before citing a packet: one geometry
sha256 across inputs.json and provenance.json, one engine build id across result.json and
provenance.json, a result.json that still hashes to what provenance.json AND convergence.md's
new `result_sha256:` header cite, reduction fields present (`max_von_mises_reduction` /
`max_von_mises_raw_mpa` / `min_fos_raw` — without them the reader cannot tell a fixture-masked
peak from the field maximum), the recorded `.vtu` still intact, and all 16 runbook checklist
items verbatim. Findings are split by severity because the remedies differ: `problems`
(`reason:"packet_inconsistent"`) means the files contradict each other or required evidence is
absent — do not cite; `unverifiable` (`reason:"packet_unverifiable"`) means the packet predates
the hash stamp, so a single-file swap can neither be shown nor ruled out, and re-running
`sim_evidence_packet` on the same `study_result` archives it and stamps the replacement. Open
checklist items are reported and never fail the check — they are the human release gate.
Measured on the mission's own folders: FRAME-PEDAL-BRAKE flags `convergence_evidence_present`
(a contact result with no table and no adaptive metadata), WHEEL-RADIAL and FRAME-GLOBAL-3G
flag `reduction_fields_present`, and all three are `unverifiable` on the hash stamp.

**The cross-check lane had the same hole.** `polymesh_cli` writes `<case>_result.vtu` /
`<case>_mesh.vtu` / `<case>_diag.json` + `<action>.log` into an out_dir the runbook points at
the case's packet folder, and it truncated them on every re-run. It now takes the same
`overwrite=False` and archives the previous run's artifacts AND its log together, after the
binary has resolved and the argv validated so a refused call moves nothing. That also closed
a second defect: `written = [p for p in outputs if is_file()]` accepted a LEFTOVER artifact
from an earlier run, so a CLI that exited 0 without writing anything reported
`ok:true` with a stale `.vtu` — the archive empties the path first, and the check now means
what it says (`test_polymesh_cli_cannot_pass_on_a_leftover_artifact`).

**Files.** `services/mcp/cad_common.py` (`archive_before_write`, `file_sha256`,
`SUPERSEDED_PREFIX`), `services/mcp/sim_evidence_tools.py` (packet archiving + `verify_packet`
+ `result_sha256` in provenance.json and convergence.md), `services/mcp/cad_sim_tools.py`
(`.vtu` guard), `services/agent/tool_categories.py` + `tool_guidance/sim.py` (advertise
`verify_packet`), `services/agent/skills/builtin/simulate/SKILL.md` (steps 4-6),
`services/mcp/test_sim_evidence_packet.py` (16 tests, pure filesystem — no engine, no solve).

**Gotchas.** The packet resolves geometry/vtu/render hashes BEFORE archiving, and re-points a
render that WAS the folder's own `annotated_render.*` into the archive it just moved to —
otherwise handing a packet's own render back to the tool silently dropped the annotated view.
Archives are flat-file only: a `geometry/` subdirectory in a packet folder is left alone.
There is deliberately no in-place option for a `.vtu` (nothing can re-derive the old field, so
replacing it is a deliberate `rm`), and the run says where the old one went through its notes.

## A mate solve that fails must not move the assembly, and must name the mate (2026-09-06)

**What went wrong.** ME 490 phase 5A tried to place the merged rover with mates and gave up:
"a 20-mate network over 9 components diverged (components thrown thousands of mm; hub FR
reached y ≈ 5900)", so both released assemblies are measured-transform placements and
`placement_source` reads `transform`, not `mate`. The solver was not diverging. The escape
hatch was. When the first LM pass misses, `mate::solve_mates` perturbs rotations and
re-solves (the antipodal restart ported from the Python oracle) — and it perturbed EVERY free
component in the document, then kept whichever restart scored a hair lower on the GLOBAL max
residual. A four-corner rover is four independent chains off the fixed frame, so one
unsatisfiable mate on one corner randomized the other three. Reproduced with a 9-component /
20-mate rover fixture through `mate_cli`: a single 5 mm bore mis-pick on corner FR left
`wheel_FL` **1465.40 mm**, `hub_FL` 194.92 mm and `hub_RL` 117.29 mm from their authored
poses — at the *same* 2.5 mm residual the unperturbed solve already had. The restarts never
improved anything; they only scattered the model.

**What changed.** The restart is now an escape instead of a wrecking ball: it perturbs only
components in a mate-connected group that actually holds an unsatisfied mate (union-find over
the active mates), and it accepts a restart only if that restart CONVERGES or at least halves
the residual. Same fixture after: every component within 7.89 mm of its authored pose, the
untouched corners within 2.7 mm. `System::set_step_limits` (opt-in, empty by default — the
sketch path and the differential harness keep their exact step sequence) adds a per-variable
trust region that the mate path fills with a quarter of the assembly's size for translation
and half a radian for rotation, scaling the whole LM step down by the smallest ratio so the
direction is preserved. Without it a near-null direction on the normal equations — whose
condition number is the SQUARE of the Jacobian's — buys a thousand-mm step for a hair of cost.

**Now it says what failed.** `MateSolveResult` carries `residuals[]` (per mate: `max_abs`,
`norm`, worst first), `worst_mate` and `tolerance`; `AssemblyResult`/`AsmMateStatus` carry
them through, and the assembly ops-response gained `converged`, `max_residual`, `worst_mate`,
`tolerance` and a per-mate `residual`. `doc_status.mates` can now read `"unsolved"`: it used
to report `"ok"` for a solve that never converged, which is how a mission lost a day. The MCP
`solve_mates` returns `ok:false` with `worst_mate` + a worst-five `mate_residuals` table, and
`asm_mate(solve=True)` reports `converged` + `solve_error` while still confirming the mate
landed.

**Tolerance.** `tol` is quoted for a ~100 mm part and applied as `tol * max(1, L/100)`, L
being the largest world coordinate the solve touches; `tolerance` reports what was used and
nothing changes at or below 100 mm. Measured: at 2 m the LM reaches 1e-13 either way (the flat
threshold was NOT the rover's problem), but a seated 20 m assembly a tenth of a micro-radian
off settles at 5e-8 mm and the flat 1e-9 called it divergent.

**Gotchas / deferred.** Jacobi-preconditioning the LM step (D A D, D = diag(A)^-1/2) was tried
first to attack the same conditioning directly and REVERTED on evidence: it regressed
`chain_coincident_4` in the mate corpus from converged to a 6.9e-3 crawl, because the min-norm
cutoff in scaled coordinates retains near-null directions the unscaled cutoff drops. The 20 m
regime still stalls around 1e-4 mm when the correction is large — a real conditioning limit,
now at least reported instead of silently scattering the model. Pinned by `test_mate.cpp`'s
"one unsatisfiable mate cannot move a different chain" (fails pre-fix: hub_a 84.07 mm,
hub_b 184.58 mm off; passes at 0.46 / 2.41 mm) and "the convergence tolerance scales with the
assembly", plus `test_insert_component.cpp`'s "an unsatisfiable mate network is reported,
named and bounded". Green in core/build-fix2: mate, mate_corpus, insert_component,
interference, simulate_motion, motion_dynamics, platform_parity (7/7).

## A grounded component is a constraint, not a pose with a stiff spring (2026-09-06)

**What went wrong.** ME 490 phase 5D measured a 73.7 mm3 frame<->hub overlap at every
corner of the mated rover that vanished when the same two solids were placed by transform,
and traced it to the GROUNDED frame: `fixed:true`, yet its bbox moved ~0.046 mm through the
mate solve. The frame never was an unknown — `CompFrame::alloc` has always skipped fixed
components, so there was no penalty and no variable to blame. The re-pose came from the
PARAMETERIZATION. Every component went `transform -> (t, exp-map w) -> transform`, and the
exp-map needs an orthonormal rotation, so `transform_to_params` SVD-projects the 3x3 block.
A merged import's placement is authored from MEASURED axes (`fit_cylinder_axis` on a faceted
bore is good to ~1e-4 rad), so that block is rigid only to within the measurement and the
projection is a real rotation of the anchor — ~5e-5 rad, which at the rover's 0.93 m corner
radius is the reported 0.046 mm. Worse, the whole assembly was then seated against the
PROJECTED frame while `check_interference` measured the authored one, and
`session.cpp::rebuild_assembly` writes solved transforms back over the document, so every
rebuild re-authored the anchor and a save captured the moved frame.

**What changed.** A fixed component now carries the document's own numbers end to end:
`CompFrame` keeps the authored 3x3 block (`R0`) and the authored 16 doubles (`tf0`),
`CompFrame::rot` returns `R0` instead of `rodrigues(w)` for a fixed component (so the
residuals are evaluated against the placement the document actually has — its Jacobian
block was already dropped, being constant), and the result echoes `tf0` bit-identically
instead of `params_to_transform(t0, w0)`. `transform_to_params` grew an optional
`R_authored` out-param for exactly this. Free components are untouched: they are solved in
exp-map coordinates and must be.

**Tolerance.** The scale-widened threshold from earlier today is now CAPPED at 1e-6 mm
(`kMaxAbsTolMm`): the positional rows are lengths in mm, and left proportional the
threshold grows without bound with the model, so a big enough assembly would report a
visible gap as a seated mate. Nothing at or below 100 mm changes, the 2 m (2e-8) and 20 m
(2e-7) cases are unchanged, and a perfectly seated 400 m model still converges at 1e-6.

**Files.** `core/mate/mate.cpp` (+`mate.h` docs), `core/doc/assembly.h` (pipeline note),
`core/tests/test_mate.cpp` ("a grounded component is pinned, bit-for-bit"; "the convergence
threshold is absolute in mm however big the assembly"), `core/tests/test_insert_component.cpp`
("a grounded component's measured placement survives the rebuild unchanged").

**Pre-fix values.** The solver test's anchor geometry moved 0.045758 mm and 0.045756 mm at
the two corners (mission: ~0.046 mm), the authored shear 1.0e-4 coming back as +/-5e-05;
the document-level test showed the same numbers written back over `components[].transform`
on the rebuild the edit triggers. The tolerance test read 4e-6 mm at 400 m. Note doctest's
`Approx` is useless at these magnitudes — its default scale of 1.0 makes every value below
~1e-5 "equal" — so the new threshold checks are strict comparisons.

Green in core/build-FixedPose: mate, mate_corpus, insert_component, interference,
materials, simulate_motion, motion_dynamics (7/7).

## A left-handed fill cell is not an unsupported element type (2026-09-06)

**What went wrong.** ME 490 phase 5D tried to earn the R1 mass credit from a 90 deg cyclic
sector of the Comet wheel disc (Ø508, 3.175 mm web, Ø25.4 bore, one lightening window) and
`run_fea_static` refused it twice, at fineness 0.5 and 0.7, with *"polyfea: mesh contains
unsupported tet4 connectivity; Chudware studies currently require tet4 or tet10"* — on a
valid watertight solid whose volume checks against the exact quarter. The same string had
already cost WHEEL-RADIAL its h=6 order-2 leg and the showcase run one of four wheel legs.
Nothing about it was true: the fill produced tet4, the study asked for tet10.

**Root cause, in three layers.** `build_mesh`'s fixed-stride extract refuses any cell whose
type is not the one the order asked for, and reported the type it FOUND — so a tet4 left
behind in a tet10 mesh printed as an "unsupported tet4". What left it behind is
`pm::fea::promote_to_quadratic`: it admits an element only when the tet10 Jacobian is finite
and POSITIVE at every Gauss point, and with mid-edge nodes at the straight chord midpoints
that determinant IS 6·V_signed. polymesh's graded fill emits a few LEFT-HANDED cells where
boundary snapping flips a lattice tet on a sharp wedge face — 33 of 467162 on the mission
sector at fineness 0.5 — and every tet4 consumer in Chudware (`fea_static::tet4_B`,
`fea_thermal::tet4_grad`, `fill_quality`) takes |det|, so order 1 never noticed while order 2
lost the whole study. Behind that sat a second class the same predicate declines: cells the
fill ships FLAT (shape quality 7e-17, signed volume 2e-15 mm3), which its own ship gate
cannot fix because it may only relax INTERIOR nodes and in a thin web every node of a
boundary sliver is a boundary node (its note says so: `ship_gate N non-integrable cells`).
And behind THAT, once a mesh finally reached the solver, `fea_static` refused it for
*"22026 node(s) on element fragment(s) that touch the rest of the body at two corners or
fewer"* — all of them: "touches at two corners or fewer" is a SYMMETRIC measurement, and the
only corners the 102750-element body shared outside itself were two isolated tets'.

**What changed.** `mesher.cpp` gained `orient_tets_right_handed` (swap two corners of a
left-handed tet4 — pure relabelling: same nodes, same region, same |volume|, and `faces` is
empty for every FEM element type) and `repair_degenerate_tets` (a cell below 1e-6 of a
regular tet's quality is given volume by moving ONE node along the exact gradient of its own
signed volume, the smallest distance reaching 1e-4 quality, never more than 5% of h — the
same fill's boundary snap already moves nodes by 0.18·h — accepted only if every cell in that
node's star keeps a positive volume and the star's worst quality strictly improves). Both run
after `deliver_local_contract` and before promotion, count into `MeshOut::reoriented_elems` /
`degenerate_elems_repaired` / `degenerate_elems_remaining`, ride into `mesh_quality`
(`reoriented_elems`, `degenerate_elems_inflated`) and say so in the mesh note. The extract
refusal now separates an element FAMILY it cannot solve from a promotion that declined, and
the declined case reports the worst cell's quality, signed volume and edge span. In
`mesh_quality.cpp`, `Fragments` gained `is_body`: the body of a node-connected component is
the fragment holding a strict MAJORITY of its elements, and a body is never a flap. The
majority is strict on purpose — two bodies fused at a single node are two equal fragments,
neither is the other's flap, and both stay flagged so the mechanism is reported instead of
half the model deleted.

**Measured.** Full-size mission sector, fineness 0.5, `core/build-Tet4Conn`: order 1 meshes
467162 elements / 99742 nodes (33 relabelled, 14 inflated) and SOLVES; order 2 meshes
467162 / 694445 (316 quadratic-fallback). Pre-fix, order 2 refused with the connectivity
string and order 1 shipped 33 left-handed cells. The order-2 SOLVE of that part remains out
of reach and is refused for the right reasons — 2.08M DOF needs an estimated 39.1 GB against
a 12.0 GB study budget, and coarsening it (explicit 6 mm → governor 9.21 mm) fills a 3.175 mm
web with 9 mm cells and hands back 3 components, 2 of them unfixtured.

**Files.** `core/sim/mesher.cpp`, `core/sim/mesher.h`, `core/sim/mesh_quality.cpp`,
`core/sim/fea_run.cpp` (two `MeshQualityFacts` counters), new `core/tests/test_mesh_sector.cpp`
+ its `core/CMakeLists.txt` block (`mesh_sector`). The new ctest carries the sector at quarter
radii — same wedge, same 3.175 mm web, still 34 left-handed and 2 flat cells at fineness 0.5 —
meshed AND solved at fineness 0.5 and at an explicit 4 mm, both orders (4 legs, 3m48s), plus
the full-size mission geometry behind `CHUDWARE_ME490_SECTOR=1`.

Green in core/build-Tet4Conn: mesh, mesh_quality, mesher_advanced, tet10_thin_wall,
mesh_mechanism, mesh_sector, fea_contact (7/7, 488 s).

## The site's live renders share one stage, and a heightfield is not a distance field (2026-09-06)

**Symptom.** The landing page's "parametric rebuild" render showed with its upper-right half
black. The fullscreen-triangle vertex shader in `Webservice/public-site/src/scripts/hero.ts`
built `gl_Position = vec4(p - 1.0, …)` from `p ∈ {(0,0),(2,0),(0,2)}`, i.e. `(-1,-1),(1,-1),(-1,1)`:
a triangle covering exactly half of clip space. The standard trick needs `p * 2.0 - 1.0` so the
far vertices land at 3 and the hypotenuse clears the corner. It had been that way since the
render was introduced; it only ever filled the lower-left half.

**Change.** The WebGL2 driver (context, budgeted resize, parallax, visibility, context loss,
clock) moved out of `hero.ts` into `scripts/stage.ts`; the shared GLSL (palette, primitives,
tetra normal + curvature, shadow, AO, grid floor, tonemap, orbit camera) into
`scripts/sdf-glsl.ts` as `glslPrelude({shadowStep, shadowSteps, normalEpsilon})`; and the
boot/teardown/fallback logic into `scripts/showcase.ts`, which mounts every `[data-show]` root
of the new generic `components/Showcase.astro` (`HeroCanvas.astro` is gone). Second showcase:
`scripts/machining.ts` + `machining-timeline.ts` — a CAM job (Face1, Pocket1 ×2 stepdowns,
Contour1 ×3 stepdowns, Drill1 ×4) whose toolpaths are generated on the CPU, fitted to the
Operations table's time windows, and stamped into a 384×262 dexel heightfield (RGBA16F: height,
programmed path, cutter-trail time) that the fragment shader raymarches as
`stock ∩ {y < h(x,z)}` with the cutter/holder as SDFs in the same field. It sits on the landing
page (flipped, after the modeling showcase) and on `/product/cam/`; the modeling showcase is
also on `/product/modeling/`. The cam.png figure left the landing page; the "one project"
section became a 3-column `.workflow-grid`. Phones: showcase panel rows in two columns,
details wrap, subject framed 16–18 % closer under 520 CSS px, stages boot only within one
viewport of the box.

**Gotchas that cost time.** (1) `y − h(x,z)` climbs ~30× faster than distance at a wall, so
the tetra curvature estimate is garbage there and the edge/crease shading striped every wall;
`normalAndCurvature` now divides by the gradient magnitude and fades curvature out where it
leaves 1 (an exact SDF is unaffected), and the machining shader draws its own rims from the
field. (2) Stamping cutter discs binary produces toothed walls; each cell instead keeps up to
4 nested (depth, max-coverage) levels and shows `top − Σ cover·Δdepth`, which is
order-independent — an earlier base/floor/cover history made adjacent cells disagree by
which pass reached them first. (3) Shadow steps must be capped below the thinnest wall for a
heightfield (0.05 here vs 0.3 for the SDF). (4) An op-status change that keeps the same row
index but flips active/inactive must still be reported. (5) Headless Chromium here is
SwiftShader: use `prefers-reduced-motion: reduce` for full-resolution stills; the adaptive
pixel budget otherwise drives the render to its floor. Verified in the production bundle on
`/`, `/product/cam/`, `/product/modeling/` at 390 px and 1400 px, zero page errors. Deploy is
`npx wrangler pages deploy dist --project-name chudware-site` from `Webservice/public-site`
(not authenticated on this box). Product side needed nothing: `core/cam/stock_sim` + the host's
CAM HUD already do dexel stock removal with playback (`host/src/ui/cam/cam_panel.cpp`,
`host/src/ui/viewport_panel.cpp` aux mesh).
## A whole refinement wave is not the unit a fold should be judged on (2026-09-06)

**What went wrong.** `contact.interface_size_mm = 0.35` on the FRAME-PEDAL-BRAKE keyway bore
stalled at 2.434 mm after 7 Rivara LEB waves and the study refused. Three separate mistakes,
each hiding the next. (1) The wave gate judged every cell of the whole refined mesh against
`pm::mesh::validity::kCellShapeFloor` (0.02) — a SHIP-QUALITY target, not a validity bound —
and discarded the entire wave when one cell missed it. Bisection legitimately produces cells
under 0.02 on an anisotropic wedge and the fill itself ships some, so from wave 8 on every
wave was vetoed, projected and plain-chord alike. Downstream needs far less: `mesh_quality`
refuses a cell only when `|6V|` is under `64·eps·max(1, span³)` and the quadratic promotion
needs a positive Jacobian. (2) The fold the gate was reacting to is a per-EDGE fact, not a
per-wave one: `local_refine_tets` projects free-surface mid-edge nodes onto the CAD surface,
and where the chord sag is large that "closest point" has stopped describing the curve the
edge belongs to — through a thin wall, across a hole rim — so it drags the node through the
material ("element 113043 is degenerate, inverted, or has a sign-changing Jacobian").
(3) The delivered size was measured as the coarsest single boundary face in the core while
the contact guard measures a MEDIAN of per-node mean boundary-edge length, so one stretched
face could report a 70% miss on a mesh the guard was happy with.

**What changed.** `local_refine.cpp` gates the projection on its own measured chord sag —
`|projected − chord|` IS `L²κ/8`, no curvature estimate needed — and keeps it only while that
sag is under a fifth of the child size being created; otherwise the node goes on the Euclidean
chord and is counted in the new `LocalRefineStats::n_chord_mids`. `mesher.cpp` judges a wave
on `kWaveDegenerateFloor` (1e-5 shape quality, nine orders above the downstream degeneracy
test and comfortably positive for promotion) instead of the ship floor, counts what is left
under 0.02 rather than refusing on it, and gained `smooth_nodes`: a bounded boundary-aware
pass over an explicit movable set that makes two per-node decisions — a FIDELITY move
(project onto the CAD surface, line-searched 1/½/¼ of the way, capped at the node's own
shortest incident edge, accepted when the star stays positive and ends either healthy or no
worse) and a QUALITY move (umbrella relaxation, tangential-and-reprojected on the boundary,
accepted only on a strictly improving star). It runs on each wave's own new nodes, then once
more at the end over the core's boundary nodes unioned with the corners of every sub-floor
cell. `scan` now returns the 90th percentile of the per-node mean boundary-edge length —
the guard's statistic, one percentile stricter — and `kLocalContractSlack` moved to
`mesher.h` so `fea_run`'s refusal and the mesher's own last-resort re-fill cannot disagree.
That re-fill (`deliver_contract_by_refill`) is the specified last resort before a refusal:
when bisection still misses, the solid is re-filled at a 4×request background so the graded
lattice places the request on its own children, guarded by polymesh's grid-cell-budget floor
and an element-count prediction, keeping the bisected mesh unless the re-fill is strictly
finer.

**Measured, keyway isolate** (`keep_bodies [715,724]`, fixture F7b4f57, 1500 N −Z on F8fd7b4,
`max_gap_mm` 0.4, order 1, in-process libchudware, `core/build-fix`, 12 G scope). The
contact guard measures a delivered `h_interface_mm` of 0.2535 mm for a 0.35 mm request and
0.3377 mm for a 0.5 mm request; pre-fix the same isolate refused at 2.434 mm. The 0.35 mm leg
SOLVES — 11 active pairs, `contact_pressure_reliable:true`, 454376 tet4 / 101702 nodes,
297 s, 0.77 GB peak RSS. The curvature gate declines ~520 projections per run on the post;
the fidelity round moves ~11k nodes back onto the face and cuts the coarse fill's interface
residual (its own note: `snap max|d| = 1.21 mm` at the governor's 13.94 mm background) to
0.40 mm max / 4.7e-3 mm mean. The re-fill fallback was exercised end to end on this geometry
while the measure was still a raw max: "bisection stalled at 0.6014 mm … RE-FILLED at a
1.4 mm background … delivers 0.4469 mm in 2511712 element(s)".

**Gotchas.** The fidelity round must run AFTER the waves, never before: on the coarse mesh a
node's shortest incident edge is a whole background cell, and the same accept test that is
conservative on a 0.3 mm star lets a coarse one move millimetres and distorts the faces the
waves are about to bisect (measured: a pre-wave round left 0.484 mm residual and −0.248 mm
closest approach against 0.034 mm / −0.0198 mm without it). And the re-fill fallback is only
safe behind the percentile measure — with the old max-based one it fired on the keyway for a
size bisection had already delivered and bought a 2.7M-element whole-solid re-fill, whose
2.08M-DOF solve then wanted 60.7 GB. And the sag gate is OPT-IN
(`local_refine_tets`' `max_sag_fraction`, 0.2 from Chudware, 0 by default): the Cartesian
fills' own multi-level LEB has parent edges a whole lattice cell long, so the sag on a coarse
curved wall is legitimately large, and gating it there cost `test_curved_mesh_quality`'s
cylinder_prism 0.0350 mm of boundary residual against its 0.0128 mm bound.

What is NOT fixed and is not a mesher matter: the two bodies as placed touch — median pair
gap 0.352 mm (the nominal radial clearance) but closest approach at or below zero — so
`fea_contact`'s `seat_limit = 2·min_gap0` collapses to 0 and only pairs at gap ≤ 0 can seat.
That population is a knife edge whose size is mesh luck: across runs the 0.5 mm leg seated
8, 12, 13 and then 2 pairs, and at 2 the study is (correctly) refused as a point-load
artefact even though the SIZE was delivered at 0.3377 mm. The delivered size is now honest;
the seating population needs a contact-model answer (a seat limit that is a length, not a
multiple of a possibly-negative closest approach), not a finer mesh.

**Files.** `core/sim/mesher.cpp`, `core/sim/mesher.h` (`kLocalContractSlack`),
`core/sim/fea_run.cpp` (uses it), `core/tests/test_mesher_advanced.cpp` (the flat-pad case's
refusal branch now accepts only an element-ceiling refusal, plus a new seated pin-in-bore
case), and in Hunter's Polyhedral-FEA tree `src/mesh/src/local_refine.cpp` +
`include/mesh/local_refine.hpp`.

## An angle mate is a signed angle about an axis, not a dot product (2026-09-06)

**What went wrong.** ME 490 phase 5F could not animate its steer joint. `core/mate/mate.cpp`'s
`angle()` residual was `da . db - cos(target)`, whose derivative in the angle it controls is
`-sin(theta)`: at 0 deg and at 180 deg the Jacobian row is EXACTLY ZERO, so the mate added no
rank at all — reported `status: redundant` with residual 0, dof left over, and the solver
literally could not drive it. Authored 10 deg off zero the same first-order-flat residual
stalled the LM at 3.589e-03 across three passes and `simulate_motion` refused the sweep. And a
dot product cannot tell +30 deg from -30 deg: measured end to end through the engine, a
pre-fix sweep of `simulate_motion` from -30 deg to +60 deg went -30 -> 0 (stalling at
-0.0024 deg where the driver asked for 0) and then FOLDED — driver +5 gave pose -5, driver
+60 gave pose -60, the whole positive half a mirror image of the negative one.

**What changed.** The residual is now the SIGNED angle about a reference axis:
`theta = atan2(ref . (da x db), da.db - (da.ref)(db.ref))`, the angle from `da` to `db`
measured on their projections into the plane perpendicular to `ref`, wrapped into (-pi, pi]
against the target. Numerator and denominator are both bilinear in (da, db), so the quotient
needs no normalization and is exactly invariant to their lengths; `d(theta)` is 1 per radian of
relative rotation at EVERY angle, zero included, so the row always carries rank. The analytic
Jacobian carries all three blocks — `d/d(da)`, `d/d(db)` and `d/d(ref)` — because `ref` is a
direction attached to the mate's FIRST component and turns with it. Residuals for an angle mate
are now RADIANS (documented on `MateResidual`), and `align: "anti"` finally means something:
it measures to the reversed second direction, so an anti target of A deg is the aligned
A - 180 deg. `angle_limit` is untouched.

**The reference axis.** Priority: (1) authored on the mate as
`value: {"value": <deg>, "axis": [x,y,z]}` (alias `reference_axis`) in the FIRST ref's
component-local frame; (2) a third resolved geometry ref; (3) the cross product of the two
directions at the components' AUTHORED poses; (4) the geoms' companion vector (a plane's
`direction`, an axis's `normal` — which the document's selector resolver leaves at the
component's local +Z, so it is a guess); (5) any perpendicular of `da`. The derived cases are
sign-canonicalized (`canonical_sign`), because a cross product flips as an assembly passes
through 0 deg and a reference that flips is a mate whose +30 deg silently becomes -30 deg
mid-sweep. **Nothing in the derivation reads the solver's seed** — `CompFrame::R_auth` carries
the document's own rotation whatever the warm map says. That is load-bearing: a driven sweep
re-solves the same document warm-started from the previous frame, so a warm-derived axis is
re-derived per frame and changes KIND (case 3 -> case 4) exactly at the frame that lands on
zero. Measured: on a hinge about +X whose fallback guess is +Z, the warm-derived version fails
to converge on the frame right after 0 deg; document-derived, all nine frames solve. Author the
axis for any mate that sits at, or is driven through, 0 deg / 180 deg — case 4 is a guess.

**Measured.** `core/build-AngleMate`, all green: mate (16 cases / 348 assertions),
mate_corpus, simulate_motion, motion_dynamics, insert_component, op_registry. New cases in
`core/tests/test_mate.cpp`: a 0-deg mate that is `ok` with dof 0 and residual < 1e-9 (pre-fix:
`redundant`, dof 1, pose 0.0024 deg off); the 10-deg-off-parallel-seed case converging to
< 1e-9 and 10.000000 deg (pre-fix: -10 deg, the mirror branch); driven sweeps 0 -> 60 deg and
-30 -> +30 deg through zero, every step converged with residual < 1e-9, no `redundant` status
and the pose tracking the driver to < 1e-6 deg; 180 deg; an authored reference axis and its
reversal; the anti convention; the moving-component-first case (the only one that exercises the
`d/d(da)` and `d/d(ref)` blocks); and the off-Z hinge sweep above. End to end through
`libchudware`, `simulate_motion` on the engine's own swinging-arm network sweeps -30 -> +60 deg
with `ok: true`, all 19 frames converged and the pose equal to the driver at every one.

**Files.** `core/mate/mate.cpp` (`wrap_pi`, `canonical_sign`, `CompFrame::R_auth`, the authored
transform now always parsed in `solve_mates`, `MateBuilder::param_vec3`,
`MateBuilder::angle_reference_local`, `MateBuilder::angle`), `core/mate/mate.h` (angle-mate
authoring note + the radians unit on `MateResidual`), `core/tests/test_mate.cpp`.

## Contact studies: the seat is a length, the load path is a verdict, a selector may not span the interface (2026-09-06)

**What went wrong (ME 490 keyway).** Three defects hid behind one converged-looking contact packet.
The seat limit was `2 * min_gap0`, so bodies placed touching (closest approach <= 0) seated only
pairs at gap <= 0 and the seated set was run-to-run mesh luck (8/12/13/2 pairs on identical
requests); the active set also started with EVERY candidate glued, so iteration 1 carried k*gap0
of TENSILE preload (a measured 3.0e6 MPa "pressure" = 56 kN of penetration). A fixture cylinder
filter with `perp_tol_mm 1.5` across a 0.35 mm clearance captured the MATING body's bore nodes as
fixed too, grounding both bodies — the joint carried 0 N in either load direction and the study
still converged with equilibrium error 2e-10 %. And `contact_pressure_reliable` was true with
6 pairs carrying 9.9 N of a 1500 N load.

**What changed.** `core/sim/fea_contact.*`: `seat_limit_mm = max(0.5*h_interface,
0.25*max_gap_mm, 1e-4)`; negative initial gaps are clamped to 0 and reported as
`initial_penetration_max_mm` (never turned into force); `FeaContactOptions::seat_tolerance_mm`
starts the active set at the seated rung (closed, then seatable, then all) so the seated set is
deterministic — the keyway now reproduces to the last digit on consecutive runs. A rejected
alternative ("adjust to touch") is recorded at fea_contact.cpp:442 with its numbers: it makes the
seated arc a function of element size. `contact.load_path{applied_n, carried_n, fraction, note}`
and `contact_carries_load`: reliability now requires >= 50 % of the applied resultant on bodies
held only by contact to cross the interface; a doubly-grounded interface reports
`contact_carries_load:false` with a REDUNDANT-path note. New `core/sim/fea_selection.{h,cpp}`
(chudware_sim, testable without OCCT): a fixture or load selector whose nodes land on BOTH bodies
of a declared candidate pair group is refused before the solve with
`selection_spans_contact{kind, selector, bodies, nodes_captured, body_nodes, median_gap_mm,
min_gap_mm}`; facesets now carry the caller's `selector` text; the MCP layer warns pre-solve when a
plane/cylinder filter's `perp_tol_mm` exceeds `max_gap_mm`. Also this wave: `run_fea_static`
takes an exact `mesh_size_mm` (refused beside a non-default fineness), `run_fea_convergence`
takes `vtu_dir` (one field per leg, `released_leg` named in the reply and hashed by
`sim_evidence_packet`); `steering_turning_radius`/`sweep_envelope`/`render_animation(kind=motion)`
accept `steer=` (uprights driven about a kingpin on a detached clone; ICR from posed axle lines,
4WS via `rear_steer`) and `render_animation` a `poses=` frame list; the motion encoder's frame-hold
path was materialising only rendered samples (3 of 8 frames) and now always writes the ordered
sequence.

**Measured.** ctest `fea_contact` 10 cases (seat determinism: seated arc 4.17/3.64/3.61 mm across
three sizes, byte-identical across two runs; selector span refused at perp_tol 1.5, solves at 0.15
with 1487.8 N through 12 pairs; load path 4000/4000 N fraction 1.000 vs 0/4000 doubly grounded);
`mesher_advanced` and `mesh_mechanism` unchanged; `test_cad_motion_tools.py` 22 (Ackermann radius
to 1e-9, 4WS exactly half); `test_animation_tools.py` 6; `test_fea_static_suite.py` +3 live
(mesh_size_mm → same element count as `sim_mesh` at that size; two per-leg VTUs).

## A contact clearance is a property of the surfaces, not of the facets (2026-09-06)

**What went wrong.** ME 490 O-22 (HINGE-LATCH-LOCAL) was refused at every setting because the
resolution guard measured the pin/saddle clearance on the MESH. A candidate pair is two boundary
NODES, so the gap it records is `n̂·(p_b − p_a)` = `r_bore·cos(dtheta) − r_pin`, where `dtheta` is
however far apart the two meshes happened to sample the arc — up to a whole element of lateral
offset, because that IS the pairing tolerance. On a Ø12.000 pin in a Ø12.600 saddle
(`check_interference`: 0.2999999510990425 mm) the 1899 candidates' closest approach read
**0.03665 mm**: facet sampling ate **87.8 %** of the real clearance. The guard then correctly
refused a ratio of 48.4 against its limit of 4 and prescribed an interface size of **0.147 mm**,
which no fill on the machine can place at any background size — and the advice was illegal
anyway (`GradedSizing: need 0 < h_min <= h_max`). The number the whole refusal chain rested on
was simply not the clearance. On a coarser ring the same measurement flips SIGN: the new ctest's
r 6.000-in-r 6.300 case reads **−0.247 mm** on a fit that is 0.300 mm apart, i.e. the facets
report interference, the guard loses the clearance criterion altogether, and
`initial_penetration_max_mm` reports 0.247 mm of discretization artefact as if it were geometry.

**What changed.** `ContactCandidateOptions::gap_probe` (new `ContactGapProbe` interface in
`core/sim/fea_contact.h`) measures each candidate's gap as the SIGNED distance from the node to
the other body's own analytic face, taking the closer of the two perpendicular measurements. The
PAIRING is unchanged — which nodes face each other across how much lateral offset is a mesh
question — but the recorded `gap0_mm`, and therefore the h/gap guard, the seat limit and the
penetration accounting, are the CAD's. The facet value is kept beside it: `min_gap_facet_mm` /
`median_gap_facet_mm` / `facet_error_mm` on the candidate set, `gap_facet_mm` / `facet_error_mm` /
`gap_source` / `gap_fallback_nodes` in the `contact.resolution` block and in both the note and the
refusal text. A pair whose ANALYTIC gap exceeds `max_gap_mm` (the facets had understated it) is
dropped and counted in `gap_beyond_max_pairs` — `max_gap_mm` is the caller's statement of what
counts as contact. A node with no CAD attribution (repaired/dissolved region, inline mesh) keeps
the facet gap and is counted in `gap_fallback_nodes`; nothing regresses without a probe, which is
how `test_fea_contact`'s ten existing cases stayed bit-identical.
`detail::make_cad_gap_probe` (`core/sim/fea_run.{h,cpp}`, the only OCCT in that TU) re-reads the
study's own STEP/BREP, explodes it to solids, and answers with `BRepExtrema_DistShapeShape`
against bbox-prefiltered faces plus `BRepClass3d_SolidClassifier` for the sign, memoized per
(node, solid) — a node's own body is the solid whose boundary is nearest it, which is unambiguous
because a boundary node lies ON its own surface and the partner is a whole clearance away. One
guard-arithmetic fix rode along: `required_interface_size_mm` no longer takes `min(4*gap, h/2)`
when the RATIO is the failing criterion. There `4*gap` is finer than `h` by construction and is
exactly the size that makes the interface legal; halving `h` on top demanded twice the resolution
the criterion needs (0.887 mm instead of 1.200 mm on the hinge). The `h/2` floor still applies
where the geometric size would be COARSER than what the interface already has — an interface that
funnelled at a legal ratio has to be refined to place more pairs.

**Measured, hinge isolate** (`_src_C.step` bodies at volumes 79624.512 / 21488.494 mm³ — the
evidence record's leaf-A plate and pin — re-exported as a two-solid STEP, meshed at an explicit
1.2 mm tet4: 415916 elements / 82458 nodes, `core/build-fix`, 12 G scope). Facet-measured, the
run reproduces the blocked report to every printed digit: 1899 candidate pairs, `h_interface`
**1.774511 mm**, gap **0.0366475 mm**, ratio **48.4211**, required **0.146590 mm**. CAD-measured,
the same mesh: gap **0.3000000 mm**, `gap_facet_mm` 0.0366475, `facet_error_mm` **0.2633525**
(87.8 % — the report's own figure), ratio **5.9150**, required **1.200000 mm**, 1899 of 1899 pairs
analytic, 0 fallback nodes, 0 dropped. End to end through `fea_run_static` on a synthetic
r 6.000-in-r 6.300 fit at h = 1.5 mm: `gap_source: cad_faces`, gap 0.3 vs facets 0.171, ratio
1.1147, **solves** — 201 active pairs, `contact_pressure_reliable: true`, 3996.68 N of 4000 N
across the interface (fraction 0.99917), max displacement **0.329433 mm**, which is the 0.300 mm
clearance plus 0.029 mm of squash: the solve recovers the analytic clearance as travel.

**Files.** `core/sim/fea_contact.h` (`ContactGapProbe`, the facet-gap facts, the penetration
semantics), `core/sim/fea_contact.cpp` (per-pair measurement, facet statistics, the refusal's
clearance source, the required-size derivation), `core/sim/fea_run.h`
(`detail::make_cad_gap_probe`), `core/sim/fea_run.cpp` (the probe, its attachment at candidate
construction, `contact_resolution_json`'s four new fields, the two notes),
`core/tests/test_fea_contact.cpp` (case (k), 11 cases / 315 assertions green).

**Gotchas.** `fea_contact.cpp` is in `chudware_sim`, which is OCCT-FREE and is what
`test_fea_contact` links — the probe is therefore an interface, the OCCT implementation lives in
`chudware_mesh`, and the ctest supplies the closed-form nested-cylinder oracle instead.
`BRepClass3d_SolidClassifier` is neither copyable nor movable (its solid explorer's copy ctor is
private), so each body holds one by `unique_ptr`. The interface-refinement pre-pass
(`contact.interface_size_mm`) deliberately keeps the facet measurement: it wants seed LOCATIONS,
and since the facet gap only ever understates a curved clearance its candidate set is a superset
of the analytic one. And the guard was never the conservative party here — the independent Hertz
check in the blocked report needs h ≤ 0.136 mm for a 0.4082 mm patch, within 8 % of the 0.147 mm
the facet-measured guard demanded; what was wrong was the clearance it was resolving, not the
rule.

## A local size request is the size field's h_min, so it may never exceed the background (2026-09-06)

**What went wrong.** O-22's hinge saddle (`CONTACT_BLOCKED_2026-09-06b` §4) proved a box with no
exit: at a 1.2 mm background every `contact.interface_size_mm` at or below 1.2 mm came back
"not delivered", and the refusal's own advice — *"pass 2.874 mm or coarser"* — mesh-FAILED with
`GradedSizing: need 0 < h_min <= h_max`. Reproduced exactly on a throwaway nested-cylinder
saddle (r 6.000 in r 6.300, 62.28°, 144.03 mm): `REFINED MESH FAILED: solid 1: GradedSizing:
need 0 < h_min <= h_max and beta > 0`. `local_size_field` passes the request straight in as the
graded field's `h_min` against a `h_max` of the background, and a request coarser than the
background is not a coarser request — it is an illegal one. It is also not an error condition:
"no coarser than 2.9 mm" inside a 1.2 mm mesh is already true everywhere.

**What changed.** `local_size_field` clamps the request into the field's own legal band at BOTH
ends — `std::clamp(size_mm, h*0.25, h)` — so a coarse request collapses to the uniform
background field, `deliver_local_contract` finds nothing to bisect, and the study runs (measured
on the saddle: request 2.874 mm at a 1.2 mm background now meshes, 0 waves, delivered 0.979 mm,
contract met). `fea_run.cpp`'s refusal was rewritten to name the binding limit and only ever
quote a value inside `(0, background]`: it now states the background, the fill's own graded
floor, that `interface_size_mm` IS the field's `h_min` and must satisfy
`0 < interface_size_mm <= mesh_size_mm`, and offers the three legal remedies (a request in
`[achieved, background]`, a `mesh_size_mm` of about 4x the request so the fill's own lattice
places it, or a smaller isolate). The `achieved > background` case — stretched interface faces
rather than merely unrefined ones — says so and does not pretend a legal request exists.

**The wave loop stops when the CONTRACT is met, not when the last stretched edge is.**
`deliver_local_contract`'s marks were a per-FACE maximum while the delivered size is the 90th
percentile of the per-NODE mean boundary-edge length (the currency `fea_contact`'s guard reads).
The two diverge exactly at the end: the fidelity round stretches individual faces by pulling one
node back onto the CAD surface, and LEB closure leaves long diagonals, so the last waves chase
edges the measure has already passed — and each of those waves DOUBLES the mesh. Marks are now
every core face incident on a node whose own mean is still coarse, which is strictly
conservative against the guard's median and makes the loop's termination test identical to the
contract's. Measured on the pin-in-bore ctest: 11 waves → 6, delivered 0.264 → 0.304 mm for a
0.35 mm request, and — the real surprise — active pairs 12 of 2402 → **1308 of 1647** on the
flat pad and 16 → 82 on the bore, because a uniformly-sized interface seats where a
differentially-refined one does not.

**Cost.** `mesher_advanced`'s new bore case was over its own 90 s product bound (106.5 s). The
profile put 52 % of it in polymesh's fill, run twice (coarse probe + refined), and the two
hottest things in the fill were pure waste: `fea::element_jacobians_positive` ran the generic
`eval_shape` Gauss loop with heap-allocated dynamic matrices for a tet4, whose isoparametric map
is AFFINE and whose Jacobian determinant is exactly `6·V_signed` (one 3x3 determinant), and
`tet4_aspect_quality`/`tet_shape_quality` took six square roots to find one longest edge they
then cube. Both now have exact fast paths (`tet4_positive`, `mesh::max_edge_squared`) — same
verdicts, byte-identical element counts and delivered sizes on the bore. Mesh wall time on that
case: 55.1 s → 41.1 s; the whole ctest 106.5 s → **80.9 s**, and the hole-array bracket 88.6 s →
25.6 s.

**Gotchas.** `tet10_thin_wall` was red for MEMORY, not for a regression: polymesh's own curvature
rule meshes the Ø60.325 x 4 mm tube at a 0.5x accuracy lattice (`h_bulk=2.857/h_L2~0.714` for a
6 mm request), so 204 600 tet10 / 307 012 nodes is the intended mesh and its 921 k DOF solve
needs 11.6 GB — under a `CHUDWARE_HEADROOM_GB=6` policy it wants ~17.6 GB free and fails the
governor whenever a sibling agent holds RAM. Nothing in the product changed for it. And the
"polymesh cell shape floor" the blocked report names no longer exists as a wave gate (BoreLEB
replaced it with `kWaveDegenerateFloor`): the report was written against a staged build from
before that landed, which is why its 2.874 mm pin and 0 waves do not reproduce.

**Measured, throwaway saddle** (nested cylinders r 6.000 in r 6.300, 62.28° arc, 144.03 mm,
coaxial 0.300 mm clearance, tet4, `interface_size_mm` 0.6 mm, `max_gap_mm` 0.35 mm):

| background | delivered (90th pct) | guard median | LEB waves | elements |
|---|---|---|---|---|
| band default 4.279 mm | 0.575 mm | 0.5065 mm | 8 | 333 379 |
| 5.611 mm | 0.586 mm | 0.5082 mm | 7 | 345 244 |
| 1.200 mm | 0.569 mm | 0.4795 mm | 3 | 864 000 |

Pre-fix the same interface reported 2.874 mm at every background with 0 waves.

**Files.** `core/sim/mesher.cpp` (`local_size_field` clamp, `deliver_local_contract`'s `scan`
marking on the measured statistic), `core/sim/fea_run.cpp` (the interface-contract refusal only),
and in Hunter's Polyhedral-FEA tree `src/fea/src/element_validity.cpp`,
`src/mesh/src/quality.cpp`, `src/mesh/include/mesh/cell_validity.hpp`.
## A locally-refined region has to be a MESH, not just a fine interface (2026-09-06)

**What went wrong.** O-28's hinge-latch packet (`CONTACT_RESOLVED_2026-09-06b`) shipped a contact
study whose interface size was exactly what was asked for and whose mesh AWAY from it was debris:
`aspect_max` 263, `jacobian_min` 0.0029, 50 cells under polymesh's 0.02 shape floor. That is not a
sizing failure, it is what conforming bisection COSTS — LEPP closure has to bisect a whole chain of
neighbours to stay conforming and pays with stretched cells one size class out from the refined
shell — and a mesh like that cannot carry a plasticity Newton loop, because the tangent stiffness
of a 0.003-Jacobian cell is noise.

**What changed.** Every LEB wave in `deliver_local_contract`, and the mesh the last wave leaves,
now gets a bounded quality pass over the refined region (`quality_pass` in `core/sim/mesher.cpp`):
boundary-aware smoothing (interior nodes placed by max-min pattern search on the star, surface
nodes sliding on their CAD face by projection, feature-EDGE nodes with one degree of freedom along
the edge tangent, corners pinned), 2-3 flips, edge removal for rings of 3 to 7 by Klincsek's
dynamic program, and sliver removal by edge collapse. The loop stops when the region meets the
reported floors, when a pass changes nothing, or after 5 passes. The delivered interface size is
RE-MEASURED afterwards and re-marked and re-split once if the pass coarsened it past the request;
`run_wave` was extracted so the wave loop and that re-split cannot disagree about what a wave is.
The census rides out as `mesh_quality.local_refine{waves, smoothing_passes,
jacobian_min_before/after, aspect_max_before/after, cells_below_floor_before/after, resplit}`.

**One metric, two reported floors.** Every move, flip and collapse is judged by the minimum shape
quality (`6*sqrt(2)*V / L_max^3`) of the cells it changes, because that one number bounds both
reported statistics exactly: `scaled_jacobian >= shape_quality` (no corner's edge product exceeds
`L_max^3`) and `aspect <= 1 / shape_quality` (the largest face of a tet whose longest edge is
`L_max` has area at most `sqrt(3)/4*L_max^2`, and `2.3094 * 0.4330 = 1`). A region driven to 0.05
therefore has `jacobian_min >= 0.05` and `aspect_max <= 20`. `detail::tet_shape`
(`core/sim/mesh_quality.cpp`, declared in `mesher.h`) is now the single definition of both, so
`fill_quality` and the pass cannot drift apart.

**Measured** (`core/build-fix`, tet4, both cases new in `test_mesher_advanced`):

| case | waves | passes | delivered | jacobian_min | aspect_max | under 0.02 floor | wall |
|---|---|---|---|---|---|---|---|
| pin-in-bore, 0.35 mm request | 6 | 3 | 0.3107 mm | 0.0304 -> 0.0885 | 39.50 -> 13.67 | 4 -> 0 | 19.6 s |
| nested saddle, 0.4 mm at 4 mm | 6 | 10 | 0.3542 mm | 0.0198 -> 0.0343 | 75.3 -> 28.5 | 44 -> 0 | 13.3 s |

The existing contract cases improved with it: the flat pad seats 1331 of 1694 pairs (was
1308/1647) and the tight bore 49 of 3595, both contracts still met.

**Gotchas.** Four things were measured and are load-bearing, all commented in place. (1) An
unrestricted surface-node collapse fights the wave loop — a collapse coarsens an interface face,
the next `scan` marks it, the wave bisects it, the next pass collapses it again: 6 waves / 150 484
elements / 20 s became 10 waves / **3 901 081** elements / 366 s with the region ending at
`aspect_max` 724. Collapses are bounded by `scan`'s own marking threshold or the longest boundary
edge already there, whichever is more permissive, which makes the invariant exact rather than
prophylactic. (2) `dirty` marks may NOT carry from the flip sweep into the collapse sweep; the flip
sweep is deterministic, so it starved the same collapse every pass. (3) A near-collinear boundary
facet's normal is rounding noise, and feeding it to the feature test PINS the nodes that most need
to move — facets under `kNormalConditionFloor` are left out of the fan. (4) The feature-edge slide
is a genuine fidelity trade, not a free win: spending the full face budget takes the saddle to
0.0621 and costs the pin-in-bore 0.0881 -> 0.0422, because the same budget on a 6.35 mm rim buys a
slide across it. The bound therefore stays tight and monotone, and the saddle's asserted floor is
the measured 0.034 with that reasoning in the test — the pass's own 0.05 target is met on the
pin-in-bore.

**Files.** `core/sim/mesher.h`, `core/sim/mesher.cpp`, `core/sim/mesh_quality.cpp`,
`core/sim/fea_run.cpp` (four hunks: `MeshQualityFacts`, `mesh_quality_json`, the `MeshOut` copy),
`core/tests/test_mesher_advanced.cpp`.

## Sub-element debris is not a body, and a coarser mesh is not always cheaper (2026-09-07)

**What went wrong (ME 490 O-01, BRAKE-CALIPER-BRACKET).** Six refused solves, re-attributed by
the released packet to "an un-meshable design model" with a 9.8 um wall and ~6e13 elements. It
was neither compute nor an impossibility. Reproduced on the isolate (source body 201, one closed
manifold solid, 88864.793755 mm3, `core/build-fix`, tet4): h = 4.0 -> 345,716 elements in 8
components, h = 3.0 -> 271,233 in 4, h = 2.2 -> 243,586 in 5. Every island but one was
SUB-ELEMENT DEBRIS: 1-15 elements, 0.0002-0.058 mm3 each, 0.005-0.106 mm3 per mesh against
88,505 mm3 of domain — cells shed where the r 9.000 rod runs nearly coaxially INSIDE the r 10.000
rod and emerges over microns (faces of 9.168 and 1.502 mm2 are the whole knife-edge). The
hanging-fragment rule cannot reach them by construction: each is internally face-connected and
shares NO node with the body, so it reads as a separate BODY (`outside_corners == 0`) and is
deliberately spared there — that guard is what stops the repair deleting a real second body. And
`fill_quality` counted every UNREFERENCED node as its own component (57 for a domain of 4, 45 for
8), which the dissolve leaves behind by design; `mesh_loaded_model`'s honest-coarsening rebuild
reads that number, so it fired on every mesh and kept whichever fill had fewer orphans. That is
the packet's non-monotone memory, reproduced to the digit: tet10 345,716 el = **25.07 GB** at
h = 4.0 against 271,233 el = **17.52 GB** at h = 3.0.

**What changed.** `core/sim/mesh_quality.cpp` (fragment rule): `fill_quality` counts components
over the nodes an element REFERENCES, and a new `dissolve_debris_components` drops any component
that is not the domain, is under a millionth of its volume AND is smaller than one average
element of that mesh — measured, not a size constant, so it cannot be tuned into deleting
geometry. `core/sim/mesher.cpp`: solids whose closest approach is under `kFuseTolMm` = 1e-3 mm are
FUSED (`BRepAlgoAPI_Fuse`, fuzzy at the same tolerance) into one body before the fill, because
separately-meshed solids share no nodes and an unfused weld is a rigid-body mechanism at every
element size; skipped when the study declared `contact` / `bond_gap_mm`
(`MeshOptions::declared_interfaces`, hashed into the mesh-cache key). `MeshOut::bodies` records
each meshed body's node RANGE plus the 1-based STEP solid ordinals in it (ranges, not a per-element
id: 4 bytes/element would be 34 MB on an 8.6M-element mesh for what the append order already
knows). `mesh_size_band.{h,cpp}` publishes the prediction basis (`predicted_elements`,
`predict_exponent`, `element_limit`, `memory_coarsened`) and `mesh_loaded_model` re-decides on
MEASUREMENT: re-fill at the requested size when the model and the probe disagree by more than
30 % or the coarse mesh misses the limit anyway, keep the cheaper, and — only when the coarse fill
was the more expensive one, which is the feature-dominated signature — step once more to 0.8x.
`core/sim/fea_run.cpp`: `mesh_quality.fused_bodies` / `fused_overlap_mm3` /
`debris_islands_dissolved`, and the rigid-body refusal now NAMES the island: "body 2 (10589
node(s)) sits 1 mm from the nearest fixtured body (body 1)" plus an `unfixed_islands` array, with
remedies in order.

**Measured.** The bracket isolate SOLVES for the first time — tet4 h = 3.0 mm, 271,226 elements /
56,613 nodes, `connected_components` 1, `unfixed_components` 0, 3 debris islands (7 elements,
0.010050 mm3) dissolved, peak vM **199.9448 MPa** masked / **542.7539 MPa** raw, `min_fos`
**1.3804** (raw 0.5085), max disp 0.048190 mm, `equilibrium_error_pct` **0.0** with the reaction
exactly the applied [-2057, 0, 241.3] N. The tet10 estimate table is monotone and the finest leg
now FITS: h = 4.0 -> 3.20 mm delivered, 287,555 el, **19.10 GB**; h = 3.0 -> 2.40 mm, 237,067 el,
**14.37 GB**; h = 2.2 -> 1.76 mm, 190,197 el, **10.38 GB against a 12.0 GB budget** (was 25.07 /
17.52 / 29.9 GB, down then up). Fusing source bodies 200+201 removes **9300.630370 mm3** where the
project's own `check_interference` recorded **9299.81 mm3** (0.009 % apart) and yields ONE solid,
which meshes in one component. ctest `mesh_mechanism` 6/6 (77 assertions) with two new cases — a
plate and two rods that interpenetrate fuse and solve, overlap **1750.4720 mm3** against the
closed-form 1750.4720 (inclusion-exclusion over the two rod/plate lenses and the triple region);
two rods 1 mm apart are refused by ordinal and by that 1 mm — plus `mesh` 6/6, `mesh_sector` 2/2,
`mesh_quality` 4/4, `fea_contact` 11/11 (315 assertions).

**Gotchas.** The fuse tolerance is an IMPORT tolerance, not a modelling one: at 1e-3 mm nothing a
study could call a clearance is welded shut, and the ctest's 1 mm gap is still refused. A selector
tolerance must stay BELOW the gap it is not meant to cross — at `tol_mm` 1.2 across that 1 mm the
fixture disc captured the OTHER rod's nodes, grounded both bodies, and the study "solved" a
non-positive-definite system at 1e-7 mm instead of refusing (the sampler is at 0.4 mm pitch with
`tol_mm` 0.3). The debris rule runs AFTER the hanging dissolve and its two guards and keeps its own
counters, because it REMOVES components on purpose and must never trip the guard that refuses a
repair for adding them. And the reason a coarser request costs more here is polymesh's own feature
pass: at h = 4.0 the coarse lattice straddles more of the knife-edge, so the fill spends its budget
on feature seeds (2260 feature + 1777 seed cells at 4.0 against 1885 + 1697 at 2.2) — which is why
the honest remedy on this geometry is to mesh FINER, and the measurement is what says so.

**Files.** `core/sim/mesher.h`, `core/sim/mesher.cpp` (fuse pass, `BodyGroup`,
`mesh_separate_solids`, `mesh_step`, `mesh_loaded_model`), `core/sim/mesh_quality.cpp` (fragment
rule), `core/sim/mesh_size_band.{h,cpp}`, `core/sim/fea_run.cpp` (options, packet, refusal, cache
key), `core/tests/test_mesh_mechanism.cpp`.

## Real-mesh steady thermal evidence (2026-09-07)

`run_fea_thermal` now shares the transient STEP/fixture/load path and invokes the
existing steady `sim_run_thermal` engine route. Removed the CalculiX thermal deck
and migrated its benchmark, tests, advisor, and tool/user guidance. The kernel
reports maximum Fourier flux at tet integration points alongside temperature
extrema, mesh quality, engine provenance and temperature VTU. The live 20 mm
6061-T6 bar regression exposed planar picking tolerance prescribing interior
node layers; thermal planar selectors now use exact-plane tolerance. Scoped
thermal/advisor verification: 18 passed in 3.11s with build-fix libchudware.

## Copilot preferences and Auto resolver — overhaul Phases 0–1 (2026-09-07)

`services/agent/preferences.py` now owns the four Copilot preferences, defaults,
help/derivation metadata and task/document policy. The bundled fanout classifier
uses only a matching loopback-served model; an unavailable local runtime takes the
deterministic keyword path without downloading or contacting a cloud model.
`role_models.py` resolves the new chat/worker/private seats while leaving the
Advanced per-role table readable. `worker_rpc.py`, `mission_dispatch.py`,
`copilot_mode_tools.py`, `chudcode_tools.py` and `scripts/run_mission.py` consume the
same policy; explicit turn model/mode choices remain overrides. Auto reads the
authenticated Chudcode catalog, emits its selection, and only moves to the next
cost class after Chudcode's own rate-limit retries finish. Read-only restrictions
are re-applied after capability floors/on-demand loads; document pressure trims
tool descriptions and enables session auto-compaction.

Retired the `ai_effort` preset/override API and `effort_presets.yaml`, tier
`deliberation`, the gym preset tuner/router scaffold selection, and native
`agent.prewalk_target` resolution. `effort.py` is now task evidence for the
resolver; tier detection/budgets remain. Council, trigger, capability, DAG,
gym/bench and regression consumers were migrated without compatibility aliases.
Native explicit `provider_cfg.prewalk_provider` routing remains; the removed
`PrewalkConfig`/target resolver has no remaining reader.

Scoped Python proof through the systemd memory wrapper: **685 passed in 24.46s**
across the preference/model/tier/mission/mode/host-tool/council/trigger/prompt
regressions; the complete gym diagnostic file: **50 passed in 137.81s**.
A throwaway resolver smoke exercised the actual unavailable-local-classifier
fallback, four task kinds, tool restrictions, and local-only refusal; it was
removed. Native `chudware_agent`/`test_agent` build and CTest `agent` are delegated
to the session driver's union validation. Settings UI and transparency UI
Phases 2–3, §14 provider-table retirement, and every VERIFY LATER benchmark remain
untouched/unrun.

Final integration adds the resolver's simulation-precheck floor at
`advisor/sim_advisor.py::precheck` while retaining the explicit
`CHUDWARE_SIM_PRECHECK_BLOCK=0` diagnostic escape. Unpinned mission workers now
resolve from the actual brief/task class, not an empty-prompt seat lookup.
Final expanded scoped verification: **811 passed in 22.84s**, including both
simulation advisor files. Removed a mocked assignment-forwarding test rather
than repinning its private resolver-call assumption; the Auto rules table tests
actual model/tool policy outcomes.

## Assembly live-geometry reporting and empty delivery guards (2026-09-08)

The ME 490 `CC-006_Saddle_Bushed_2026-09-07.step+` was never empty: its assembly graph
references three live components and its container already includes `geometry.step`.
The reported zero-feature count is normal for an assembly. `core/doc/session.cpp`
incorrectly used the part compiler for bounding boxes, mass properties, and native
assembly import into another document; these paths now gather world-placed assembly
solids. The copied original now describes 3 bodies, 35 faces, 96 edges and a
16.7500001 × 190 × 152.4 mm envelope. No ME 490 project file was modified.
`services/mcp/gsl_tools.py` checks live world bodies before topology queries and states
body counts, including an explicit zero-body digest. `cad_doc_tools.py` and
`cad_exchange_tools.py` refuse empty delivery with `no_bodies_empty_document` before
writing unless `allow_empty=True` (native empty documents remain intentionally savable).
Native interference refuses an EMPTY document (`insufficient_bodies`, body_count 0) and
`cad_eval_tools.py` does not record that refusal as placement verification; a single body
or a one-member filter stays a legitimate no-pair success with `pairs_checked: 0` (the
members-filter contract in `test_interference.cpp`). Regression coverage in
`test_gsl_tools.py` exercises assembly save/close/open/describe, native flattening,
empty delivery and insufficient pairs; delivery-path mock fixtures were updated and
the obsolete single-body interference-pass test removed. Reclassify O-38 as
assembly-blind evaluation/reporting, not a lossy `.step+` writer.

## Motion hinge-line transport and carried steering poses (2026-09-08)

ME 490 motion defects A/B were reproduced before fixing them: the angle-only
two-plate hinge at `(31,17,23)` returned a world-Z rotation and zero translation
at −10° instead of `Ry(-10°)` with `(4.464867743,0,-5.033671827)` mm translation;
a hub carrying a concentric wheel with a nonidentity `Rx(90°)` placement steered
about world Z yet its reported wheel z-min drifted from 3 to −3.476685 mm.
`core/doc/session.cpp` had replaced the complete mate value object on each angle
sample (discarding its axis), leaving an orientation-only residual to choose
unconstrained rotation/translation; the steering path correctly rotated the hub
but left its child's previous warm pose untransported, so the solver introduced
free axle spin and the transformed local bbox corners dipped. The engine now
preserves the mate definition, resolves and validates a hinge line, seeds the
complete composed rigid drive, and transports single-anchor floating mate islands
with their re-posed hub without grounding their free DOFs. `core/mate/mate.*`
preserves authored 3D axes and shares signed-axis canonicalization;
`core/doc/validate.cpp` and `assembly.cpp` allow/resolve the angle's third axial
reference. Missing lines are explicitly refused. MCP `cad_asm_tools.py` accepts
native angle value objects; it and `cad_motion_tools.py` return per-body/per-frame
`pose_check` evidence (including carried wheels), preserve failed checks, and
withhold interference for invalid poses. Regressions live in `test_cad_tools.py`
and `test_cad_motion_tools.py`; the planar fixture in `test_multiphysics_misc.py`
now authors its pin line, and the obsolete exact result-key whitelist was removed
from `core/tests/test_simulate_motion.cpp`. The two required files passed together:
**161 passed in 104.55s (0:01:44)**. Scoped native `mate|simulate_motion`:
**100% tests passed, 0 tests failed out of 2**. Migrated motion fixtures:
**3 passed, 15 deselected, 4 warnings in 5.71s** (build123d deprecations). An
additional nonidentity-placement, tilted-axis smoke passed ten frames with maximum
matrix error **1.42e-14**. All native/pytest runs used the systemd memory wrapper
and build-fix library; both throwaway scripts were deleted. Append-only contracts
were extended in `doc_ops.md`, `doc_schema.md`, and `worker_rpc.md`; no project
assembly files were modified.

Final driver-selection proof also covers an ungrounded first reference with a fixed
second reference (opposite drive sign), both references floating with an explicit
line, and both fixed (pose-check refusal for nonzero motion). Grounding is not an
extra prerequisite when the mate identifies its line. That smoke passed all three
cases and its script was removed. Final rebuilt-library acceptance:
**161 passed in 103.34s (0:01:43)** across the two requested MCP files, followed by
**100% tests passed, 0 tests failed out of 2** for native `mate|simulate_motion`.

## Steering envelope reference frames and free axle spin (2026-09-08)

The exact ME 490 rover's reported wheel z-minima (−99.866533738, −109.754975857,
−99.254518607, −104.851604360 mm) were reproduced on a temporary copy of
`CC-001_Merged_Rover_v4b.step+`. They already occurred at zero steering and stayed
constant throughout the sweep: the steer driver and carried-island transport
were correctly WORLD-left, and `pose_check` correctly certified their relative
motion. The defect was transforming square source-AABB corners through the
wheel's pre-existing free axle spin (~37–45 degrees), inflating a circular wheel
into fictitious ground penetration. `cad_motion_tools.py` now transports the
actual reference WORLD-shape bbox corners by relative placement for envelopes,
wheel geometry inference and outer swept radius; source placement is retained
only for fused solids. `session.cpp` supplies first-frame reference bounds and
row-major placements for native dimensional sweeps and independently reconstructs
the requested rotation in `motion_pose_check`. Reference queries stay on detached
clones; the caller's document remains unchanged. The expanded motion tests cover
Rx(0/±90)-placed hubs with a concentric wheel carrying 37 degrees of free spin,
zero-sample agreement with static world bounds, height invariance, and kinematic
radius equality across equivalent hub frames to 1e-9 mm. Pre-fix: **3 failed,
22 deselected in 1.54s** (spun wheel reported −101.117 instead of +2.976 mm).
Post-fix: **26 passed in 10.60s**; native `simulate_motion`: **100% tests passed,
0 tests failed out of 1**. Exact-rover static versus zero-sample discrepancy is
below 4.2e-8 mm and maximum wheel-height variation across five steering samples
is 5.2e-13 mm. Its fresh static solve itself places one wheel at −1.971 mm; that
real pre-existing pose is not hidden by the envelope correction. Original
project files were untouched; all native/pytest proof used the systemd wrapper.

Final motion acceptance, including an explicit wrong-local-axis pose-check
rejection regression: **27 passed in 10.53s**. The temporary reproduction script
was removed; its copied-rover result is retained at
`/tmp/motion42-cvt0od8h/result.json` for review.

## MCP revolute fold: parallel reference normals and persistence (2026-09-08)

The ME 490 fold failure was not dropped `value` metadata, stale mates, or a
world/local placement mismatch: the saved/reopened value retained `[0,1,0]` and
`[910,0,545]`, the first reference placement was identity, and its selected planar
normal was exactly parallel to the hinge axis. `angle_reference_local` threw
before adding the six revolute rows; the builder catch marked the mate unresolved
but still advertised convergence. The copied study reproduced `dof:6`, absent
solve `residual_dof`, and a refused sweep with zero interference samples (37 seeded
trajectory poses existed, but none was a valid collision sample). `core/mate`
now uses a deterministic body-frame phase datum for explicit-line joints whose
selected directions cannot measure pin rotation, and unresolved builders carry
their diagnostic and force nonconvergence. `core/doc/assembly.{h,cpp}` transports
the diagnostic; `session.cpp` reports mechanism and residual DOF separately.
`cad_asm_tools.py` converts MCP world lines to persisted local coordinates and
returns named unresolved refusals; `gsl_tools.py` narrates both DOF fields.

The extended `test_cad_tools.py` regression authors through `asm_mate`, saves,
closes, reopens, solves, and sweeps, covering parallel normals, a non-identity
reference placement, and a genuinely invalid scalar-only axis. Prebuild:
**3 failed, 1 passed, 137 deselected in 3.40s**. Post-fix selection
`-k "simulate_motion or asm_mate or solve_mates"`:
**13 passed, 128 deselected in 15.89s**. Wrapped native ctests:
**simulate_motion Passed 0.29 sec; mate Passed 0.06 sec; 100% tests passed,
0 tests failed out of 2**.

The copied seven-component study was reset through recorded assembly operations
to the design transcript's fresh authored poses/fixed flags, then the exact
world-line mate was added, saved, closed, reopened, and driven 0 to −180 degrees
in 5-degree increments. All **37 interference samples** converged with **dof 1,
residual_dof 0, pose_check.ok true**; maximum translation-check error was
**6.732e-11 mm**. Front–Rear residual overlap at −180 degrees is
**4305.319107935 mm³**, confirming the earlier pose-driven number. Excluding the
Front–Rear split pair, first interference is still **0 degrees**: eight other
pairs already overlap at the initial pose (including Rear–Leaf at 121.961638 mm³).
No previously absent pair appeared during the sampled sweep. Total endpoint
overlap across all pairs is **20633.824076625 mm³**, not 4305.319 mm³; the latter
is specifically Front–Rear. Original project files were untouched (`cmp` against
the initial copy passed). All native/pytest runs used the required cgroup
wrappers. Evidence remains in `/tmp/revolute-fold-before/` and
`/tmp/revolute-fold-after/` (`motion.json`, `summary.json`, and saved study);
the throwaway driver was removed. Contract clarification is `doc_ops.md` §13.

## ME 490 thin-wheel measured-count governor

The CD-210-01C t4p7625/w60 slice exposed two independent estimate errors:
`mesh_size_band.cpp` used the bulky-box `105*A_bbox/h^2` count envelope as a
study cost, and `mesher.cpp` let a long-range count forecast overrule an actual
requested-size refill. This was not a units error. At 2.84 mm the copied STEP
produced an unmeasured 6,845,066-element / 2052.792 GiB envelope. Under the
required 10 GiB cgroup with `CHUDWARE_MEM_FRAC=0.6` and
`CHUDWARE_HEADROOM_GB=4`, the old bootstrap was 17.254622 mm; it is now
bounded to 5.68 mm and explicitly labelled unmeasured.
The historical 16 GiB host budget was not the effective budget of this proof.

Three raw mesh-only measurements were 211,191 elements / 0.266083 GiB peak RSS
at 8 mm, 308,370 / 0.354439 GiB at 5.68 mm, and 227,239 / 0.291901 GiB at 4 mm.
Counts are nonmonotone: adjacent log-log slopes are +1.10525 and -0.87065,
not one transferable box exponent. The RSS fit is about 928 bytes/element plus
0.089 GiB; it is mesh memory, NOT a solver-factor calibration. The old predicted
counts at these sizes were 862,649, 1,711,267, and 3,450,598.

`MeshCountModel` fits local measured slopes, declines forecasts beyond one
octave, and uses exponent 1 before a second measurement. The probe ladder
measures every half octave. Actual counts, not forecasts, determine delivery;
the existing CHOLMOD/assembly solve-peak envelope and hard admission remain.
The native replay forecasts 297,452 at 5.68 mm and 454,348 at 4 mm, respectively
0.964595x and 1.999429x measured counts. At actual counts the unchanged solve
envelopes are 12.1146, 21.1776, and 13.4971 GiB; these are not measured solve
peaks. Recovery forecasts use delivered h and only preceding counts.

`sim_mesh` and static FEA provenance now expose the named `measured_octave`
mode, solve estimate, actual count, measured h/count/RSS probes, and model;
static memory refusals retain this record. `simcache/5` prevents reuse of old
coarsened/contact evidence. Contracts are appended in `worker_rpc.md`.
The native governor regression covers true h^-2 scaling and the real
nonmonotone sequence. The MCP thin-slice regression checks two delivered sizes
and measured forecasts. Final focused proof: `governor` and `sim_cache`,
**100% tests passed, 0 tests failed out of 2 (10.22 sec)**; MCP thin-slice case,
**1 passed in 114.57s**. The existing material-swap test now uses its file's
job-polling helper instead of treating a legitimate `status:running` response
as a completed summary. Native size-band inspection proved that study does not
enter the new probe ladder (`memory_coarsened=0`); physics assertions remain.
Full touched MCP file: **114 passed in 1580.86s (0:26:20)**. All native and
pytest runs used the prescribed cgroup wrappers; builds stayed in `build-fix`.
The unchanged 2000-byte/tet10 mesh ceiling model predicts 0.393374, 0.574384,
and 0.423266 GiB at the measured counts: 1.4784x, 1.6205x, and 1.4500x actual
mesh peak RSS. It already meets the 2x mesh-memory bound; it was not substituted
for the distinct solve-factor model. Evidence is `/tmp/governor-evidence.json`.

## ME 490 independent feature bodies (2026-09-10)

`core/doc/document.cpp` now shares the `new`/`add`/`boss`/`cut` body policy across
primitives, extrusion, revolution, sweep, and loft. `new` and `merge:false` never
boolean against existing bodies; additive modes select only actual B-rep contacts,
including earlier bodies and multiple contacted targets. Unknown modes fail by name.
Primitive base placement excludes bounding-box tolerance padding so a nominally
touching `centered:false` boss remains touching. `services/mcp/gsl_tools.py` counts
the source-solid ordinals of multibody assembly components, not component rows.
The policy is appended to `contracts/doc_schema.md`. Regressions in
`core/tests/test_document.cpp`, `services/mcp/test_add_features_shape.py`, and
`services/mcp/test_gsl_tools.py` defend untouched original volumes, contact-only
merging, mode rejection, and four-body ring geometry through assembly description.
The obsolete native test assertion that disjoint bosses share one compound was
removed; its physical mass/body-count checks remain. Pre-fix box reproduction:
2 bodies, second original volume 1000→1500 mm³; corrected: 3 bodies, both originals
remain 1000 mm³. Measured reproduction elapsed time: 98.17→76.60 ms (single runs,
not a performance benchmark). Both touched MCP files passed: **605 passed in
10.04s**, through the memory-capped in-process `build-fix` engine.

## ME 490 contact preflight latency and durable progress (2026-09-08)

`core/sim/fea_run.cpp` now checks CAD interfaces, selector spans, and analytic
plane/cylinder support-normal envelopes before any volume fill. Whole-face
parameter extrema and conservative selector bounds avoid proving equilibrium
from incomplete samples; unsupported normal families and ambiguous load
attribution retain the existing full-system checks. Geometry refusals report
zero mesh counts and `provenance.governor.mode:"not_started"`. `mesher.cpp/.h`
separate background-only measured-octave probes from one delivered interface
band, preserve the final actual count for admission, and disclose optional-probe
time-budget skips. `sim/progress.h`, `api/libchudware.cpp`, simulation jobs/worker,
and MCP job readers stream real phase/count records and retain terminal progress,
with a 30-second heartbeat during opaque fills. `resource_governor.cpp/.h`
add a thread-local injectable host-memory probe; the cgroup regression now uses
fixed host memory instead of comparing two racing `/proc/meminfo` snapshots.
The wire behavior is appended to `contracts/worker_rpc.md`.

Exact original tet4/fineness 0.5/h=8 study, with geometry/output paths redirected
to `/tmp`: pre-fix **2700.17 seconds, deadline exit 124, no refusal**. Native
stack samples at 420 and 1801 seconds remained in `local_refine_tets`; instrumented
development runs also exposed the expensive first background fill. Passing the
contact band into every disposable governor probe was an additional source-level
multiplier, not an observed completed probe in that timed-out baseline.
Corrected asynchronous job **1.0608 seconds wall**, native CAD refusal **0.9105
seconds**, zero elements/nodes, 0.0702 GiB process peak RSS. Its phase log reaches
geometry/interface/selector/cone/refusal at **0.042/0.337/0.950/0.952/0.953
seconds**. A real admissible-load fill ran for 65 seconds and emitted heartbeat
records at **1.258, 31.261, 61.273 seconds**, then its own worker group was cancelled
cleanly. Scoped proofs: **11 contact live tests passed in 161.12s** including the
private stack; **20 job tests passed in 0.73s**; **32 local-band governor assertions
passed**; final ctest **governor + fea_contact: 2/2 passed in 3.31s**. No user
document or output-field file was changed.

Gate integration tightened the progress regressions: the MCP partial-results test
had raced the new startup phase and indexed a nonexistent `pct` before an
iteration snapshot arrived. It now publishes controlled startup and iteration
states and checks collection semantics without a child-scheduling race. The
native worker regression now isolates its cache and checks phase order and
completed mesh evidence, leaving heartbeat cadence to the injected-clock test
instead of asserting wall-clock record spacing under gate load. The native
gate failure did not reproduce in the paired scoped run; the timing-dependent
assertion was removed, not represented as a reproduced solver failure.
`sim_results` documents that a phase-only snapshot need not contain a percentage
or field values. Full scoped files: **114 passed in 295.89s**
(`services/mcp/test_cad_sim_tools.py`) and **20 passed in 0.64s**
(`services/sim/test_jobs.py`), through the same 10 GiB `build-fix` wrapper.

## ME 490 transform ownership and component suppression (2026-09-08)

Fixed `core/doc/document.cpp` and `session.cpp`: a transform could resurrect a
consumed non-transform feature's historical solid, reporting success while adding
a body. It now refuses that stale owner by name; live-body transforms remain moves
by default, and only `copy:true` duplicates. Nested `rotate:{axis,angle}` now rotates
rather than silently translating only. Assembly transform features become composed
component-placement mutations through the existing warm-start/island-transport
path, refusing missing components and mate-owned poses. `set_suppressed` dispatches
by document kind, accepting assembly components and part features with named
wrong-kind refusals. MCP guidance is updated in `cad_doc_tools.py` and
`cad_asm_tools.py`; append-only contracts document the behavior.

New end-to-end cases in `test_add_features_shape.py` failed against the prebuild
library: stale-owner transform returned `ok:true` and 3→4 bodies; assembly transform
left placement at `[10,0,0]` instead of `[20,10,30]`. The throwaway also reproduced
`set_suppressed: no feature`. Fixed smoke: 3→3 bodies, 3000→3000 mm³,
middle-body COM `[1720,0,1090]` with the other COMs unchanged; stale-owner refusal
preserved 3 bodies and 3500 mm³. Proof through the prescribed 10 GiB wrapper:
**581 passed in 12.03s** (full feature file); ctest **document 1/1 passed in
14.51 sec**. Assembly tests exercise composed rotation, explicit copies,
suppression/restoration, mate-owned refusal, and wrong-kind ids. The reported
275-body fold-study file was not modified.

## Measured convergence admission and wall caveats (2026-09-08)

The convergence driver launched independent durable studies without carrying a
measured mesh-count table; failure collection also discarded native admission
provenance. The current baseline was already `measured_octave`, not a separate
`bbox_envelope` solver: its unmeasured bootstrap still restarted per leg, and the
60-second optional-probe budget was exhausted by a 251.7-second initial fill.
`cad_sim_tools.py` now carries measurements and the delivered anchor across legs,
retains success/refusal provenance, treats failed recovery jobs as terminal, and
names degeneracy causes. Falling DOF is not convergence. `mesher.*`,
`mesh_size_band.cpp`, and `fea_run.*` advance directly by measured half-octaves;
refused finer counts can rebuild the finest prior admitted mesh, explicitly
recorded as `admission_fallback`, never as a multi-octave forecast.

Live replay additionally exposed a post-solve caller mismatch: API overlay mesh
lookups dropped the shared probes and re-meshed after `solve.complete`.
`libchudware.cpp`, `fea_run.*`, and `fea_adaptive.cpp` now share one study-options
parser (the duplicate adaptive parser was removed). A real three-leg wrapper
smoke returned shared-memory overlays with no post-solve mesh phase on any leg.
The long FRAME replay had started before this last caller migration; the separate
smoke verifies that final correction.

The requested FRAME-PEDAL-BRAKE geometry directory was empty. Its packet
`inputs.json` identifies `MECHANISM/geometry/_src_B.step`, ordinal 715; the source
was copied to `/tmp/brake-ladder/source.step` and isolated through the wrapper.
Source SHA-256 remains
`eda65dab8a7dc06b5b6e7d1c220db7aee1dddb3dc748aadb834b29445a0c01d4`;
the exported `/tmp/brake-ladder/isolate.step` hashes to
`b3669503840ccc4cc7ecc822ee56fb00d0aba83b1039a5e6ddb629cce92d59bd`.
Original study geometry was not edited.

Acceptance replay used the approved 16 GiB scope, `CHUDWARE_MEM_FRAC=0.6`,
`CHUDWARE_HEADROOM_GB=2`, and `CHUDWARE_MESH_PROBE_BUDGET_S=1800` (the native
bootstrap alone exceeds the default 60-second optional-probe allowance).
All three rows solved and report `model=measured_octave`:

| Requested mm | Delivered mm | Tet10 | DOF | Estimate GiB | Decision |
| --- | --- | --- | --- | --- | --- |
| 6.0 | 6.0 | 199964 | 945534 | 11.176498 | requested_size_admitted |
| 4.0 | 4.0 | 192556 | 916620 | 10.570982 | requested_size_admitted |
| 2.8 | 4.0 | 192556 | 916620 | 10.570982 | memory_admission |

The last leg measured 259252 elements at 2.828427 mm and 259036 at 2.8 mm,
then returned the admitted 4 mm mesh. The reported historical delivery was
6/5.383/5.306 mm with 959634/958410/957930 DOF. The replay is correctly
`converged:false`: its first pair names `feature_resolution`, its second
`memory_admission`. **Separate mesher defect to file:** on this same
FRAME-PEDAL-BRAKE isolate, actual count falls from 199964 at 6 mm to 192556 at
4 mm (DOF 945534→916620), despite the requested sizes being delivered. This is
not a memory refusal and must not be relabeled as one.

`mesh_quality.underresolved_wall` uses measured CAD minimum thickness, not global
size times a local ratio. Static summaries, ladder rows, and evidence-packet
checklists retain the bending/FoS caveat. Real 20×10×0.53 mm wall proof:
delivered h=6, measured min wall=0.530372 mm, elements_across=0.327045; the
caveat clears on refinement above 0.5 elements across. A fixed-3-GiB cube
smoke delivered 10/10/10 mm for requests 10/5/2.5, with both refused finer legs
explicitly `memory_admission` and valid admitted solves. Regression additions
cover falling DOF, terminal refusal recovery, and packet limitations.
Cache namespace `/6` invalidates earlier evidence; contracts are appended.
Evidence JSON is retained under `/tmp/brake-ladder/`.
Final checks after the shared-parser migration: `test_cad_sim_tools.py` plus
`test_sim_evidence_packet.py`: **133 passed in 302.21s (0:05:02)**.
ctest **governor 1/1 passed in 0.00 sec; 100% tests passed, 0 failed**.
The targeted native `chudware`/`test_governor` build passed; no project-wide gate
or formatter was run. Throwaway reproduction scripts were removed.

## 2026-09-08 — explicit mesher counts, local geometry fields, and edge evidence

Changed `core/sim/{mesh_size_band,mesher,mesh_quality,fea_run,fea_adaptive,sim_cache}`,
the `sim_mesh` RPC output, native/MCP regressions, and the upstream graded-fill
pipeline/headers. Explicit-size bounding-box forecasts are diagnostic rather
than silent coarse bootstraps; CAD curvature/thickness fields replace global
curved-area pitch halving and unconditional planar-crease floods. The fixed
200000-tet LEB stop is replaced by the caller's memory ceiling. The background
pitch is `h/sqrt(2)`. Local sources retain their requested size instead of raising
the entire distance-graded field to nominal `h/4`. Crucially, regular parents are
refined BEFORE boundary projection: projecting coarse corners first left locally
refined children with boundary slivers. Quality/fidelity floors and pass limits
were not loosened. Mesh and solve reports now carry `mesh_quality.size_band` and
unique tetrahedral corner-edge chord statistics; `contracts/worker_rpc.md` is
appended. The unreleased cache namespace `/6` comment records this cutover.

Final mesh-only measurements, under the 10G wrapper, are retained in
`/tmp/brake-ladder/counts-final.json` (baseline: `counts-before.json`).
The final pipeline is the exact projection-order version restored after the
failed rim experiment below, so these final measurements remain applicable.

| Part | Requested h mm | Before elements | Final elements | Final edge min / median / max mm |
|---|---:|---:|---:|---|
| 10x10x200 bar | 8 | 24128 | 864 | 5 / 5.555556 / 8.992452 |
| bar | 6 | 13056 | 4608 | 2.5 / 4.166667 / 5.464532 |
| bar | 4 | 205504 | 6912 | 2.5 / 3.535534 / 4.496226 |
| bar | 2.8 | 94384 | 22032 | 1.666667 / 2.357023 / 3.065980 |
| brake isolate | 8 | 154992 | 16144 | 2.237240 / 4.815493 / 8.969029 |
| brake isolate | 6 | 199964 | 27162 | 0.819483 / 4.107191 / 8.013600 |
| brake isolate | 4 | 195968 | 61064 | 1.073398 / 2.840176 / 9.065066 |
| brake isolate | 2.8 | 207804 | 185700 | 0.001414 / 1.954595 / 4.549586 |

Both final ladders are strictly increasing; every final nominal delivered h equals
its request and every `size_band` is `{applied_floor_mm:0,reason:"none"}`.
Independent NumPy unique-corner-edge measurements agree with the C++ statistics.
The h=4 bar is 2.56 times the 2700-element structured reference, below the 3x bound.
The baseline brake requests 4/2.8 were actually bootstrapped to 5.968209/5.6 mm
under this wrapper/probe allowance, unlike the earlier unrestricted replay.
**The original median-within-25%-of-requested-h criterion is not met**: the brake
medians above, and the coarsest two bar medians, are lower. The native bar test
uses the agreed finite-lattice exception when fewer than four cells span its
smallest dimension; it does not establish the original brake median criterion.

**Remaining defect MESHER-CURVED-RIM-FIDELITY:** upstream `sharp BRep edges are
reproduced by mesh feature segments` still fails unchanged CAD-to-feature p99/h
ceilings: plate-hole **0.0917903714 > 0.09**, cylinder **0.3103285593 > 0.20**.
One attempted local field around protected curved CAD rims (h/8 sources, gradient
0.25) regressed core: pin-region Jacobian **0.0424415 < 0.05** and tight contact
chattered through 25 iterations. That entire rim-field attempt was reverted.
The obsolete plate-hole `>30000` elements and `>400` feature-segments pins were
removed; its actual geometry-fidelity assertions pass unchanged.

**Remaining defect MESHER-EXACT-SURFACE-ROUNDING:** upstream `boundary nodes land
on the exact BRep for every CAD mesher` still fails icecream-cone/graded node
p99/h **1.97178e-12 > 1e-12**; the tolerance is unchanged.
**Remaining defect MESHER-BULK-SLIVER:** the final brake h=2.8 global tally has
minimum Jacobian **0.0007149005**, maximum aspect **1176.663915**, and minimum
corner edge **0.001413533 mm**. This probe requests no explicit local-refinement
band, so its scoped local quality pass is not invoked; membership of that worst
cell in a CAD geometry-field rim band was not measured. It is not a certificate
that every bulk cell meets the local saddle's 0.034 floor.

Final rollback proof: core mesher ctest **100% tests passed, 0 tests failed out
of 5**, **241.70 sec**. Fresh upstream ctest: **96% tests passed, 2 tests failed
out of 53**, **79.96 sec**, with one skipped preview test; the two failures are
the named fidelity tests above. MCP size-delivery/pocket-floor and measured
governor regressions: **2 passed in 28.65s**. Inline tet-edge smoke passed
min/median/max **1 / 1.2071067811865475 / 1.4142135623730951**, floor 0/none.
Both native trees were freshly built at `-j2` under 12G; runs used the 10G wrapper.
No gate, formatter, or host-daemon termination was used. Throwaway measurement
scripts were removed; JSON evidence is retained.

## 2026-09-09 — projected brake sliver repaired; curved-rim attempts rejected

Retained changes are `core/sim/mesher.cpp`, upstream
`src/mesh/src/hybrid_fill.cpp`, and the append-only disclosure in
`contracts/worker_rpc.md`. The core now runs the existing guarded sliver collapse
globally on cells below scaled Jacobian 0.034, before quadratic promotion,
whether or not a local band was requested. This is collapse only, not the local
LEB/smoothing loop. The graded fill records original corner spacing and reuses
its collapse after final boundary projection/pull for pairs compressed below
0.05 of that spacing (travel fraction 0.95), without its void carve. Existing
topology, volume, fidelity, quality floors and pass limits are unchanged.

**MESHER-BULK-SLIVER at brake h=2.8 is fixed.** The old worst tet was element
162304, corners 18238/39967/17551/17549: one interior corner and three boundary
corners, not an all-interior LEB child. Its short pair lies on z=457.2 mm at
y=28.2881567444 mm, x=-166.35 versus -166.3514135327 mm. The exterior gate
independently named the latter retreat node with a 0.00141 mm residual: this is
a boundary-projection collapse that escaped the local-band-only quality pass.
The final global pass collapses one short-edge star, removing its eight tets.
Minimum edge rises from 0.001413533 to **0.511805026 mm**.

Final mesh-only measurements use the unchanged isolate and no explicit local
band. Baseline is `/tmp/brake-ladder/counts-final.json`; new evidence is
`/tmp/brake-ladder/defects-final.json` and `sliver-cell.json`.

| Brake requested h mm | Before tets | After tets | Before min Jacobian | After min Jacobian | Before max aspect | After max aspect |
|---|---:|---:|---:|---:|---:|---:|
| 8 | 16144 | 16072 | 0.025594810 | 0.029107361 | 47.125258 | 45.291935 |
| 6 | 27162 | 27098 | 0.032227441 | 0.057512144 | 36.943437 | 22.670417 |
| 4 | 61064 | 61064 | 0.085907145 | 0.085907145 | 9.142881 | 9.142881 |
| 2.8 | 185700 | 185692 | 0.000714901 | **0.085466971** | 1176.663915 | **14.070272** |

Both ladders remain strictly increasing. The 10x10x200 bar is unchanged at
**864 / 4608 / 6912 / 22032** tets for h=8/6/4/2.8: zero percent count deviation,
inside the ten-percent allowance. **Remaining coarse-mesh bulk-quality defect:**
brake h=8 still has min Jacobian **0.029107361 < 0.034**, despite sixteen
guarded global collapses. A repair attempt is not a universal floor certificate;
the delivered quality summary retains that number rather than hiding it.

**MESHER-CURVED-RIM-FIDELITY remains unfixed.** The two permitted design
iterations were measured and fully reverted:

1. Exact curved-rim sample insertion by conformingly splitting only incident
   boundary-edge stars, independent of the volume field, improved plate-hole
   p99/h **0.0917903714 -> 0.0299800214** (7816 -> 7824 tets), but cylinder
   stayed **0.3103285593**. Core proof then exposed pin-region Jacobian
   **0.0477164 < 0.05** and tight-contact chatter through the unchanged 25
   iterations; this partial patch was not retained.
2. Insert and own curved-rim samples before projection, then recover remaining
   rim segments afterwards: cylinder worsened to **0.3362002470**, while plate
   remained 0.0299800214. This variant was also reverted.

The cylinder failure is missing boundary-rim topology, not inadequate CAD
polyline sampling. Its existing 33 capture intervals have chord 0.0095056 m and
sag/h 0.0283005, already below the 0.20 budget; the curvature bound allows
s <= 0.0252982 m at R=0.05 m and h=0.008 m. The worst CAD sample instead sees
a chamfer segment joining a point inside the cap to a point below the rim on
the cylindrical wall. Inserting its exact rim target into that projected star
produces inverted children, correctly rejected by the unchanged quality gate.
The existing exact-edge connector reports **connected=0** on the cylinder.
Further work must address the boundary cavity/rim chain; simply densifying the
CAD sampling cannot create the missing mesh adjacency. Detailed measurements
remain in `/tmp/brake-ladder/rim-designs.json`.

Final B-only rollback proof after rebuilding both trees at `-j2`:

```text
1/5 mesh              Passed 110.01 sec
2/5 mesh_quality      Passed   3.43 sec
3/5 mesher_advanced   Passed 101.81 sec
4/5 mesh_mechanism    Passed  11.32 sec
5/5 mesh_sector       Passed  26.66 sec
100% tests passed, 0 tests failed out of 5; 253.24 sec
```

The pin-region floor passes at **0.0513328**, aspect 25.2043, delivered 0.31517 mm
for 0.35 mm. Tight-bore contact resolves at 0.297528 mm, with 51/3083 active
pairs and 168009 elements. Fresh upstream `ctest --test-dir .../build -R mesh`
after rebuilding `polymesh_tests`: **50 passed, 1 skipped, 2 failed out of 53,
86.45 sec**. Test 266 still reports plate-hole **0.0917903713775 > 0.09** and
cylinder **0.3103285592707 > 0.20**. Test 265 retains the separate icecream-cone
exact-surface rounding failure **1.97178e-12 > 1e-12**. Thus the requested B
h=2.8/count/core-test acceptance is met, but the requested A acceptance is not.
All native builds and runs used the 10G systemd wrapper, never the live
`build-occt-linux` tree. No gate, formatter, full sweep, or host-daemon
termination was used. Throwaway probe programs/scripts were removed; JSON
measurement evidence is retained.

## 2026-09-12 — classify sub-resolution CAD rims as absorbed, not missing walls

The CC-006D LeafB/Bushing4/Pin `sim_mesh` refusal was reproduced at h=3.4,
order 1, from `geometry.step` extracted from a `/tmp` copy of the STEP+.
Solid 1 face 0 is the leaf's radius-14 cylindrical pad rim, not the press-fit
interface: area 1.01159283445 mm², bbox (920.75,-14,496)–(920.7615,14,524),
axial extent 0.0115 mm. Every boundary facet was already checked against every
CAD feature face, with centroid distance ≤0.3h and |normal dot|≥0.5; no owner
assignment or patch-area fraction was involved. Projection/sliver cleanup
absorbed this 11.5 µm rim while leaving sampled distance 0.002875 mm. Demanding
a separate aligned patch then incorrectly refused the whole mesh.

Upstream `geom/cad_topology.hpp/.cpp` now records the exact oriented intrinsic
face extent (excluding a plane's zero normal thickness). `pipeline/src/scene.cpp`
classifies an otherwise unresolved face as `feature_absorbed` only below
0.05×min(requested h, delivered fine lattice pitch), with the existing sampled
0.3h distance bound still satisfied. Notes name face, extent, area, scale and
distance. Size/quality floors and volume/distance/normal tolerances are unchanged.
The CAD caller already meshes the bodies independently and forwards the notes;
`core/sim/mesher.cpp` needed no additional change. Upstream `docs/cli.md`
documents absorption distinctly from an aligned delivered patch.

Real-file results (linear tetrahedra; edge values min/median/max in mm):

| h | Elements | edge_mm | size_band |
|---|---:|---|---|
| 3.4 | 95,468 | 0.104771269 / 2.382688603 / 6.132101358 | floor 0, reason none |
| 6 | 20,675 | 0.706677288 / 4.237495722 / 12.116276690 | floor 0, reason none |

At h=3.4, solid 1 face 0 is absorbed (scale 0.0595672 mm). At h=6, faces
0,30,31 are absorbed (scale 0.10591 mm); faces 30/31 have extent 0.0115063 mm
and area 0.13990669 mm² each. All other feature faces retain aligned patches.
Full smoke summaries remain in `/tmp/feature_patch_fixed_h3.4_summary.json` and
`/tmp/feature_patch_fixed_h6_summary.json`; the original refusal is preserved in
`/tmp/feature_patch_prefixed_refusal.json` (this tree's pre-fix volume error was
0.003355, with the existing uncommitted sliver changes).

The two new `core/tests/test_mesh_mechanism.cpp` cases pass (12 assertions):
a zero-gap sleeve/shaft at h=0.35R delivers both cylindrical boundary patches,
each with >50% exact wall area, and a micron-height pad is explicitly absorbed.
The archived box-hole parity case skips because its corpus fixture is absent.
A self-contained replacement witness in upstream `test_mixed_fill.cpp`, using
`tests/fixtures/parts/missing_bore_guard.brep` (40×40×1 plate, R1.5 bore), passes
four assertions: the coarse hybrid fill still refuses the missing aligned bore
despite only 0.00443747 volume relative error.

```text
1/5 mesh              Passed 113.29 sec
2/5 mesh_quality      Passed   3.64 sec
3/5 mesher_advanced   Passed 102.35 sec
4/5 mesh_mechanism    Passed  16.60 sec
5/5 mesh_sector       Passed  25.76 sec
100% tests passed, 0 tests failed out of 5; 261.64 sec
```

Upstream `ctest -R mesh`: 50 passed, 1 skipped, 2 known failures out of 53,
83.49 sec. The unchanged failures are `boundary nodes land on the exact BRep
for every CAD mesher` and `sharp BRep edges are reproduced by mesh feature
segments`. Builds used ninja -j2 with 12G cgroups in `core/build-fix` and the
approved upstream build; native runs used 10G cgroups. The mission's
`core/build-occt-linux` tree, source STEP+, host daemon and user changes were
not touched.

## 2026-09-09 — live mesh probe counters and counter-based inactivity

The worker was heartbeating the last native phase's zero element count while
`volume_mesh` spent minutes inside a healthy fill. Polyhedral-FEA now accepts
an optional `FillOptions.on_progress(const FillProgress&)` observer and polls
actual lattice, LEB, projection and quality work through `mesh/fill_progress.hpp`;
OpenMP classification remains parallel and only its calling thread publishes.
CAD `core/sim/mesher.cpp` forwards live probe size/count/work/sub-phase through
`core/sim/progress.h` and the existing FEA/C event sink; `core/api/libchudware.cpp`
also brackets mesh-only calls. Local interface repair, pipeline exterior/ship
checks and quadratic boundary projection report their own work. The first
real-file smoke exposed a 212-second false inactivity age after fill, where
quadratic projection reused the last collapse counter; the final implementation
instruments those actual midpoint and quality loops rather than refreshing the
clock on unchanged events.

`services/sim/job_worker.py` preserves native probes in atomic partial/log
snapshots and retains the existing 30-second fallback heartbeat. `jobs.py`
computes elapsed time since actual counter/phase advancement at every poll;
unchanged callbacks and heartbeats do not refresh it. MCP `get_job` and both
`sim_results` modes expose `progress.last_advance_s` and `stalled` (running and
strictly over 120 seconds). Simulation tool guidance says to cancel for
inactivity only when stalled, not because a probe is long.
`services/sim/test_jobs.py` now observes a real exported box's durable positive
background-lattice record before solve completion, and
`services/mcp/test_cad_sim_tools.py` exercises the injected-clock threshold,
silent-worker aging, unchanged callbacks, cell-only advancement and terminal
behavior. Obsolete assertions pinning cancellation-guidance wording were removed.
The schema is appended to `contracts/worker_rpc.md`.

Verification used only `core/build-fix`, ninja `-j2`, and 10G systemd cgroups:

```text
governor  Passed   0.01 sec
mesh      Passed 102.98 sec
100% tests passed, 0 tests failed out of 2
services/sim/test_jobs.py + services/mcp/test_cad_sim_tools.py:
138 passed in 234.26s
```

The requested WHEEL-LATERAL geometry directory was empty. The approved released
source `Engineering/CAD_Merged_v4/CD-210-01C_Wheel_Slice_t4p7625_w60_2026-09-07.step+`
(SHA256 `3d1ee64e26afd26c81620f5b0cbde482e8f2c860125d082f16b14fa7d1442086`)
was copied to `/tmp/probe-heartbeat-wheel`, opened locally and exported to STEP.
The final `sim_mesh` run delivered **844,478 tet10 / 1,288,768 nodes at h=2.84 mm**
without coarsening. `provenance.governor` reports `requested_size_admitted`,
`measured_probe:{h:2.84,elems:844478,elapsed_s:1671.573936688,rss_gb:0.8564262390136719}`,
and `estimate_gb:93.63321647699922`. This is mesh-only success, not solve-memory
admission. Both real-file runs delivered identical mesh counts and quality.

The final log contains 512 real progress records; maximum observed counter age
was 8 seconds, so no healthy interval would be labelled stalled. Instrumented
fill/projection loops published about every four seconds (largest such gap
4.055 seconds); the largest whole-pipeline gap was 6.323 seconds at the
post-fill region-mapping/validation boundary. Individual synchronous CAD calls,
allocation and sorts remain non-interruptible, not fabricated work.
`worker.log`, `partial.json`, `result.json`, `cadence.json` and the first-run
comparison artifacts remain under `/tmp/probe-heartbeat-wheel`; the throwaway
driver was removed. The live host daemon, mission build tree and source STEP+
were untouched. The CAD build depends on the corresponding upstream FillOptions
API/header changes; fetched-upstream release pins must include those changes.

## 2026-09-09 — source-faithful CAD presentation and explicit model-integrity evidence

Assembly compilation, AICM material regions, native rendering, both viewport loaders,
and host simulation uploads now preserve stored source appearance instead of assigning
ordinal/rainbow colors; unassigned geometry stays neutral. The native integrity query
and MCP `inspect_model_integrity` enumerate every world solid and non-solid, preserve
source/child identities, report exact pair failures and nearest-distance unknowns, and
separate geometric proximity from fastening or structural acceptance. Imported mixed
geometry and nested source failures are no longer silently discarded; invalid B-reps
always produce an `eval_check` finding. `asm_edit_component.source` now actually
replaces a path/embedded source while retaining component identity, placement and mate
references; save/reopen/export regressions defend that contract. Animation typography
uses measured wrapped text and reserved canvas space; CAD footer height follows its
content. Full-orbit projected bounds replace the overconservative sphere fit, with a
bounded camera-fit proxy preventing the native margin cap from pumping the zoom on
end-on slender parts. The native reproduction changed from approximately 0.31034 to
10 pixels/mm under the old algorithm; the regression now verifies constant native
scale and unclipped world-bound corners, including polar views. Playback now grounds
components and suppresses mates only on its detached render copy, so re-solving cannot
hold or distort already-solved poses. Native motion transports the solved first-sample
component placement rather than assuming the requested start equals the authored pose;
steering uses its reported component placements directly. A live translated-hinge
regression checks rendered world bounds at nonzero start/end angles and verifies the
source document remains unchanged. Explosion selection is captured before grounding;
a three-component render regression verifies originally fixed components stay put.
Focused verification: 108/108 native tests and 20/20 animation tests passed.
The built host also opened the repaired ME-490 rover in a 1920x1080 Xvfb session;
the actual viewport capture was inspected and the protected source SHA256 remained
unchanged (`e0cb30b48162`). Diagnostic drivers and captures were removed afterward.
The Linux gate passed: native/host/WASM builds, 108 native tests, service suites,
hostd/web parity, all 13 golden-demo legs and host screenshots. After the final
playback cutover, the isolated MCP rerun passed 3,021 tests (22 skipped, 1 xfailed).
Opt-in gate stages were not enabled.
Curated integrity-tool guidance
and generated host tool tables are updated; the metrology immutability test now
checks that classification cannot change instead of pinning a Python-version-specific
frozen-slot assignment exception. ME-490 repair generators, native revisions and
geometric evidence remain in the project workspace, not this code repository.

## 2026-09-10 — owned measurements, shared analysis windows and synchronized motion

Measure now preserves body ownership through asynchronous picking, native queries,
MCP annotation and viewport emphasis; selection/revision changes invalidate stale
readings. Section properties integrate all intersected world solids, including
nested placements and holes, and report failed/open intersections rather than
fabricating an empty result. Measure, Mass/Section Properties, BOM, Cut List and
Design Table share resizable, DPI-aware placement with off-screen recovery. The
native-only GUI smoke exercised two-body measurement, separate and combined
sections, scrolling, narrow-window recovery and light/dark tool windows; BOM
launch remained blocked by the isolated profile's read-only entitlement.
Motion accepts synchronized drivers, preserves component identities in complete
sampled-interference results, invalidates results when checking is disabled, and
jumps to collision poses with visible highlighting. The two-driver native/GUI
smoke checked all nine poses and found overlaps of 6.8966377 and 96 mm³ at the
last two samples; it is not evidence about the rover or continuous clearance.
Sketch diagnostics now project only active Jacobian columns and validate
coincidence proposals together before the conflict fallback: the measured
24-spline construction fell from 0.581 s to 0.0145 s with the same 24 constraints
and enclosed area; all 39 focused sketch/autoconstraint cases passed.
Primary changes span `core/doc`, `core/mate`, `core/sketch`, their regression
tests, `host/src/ui/{evaluate_dialogs,layout,sim_manager,viewport_panel}`,
selection/mirror plumbing, MCP measurement/motion adapters and shared GUI lint.
STEP+ now admits STORED display meshes within the remaining 256 MiB archive budget,
after reserving required and unknown payloads; their existing limits are unchanged.
Seven archive regression cases passed. Native assembly consumers reopened and
resaved both 68 MB and 140 MB mesh-cache inputs with unchanged body counts, volumes
and source bytes. This is archive preservation evidence, not geometry acceptance.
ME-490 mechanism generators and geometry verification remain in the external
project workspace. Full repository delivery-gate verification is pending.

Native `doc_apply_ops` now honors `with_tess=false` through JSON dispatch and part,
assembly, read-only and rejected-batch responses. It retains edit/solve diagnostics
and avoids assembly response mesh construction/publication. Normal later display
requests still expose the committed geometry. The persisted geometry epoch is `/2`:
pre-fix primitive placements could leave five near-coplanar hub patches where the
current compiler produces one face, so old snapshots cannot resolve current selectors.
No selector tolerance was relaxed. Three operation cases, one stale-topology case
and seven archive cases passed (4,485 assertions). The real ME-490 hub probe now
converges at zero DOF, rejects malformed selectors, exercises mesh opt-out/default
restoration and leaves the source archive byte-identical. The complete proposed
mechanism and presentation still require their separate geometry/motion verification.

Assembly diagnostics now request rank-only `System::degrees_of_freedom` instead
of unused chronological row attribution. Mate-level classification retains the
same absolute-SVD threshold and chronological accepted rows, but factors only
connected variable groups touched by the next mate. All 36 numeric/mate/corpus
cases passed (1,792 assertions). The actual 275-mate ME-490 operation converged
in 10.2 s (271 active mates `ok`, four intentionally suppressed), versus an
interrupted pre-fix call still in diagnostics after 398.4 s. This does not replace
the mechanism's separate solid census, round-trip or sampled-interference checks.
The delivery gate also found desktop-only depth-clear APIs in concurrently edited
reverse-engineering previews. Both preview sites now use OpenGL/GLES
`glClearBufferfv`, without changing global depth-clear state. Native preview smoke
passed all three views without GL errors; WASM build and 15/15 asset parity passed.

## 2026-09-10 — native live/video Gaussian reverse engineering

The Source lane now performs real environment and stationary-object Gaussian
reconstruction: canonical photo/video ingest, continuous native camera keyframes,
COLMAP registration/undistortion and headless Apache-2.0 Brush v0.3.0 training.
`scripts/provision_reveng.py` installs a checksum-pinned binary or checks out its
pinned editable source; `BRUSH_BIN` remains the local-fork seam. Gaussian references
stay in reconstruction units. Measured CAD remains a separate OpenMVS workflow
with scale/dimension gates; the fabricated Brush-to-mesh path is removed.

`services/reveng/gaussian_jobs.py` owns atomic sidecars, immutable exports, verified
Linux process groups, GPU reservation, recovery and cancellation. Live capture
builds successive frozen batches while the camera continues, then finalizes all
accepted views. This is not video-rate SLAM, optimizer resume, moving-object
reconstruction or automatic object segmentation. Explicit 3D object crop/reset
preserves the original cloud. Worker shutdown and pending-start finalize are
covered; session identity fences prevent cross-session frame/artifact delivery.
Source and compiled child commands have explicit dispatcher routes, and Linux
packaging includes pycolmap. A compiled release artifact was not built in this work.

The native viewer now projects quaternion covariance and SH0–3, shares the
2,000,000-Gaussian budget with training, uses a matching registered capture camera,
and supports orbit/pan/zoom/capture-view reset. Full-rectangle projection plus
visible scissoring fixes partially clipped previews. Matching Brush's frustum
Jacobian clamp removed a reproduced offscreen smear: 28,606 stray pixels before,
zero after. The smoke retains that behavioral probe alongside anisotropy and
two real render views; no GL errors on a 90,638-Gaussian, 10,000-iteration artifact.

Verification: native build; 250 focused Python tests passed, 20 skipped; 22 Linux
packaging tests passed using system Python 3.14; renderer CTest passed; changed-scope
GUI lint clean. Real NVIDIA training registered 24/24 imported video views. Native
V4L2 loopback capture produced six progressive previews and a 2,000-step final pass;
this exercised the camera path, not a physical camera. The live-object smoke grew
8→14 views, finalized, cropped/restored, then cancelled an actual Brush process
group and verified its descendants exited. Pausing the owned worker before native
Start/Finalize proved the pending finish request survives startup. Native gestures
changed the image without scrolling its parent, and Capture view restored it
pixel-for-pixel. New-session selection survived rail refresh.

Local evidence is under `scratch/reveng-overhaul-smoke/`: `proof.json`,
`native-quality-proof.json`, `pending-start-proof.json`,
`native-navigation-proof.json`, and native PNG captures. Source-dispatch smoke
completed real SfM and Brush training; this is not Nuitka/AppImage execution proof.
Contract additions are append-only in `contracts/reveng.md` §13; workspace and
viewport skills carry the new file map and invariants.

## 2026-09-11 — exact-world integrity evidence reuse

`core/doc/session.cpp` retains only the preceding complete integrity query's
valid-solid metrics and completed distance/Common evidence. Identity is retained
immutable TShape, orientation and exact absolute placement; no rounded transform,
bounding-box signature or inferred rigid-group equivalence is admitted. Common
is non-destructive. Source completeness, ownership, graph policy and nearest
reporting remain fresh; failures cannot seed reuse. Entry caps drop memoization
without dropping checks. `contracts/worker_rpc.md` documents the additive counters.

The native interference regression suite passed, including source replacement,
movement, nearest-only evidence promotion and timeout recovery. A read-only run
on the 1,191-solid proposed ME-490 mechanism checked all 4,324 candidates at 1 mm
proximity: 500.850 s cold, 7.793 s warm. The warm query reused all 1,191 metrics and
4,324 completed pairs; geometric/provenance/graph evidence compared identically.
The model remained byte-identical. Full replies and runtime identity are in
`ME-490/Engineering/Verification/me490_integrity_cache_probe.json` and its sibling
directory. This proves unchanged-query reuse, not moving-pose clearance or safety.

Motion pose evidence now publishes the hinge line in the posed reference's world
frame while retaining reference-local validation math. A tilted-reference case
checks the corresponding world-coordinate invariant. Conflicting poses must
retain explicit incomplete/null interference, rather than relying on an absent
optional collision list. The focused MCP motion/measurement suite passed all
150 cases. The obsolete stale-SID message-wording test was removed; native and
MCP invalid-selector refusal coverage remains.

## 2026-09-11 — sparse damped least-squares mechanism steps

`core/solver/nsolve.cpp` builds large block-sparse Jacobians directly and solves
the augmented damped least-squares system with sparse QR. This avoids both dense
normal-matrix multiplication and loss of weak rotation directions from squaring
the condition number. Rank-deficient QR falls back to the original minimum-norm
normal solve, not a QR basic solution. Dense oracle cases, damping, step caps,
residual acceptance and deadlines remain unchanged.

The mixed-scale coordinate regression fails against the preserved pre-fix native
library (residual 0.0340349) and passes with augmented QR. All 13 numeric cases
pass, including free-coordinate preservation and equivalent equation grouping;
the 23 mate cases and two differential corpus cases also passed. Actual rover
motion/interference acceptance remains separately gated by its native pose proof.

## 2026-09-11 — moving-reference joint derivatives and stable locks

The real ME-490 first driven pose exposed derivatives differing from independent
central differences by up to 1397. `core/mate/mate.cpp` now differentiates moving
perpendicular bases, point-to-axis distance, signed-angle reference directions,
and the full relative rigid transform of a lock. Lock rest placement comes from
authored frames, never from a perturbed warm-start seed.

Both new behavioral regressions fail on the preserved preceding library: an
offset locked child drifts 0.911043 mm at zero drive and fails at 20 degrees; a
concentric slider fails at 15 degrees. All 25 mate cases pass after correction.
Before/after executable output is retained under
`ME-490/Engineering/Verification/me490_joint_derivative_probe/`.

## 2026-09-11 — persistent mate datums and advancing rover proof

`core/doc/session.cpp` binds engine-owned component reference frames before
assembly compilation can write solved placements back. `core/mate/mate.cpp`
uses those frames for virtual pins, angle references and lock attachments.
Scalar edits, rebuilding, native save/reopen and private motion snapshots retain
the same datum. This fixes the actual ME-490 -1.5-degree input-yoke request
silently becoming -3 degrees on the next compilation despite passing residuals.
The rotation logarithm also preserves near-pi signed axes; reversed alignment
remains effective when parallel face normals require a body-frame angle datum.

The damped solver now permits representable cost progress even when a mixed-unit
step norm is tiny. A high-damping regression fails on the preserved old library;
the corrected numeric suite passes 14 cases. Current focused validation passes
26 mate cases, two differential corpus cases, 10 native motion cases and 151 MCP
motion/measurement cases. Native save/reopen assertions respect canonical
six-decimal document serialization while rebuilt motion is checked at solver
precision with a dimensioned offset-lever error bound.

The actual saved rover passes poses 0 and 1, including independent Cardan,
plunge, pedal and steering transform comparisons, without changing its source.
Evidence: `ME-490/Engineering/Verification/me490_advancing_pose_probe.json`;
the preceding doubled-angle failure is retained in the sibling
`me490_advancing_pose_probe_reference_failed` directory and JSON record.
This is only initial advancing kinematics; the full sampled interference sweep
and presentation remain separately gated.

The retained native-library comparison in
`me490_persistent_reference_regression.json` records 66 failed assertions before
the binding fix and all 513 passing afterward for the same offset-revolute
rebuild/save-reopen case. `me490_reference_rotation_regression.json` separately
records failing-before/passing-after near-pi and reversed-alignment behavior.
Both comparison executables link their named preserved native libraries under
isolated library search paths.

The final Linux integration gate passed all enabled stages, including native,
host and WASM builds, CTest, the MCP suite (3013 passed, 26 skipped, one expected
failure), the sensor-node golden demo and GUI smoke. The separate native GUI
motion scenario produced 24 frames, applied frame 12 and exited successfully.
Gate logs and the GUI evidence are retained under
`ME-490/Engineering/Verification/me490_reference_final_gate` and
`me490_motion_gui_smoke`. These program checks do not establish rover clearance.

## 2026-09-11 — lossless joint persistence and normal motion smoke

The actual ME-490 advance at pose 12 exposed a persistence defect, not solver
stagnation. Six-decimal JSON serialization changed the oblique steering axis by
3.6637e-7 radians, making the angle joint inconsistent with its two spherical
pivots. The captured Jacobian and native rotation reproduce the failed angular
residual exactly; direct-J corrections cannot solve those contradictory inputs.
Original native requests, full-precision declarations and numerical captures are
retained in `ME-490/Engineering/Verification/me490_pose12_probe`.

Native editable containers and Python canonical history now preserve round-trip
numeric precision. Live-state snapshots retain the same values and hash as
clients; semantic CAD/ECAD diffs no longer hide small numeric changes. The lossy
helpers and their rounding-only tests are removed. Existing six-decimal files
remain readable, but lost declarations must be restored from original sources,
not compensated with a looser solver tolerance.

An independently reproduced minimum-norm defect is also corrected: the sparse
rank-deficient fallback solves the augmented Jacobian directly rather than its
normal matrix. A mixed-scale constrained system must still converge when an
unrelated coordinate is free, without moving that free coordinate.

The GUI motion smoke now waits for the RPC busy guard and verifies that a study
was actually created. The former daemon failure was premature scenario dispatch,
not production snapshot routing; production source matching remains unchanged.

Focused verification passes `nsolve`, `simulate_motion` and `sim_cache` in both
native build trees, plus 68 Python canonical/diff/live-state cases. A real native
three-sample oblique-pivot sweep succeeds before save, after reopen, and after
PLM history restoration at unchanged tolerances. The save/reopen smoke's maximum
pivot shift is 3.553e-15 mm. Both daemon and in-process GUI scenarios produce 24
samples and apply midpoint 12; their logs and images are retained under
`me490_motion_gui_smoke/{daemon_fixed,direct_fixed}`. These checks do not establish
the rover's full sampled motion or clearance.

The regenerated 1191-solid rover now passes the formerly failing pose 12:
3.333298081997782e-10 residual against the unchanged
1.4375923640545746e-8 tolerance, including both exact-current-value frames.
`me490_pose12_lossless.json` binds that result to the regenerated source and
pinned native runtime. The full 241-pose solve/interference run remains separate.

The complete Linux gate also passes (native/host/WASM builds, full CTest, hostd,
web parity, packaging, service/tool/training suites and GUI shot smokes).
MCP reports 2994 passed, 26 skipped and one expected failure; PLM reports 392
passed. The golden-demo gate reports 13 passed, zero failed and one skipped.
Opt-in AppImage, V&V, modeling and benchmark stages were not enabled. Gate logs,
demo summary and smoke images are preserved in
`ME-490/Engineering/Verification/me490_precision_final_gate`.

## 2026-09-11 — preserve the authored hinge orientation

The full rover sweep passed independent comparisons through sample 64, then
sample 65 exposed a separate native hinge defect: two aligned virtual pins had
axis dot products of -1 while their cross-product residuals were effectively
zero. The native solver accepted a disconnected half-turn branch in the keyed
front-right output spindle and a rear cross; the independent Cardan witness
remained continuous and correctly refused the result.

`core/mate/mate.cpp::hinge` now uses oriented normalized-vector differences and
their analytic derivatives for both bodies. Five physical constraints still
leave hinge rotation free; limits, reference frames and tolerances are unchanged.
Direction units are clarified in `mate.h` and `contracts/doc_ops.md`. Two
behavioral cases in `test_mate.cpp` fail against the pre-fix mate archive and pass
against the corrected implementation: opposite-hemisphere correction with free
spin, and refusal of a locked antiparallel contradiction. The targeted OCCT
`mate`, `simulate_motion` and `nsolve` suites pass.

Evidence lives in `ME-490/Engineering/Verification/me490_pose65_probe`.
An attempted native64 checkpoint restoration failed its exact-placement guard
and was not used as a continuation. The full 241-pose verification restarted
from the original saved model with no skipped samples or prescribed analytic
poses; its eventual motion/interference result remains separate from these
focused tests.

The direct native build's same three suites also pass, followed by the full
Linux gate (native/host/WASM, full CTest, hostd, web parity, packaging,
service/tool/training and GUI smokes). All 12 golden-demo legs pass; the nested
firmware emulator remains an explicitly skipped timeout. Opt-in benchmark,
V&V, modeling and AppImage stages were not enabled. Full logs and demo counts
are retained under `ME-490/Engineering/Verification/me490_oriented_final_gate`.
The corrected runtime is staged for normal daemon and in-process launches.

## 2026-09-11 — complete the oriented-axis cutover

The repaired sequential rover run passed sample 65 (maximum affected transform
translation discrepancy 9.095e-13 mm, rotation-entry discrepancy 1.776e-15), then
sample 74 exposed the same antiparallel zero in the remaining concentric and
explicit-angle pin residuals. The pedal angle pin m231 had axis dot -1 with
cross norm 1.225e-16 despite a passing native residual. The full independent
named-transform comparison rejected the halfshaft and pedal branches.

Both remaining two-component cross-product paths now use oriented normalized
direction differences and analytic derivatives. Coincident axis/axis dispatch
shares the corrected concentric implementation instead of duplicating its old
cross-product defect; point coincidence stays unchanged. The obsolete cross
helpers are removed. Coaxial slide/spin freedom, angle phase conventions,
reference frames, limits, model geometry and solve tolerances are unchanged.
`mate.h` and `contracts/doc_ops.md` describe the residuals and alignment rule.

Two behavioral regressions fail against the previous native mate archive and
pass after the cutover: coaxial aligned/anti branch correction with slide/spin
freedom, and a prescribed line angle refusing the reverse pin branch. Full
`mate`, `simulate_motion` and `nsolve` suites pass in both native build trees.
Evidence is retained in `ME-490/Engineering/Verification/me490_pose74_probe`.
The full rover verification again starts from zero; no incomplete earlier
trajectory is used as motion or clearance evidence.

The complete integration gate exited zero with native/host/WASM, full CTest,
hostd, web parity, packaging and GUI smokes passing. It recorded one MCP
multiuser-event test failure; that original output is preserved. The isolated
four-test file then passed, followed by the complete MCP directory with 3019
passed, 26 skipped and one expected failure. No unrelated test or production
code was changed to obtain that result. Gate and followup evidence is archived
under `ME-490/Engineering/Verification/me490_all_axes_final_gate`; its pinned
runtime matches the staged normal-launch copies. This is a development build,
not a tagged release or rover clearance approval.

## 2026-09-11 — mobile browser workspace input and layout

`host/wasm/shell.html` and `host/src/app.cpp` now size the WASM GUI in CSS pixels,
with GLFW alone owning the DPR-scaled drawing buffer. A safe-area/visualViewport
surface and reserved 56px HUD keep controls above the keyboard/browser chrome.
`title_bar.cpp`, `widgets/ribbon.cpp`, `layout.cpp`, and `copilot/panel.cpp` provide
compact workspace/command menus and independently toggled, bounded Files/Chat
drawers. The shell sends actual GLFW mouse events, cancels presses when a touch
becomes a scroll or pinch, and supplies a trusted-tap textarea keyboard bridge.
UTF-8/composition, control-key ordering, first-input focus, and pasted newlines
use ImGui's existing input queue. `viewport_panel.cpp` owns viewport-hit-tested
two-finger pan/zoom instead of the former global-window touch tracker.

Native and WASM builds pass; GUI lint is clean and all 15 preloaded assets match.
An isolated real gateway/PostgreSQL/hostd smoke opened a saved box through the
touch file picker, created a new document through File, exercised orbit/pinch,
Files/Chat and command scrolling, and verified first-focus text, backspace,
multiline paste without submission, and a single IME commit. Chromium touch
viewports covered 360/390/430px phones, 844x390 landscape, a 390x500 reduced
viewport, 1024x768 tablet and 1365x768 desktop, with DPR 1/2/3 and no page overflow.
Physical iOS/Safari keyboard and notch behavior remain device-verification items.
An initial direct-RPC unsaved-document injection hit a WASM worker
exception; saved-file loading and normal UI document creation passed after reload.

The existing production gate also required a sandbox webhook despite the
provisioner's deliberate live-only endpoint policy. The sandbox test now checks
the declared environment contract and rejects enabled sandbox delivery to a
live-only URL. No Stripe configuration or live billing changed. The real
sandbox endpoint check and seven provisioning tests pass.

## 2026-09-11 — unblock the mobile release's public mesher dependency

The first mobile promotion passed Stripe/source audits but stopped compiling
`core/sim/mesher.cpp`: the deployed `9554579` Polyhedral-FEA pin lacked
`mesh/fill_progress.hpp`. The local `CHUDWARE_POLYMESH_ROOT` override had masked
that mismatch. Public `master` contains a separate 22-commit product-release
sequence after the same baseline, so it was not overwritten.

With explicit approval to publish the required upstream changes, reviewed the
seven-commit source delta and published a sanitized squash as
`Hunter-124/Polyhedral-FEA` branch `cad-fill-progress`, commit
`ef464dc1236666770985c75a8fb50a512a463f62`. Private project details were removed
from commentary and the private commit history was not published. The private
working checkout was not changed. CAD now pins this immutable public revision.

Release review reproduced and repaired SparseLU's premature `info()` query,
the unused CHOLMOD lambda in strict disabled builds, and an early refinement
ceiling that bypassed bounded automatic coarsening. Both runtime regressions
failed before their fixes and pass afterward. Five mesh-budget cases, eight
focused solver/refinement/geometry cases, and the isolated forced-LU cantilever
pass. Fill observation preserves exact geometry, runs callbacks synchronously,
propagates cancellation, and restores nested observer scope. The real CLI solved
a STEP plate with 31,808 quadratic tets and 146,205 free DOFs through CHOLMOD;
all numeric arrays in the 49,176-point VTU are finite.

The CAD configure log now confirms `PINNED FETCH` from the public repository,
with no local-source override. The engine/daemon and selected tests build;
`mesh`, `mesh_quality`, `mesher_advanced`, and `tet10_thin_wall` all pass
(334 seconds total). All nine `scripts/check.py` source gates pass.

The queued promotion `34660550926` was then cancelled when an older Linux gate's
completion scheduled `34666614854`. GitHub concurrency retains one pending run,
not a FIFO queue; the redundant completion event displaced the newer master push.
Removed the obsolete `workflow_run` trigger and its payload-dependent branches.
Master pushes and explicit manual requests remain the only production triggers;
active promotions, the Stripe gate, source audits, builds, verification, and
rollback retain their existing protection. The workflow policy reports zero
errors (two unrelated advisories), and all ten embedded shell steps pass
`bash -n` using literal YAML bodies. Production promotion is tracked separately;
a successful workflow with a skipped promotion is not deployment evidence.

Run `34671349919` then passed the production native/WASM build and validated joint
`d1fff6d004cf`, but refused promotion after one browser bridge remained for 600
seconds. The live release/link and running joint remained
`20260905T043941Z-bcb447fb22a8-42710` / `bcb447fb22a8…`. Fixed
`install_webd.sh` to report drain/authentication refusals as exit 4, matching the
updater/CI contract, and install service definitions only after idle is proven.
A full-installer smoke using real native, Chudcode, and browser artifacts with an
isolated control-plane fixture reproduced exit 1 plus changed unit files before
the fix. Busy/unreachable cases now exit 4 with units, release link, and running
joint unchanged; an injected reload failure after idle admission still exits 1.

The rollback alert was also misleading: its all-platform `check-lock` rejected
otherwise valid retained Linux releases because ARM download pins were absent.
Added `pin_chudcode.py verify-notices`; rollback now verifies notice hashes
independently and still verifies the actual Linux binary against its own lock.
The global publication check remains strict. Read-only execution of the actual
candidate validator on the production host rejected the current release before
the fix and accepted all three retained releases afterward, without changing any
release files or restarting services. The targeted artifact/deployment suite
passes 27 cases and all nine source gates pass. Removed source-text/wording and
incidental checked-in pin assertions rather than repinning brittle tests.

Promotion `34691023676` deployed `c0fbc8628` successfully, including running-daemon
verification. Live touch-emulated Chrome then exposed a separate startup abort:
gateway logs recorded unauthorized document requests while another account was
connected. Registry notifications bypassed response authorization, exposing a
foreign `doc.opened` title and triggering an unauthorized mirror request. The
gateway now applies existing document capabilities to nested notification IDs.
The regression failed with a leaked private title before the fix and passes
with authorized updates delivered and foreign events withheld.

`engine_backend.cpp` also treated every error code as a string, although the
gateway emits numeric JSON-RPC codes. A real IPC probe reproduced type_error.302;
the fixed backend preserves the numeric refusal without throwing, and both
loading retries and read-only upgrades still succeed. No exception-catching flag
or authorization relaxation was introduced.

Production logs additionally showed EROFS when creating a new account workspace.
The production gateway unit now grants its existing default CAD workspace root
write access, not all of Documents. An actual systemd sandbox probe reproduced
EROFS before the change; afterward workspace writes succeed while an unrelated
Documents directory remains read-only. Staging's write grants are unchanged.

## 2026-09-11 — independent body material persistence

Fixed a production Chudware session reproduction where `set_material("A36",
body="b2")` recolored only b2 but silently replaced the entire part's physical
material. `core/doc/session.cpp` now stores body overrides independently of
the part default; `core/doc/validate.cpp` validates their shape and body keys;
`services/mcp/cad_doc_tools.py` forwards the selected body and checks its
presence through the read-only world-body inventory. Whole-part assignment
clears overrides; appearance-only changes preserve identity. The regression
in `test_cad_tools.py` covers save/reopen, absent-body refusal without revision
change, appearance edits, and whole-part replacement. Removed a preset-helper
echo test and an incidental error-wording assertion. Native/GUI builds and
targeted material tests pass. A resumed compiled Chudcode/hostd session
independently saved, closed, and reopened a two-body aluminum/steel part;
ME-490's `Engineering/Verification/me490_harness_material_verified.json`
retains the actual calls and source/output hashes. This verifies persisted
identity, not automatic mass/FEA material inheritance; those APIs retain
explicit analysis inputs. The additive operation contract is in `doc_ops.md`.

## 2026-09-11 — condition-qualified materials and metric bolt geometry

Added source- and product-condition-qualified material records for the ME-490
workflow. The 42CrMo4 hub's 70 mm flat-stock selection uses the supplier's
550 MPa minimum, not a thinner or smaller round-stock rating. Wood, printed
TPU, HDPE, natural POM and bearing bronze retain incomplete constitutive
markers rather than fabricated isotropic constants. Density-only mass queries
now work without stiffness/yield fields; isotropic FEA refuses those incomplete
models before meshing. Registry appearances preserve product-specific colors.

Replaced the metric hex-bolt head-height heuristic with nominal ISO 4017
values and made standard/options refusals explicit through native entrypoints.
SAE geometry remains marked approximate. A real production harness session
saved/reopened the selected material identities, measured a 10 mm TPU cube
at 1.22 g (or 2.7 g with explicit density override), observed the native FEA
refusal, and measured the corrected M12x40 BRep at 7.5 mm head height,
18 mm across flats and 47.5 mm total length. Its isolated four-component
hardware stack completed all six interference pairs without overlap;
no shaft engagement or manufacturing qualification is implied.
Evidence: ME-490 `Engineering/Verification/me490_harness_catalog_bolt_verified.json`.
Targeted material tests: 98 passed; native `parts` CTest passed.

## 2026-09-11 — assembly body-specific interface queries

An actual hub hardware harness session exposed `interface_check` failing with
“document has no bodies to query” on a valid assembly. The scoped `doc_query`
branch compiled the assembly as a part while the unscoped branch already used
world-placed export shapes. Reused that existing assembly collection in the
scoped branch; part behavior and out-of-range refusal remain unchanged.

Two real-engine regressions failed before the fix and passed afterward:
translated plate stacks at 0 mm and 1.5 mm hole-pattern offsets. All 14
interface tests pass. A fresh production Chudcode session independently
measured both components' world coordinates, observed the aligned pass and
four 1.5 mm mismatch findings, and verified body 3 refuses on a two-body
assembly. It also queried the actual hub/washer stack successfully.
Evidence: ME-490 `Engineering/Verification/me490_harness_assembly_query_verified.json`.

The combined material, fastener and assembly-query unit passed the complete
`scripts/gate.sh` run in `/tmp/chudware-gate-3344546`: native/GUI/WASM builds,
all 108 CTests, service/tool/training suites, hostd and web-parity probes,
FIN25 golden demo (13 pass, 0 fail, 1 skip), and host/slash screenshot smokes.
Optional V&V/modeling/benchmark/AppImage switches remained unset; those skips
are not claims of having exercised their extended suites. The gate's real
thermal board result was 133.3956 C. Separate project wheel/contact studies
are engineering work and are not software-gate strength approvals.

## 2026-09-11 — additional product-qualified material selections

Added thirteen sourced records for welded 6061, DH spring wire, C75S strip,
SUS420J2 rotor stock, 316 wire rope, Optibelt OMEGA 8M, RF38 lining, Bowden
housing, silicone grips, DP620NS adhesive, class-10 nuts, 24 mm/17-ply Finnish
birch plywood, and natural machined PA66. Missing constitutive data remains
missing. Linear mass is not bulk density; RF38 density is an upper bound;
nut class 10 is not bolt class 10.9. Product dimensions and stock-condition
limits accompany the records rather than implying that unlike CAD geometry
is the catalog product. The belt's minimum backside-idler diameter is 60 mm.

A production harness smoke saved/reopened all thirteen identities and their
appearances. Eight 10 mm cubes returned density-derived masses, five records
without bulk density refused mass calculation, and all thirteen incomplete
models refused native isotropic FEA before meshing. An explicit density
override changed a mass calculation without changing the saved identity.
Evidence: ME-490 `Engineering/Verification/me490_harness_additional_catalog_verified.json`.
Installed-registry material tests: 94 passed.

## 2026-09-11 — honored shell deadlines and bounded timeout cleanup

The registered `run_shell` tool silently clamped a requested long analysis to
900 seconds, then could leave child solvers running after killing only the
shell. Timeout output could also be bytes rather than JSON-safe text.
The tool now honors 1–86400 seconds, retains its 60-second default, and
refuses out-of-range deadlines before executing anything. POSIX commands
have an isolated process group; Windows commands use process-tree termination.
Timeout cleanup has a bounded drain and explicitly refuses completion when
an escaped descendant still owns the output pipe. Combined output is capped
in bytes and decoded consistently, including partial non-UTF-8 timeout output.

Three regressions failed before the repair; all 34 filesystem-tool tests
passed afterward. A real registered-tool call timed out at one second,
returned its Unicode/non-UTF-8 partial output as text with exit 124, and
prevented the child's delayed file write. Its throwaway probe was removed.
Evidence: ME-490 `Engineering/Verification/run_shell_timeout_probe_result.json`.
Windows process-tree termination was implemented but not runtime-tested here.
Interrupted engineering outputs are quarantined, not accepted as valid results.

## 2026-09-11 — classify solid skin boundaries rather than imprint ancestors

A native three-feature fork reproduction exposed two false free edges where
round stock overlaps tangent rectangular ears. Those imprints are internal
to closed faces; having one ancestor face does not make them skin boundaries.
Both `doc_eval_check` and `doc_eval_solid_health` now count oriented boundary
uses. Seams contribute two uses; embedded INTERNAL/EXTERNAL occurrences do
not contribute skin boundaries or hide a genuine single-use boundary.
Degenerate-edge handling and independent BRep/orientation/self-intersection
checks remain in force.

The reproduction failed four assertions before the fix and passes afterward.
All 22 native interference/health tests pass (331 assertions), including
open surfaces, invalid orientation, missing assembly sources, placed bodies
and distinct touching solids. This corrects the health classifier, not the
geometry or a structural-strength result.

Fresh production-harness verification (`ME-490/Engineering/Verification/
me490_harness_native_health_accepted.json`) found all 252 bodies in 88 drivetrain
clones sound. It separately retained the two genuinely self-intersecting steering
uprights among 19 bodies; the classifier repair did not waive those failures.

## 2026-09-12 — bounded native ownership and exact selected-pair checks

The watchdog now reserves the short deadline for ping/test-stall probes.
Document rebuilds, preflights, undo/redo and lock-taking native calls use the
bounded long deadline (3600 seconds by default). This is not durable background
modeling or cancellable OCCT execution. `DaemonSessionLost` distinguishes lost
established write/reply delivery from an initial pre-dispatch startup failure.
Ambiguous delivery reports `engine_connection_lost`, an unknown outcome and
`retryable:false`; it is not replayed or routed through the legacy engine.
The next explicit request may reconnect. Rebind documents from their saved
sources rather than reusing ordinal clone IDs after session loss. Checkpoint,
preflight, rollback and save paths preserve that refusal instead of manufacturing
a success or closing a potentially reused clone ID.

The registered `check_interference(members=[...])` now exposes the existing native
exact-name pair scope. Unknown or malformed selections are refused, not silently
reduced to fewer bodies. Reports identify selected versus total membership;
`complete` applies only to that scope. Only a complete document-wide check clears
transform-placement warnings. Three invalid-selection regressions failed before
the fix; the complete native interference suite passes 22 cases/338 assertions.
Four registered-tool tests also pass, including saved/reopened placement warnings
after a partial check and their removal only after the full exact check.

The standalone watchdog check and 85 transport/modeling-loss/preflight/filesystem
tests passed. A retained project correction was explicitly saved/reopened and
hash-archived before cancelling its redundant document-wide read-only query;
its continuation uses the scoped production tool, not an inferred query result.

## 2026-09-12 — bounded interface refinement and explicit native ICCG

`core/sim/mesher.cpp` materializes the centroid Eigen vector instead of returning
an expression referencing a dead local. Centroid grading stops at its computed
transition depth rather than marking the distant background once the requested
size has saturated. Boundary owners, conforming closure, delivered size and
quality floors remain enforced. Reports distinguish transition and outside-region
centroid marks from closure. The regression exposed 4237/3810 outside marks before
the bound; afterward both are zero and distant coarse cells remain coarse.
The full advanced mesher suite passes 10 cases/213 assertions.

`CHUDWARE_FEA_SOLVER=iccg` explicitly selects guarded incomplete-Cholesky PCG for
linear static/frictionless contact; direct solving remains the default and is
never an automatic fallback. PCG retains a fresh true-residual gate of 1e-10,
bounded iterations and cancellation checks. Every changed active-set matrix
refreshes its numerical preconditioner. Nonfinite/breakdown/stagnation/exhaustion
paths refuse. Plasticity is unsupported, and iterative convergence is not a
positive-definiteness certificate for buckling prestress or structural safety.

Iterative mesh admission skips only the direct-factor count model, not mesh
allocation limits. Solver admission uses the actual reduced sparse pattern and
includes matrix/preconditioner/workspace/recovery storage and process RSS.
Provenance records per-step iterations/residuals and current/maximum projected
memory. Mesh, prepared-system and outer static-result caches include solver
policy; the ICCG iteration limit cannot be bypassed by a cached successful result.
The iterative path does not reuse a direct-factor-only prepared cache.
Six ICCG cases pass 1978 assertions; complete contact and simulation-cache CTests
also pass. These are software proofs, not a solved ME-490 hub contact study.

The staged production API also passed an isolated synthetic 60×10×10 mm bar
smoke: 8991 DOFs, 282 ICCG iterations and a fresh relative residual of
6.651358227687084e-11 against the unchanged 1e-10 gate. Provenance and native
setup/result/VTU hashes are retained in
`me490_harness_iccg_native_smoke.json`. This is an iterative API smoke, not a
reduced-load substitute for the unchanged five-body 765 Nm hub contact study.

## 2026-09-12 — sourced housing and independently checked external membrane

Added the distinct Jagwire BHL100 CGX-SL 5 mm brake-housing identity. Its steel
coil, lubricated liner and black jacket are a composite envelope, not a solid
steel cable. Missing bulk density and constitutive properties stay missing.
The sourced material resolver suite passes 94 tests; physical selection,
cross-sections and native refusal/persistence evidence remain project artifacts.

ME-490's project-local `Engineering/Analysis` now contains an explicitly external
scikit-fem quadratic isoparametric membrane backend with SciPy CG/PyAMG.
It stores reduced stiffness plus constrained reaction rows, enforces finite
cgroup memory admission and preserves raw integration-point fields. Float64
PCG/AMG uses bounded defect correction with streamed long-double residuals and
accumulated/raw fields; all correction stages share the original iteration
budget. The true-residual gates were not relaxed. Ordinary engineering JSON/VTU
summaries remain float64; this is not an all-wide stiffness assembly.

Registered `run_shell` verification passed the analytic coupons, all nine retained
CCX coarse comparisons and tighter-solve/higher-quadrature sensitivity checks at
their original limits. Evidence is under `orthotropic_direct2d_run03`; failed
runs remain preserved. The separate 9.2 mm PCD350 candidate completed nine coarse
cases in run04, failed the sourced component-stress screen, and refused the first
h=1 case on memory admission. No fine-mesh convergence or wheel qualification is
claimed. None of this adds or advertises a native orthotropic Chudware solver.

## 2026-09-12 — native corrections release gate

The complete gate at `/tmp/chudware-gate-372191` reported PASS: native/GUI/WASM
builds, all core CTests, 9904 Python tests, hostd/web parity, Linux packaging,
13 golden-demo legs and both GUI captures. Unset opt-in suites remain explicit
skips. The outer one-hour command timed out while the systemd service continued;
its abandoned output pipe was reattached without restarting passed stages.
`recovered_tail.out` preserves the actual final gate summary and PASS.
The temporary watchdog executable and output-recovery helper were removed.

## 2026-09-12 — selected-face finishes and cached native preflight

Source parts now persist visual-only face finishes through `set_appearance`
and registered `set_material(..., faces=...)`. Exact unique face SIDs and
canonical fingerprints guard assignment and rendering; ambiguous selections
reject the transaction, stale records remain inert, and clearing affects only
the specified records. Physical material identities do not change. Compile
cache keys, AICM appearance regions, native reopen, direct rendering and nested
assembly propagation retain the finishes.

A production cylindrical-face refusal exposed inconsistent SID inputs:
geometry queries classified bores/bosses, while descriptor-based validation
omitted that classification. Both paths now share the existing geometric
concavity helper. The retained multibody bore/boss regression failed before
the correction with `unknown face SID` and now verifies query-authored finishes,
viewport-pick clearing and isolation from the other body.

Native preflight now uses unregistered scratch state with copied mutable cache
indexes and shared immutable payloads instead of serializing/replaying a cold
clone. It preserves source revision, geometry, history, warm state and live
dependent documents, and returns actual rebuild diagnostics. Missing diagnostics
or revision races are not passing rebuilds; connection loss fails closed.
The removed post-call deadline never bounded native execution: cached preflight
avoids replay but does not promise a deadline for changed geometry.

Focused verification: four native suites (`appearance`, `selectors`, `picks`,
`render_view`) pass, as do 198 preflight/incremental/connection-loss/MCP tests
and 94 material-resolution tests. A registered production smoke exercised
face set/clear/reopen/nesting and cached preflight source isolation. The actual
desktop opened its saved specimen using the corrected pinned library and
rendered the distinct top and side finishes; it exited normally with the source
hash unchanged. Numeric APRG transport is covered by the native loader tests,
not inferred from screenshot colors. Project evidence is recorded in
`me490_native_face_preflight_smoke.json` and `me490_native_face_desktop_proof.json`.

Added sourced SKF GE10E ring, SSAB Domex 355MC sheet and ISO 10642 class 010.9
reduced-head screw records. SKF's undisclosed alloy/density/stiffness/strength
remain absent; both mass and isotropic FEA resolution refuse substitution.
The complete bearing's 11 g catalog mass is not a per-ring density. Domex uses
the published 355 MPa minimum yield and 430 MPa lower ultimate bound, with
separately identified nominal elastic/density data. The reduced-head screw's
bulk 10.9 properties do not establish finished-fastener loadability. A direct
resolver smoke verified these values, refusals and phosphated SKF appearance.

The cast Shore A85 tread gap now has a distinct Polytek PT Flex 85 product
record: nominal base-product density 1.06 g/cm³ and natural yellow/amber
appearance. It is not printed TPU95A or generic black plastic. Compatible black
dye is a documented process option, not a sourced compound-density certificate.
Conflicting manufacturer tensile values are retained as a caveat, not imported
as strength; the resolver smoke permits nominal mass and refuses isotropic FEA.
SKF's initial CN range is separately sourced to its actual catalogue Table 3,
not the previously cited three-page product sheet or an installed-clearance claim.

## 2026-09-12 — exact registered belt-route geometry

Added `belt_route` to the existing deterministic engineering calculator
registry, reachable through `list_calcs` and `research_eng_calc`; no parallel
tool catalog or outside-harness project solver. Ordered pitch/neutral circles
and signed wrap senses determine exact common tangents and circular wraps,
including backside idlers. Results expose every endpoint, arc and length;
impossible, obstructed and self-crossing/touching routes refuse. Normalized
coordinates keep the geometric predicates separate from physical tolerances.
The calculator deliberately does not turn supplier center-distance tolerances
into belt-length allowances or claim tension, strength or hardware clearance.

Synthetic equal/unequal two-pulley, convex three-pulley and reversed backside
routes were exercised against independent closed-form lengths. Focused
calculator and registered-tool tests passed (78 tests), including impossible
tangents and a backside route crossing or kissing the opposite span. Removed
the old test that merely asserted its own beam-reference arithmetic.

## 2026-09-12 — nonbinding native saves and explicit interference thresholds

Recovery and live-publication saves now serialize an unregistered cached
snapshot instead of moving the live document into an autosave directory.
Snapshot component paths retain their original source base without rewriting
the live relative tree. Ordinary batch project saves preserve an established
destination even when given a display name; failed metadata reads cannot
silently select a new destination. Human undo history is not cleared by a
nonbinding snapshot.

An explicit static interference threshold mode shares motion classification:
after completed overlap classification, zero-clearance/no-contact checks no
longer run irrelevant exact distance optimization through helical curves.
Default exact work remains available. Bounds, measured gaps, unavailable
distances and incomplete evidence stay distinct in registered narration,
archived fit cards and desktop results; no minimum measured gap or internal
OCCT cancellation guarantee is invented.

Native library/daemon and desktop builds passed. Focused native persistence,
interference, motion and appearance suites passed (4/4). Registered threshold
tests passed (7), in-process preflight/save/loss tests passed (50), the isolated
daemon relative-save regression passed (1), and save-policy/recovery/route
tests passed (111). Actual threaded-wheel and clean teammate bundle workflows
remain separate production acceptance checks.

Added a sourced plain ISO 7089 class 300 HV washer selection, including the
listed BelMetric M12 SKU and Westfield dimensional/hardness tolerances.
It replaces neither a supplier certificate nor a joint qualification.
Nominal steel density/elasticity remain explicit assumptions; a fresh resolver
smoke accepted nominal mass and plain appearance and refused isotropic FEA,
without converting hardness to invented yield or tensile strength.

## 2026-09-12 — implicit fit checks and initial face-cache display

The real threaded wheel completed a 253-pair threshold check, then exposed
another exact-distance path through `describe_assembly`'s legacy `quick:true`.
A debugger capture found OCCT curve/curve global optimization, with autosave
waiting on the document mutex. `gsl_tools.py` and `cad_motion_tools.py` now
request collision-only threshold classification explicitly. Narration retains
raw fit evidence; steering retains per-frame accounting and failures through
the registered turning-radius response. Missing or incomplete checks cannot
qualify collision-free motion. Native-backed descriptor and motion regressions
passed (53 tests); the full threaded descriptor is a separate production check.

The display-cache semantic epoch advances to `chudware-geomcache/3`. A native
regression reproduces an older writer preserving a red selected-face override
while retaining a blue cached mesh: before the fix, reopen accepted that cache
and initially displayed the wrong face color. The fixed reopen rejects the
older epoch and the actual AICM loader sees the authored red face immediately.
Focused appearance, export, motion and interference CTests passed (4/4).

A clean production teammate mission created and normally saved a fresh nested
relative-source bundle, moved it to a second folder, hid the original sources,
edited and restored a real hub feature, and reopened the full ten-solid result.
All fourteen final native-file hashes matched the proof. The final native
integrity query checked 45/45 pairs: eight touching and 37 proximity relations,
no overlaps or failures; this is not load-path or strength approval. A subsequent
post-proof `doc_list` connection-loss event remains recorded, not hidden.

The actual desktop zero-pair guard was visually observed: it displays
`No body pairs checked`, not a collision-free result. The surrounding `asm`
screenshot scenario failed to insert its peg after a stale-revision response,
so this is not a passing assembly/mate scenario. Bound-only desktop display
was not visually exercised; its native/Python representation is tested.

## 2026-09-12 — exact face finishes and ME-490 handoff pivot

The production inboard wheel carrier exposed two distinct planar faces per
short SID. Full query selectors were being reduced to the SID and rejected.
Native finish assignment now compares complete canonical fingerprints, retains
independent records for colliding SIDs, and clears either an exact target or an
entire explicitly requested SID bucket. Partial/stale/ambiguous selections
remain refusals. The cache semantic epoch is 4. The actual regression failed
before the fix with `ambiguous_face_sid`; after the fix all 14 appearance cases
and 967 assertions passed, including different colors, native save/reopen,
exact clear, and stale-fingerprint rejection.

The user redirected ME-490 to organization, UA Box publication, university
website workspace access, and a Desktop continuation report. Further physical
optimization, FEA and presentation replacement are paused. Prior full-gate
results are not a green release: native 108/108 passed before this extension;
the Python gate encountered a live-dispatch DAG fixture and nested native
thread-pool failures. The hermetic DAG fix passed 347 tests with guidance;
bounded native threads passed the unchanged contact regression in 156 seconds.
The complete gate still requires a final run. The approved presentation and
all failed/partial study evidence must remain preserved.

## 2026-09-12 — metadata-safe organization and UA Box workspace

Added native `doc_file_operations` in `stepplus.cpp` and the JSON dispatcher,
with registered MCP inspection/relocation adapters, mode policy, category/menu
reachability and regenerated catalogs. Copies preserve stable identities,
authored geometry/poses/finishes and opaque payloads, rebase supported references
relative to the new owner, and invalidate optional caches without solving.
The real project exposed historical `/meta` paths misclassified as required
geometry inputs; a regression failed before the generic documentary-metadata
correction, then the complete native cache suite passed 23 cases/4847 assertions.

The gateway now supports a configured, verified-UA-only team default backed by
an independently authorized Box FUSE mount. Explicit personal/team selection
wins, absent mounts fail closed, shared chats remain private, and account
plans/admin/AI grants are unchanged. Expired-trial access is workspace-scoped.
Mounted storage exposed blocking filesystem/quota work on the event loop:
file handlers now run in FastAPI's threadpool, with synchronized rate limits
and bounded root-shared mutation locks. External Box/native writers are not
covered by those process locks. The focused gateway suite passed 216 cases,
including health responsiveness during slow storage and cross-surface quota
admission. The production engine was intentionally not restarted or upgraded;
its existing recovery state and browser/desktop version alignment remain
explicit continuation items.

## 2026-09-13 — measured PLM and assembly loading reductions

Profiled read-only ME-490 sources and isolated native runs, without restarting
the live daemon. PLM (`services/plm/{vault,gitbind,worker_rpc,snapshot,cad_graph}.py`)
now avoids open-time Git maintenance and redundant status/network scans, reads
history by content-addressed subtrees instead of whole commits, seeks native ZIP
documents without copying derived geometry, and retains truthful dirty verdicts
against HEAD and strong file identities. The former global 30-second dirty TTL
expired during the scan itself: on 5,652 tracked items, the immediately following
overview fell from 76.02 profiled seconds to 0.142 seconds. The cold dirty scan
still takes 58.15 profiled seconds (83.49 before the final dirty changes); large
noncanonical recipes and Git blob reads remain real work. Windows, where Python
ctime is creation time, retains bounded per-entry rechecks in both verdict and
exploded-container caches. The full CAD graph had 80,623 nodes/1,555,555 edges and
an 808,143,528-byte pretty cache that its own 64 MiB reader rejected on every use.
Compact bounded gzip storage is 20,041,948 bytes; cold profiled build is
108.47 → 28.89 seconds and validated warm load 4.175 seconds, with exact graph
equality. Complete source stat coverage includes display-capped and unreadable
files; restored permissions and corrupt DEFLATE caches are regression-covered.

Native loading (`core/doc/{assembly,stepplus,document,session}`,
`core/geom/tessellate`, `core/util/aicm_handoff`) now uses selective archive reads,
recursive dependency-aware component reuse, parallel selector-kind-only analytic
topology, and assembly-only compilation without discarded part display packets.
Shared meshing retains the identical per-body Fine settings. Session activation
reuses a valid assembly display publication; mapping immutable in-process AICM
data pins storage rather than copying it. Publication replacement/release and
preserve-path autosaves retain correct reader lifetime and exploded-view state.
The real 284-component Rover had 278 unique sources, none with compiled caches:
fresh-process open improved 62.424 → 54.906 seconds (12%), activation
2.83–2.90 seconds → 15.8–19.0 milliseconds, and peak RSS 4,396 → 3,271 MiB (26%).
All 431,190,175 display bytes retain SHA-256
`d17fbeaacf9fe6b6ef7faa31be00c00f615ef315c4dd76a1b04404838461f2d0`;
284 bodies, mate convergence and zero component errors are unchanged.
Cold opening is still substantial; no geometry, material or display-quality
reduction is hidden in these gains.

The PLM GUI (`host/src/ui/plm/` and `host/src/core/app_state.cpp`) shares item/status
responses across pages and the document quickbar, caches sorted file indices and
the Git directory tree, and defers seven detailed Git requests until Git/Planning
needs them. One hub entry point owns outside-change recording; asynchronous
fanouts retain loading and generation/filter fences. Dirty badges invalidate on
saves even with automatic history disabled. Actual isolated desktop Files and
Git scenarios completed: Files fetched two basic Git endpoints, Git fetched all
nine, and each captured outside changes exactly once. The final PLM suite passed
403 tests; native cache/lifetime/appearance, nested dependency, cycle, symlink,
configuration, and display-mode regressions accompany these changes.
Both desktop and native builds passed and runtime staging used atomic replacement;
the live daemon was not force-stopped. The AST-only developer code graph refresh
completed; its existing extraction gaps remain visible (334 files yielded no AST
nodes and 19 SQL files require the optional `tree_sitter_sql` dependency).

The complete isolated `scripts/gate.sh` exited PASS: all 108 native targets,
all nine source audits, desktop/WASM builds, daemon lifecycle, web parity,
Linux packaging and GUI/golden-demo smokes passed. Its per-unit Python sweep
reported 9,982 passed and one nonfatal FAIL-RECORDED:
`services/mcp/test_reveng_e2e_scan.py::test_full_reconstruction_numbers`.
A focused rerun diagnosed `reveng_build_parametric` refusing with
`backend_not_provisioned` because no dense/mesh capability or reconstructed mesh
was available. No reverse-engineering code or test was changed to hide that
environmental failure. Optional AppImage, V&V, modeling-combination and larger
benchmark stages remained opt-in skips. Gate logs:
`/tmp/chudware-gate-loading-perf-gate-20260913/`.

## 2026-09-14 — cold PLM comparison without redundant blob decoding

The next measured pass changes `services/plm/{snapshot,vault}.py` and
`test_dirty_cache.py`. Dirty scans compare working recipes to Git blob ids before
reading historical content: ME-490's 4,728 native containers now require only
247 reference-blob reads. The snapshot reference resolver runs after retained
ZIP members have been read and CRC-checked. Noncanonical recipes compare through
the C-backed, sorted JSON encoder before reusing the exact canonical vault bytes;
the persisted pretty format is unchanged. This deliberately distinguishes booleans,
integers, floats and signed zero rather than using Python value equality.
The bounded explode cache retains recipe bytes but defers metadata parsing until
a save needs it. Save/restore content, attachments, HEAD invalidation, file-stamp
freshness and Windows bounded rechecks remain covered.

Fresh-process, unprofiled scans of the same read-only ME-490 HEAD
`ff291512f8b87a9f36696d9232b8bdbadc11b41e` fell from **24.017 to 9.535 seconds**
(60%); peak RSS fell from **662.5 to 392.9 MiB**. Both contain 5,652 manifest
items and report zero dirty items. The complete overview response is identical,
SHA-256 `dc49a92da8c8fad291993e654cb131b8511d00bbddfc52c4bbdc5374529b501f`.
The real worker overview entrypoint, opening/closing its own Vault each time,
took **9.095 seconds cold / 0.072 seconds warm** with the same response hash.
These are backend measurements, not desktop wall-clock latency, and are not
directly comparable with the earlier profiled 58-second scan.

Verification: **407 PLM tests pass**, all nine `scripts/check.py` gates pass,
and the desktop build and atomic runtime staging pass. New regression cases cover
scalar representation changes and a reformatted Unicode recipe followed by a save
that preserves both canonical bytes and its attachment. Native assembly code is
unchanged; its approximately 55-second cold Rover open remains a separate bottleneck.
The full native/agent gate was not rerun for this PLM-only pass; its previously
documented reverse-engineering backend-provisioning failure remains unresolved.
Measurements are retained under `scratch/plm_cold_*.json` and
`scratch/plm_overview_final.json`; disposable probe scripts/profiles were removed.
The project was mounted read-only during real-data probes, and the user's running
application and recovery state were not restarted or modified.

## 2026-09-14 — cold assembly loading and large mate diagnostics

The next native pass changes `core/geom/tessellate.cpp`, `core/doc/document.cpp`,
and `core/mate/mate.cpp`. OCCT's shape-taking `BRepMesh_IncrementalMesh` constructor
already performs meshing; the second `Perform()` was another validation pass.
Geometry-only component compilation now omits host-picking ownership maps that
its consumer never reads. Display-mode cache identity is unchanged, and the
existing geometry/display mode-switch regression now also checks picking owners.
Neither change adjusts Fine deflection, tessellation quality, or source geometry.

Mate classification retains the analytic Jacobian callback already registered
with the pose solver and evaluates residuals only for rejected mates. When the
prior groups already span every candidate column, rank cannot increase: the
candidate allocation and SVD are skipped, but the residual still distinguishes
redundant from conflicting. Rejected mates still contribute no rows or group
merges. Large remaining rank calculations use Eigen's divide-and-conquer SVD
with the existing absolute singular-value cutoff. Unsuccessful/nonfinite results
and values near the cutoff fall back to the original Jacobi SVD. The global DOF
QR, pose solver, convergence tolerances, and restart policy are unchanged.

Fresh-process, unprofiled Rover opens fell from **53.652 to 37.851 seconds (29%)**.
Activation remains **17 ms**. Separate stage probes put mate classification at
**15.798 seconds before / 2.224 seconds after**; pose solving was already seated
and took less than a millisecond. Component loading, about 30 seconds, is now the
dominant measured stage. These are native `doc_open`/`doc_activate` timings, not
desktop frame timings, and variable scheduling/RSS is not a claimed memory win.

Both runs contain 284 components and 284 bodies, no component errors, and exactly
431,190,175 AICM bytes with SHA-256
`d17fbeaacf9fe6b6ef7faa31be00c00f615ef315c4dd76a1b04404838461f2d0`.
The complete rebuild response also matches: component records, every mate status,
DOF 94, residual DOF 19, convergence, tolerances, and maximum residual
`1.1102230246251565e-14`. Every one of Rover's 20 accelerated rank calculations
was additionally checked against the original Jacobi SVD. All 105 extracted-helper
stress cases matched it, including cutoff neighbours, zero, scaled, ill-conditioned,
and rectangular matrices. A long grounded-chain regression checks chronological
acceptance and the redundant/conflicting distinction after saturated bridges.

A disconnected global-QR candidate passed 400 numerical comparisons but did not
materially improve this assembly; it and its extra code were discarded. All phase
probes, numerical self-check instrumentation, throwaway programs, and the sampled
profile have been removed. Measurement/response JSON remains in
`scratch/assembly_*.json`, with the comparison summary in
`scratch/assembly_verification.json`. Real project data was mounted read-only
and shared-memory packets were isolated; the live application was not restarted.
The AST-only developer graph refresh completed; the previously recorded 334
empty-file extractions and 19 SQL files lacking `tree_sitter_sql` remain visible.

The full isolated `scripts/gate.sh` exited PASS: all 108 native targets, nine
source audits, desktop/WASM builds, daemon lifecycle, web parity, Linux packaging,
and GUI/golden-demo smokes passed. Its Python sweep reported 9,986 passed and the
same one nonfatal FAIL-RECORDED as the preceding native gate:
`services/mcp/test_reveng_e2e_scan.py::test_full_reconstruction_numbers`.
No reverse-engineering code or assertion was changed to conceal that failure.
The optional AppImage, V&V, modeling-combination, and larger benchmark stages
remained opt-in skips. Logs: `/tmp/chudware-gate-assembly-perf-20260914/`.

## 2026-09-14 — explicit cloud workspace switching

`host/src/ui/title_bar.cpp` now presents the cloud project control as **Workspace**
and opens **Workspaces and files**; native folder selection is unchanged.
`Webservice/gateway/static/bucket.html` and its content-hashed JS/CSS put an explicit
**Open workspace** action beside the readable personal/team chooser. Browsing files
no longer writes the CAD workspace cookie: only same-tab `/app/?team=…` navigation
commits a switch through the existing authorized app-entry route and recreates the
CAD connection. Embedded navigation warns to save first, supports cancellation,
and refuses while uploads are active, including across modal remounts. Failed
discovery and unavailable selected teams cannot silently select personal storage;
Retry or an accessible choice recovers. Browser smoke exercised the real portable
surface and title-bar JS bridge with local team fixtures: selection, cancel/open,
discovery failure/retry, unavailable-team recovery, upload blocking, and remount.
Real-PostgreSQL gateway coverage in `test_orgs.py` proves app-entry selection changes
subsequent file access and rejects unauthorized teams without changing the previous
selection. Removed `test_bucket_ui.py`'s markup/default-pinning test rather than
re-pinning it to the new loading state; its serving/CSP/hash checks remain.
Live account membership and cloud deployment are separate from these local checks;
no team provisioning or access policy changed.

## 2026-09-14 — ME-490 external owner access

Production ME-490 appeared in the paid external organization's owner's team list
but `orgs.resolve_team_bucket` rejected the owner before evaluating membership:
the automatic UA enrollment criterion had also become a mandatory access criterion.
The resolver now attempts verified-UA enrollment and then applies the existing
organization/team membership checks to everyone. No accounts, roles, invitations,
funding requirements, or domain enrollment criteria are changed. `test_orgs.py`
covers external-owner selection/write, outsider and platform-admin isolation,
invited external-member reads, and revocation. The targeted organization,
tenant-isolation and bucket delivery suites pass 104 tests.

## 2026-09-17 — bring-your-own-provider website billing

The account portal no longer offers AI credit packs or routes marketplace errors
to a top-up flow. Billing links to the existing AI setup for customer-owned
provider subscriptions and API keys. `CHUDWARE_CREDIT_PACK_CHECKOUT_ENABLED`
defaults off and refuses credit checkout before contacting Stripe, even when old
pack price IDs remain configured. Subscription billing, existing grants,
historical accounting, and delayed payment webhooks are unchanged. Removed stale
hard-coded portal subscription prices; checkout remains authoritative. Provider
cards fill the available width and the existing-grant card stays hidden until the
account response permits it. Rehashed the changed portal and marketplace assets.
Verification: 33 billing/portal tests passed, followed by the final asset-delivery
test after rehashing; browser smoke with isolated account/API fixtures exercised
Billing → AI setup, the visible API-key form, hidden grant card, and absence of
purchase controls. Production rollout requires a gateway restart and leaving the
new checkout switch unset or off; no provider credentials or runtime changed.

## 2026-09-17 — product-first public site and gallery

`Webservice/public-site` now leads with the engineering workspace rather than
the oversized animated brand mark; the unused hero component and renderer were
removed. Homepage, navigation, gallery, product pages, pricing, FAQ, download
guidance, AI documentation, security claims, and legal copy were reviewed.
Three new 1600×900 native captures retain the full Copilot panel and intrinsic
aspect ratio. Modeling shows a genuine public demo response beside a disclosed
scenario reproduction; CAM and FEA requests are labeled scripted examples, not
invented transcripts. Capture hashes, build identity, and commands are recorded
in `public/images/provenance.json`; the verifier now checks declared dimensions
against PNG dimensions instead of forcing the obsolete capture size.

The built-site browser sweep exposed an Astro CSS-order bug that dev mode had
hidden: page bundles could register `responsive` before `components`, allowing
desktop navigation to override the mobile menu. Every CSS entrypoint now
declares the same layer order. The corrected production build passed browser
checks on all 37 routed pages at 1440px and 390px: no horizontal document
overflow, missing images, duplicate/missing primary headings, hidden mobile
menus, or JavaScript errors. A static audit resolved 1,804 internal links,
assets, and anchors across all 38 HTML outputs. Gallery full-size navigation,
mobile menu interaction, documentation search, image aspect ratios, and the
real account-generated Linux command were exercised. No private session
history, provider credential, or scratch capture profile was published.
Portal commit `1745f2afa` was deployed separately; the live authenticated credit
checkout probe returned 503, with no purchase attempt, and gateway health passed.

## 2026-09-17 — AI-first positioning and progressive visuals

The homepage now leads with AI-first CAD for engineers and the describe, edit,
review workflow. About is available in both navigation variants. The tesseract
renderer returns as a bounded About-page illustration rather than a full-screen
hero; its static SVG remains available without JavaScript or WebGL. Modeling
and CAM keep their product captures when enhancement is unavailable, including
renderer initialization exceptions. Illustration labels appear only alongside
the active illustrations. Visitor-facing implementation commentary was removed
from captions and introductory copy; gallery scenario disclosures remain.

Verification: the 38-page build and three-image provenance check passed.
The link audit resolved 1,882 internal links, assets, and anchors without errors.
All 37 routed pages passed desktop/mobile overflow, primary-heading, About-nav,
and JavaScript-error checks. About, modeling, and CAM retained their static
visuals with JavaScript disabled, script requests blocked, WebGL unavailable,
and WebGL initialization throwing. Tesseract context-loss recovery, offscreen
teardown/remount, and pixel-stable reduced motion were exercised in Chromium.
The original website deployment succeeded; its live download catalog returned
0.1.1 on Windows, Linux, and macOS. The personal LinkedIn link remains pending
an exact URL from the owner; no profile URL or personal tagline was invented.

## 2026-09-18 — homepage motion and About attribution

Restored the bounded tesseract to the homepage hero and the existing modeling
feature-sequence and CAM stock-removal animations below the AI workflow.
The product gallery retains all three authentic captures at native aspect
ratios, with modeling full-width above CAM and simulation. LinkedIn now appears
only in About content, not either header menu.

Chromium checks exercised all three live homepage animations, static fallbacks
with JavaScript disabled, blocked scripts, missing WebGL, and throwing context
initialization; all retained loaded fallback images. Context loss/restoration
and pixel-stable reduced-motion frames passed for all three renderers.
Responsive homepage checks found one H1, no horizontal overflow, and no header
LinkedIn link; About links to the owner-provided profile.

## 2026-09-23 — ship-readiness sweep: 19 subsystem audits, ~95 defects fixed

Read-only scouts audited host shell/tabs, ribbon/PM, sketcher, viewport, workspaces, core
doc/geom, hostd/agent run, sim/mate/CAM, services/agent, MCP, PLM/parts/installer, the web
gateway, build/packaging, then ECAD, drawing, sim GUI, manufacturing, settings/recovery and
import/export; fix agents repaired disjoint slices, each commit gated by core+host builds,
core ctest (108/108), host ctest (25/25), the touched pytest units, the gateway suite and
headless `CHUDWARE_SHOT` smokes (m1/sketch/asm/fea/drawing/flow). Highlights: picking ids
now carry 12-bit body ordinals (assemblies >255 bodies used to alias) and `parseAicm`
refuses out-of-range face/edge/index data; fillet Full Round/Face and draft Parting
Line/Step (never compiled) are removed from the forms and refuse by name, while chamfer
Vertex (SolidWorks corner cut, d³/6) and Distance-Angle are real; CAM post/sim/audit
surface dropped operations (`incomplete` + `warnings`, GUI export gated), G95 F words are
mm/rev, spindle-off cutting is an audit error; manufacturing hold/start only claim
Held/Running after the device accepts, a post-nonce-burn failure journals
`command_outcome_unknown`→Faulted, serial polls are bounded, releases with an active run
refuse, and endpoint physicality alone governs execute authorization; hostd no longer
self-kills multi-hour agent turns at the 60-min watchdog; MCP CAD writes honor the
credential denylist and realpath containment; PLM item paths are contained and sync
refuses to clobber unsaved work; web mesh handoffs need a per-account doc grant, chat is
team-scoped, and web BYOK turns run in a credential-isolated `chud` process (temp
HOME/XDG/`CHUDCODE_CODING_AGENT_DIR`, scrubbed env, key-digest proof from the gateway).
glTF/3MF/DXF imports now scale to mm; partial STEP/IGES transfers keep bodies and warn.
Deferred: WorkerBridge is still one serialized worker for all tenants (hostd finding 2);
keyless pooled/subscription web turns still use the host's ambient Chudcode auth; Parts
Library "Insert" creates a new part (says so) rather than inserting into the open
assembly; Auto Balloon / Geometry Analysis ribbon rows were removed, not implemented; an
idle chat socket removed from a team keeps receiving until reconnect; the Windows-only
`revealInFileManager` rewrite is uncompiled here. Gotcha: `./build.sh core` after a
local-polymesh configure used to strand `POLYMESH_ROOT` at the private checkout
(`<checkout>/src` has no CMakeLists) — `reconfigure_core_tree` now restores the managed root.

## 2026-09-24 — closing the sweep's deferred items

Everything the 2026-09-23 entry deferred is now real. **Worker lanes:** hostd owns two
services workers (`WorkerBridge::agent()` / `general()`, `forMethod` in
`core/hostd/worker_bridge.cpp`): agent_/chudcode_/session_/subscription_/skills_/triggers_/
packs_/ai_roles_/model_rankings_/bench_/memory_/subagent_/dag_/ai_prompt_/ai_tool_ plus
`worker_control_info`/`rule_create` go to the agent lane; everything else (PLM, mfg, parts,
ECAD, sim, unknown) goes to general. The child learns its lane from
`CHUDWARE_HOSTD_WORKER_LANE`; a general worker skips Chudcode/control/bench startup, an
unflagged worker keeps the old all-in-one behavior. Because two processes now share files,
PLM vault config and the Box mirror drain take sibling-file flocks. Pinned by
`services/mcp/test_hostd_worker_bridge.py` (a blocked agent call does not block a general
call). **Web credentials:** no web actor ever reaches the host's ambient `chud` auth. BYOK,
personal Claude OAuth (`ANTHROPIC_OAUTH_TOKEN` in a temp HOME) and the pool (dedicated
`CHUDWARE_WEB_POOL_API_KEY`/`_PROVIDER` default openrouter/`_MODEL`, injected by the gateway
with a digest proof) all run in credential-scoped processes; an unset pool key refuses
`pool_credential_unconfigured`; Codex subscriptions refuse `subscription_isolation_unsupported`
(Chudcode 0.3.0 has no isolated Codex bearer injection); the model catalogue lists only what
the resolver can serve that caller. RUNBOOK documents the env. **Chat:** team/org removal,
admin demotion, deletion and suspension close affected sockets (4403) on their own loop.
**Auto Balloon** (`drawing_auto_balloon`): one balloon per BOM item anchored on that
component's visible outline, projection computed only on demand and cached per
view+source revision (ordinary `drawing_get` pays nothing). **Geometry Analysis**
(`eval_geometry_analysis`, `core/geom/geometry_analysis.*`): BRepCheck validity, small/sliver
faces, short/open edges, free-end gaps, opt-in G1 breaks; its own Evaluate dialog. Both
ribbon rows are back. **Parts Insert** into an open assembly saves a `.step+` to the project
then runs the normal `insertComponent`. **Host:** GL-thread EngineClient calls are bounded
(5–10 s) and a timeout never reads as an empty list; Windows `revealInFileManager` passes a
mingw `-fsyntax-only` check and links shell32/ole32 (still unexercised at runtime). Known,
pre-existing: `CHUDWARE_SHOT=flow` logs "numeric result did not arrive within 180 s" while
exiting 0.

## 2026-09-24 — Opus 5.5: effort ladder and silent provider rejections

Two defects surfaced by one Opus 5.5 chat turn. **Effort menu:** `worker_rpc._sanitize_model`
dropped Chudcode's per-model `thinking.efforts`, so the composer (`composerEffortLadder`) had no
catalog ladder for an id missing from `model_registry.h`'s static table and fell back to
Low/Medium/High; rows now carry `reasoningEfforts` and `vision` (from `inputModalities`, the
old `input` key never existed in the live catalog). **Silent failure:** Anthropic rejected every
Opus 5.5 request with `400 claude_code_version_too_old` (Chudcode presented Claude Code
2.1.246; 2.1.280+ required). A 400 is not retried, so Chudcode recorded it only as an assistant
`message_end` with `stopReason:"error"` and emitted a normal `agent_end`;
`chudcode_host._unrecovered_provider_failure` only knew the failed-retry shape and returned
`ok:true`, which the GUI rendered as "The agent finished without reporting what it did".
Both shapes now return `ok:false, stop_reason:"provider_error"` with the provider's sentence.
Chudcode `7f104b4558` bumps the fingerprint to 2.1.281; the local `~/.local/bin/chud` was
rebuilt from the working tree (previous binary kept as `chud.2026-09-01.bak`). Deferred: the
`vendor/chudcode/CHUDCODE_ARTIFACT.lock` release pin still names v0.3.0 artifacts carrying
2.1.246 — a packaged build needs a new Chudcode release and a re-pin of all five targets.

## 2026-09-24 — "the agent took the wheels off": harness fixes from one brake turn

A GUI turn on the ME-490 Rover Review died at exactly 15:00 with "provider_error — engine
call timed out after 900000ms (orphaned run auto-cancelled)" and the viewport showed the frame
without wheels. Nothing was mutated: `openMirrorTab` auto-focused the first agent-opened doc
(the frame part, opened with `import_part(resume:true)` to read its body roster) over the
user's own tab, and a fixed 900 000 ms client deadline in `jobs.cpp` (plus the same bound in
hostd's bridge) killed a healthy turn mid-tool-call. Fixed: online turns carry an explicit
`timeout` budget (7200 s) that the GUI and hostd wait past, client expiry is `client_timeout`;
mirror tabs never steal focus from a user tab; the error card parses tool rows through
`splitToolRowDetail` and lists only failures the turn never got past (the raw `\x1eERR\x1e`
bytes rendered as "?ERR?"); `doc_solve_mates` that moves nothing is not an edit (describe_assembly
dirtied the user's assembly); a pristine import of an existing file (`doc_list.reproducible_from`)
is neither project-saved nor retired (the cancel sweep had written a 36-body probe into the
project root). Three live verification turns through an isolated hostd (bwrap overlay over the
project, tray off, Box spool redirected) found four more: `WorkerBridge::stop()` wedged hostd
behind an in-flight turn on tray Quit/SIGTERM; bare `save_part` duplicated an opened file into
the project root and explicit saves left the harness's root copies behind; the terminal sweep
wrote a `close_part(discard=True)`-ed stale Wheel End over the rebuilt file; `asm_mate` /
`motion_fea_handoff` resolved relative component sources against the worker cwd. All fixed with
failing-first regression tests. Run 3 completed in 39 min with every doc saved in place; its
brake fix was applied to the real project (originals in
`~/Backups/ME-490-before-brake-fix-2026-09-24/`). Deferred: the remaining Frame ↔ Upright
Bracket overlap (spindle through an unbored bracket); calipers placed by measured pose, not
mates. Test-env note: running several engine-backed test files in one pytest invocation with a
non-default `CHUDWARE_HOSTD_BIN` leaves a daemon whose rendezvous breaks the next file — run
them one file at a time.

## 2026-09-25 — the 2 h steering turn: orphaned run, hidden error card, frozen viewport

One "go bang it out" turn on the user's open `Rover Review` assembly exposed three defects.
(1) `chudcode_host.run_turn` returned `Timed out waiting for agent_end` at the GUI's 7200 s
budget but never aborted: Chudcode kept running the same run for two more hours (saves,
PLM checkpoint, STATUS.md edits) with no UI attached and the run-scoped env already restored
(`render_part save_path` refused with no project dir), until a 403 OAuth rejection ended it.
Budget expiry now aborts once, waits ≤30 s for the terminal frame, and returns
`stop_reason:"turn_budget"`; `worker_rpc` forwards failure `stop_reason`s (every failure used
to become `provider_error`), and `app.cpp` shows an amber "Turn time limit reached" card.
(2) The drain REPLACED `ChatMsg::text` with the failure message; the timeline had already
drawn two streamed prose rows, so `timelineProseTail` found no matching prefix, returned empty,
and the error card never drew — the only trace was a red "System error" status word. Failures
now append (`timelineProseTail` trims the separator). (3) `engine_events.cpp` deferred every
`doc.changed` for a dirty user-owned tab, and applying the first agent revision dirties that
tab, so the viewport froze for the rest of the turn; mesh-less revisions (`with_tess:false`
from `asm_add_component`/`asm_mate`) also unloaded the displayed mesh. Now only
`userMidEdit` defers and a missing `aicm_path` keeps the current mesh. Files:
`services/agent/chudcode_host.py`, `services/agent/worker_rpc.py`,
`services/agent/test_chudcode_host.py`, `host/src/app.cpp`, `host/src/core/engine_events.cpp`,
`host/src/ui/copilot/activity_timeline.cpp`. Deferred: the 7200 s default itself
(`kOnlineTurnBudgetS`, hostd `server.cpp`) is unchanged; the asm writers still skip
tessellation, so a newly added component appears at the next mesh-bearing revision.

## 2026-09-26 — six-axis turn: sourcing, progress, Agent Hub and reference recovery

The original MG90S six-axis prompt spent 13 minutes researching with zero CAD mutations:
ordinary sourcing was outside `MAIN_CHAT_CORE_TOOLS`, there was no servo family or
URL-to-STEP acquisition, no online no-progress steer, and noisy/mislabelled RPC events.
**Reference path:** `tool_categories.MAIN_CHAT_CORE_TOOLS` now includes `part_search`,
`part_get`, `part_source`, `match_model`, `make_standard_part`, `fetch_cad_model`,
`import_artifact` and `simulate_motion`. `core/parts/catalog.cpp::hobby_servos` cites
manufacturer dimensions/proxy caveats for SG90, MG90S, MG996R, MG995, DS3218 and
micro_9g; `generate.cpp` makes editable `hobby_servo` geometry and
`core/api/libchudware.cpp::parts_make_impl` returns its `output_axis` datum.
`services/mcp/parts_tools.py::fetch_cad_model_impl(url,name,import_part)` stores CAD and a
provenance sidecar in `<project>/reference/`; direct HTTPS files need no browser,
`services/browser/adapters/printables.py` uses public GraphQL via curl (the Python HTTP
path hit Cloudflare), while MakerWorld/GrabCAD gate downloads with `login_required`.
`services/research/fetch_page.py` detects JS shells and retries rendering;
`web_search.py`/`providers.py` report DDG HTTP 202 as `provider_blocked` and genuine empty
results as `no_results`, never `ok:true` empty.

**Agent execution:** `progress_watchdog.ProgressWatchdog` steers after 12 completed research
calls or 6 minutes without CAD mutation, one nudge per stall and two maximum; reading
`xd://` docs is not a mutation, and `chudcode_host.run_turn` exempts Ask/Plan and
`plan_only`. `chudcode_events.map_rpc_frame` unwraps `custom{customType}` and keeps
lifecycle frames out of completed tool cards; `RpcFrameState` carries start args/intent
(including content-derived `xd://` targets) to terminal calls.
`roles/general_agent.yaml` prescribes `design/layout.md` → one saved part at a time →
mates for every joint → motion sweeps.
`online_turn.py` now owns worker turn-assembly helpers; retired `effort.py`,
`eta_estimator.py`, `ladder*.py`, `local_runtime.py`, `mission_criteria.py`,
`shadow.py` and their tests. Root `conftest.py` isolates the engine per non-MCP test
in mixed collections; `services/mcp/conftest.py` marks daemon tests during collection.

**Agent Hub / Parts Library:** `subagent_registry.py::list_sessions` reconciles stale
PID/start-time/heartbeat owners to `abandoned` and filters stamped records through
`subagent_list{project_dir}`. `host/src/ui/ai/ai_workspace.cpp` is the shell,
`agent_dag.cpp` the execution trace, `current_run_view.cpp` the project-scoped roster
and one docked inspector (no auto-popup); nav is Current Run / Plan / Activity /
Sessions / Insights / Options plus project chip. Activity/Documents/Plan and Insights
token spend follow `convProjectDir`; Insights has an All projects toggle (its model
leaderboard is explicitly global). `conversations.cpp::bindActiveConversationProject`
restamps project ownership to each sent workspace; `engine_events.cpp` stamps unsaved
`DocTab::projectDir` from the unique running conversation and gives an agent mirror
focus over a pristine empty active CAD tab, not edited user work. Parts Library
`applyResultSort` ranks local parametric matches first; Printables live CAD shows file/
license previews and download/import; CEF-off vendor skips collapse to one notice.
**Headless proof:** `scripts/run_mission.py` original prompt run 1 stalled before the
watchdog correction; run 3 (`scratch/mission-sessions/2026-09-27T03-01-59-466Z_01a0e0cf-f22a-7000-ab5a-67ba0cd711f9.jsonl`)
finished in ~25 minutes with seven printed parts, seven MG90S plus horns/sleeves,
23 components, six joint DOF, zero solver residual and no home-pose interference.
`simulate_motion` individual joint sweeps and bounded combined sweeps were clean;
unbounded combined ±90° folds did collide with the base, so safe travel limits
are documented in that run's `design/layout.md`.

## 2026-09-27 — "parts look unfused": same-domain faces after feature booleans

Every 6-axis arm part was one closed solid, but the viewport drew seams across flat faces
(upper_arm: 125 faces where 57 describe it). The primitive boss/cut, revolve, sweep, rib and
multi-region extrude paths used plain `geom::fuse`/`geom::cut`, so flush or overlapping
primitives left coplanar faces split. They now use `geom::fuse_unify` / new
`geom::cut_unify` (`core/geom/booleans.*`, `core/doc/document.cpp`); sheet-metal, split,
trim and hole cuts are unchanged. `stepplus_cache_engine_key` is now
`chudware-geomcache/5`, so saved documents recompile unified on open. Merged faces moved the
centroid/area of face selectors recorded earlier, and 6 of the arm's 22 mates dangled:
`selectors.cpp::resolve` now accepts, before dangling, the UNIQUE planar candidate on the
same plane with the same signed normal and at least the recorded area (two such faces still
dangle). Tests: `test_document` "primitive boss and cut leave no coplanar seam faces"
(11→8, 7→6 faces), `test_selectors` A9. Gotcha: after `./build.sh stage` an auto-spawned dev
hostd keeps serving the old engine until it drains — check `chudware-hostd --status`
before trusting an MCP smoke.

Same day, from the verification rerun: `new_assembly("sixaxis_arm")` in `/tmp/sixaxis-run4`
RESUMED the user's Desktop assembly of that name, open in the shared hostd, and the agent
added 13 components before noticing (it removed them; the file re-verified intact). By-name
resume now never crosses projects: `doc_new` takes `project_dir` and `resume_open_doc`
skips documents whose file lies outside it (`core/doc/session.cpp`, dispatch in
`libchudware.cpp`), and `cad_common._resumable_docs` / `_doc_new_scope` apply the same
filter to the Python name, path-stem and version-series lookups in `new_part`,
`new_assembly` and the `add_features` batch head. Unsaved documents stay resumable.
Test: `test_new_part_and_assembly_never_resume_another_projects_open_doc`. Rerun 4 built a
14-component arm: 7 printed parts, each one solid with zero redundant faces; 6 DOF,
residual 0, 0 collisions.

## 2026-09-27 — FeatureManager owner of a clicked face: op-history attribution + assembly owner_feature

Clicking any face of the user's turntable part highlighted `EdgeChamfer1mm`: `update_owners_for`
(`core/doc/document.cpp`) matched faces by exact TShape identity, and fillet/chamfer plus every
`fuse_unify`/`cut_unify` rebuild faces, so the last dress-up owned all 89. Geom ops now record their
`BRepTools_History` into a per-thread `geom::OpHistoryScope` (`core/geom/op_history.*`; booleans,
UnifySameDomain, fillet/chamfer/vertex-chamfer). The compile loop opens one scope per feature and
traces each source face/edge by reachability through the recorded Modified() relations; unreached
faces are generated by the feature, a face merged from several owners goes to the earliest feature,
and a feature with no reachable history falls back to same Geom handle + location. Fork-isolated
blends (`util::isolate_shape_build`) ship index-encoded provenance back through a new `sidecar`
string because the parent re-reads fresh TShapes. Turntable: 1 owner/89 faces → 24 owners, the
chamfer keeping its 36 bevels. Assemblies: `doc_resolve_pick` adds optional `owner_feature`
(`component_pick_owner` in `assembly.cpp`: a lazy `compile_document(..., with_owners=true)` per
component file stamp, pick matched by index and verified by area/length + centroid), consumed by
`featureTreeNoteAssemblyPick` to light the nested component feature row. Cache epoch
`chudware-geomcache/6`. Tests: `owner map: *` + `assembly doc_resolve_pick reports the component
part's owning feature` (test_document). Gotcha: a boss whose every face merges flush into older
faces now owns no face, so `doc_eval_overlap` can list it under `dead_features`.

## 2026-09-27 — "live previews not working": frozen assemblies, an overwritten open file, a hostd self-kill

Evidence: the GUI's journal for c217 (14:32–16:02) holds ~250 `live_mesh_prewarm` lines for
agent docs and zero `live_mesh_refresh`; c215 shows `live_mesh_refresh` on the user's open
assembly stopping at rev 130 and resuming only at rev 166, ten minutes later. Three causes.
(1) Tess-less revisions (`asm_mate`, `asm_add_component`, pose sweeps: `with_tess:false`) kept
the active viewport's previous mesh until the next mesh-bearing revision; the active view now
fetches the current tessellation (`doc_activate` on `st.rpc`, single-flight per doc,
`core/tess_catchup.h`, drawn only when it matches the displayed tree;
`host/tests/test_tess_catchup.cpp`). (2) The agent rebuilt the arm in a NEW assembly and
`save_part`-ed it over `sixaxis_arm.step+`, which the user's active tab held, so the tab showed
rev C throughout and a later save of it would have reverted the file; `save_part` now refuses
`path_open_in_user_tab` for a user-opened (`origin_kind:"gui"`) holder and names the doc to
edit in place (`cad_common.user_doc_open_at`; `test_save_over_the_file_open_in_the_users_tab_is_refused`).
(3) `WorkerBridge::call` waited unbounded for its lane: the Copilot usage rings'
`subscription_accounts` queued behind the running agent turn until the long-tier watchdog
killed hostd at 16:02 (docs to the recovery tier), and the GUI's single-FIFO `st.rpc` lane
stalled behind it, so an AI-tab click showed "Loading" for 9 min. A non-turn call behind a
running turn now fails at once with `worker_lane_busy` and every other lane wait ends at the
call's own timeout; the usage rings treat the busy reply as silent
(`test_agent_lane_call_behind_a_turn_fails_fast_and_hostd_survives`).
Not changed: agent-minted docs still open as background mirror tabs while a user doc is
active (the 2026-09-24 never-steal-focus rule).

## 2026-09-27 — "clicking the AI's part flashes Loading, then kicks me back to Part1"

Two causes. (1) The hostd lane fix above did not reach the GUI side: the agent job thread held
`WorkerClient::callMtx_` for the whole `agent_admin_turn`, so any worker-routed call on the
single-FIFO `st.rpc` lane (usage/PLM/Chudcode polls) blocked in `lk.lock()` and every later
`doc_activate`/`doc_snapshot` waited behind it for the rest of the turn (log: gen=1 never
delivered, gen=2..5 coalesced forever). In daemon mode a long run now serializes on
`longRunMtx_` only — it already streams on engine_backend's dedicated run connection
(`engine::runConnectionReady()`); pipe mode and a run without that connection keep `callMtx_`
(`services/worker/worker_client.{h,cpp}`, `engine_backend.{h,cpp}`;
`host/tests/test_worker_long_run.cpp` drives the real WorkerClient + AsyncRpc against a fake
hostd on a private socket and fails 4/4 on the old locking). (2) `AppState::ensureActiveTab`
re-points `activeTab` at the mirror's doc every frame; an async switch selects the tab before
its `doc_activate` lands, so any activation slower than one frame snapped the strip back to
the previous part and the late result landed "off-screen". `core::SwitchHold`
(`host/src/core/switch_hold.h`, `doc_tabs.cpp`) keeps the clicked tab selected until its newest
activation lands or fails; a failure still returns to the mirror's doc with the error toast, and a
mirror moved to a third doc (New/Open) is still followed (`host/tests/test_switch_hold.cpp`).
Not verified: the rebuilt GUI (no relink during the live session). Side effect worth knowing:
agent-lane calls (chudcode_*) made during a turn now reach hostd and get `worker_lane_busy` at
once instead of hanging for the turn.

## 2026-09-27 — model discovery refresh and Current Run navigation

`chudcode_models_list` had been re-reading the same no-session Chudcode process every five
minutes: the runtime never refreshed its in-memory catalog. On a catalog request,
`ChudcodeSessionHost` runs Chudcode's own `models refresh --json` off the worker call lane
when the last successful refresh is at least two hours old, with
five-minute failure backoff and a bounded/cancelled child process. On success only the shared
no-session catalog service is replaced; live conversations and running turns are not stopped.
The GUI polls every five seconds during discovery, then resumes its five-minute cadence.
Provider filtering precedes the worker's 512-row cap, so unrelated catalog entries cannot
displace late rows. A real refresh on this machine still returned five Codex GPT-5.5/5.6
models and **no GPT-6**; Chudware does not invent a model that the installed Chudcode
runtime does not advertise or accept. Existing conversation processes keep their own
catalog until they are closed/resumed; a newly discovered model may require a new session
until Chudcode exposes in-session model refresh. Current Run's main-agent trace row now
selects the main-agent inspector instead of leaving a sub-agent transcript on screen;
the sub-agent inspector has a `Back to main agent` action, step details return to the
selected agent, and conversation switches restore their own selection. Main chat remains
visible on the left. Files: `services/agent/{chudcode_host,worker_rpc}.py`,
`host/src/ui/copilot/settings.cpp`, `host/src/ui/ai/{agent_dag,current_run_view}.cpp`,
focused `test_chudcode_host.py` and `test_worker_rpc_chudcode.py`.
Gate status: focused Chudcode tests (64 passed), GUI build, AI workspace screenshot
smoke, and source/GUI lint passed. The full gate compiled core/GUI/WASM, passed
ctest, hostd and web parity, and passed `services/agent` (3436 tests); it recorded
seven failures in `services/mcp/test_face_attribution.py` (face-owner geometry, outside
this change) and timed out in the sensor-node demo. Neither failure was silenced.

## 2026-09-27 — GPT-6 discovery, c219 trace fixes, perspective close-ups, context picker

**GPT-6 Sol.** The Codex `/models` list is gated by `client_version`; Chudcode pinned
`0.144.1`, which the backend answers without the GPT-6 family (probed live: 0.144.1 → no
`gpt-6-*`; 0.156.0/0.157.1 → `gpt-6-astra|sol|luna`). Chudcode `a5ccf5afc9` bumps
`CODEX_CLIENT_VERSION` to 0.157.1; `~/.local/bin/chud` rebuilt (previous binary kept as
`chud.2026-09-27.bak`), and a live `openai-codex/gpt-6-sol` turn answered. CAD side:
`ChudcodeSessionHost.refresh_catalog_if_stale` also re-discovers when the resolved binary's
stat identity changes (`_binary_identity`), so an upgraded `chud` is picked up before the
2-hour TTL; static rows/defaults/rankings now name `gpt-6-sol`. The release lock still pins
v0.3.0 artifacts — a packaged build needs a new Chudcode release.

**c219 trace (6-axis, $64.64).** (1) 8 sub-agents and the main turn died on
`403 oauth_not_allowed_for_organization` from a second Anthropic account that was only ever
blocked 60 s while the first was genuinely exhausted; Chudcode `0ad857087f` disables such a
credential (verbatim `disabled_cause`), ignores already-reset exhausted usage windows served
from cache, surfaces the retained credential's failure, and pages `read("xd://…", off, lim)`.
(2) `enable_tools` loads now persist per conversation (`worker_rpc._turn_tool_categories`).
(3) `asm_edit_mate` merge-patches object values (a partial `value` wiped mate axes).
(4) `edit_feature` hoists op-level placement keys into `params` (`_hoist_edit_feature_keys`).
(5) `wall_thickness` returns `thinnest` spots with owning feature and now measures every body
(core `eval_thickness` defaults to the last body only). (6) Host-tool JSON reaches the model
compact (`chudcode_tools` `model_json`, −28 % on c219 results). (7) Skill tool-triggers trimmed
to workflow-specific tools; MORE TOOLS menu folds shared prefixes (9,742 → 8,005 chars).
**Prompt baseline.** CAD session clients run `chud --tools <CAD_SESSION_TOOLS> --no-skills
--no-lsp [--no-mcp]` (`--no-mcp` only when `--help` lists it; Chudcode `6b1e705d2e` adds it and
makes task sub-agents inherit `--tools`/`--no-lsp`/`--no-mcp`): Chudcode's per-request baseline
106,444 → 68,321 chars (get_state systemPrompt + dumpTools).

**Close-ups + attachment.** `render_part(target=<feature|sid|query|[x,y,z]>, view={azimuth,
elevation,dir,distance,fov,projection})` renders one 512×384 true-perspective shot via
`camera_pose.v1`, target ~70 % of frame, rest dimmed, occluded target ghosted, no labels on
the target (chip in the top band, triad, scale bar true at target depth). New engine RPC
`eval_feature_attachment` (core `session.cpp`, ctest `test_interference`) flags floating /
overhanging / edge-touching additive features; `verify_battery` `feature_attachment` and
`add_features` `attachment_warnings` report feature id + location + a suggested call, and a
no-view `render_part` on a defective feature aims the camera at the defect (hatched unsupported
patch / gap band). Root cause of the floating-rib class: sketches on "top face" follow the
part's current topmost face; role cards now seat sketches on the support face sid or a measured
datum, and verify numbers first, images only for the changed feature. `_face_attribution`
joins on (body, topo) across bodies; `test_face_attribution` updated to op-history ownership.

**Context picker.** Chips are `core::ContextRef` (`host/src/core/context_ref.*`,
`ui/copilot/context_chips.*`): background `doc_resolve_pick`, sent as structured
`context_refs` (contract addendum in `contracts/worker_rpc.md`), resolved by the worker into a
PICKED CONTEXT block with geometry summaries and targeting hints, drawn as icon chips on the
sent bubble, persisted (`"ctx"`), clickable to select in the viewport, carried by steers.
`CHUDWARE_SHOT=chat_context` shows both chip rows. Not driven live: chip click/select paths,
assembly face picks, restart re-attachment.
Gate: check.py 9/9, core+GUI+WASM build, ctest 100 %, hostd smoke, web parity, packaging,
`services/mcp` 3106 passed (test_face_attribution updated to op-history ownership),
`services/agent` 3444 passed after fixes (one test now importorskips `chudcode_rpc`, absent
from the gate venv), all other units green; the FIN25 sensor-node demo again hung until the
60-min cap, so the shot stage ran separately (`CHUDWARE_SHOT=m1`, exit 0). Chudcode commits
`a5ccf5afc9`, `0ad857087f`, `6b1e705d2e` are local on `main` (not pushed; `c0fff5e617` before
them was also unpushed).

## 2026-09-28 — stale doc ids after an app restart (c221 "no such document 'AI: upper_arm_revD'")

The app closed mid-run; on "continue" the resumed turn called
`render_part(doc_id="doc-cpp00000003")` from its transcript. hostd had restarted, only
`doc-cpp00000002` was open, and the call failed; the agent re-imported the part (which happened
to get `doc-cpp00000003` again). The card then read "no such document 'AI: upper_arm_revD' …
Open parts: sixaxis_arm_revD_final4=\"sixaxis_arm_revD_final4\"": `sanitizeActivityItem`
re-humanizes every row in place, every frame, with the CURRENT tab map, so the failed row was
rewritten after the fact with the re-imported tab's label (and persisted that way in `c221.json`;
`c221.raw.jsonl` has the real args). Fixes: (1) `activity_timeline.cpp` never humanizes a failed
tool row (glyph fold only), so a failure shows the exact ids the tool saw; (2)
`cad_common._doc` answers a well-formed `doc-cpp…` miss (also with zero docs open) with a
reopen-by-path hint instead of "pass one of the ids above"; (3) `worker_rpc._agent_admin_turn`
leads the first turn of a persisted conversation in a fresh worker (`chudcode_session_file` set,
session not yet claimed in `_HISTORY_SEEDED_SESSIONS`) with a RESUMED CONVERSATION block listing
the live `doc_list` ids/names/paths (`_resumed_documents_block`). Deferred: engine doc ids still
restart at 1 per daemon (`core/doc/session.cpp new_doc_id`), so a model that ignores the note
can still address a different part with an old id; making ids unique across restarts needs a
persisted counter in hostd. Tests: `test_current_part.py::test_stale_engine_id_is_refused_with_a_reopen_path`,
`test_worker_rpc_chudcode.py::test_resumed_conversation_first_turn_names_the_live_document_ids`
and `…::test_resume_note_survives_an_unreachable_engine`.

## 2026-09-28 — "no way for me to open/view/watch the motion simulations"

An agent's `simulate_motion` returned a trajectory to the model and nothing to the user: unlike
`run_fea_*` (`_persist_study`), it recorded no study, so the assembly's Simulation tree never
listed it. Fixes: (1) `cad_asm_tools.simulate_motion` records every successful sweep as the doc's
native Motion study (`_persist_motion_study`: GUI `studyFromJson` shape, name `Motion: <mate
names>`, keyed by type+name so re-sweeping a joint replaces it) and returns `motion_study
{doc_id, study_id, name, view}`; a `render_animation(kind="motion")` film carries it in its
`measured` caption. (2) hostd (`DocRegistry::postCall`) announces `sim_study_add|edit|delete`
as `doc.studies_changed` (contracts/doc_ops.md §29) — they stay read-only for access and bump a
tree-less revision, so an open Simulation tree never heard about an agent's study (FEA too);
`engine_events.cpp` calls `SimManager::markStudiesStale`. (3) The Motion Results pane offers
"Run motion" (it only said "Open Motion setup … then Run"); results stay unpersisted by design.
(4) GUI `runMotion` sent `min_clearance`, the engine reads `min_clearance_mm`: the GUI's minimum
clearance field was silently ignored. The sweep still never touches geometry, mates, poses or
expressions, but it now bumps the revision; the "tree unchanged" motion tests compare the tree
minus `studies` instead. Not covered: steer sweeps (`steer=`) have no mate to record, so a steer
film is still file-only. Tests: `test_hostd_registry.py::test_study_mutation_announces_studies_changed`,
`test_cad_tools.py::test_simulate_motion_records_one_study_per_joint_and_loop_closes_exactly`.

## 2026-09-28 — "no obvious way to open the motion sim; the Simulation tab shows none"

Root cause for `sixaxis_arm_revD_final5.step+`: no Motion study existed. Its saved
`document.json` has no `studies` key; the agent (session c227) never ran `simulate_motion`, it
only found two films (`design/renders/revE/sixaxis_revE_motion.mp4`, 09-27, and
`revF_arm_polished_motion.mp4`, 08:21) rendered before sweeps were persisted as studies and from
other assembly files. Separately, Model had no way into Motion at all: the Motion ribbon tab only
exists in the Simulate workspace and the legacy Simulation tab's Study group had no Motion
button. Fixes: `sim_motion_study` ("Motion Study", appended to Features > Assembly and
Simulation > Study in `ribbon_def.cpp`) switches to Simulate + the Motion tab and opens the
assembly's saved Motion study on its playback node, creating one only when none exists
(`SimManager::handleCommand`); an assembly with no studies now says "No studies saved in <doc>"
and that rendered videos are files, not studies, with a "New Motion study" action. The
`simulate_motion` `view` hint and `cad_guide('motion')` point at the new entry and tell the agent
to sweep the open assembly rather than only cite old films. Not covered: with several Motion
studies the button opens the selected/first one; the others stay in the Simulation tree.

## 2026-09-28 — 6-axis trace review (c221/c223/c227/c229/c230/c233)

Six gpt-6-sol conversations on `~/Desktop/6-axis` (arm `sixaxis_arm_revD_final5.step+`).
**(1) A turn was replayed unattended.** c221's app went away at 22:58:55 local and its worker
died mid-`agent_admin_turn`; `WorkerBridge::call` respawned the worker and replayed the same
request, which ran 23 minutes with no GUI in a NEW Chudcode session (the GUI later adopted
it as the "external" c223): it rewrote `roboarm6/firmware` (claw axis), built it, and pushed
two commits to the user's GitHub. `core/hostd/worker_bridge.cpp` now never replays a turn
the worker received ("closed waiting for response"); it answers `worker_lost_mid_turn`
and the conversation resumes from its bound session on "continue". A child lost before
the request was written still respawns once (`test_worker_death_mid_turn_is_not_replayed…`,
which replaced the test that pinned the replay). **(2) Hunter's personal coding-agent brief
reached every CAD turn.** Chudcode loads user-level context files in RPC mode:
`~/.claude/CLAUDE.md` ("`git pull --rebase` before starting; commit + push as you go") sat
in the CAD system prompt (probed with `get_state`), next to Chudcode's `<active-repo-context>`
naming the only nested git repo, `roboarm6/`, "Active project". Every gpt-6-sol turn opened
with `git pull --rebase` in `roboarm6`; c221/c223 committed and pushed. `_make_client` now
passes `--config <data>/chudcode/cad-session.yml` (written by `_cad_session_overlay`, gated
on `--help` like `--no-mcp`) disabling `context-file:user:{AGENTS,CLAUDE,GEMINI,
copilot-instructions}.md`; project context files in the workspace still load. The
active-repo block is unconditional in Chudcode, so `general_agent.yaml` HOW TO WORK 7 says
nested repos belong to the user: no git pull/commit/push, no edits unless asked.
`_binary_supports_flag` now caches `--help` text per binary identity, not per flag.
**(3) CAD work inside eval was invisible.** gpt-6-sol drives CAD through eval's
`tool.write("xd://add_features")`; those host calls carry eval-bridge ids (`js-write-…`) no
tool frame owns, so c230 showed 49 "eval" rows and zero add_features rows, and the progress
watchdog never counted those mutations. `run_turn` binds a `host_call_observer` on the run
context; `HostToolBinding.execute` reports `host_tool_start/_end` frames for top-level-agent
calls, and the host forwards only ids no in-flight Chudcode tool call owns (parented to the
single open call), through the same mapper and watchdog
(`test_cad_calls_made_inside_eval_get_their_own_feed_rows_once`).
Not fixed, agent quality: c230 edited `forearm_lower` in place but left the open assembly
unrefreshed and never swept it; c233 ended with FDM `dfm_check` failing (0.118 mm yoke wall)
and `feature_attachment` 0.6 when the machine hung (nvidia-drm pageflip timeouts, reboot
15:26); c227 linked two old films instead of sweeping the open assembly (guidance fixed in
c631d8698).

## 2026-09-28 — project memory: one real switch, per-folder, Sharpshooter default

Correction to the entry above: CAD sessions were NOT pooling memory. Chudcode's mnemo
default scoping is `per-project` keyed by the process cwd (the workspace), and the 6-axis
turns wrote only to bank `…-6-axis-1dcd9kaxtypzw` (10 rows, all 6-axis prompts). What was
wrong: (1) the Settings "Project memory (per-folder)" checkbox drove only the pre-Chudcode
`.plm/memory.db` store (last write 2026-08-28) — the memory the agent actually used ignored
it; (2) the scoping came from the user's global Chudcode config, so a `global` setting would
have pooled every project; (3) a session with no workspace ran with the worker's cwd (the
Chudware tree) and so shared the dev repo's bank. Now the setting is a backend choice —
Settings → AI → Harness and the copilot menu, "Project memory": Off / Decisions (Chudcode
`sharpshooter`, default) / Full recall (`mnemo`) — persisted as `agent.memory_backend`
(`config.memory_backend`; a legacy `memory_enabled:false` reads as off, and
`memory_enabled` still mirrors it for the offline loop). The worker sends it as
`memory_backend`; `_make_client` writes `cad-session-<backend>.yml` pinning
`memory.backend` (+ mnemo `scoping: per-project`), adds only that backend's tools to
`--tools` (mnemo: retain/recall/reflect; sharpshooter: none — chud exits on a tool its
backend lacks), gives a no-workspace session `off`, and falls back to mnemo when
`chud config list --json` shows no `sharpshooter.*` settings (an older chud exits on the
unknown backend; probe cached per binary identity like `--help`). Sharpshooter is ported
from oh-my-pi 18.4.2 in Chudcode `06dc85d486` (local, not pushed): per-project decision
files under `~/.chudcode/agent/memories/sharpshooter/<basename>-<hash(cwd)>/`, extraction
model = `sharpshooter.model` → smol role → the session's model; `~/.local/bin/chud`
rebuilt (previous kept as `chud.2026-09-28.bak`; the build also carries the uncommitted
Chudcode updater fix in that tree). The pinned release artifacts are still v0.3.0 without
it, so packaged builds get mnemo until a Chudcode release. The choice applies when a
conversation's Chudcode process starts (new conversation or app restart). Verified live
against both binaries: sharpshooter scope `6-axis-1dcd9kaxtypzw`; mnemo `per-project`;
no workspace `off`; old binary + sharpshooter → mnemo, starts.
(`test_memory_is_the_projects_and_its_tools_match_the_backend`,
`test_memory_backend_config.py`.)

## 2026-09-28 — tool-surface gaps from c211–c233 + host-tool execution edge cases

Mined the 19 Chudcode sessions since 09-20 (2,536 tool calls). Execution fixes in
`services/agent/chudcode_tools.py`: JSON-text returns (most MCP tools) now reach Chudcode
with structured `details` (they had none, so every `{"ok": false}` looked like success to
the feed, the records and the watchdog); a result over 46 000 B (Chudcode spills >50 KB and
the xd device then REPLACES `details` with truncation meta) is shortened to valid JSON by
`_shorten_largest` (largest-saving list/string/map halved first; top-level keys kept) with
`_truncated.full_result` naming the complete JSON under `<data>/cache/tool_results/<session>/`;
eval-bridged calls (`js-…` ids) get no first-touch skill prefix (it broke every
`json.loads`) and do not consume it; the host dispatch path now runs
`agent_turn._reconcile_kwargs` (was a raw `TypeError … <locals> … 'message'`), strips
declared `progress`/`cancel` from advertised schemas and never lets a model value replace
the host callables. `chudcode_events.cad_result_payload` reads `details` or
`details.xdev.inner`: CAD ok:false cards fail with the tool's own error/outcome_kind, and
`ProgressWatchdog` no longer counts a failed xd mutation as progress. New tools:
`read_native_file` (saved `.step+` tree/params/mates/materials without opening — agents ran
`unzip|jq` ~40 times), `wait_job` (bounded blocking wait; replaces `sleep 600; ps -p`),
`render_animation(keyframes=6)` (labelled keyframe sheet incl. first collision — agents
shelled out to ffmpeg), `dfm_check` FDM `overhang` + `build_dir` + `suggest_orientation`
(per-triangle on `cad_common._doc_mesh`, curved faces included; small horizontal bores
count as overhang — bridging via `max_bridge_mm` is not modelled yet). Appearance presets
gained wood/dark wood/mdf/clear/rubber (+ plywood/birch/oak/acrylic/tpu… aliases);
`simulate_motion` guidance timeout 300 → 900 s. `MAIN_CHAT_CORE_TOOLS` adds the tools the
sessions loaded on demand (`list_world_bodies`, `inspect_native_files`, `interface_check`,
`render_animation`, `dfm_check`, `analyze_stl_geometry`, `inspect_section`, `get_job`) plus
the two new ones; `contracts/copilot_modes.json` allows `read_native_file` (ask, plan) and
`wait_job` (plan). Not done: the Chudcode-side "No such tool: xd://<cad tool>" for an
unloaded tool still does not say `enable_tools` loads it.

## 2026-09-28 — text thumbnails: a file explorer for the models

`services/mcp/model_card.py` builds a CAD file's card straight from the file, never by
importing it: a `.step+` gives kind/name/material, feature histogram and suppressed count
(`document.json`), revision, volume, bodies and failing features (`cache.json`), and bbox plus
three shaded ASCII silhouettes — top (−Z), front (+Y), right (−X), denser glyph = nearer — from
area-weighted samples of `cache.aicm`; binary/ASCII STL get bbox + silhouettes; STEP a header
card. Cached per (path, mtime, size): 21 parts cold in 2.8 s, repeat free. Surfaces:
`browse_models(path, pattern, thumbnails, limit)` (files family, main-chat core, Ask/Plan;
thumbnails auto for ≤12 models; the whole 6-axis project is 14 KB with them off),
a one-line `card` on `fs_list` CAD rows (first 60), and `fs_read` of a CAD file now returns
`ok:true, kind:"cad_preview"` with the card instead of a failed `use_import_part` redirect
(an unreadable container still fails, carrying its card). `tool_registry._READ_EXACT` now
includes `browse_models`, `read_native_file` and `wait_job` — they had defaulted to WRITE,
which also let the progress watchdog count a listing or a job wait as modeling progress.
Assemblies saved without `cache.aicm` (reference assemblies such as ME-490 Rover Review) get
counts only, no bbox/thumbnail.

## 2026-09-29 — model explorer: real labelled thumbnails replace the ASCII silhouettes

The ASCII views were inaccurate and cost ~8 KB per dozen parts, so they are gone.
`browse_models` now returns numbered contact sheets (`services/mcp/model_thumbs.py`): 4×3
tiles, each the part's iso product shot trimmed to the part, with a caption (file name,
kind, size mm, material, features/components/mates, volume, revision) and a red badge on
failing features. The text rows shrink to `{n, file}` plus failures; a 12-model sheet is
~1,320×890 px (~1.5k tokens). Thumbnails come from the file's own display mesh first
(`services/mcp/mesh_preview.py`: numpy z-buffer with the engine's iso camera (1,1,0.8),
flat Lambert shading in the body colour, silhouette/crease lines, 2× supersampling; 0.4–3 s
per part, ~1 s for a 524k-triangle assembly) — no document session and no hostd traffic.
The engine is the fallback for files without a mesh (STEP/IGES/reference assemblies):
`doc_open(internal:true)` (hostd never announces it, so no GUI tab) → `doc_render_png` →
`doc_bbox` → `doc_close`. The measured bbox is written to `<key>.json` beside the thumbnail
so exchange-file tiles show their size. The cache is the GUI PLM grid's own
(`<project>/.plm/thumbs/<content_key>.png`, key = blake2b-8 of document.json =
cad_graph `meta.dh`), so either side's render is the other's hit. Renders beyond a 45 s
per-call budget are `pending` and fill in on the next call. Live folders sort before
archive/backup copies (in 6-axis, 70 of the 94 files are in archive folders). `fs_read` of a
CAD file attaches one labelled tile.

## 2026-09-28/29 — ViewNet: the agent finally sees its geometry, from the right angles

**Root cause first.** Every online CAD render the agent requested reached the model as the
string `"<mcp.server.fastmcp.utilities.types.Image object at 0x…>"`: the Chudcode host-tool
adapter (`chudcode_tools._json_safe`) stringified FastMCP `Image` objects, so render_part /
contact_sheet / inspect_* were blind in every session (c205/c213/c217 and 17 placeholders in
2026-09-28 sessions alone). `services/agent/tool_images.py` now turns them into image blocks
(downscaled to 1568 px / <3 MiB, never dropped), for top-level, sub-agent and eval-nested calls.
**Shot planner.** `render_part` with no `view` returns one 2x2 grid of the 4 best
complementary perspective shots (`services/mcp/shot_planner.py`): ~160-250 candidates (icosphere
+ target-normal lattice + x-ray/section candidates) are measured in one native
`doc_view_metrics` call (session.cpp; per-face visible true area/|cos|/occlusion, per-edge
visibility + crease shade contrast + silhouettes, normal entropy, principal alignment; 170
cameras in 0.3 s vs 1.2 s per camera in the old Python raster) and chosen greedily on a
set-valued utility (trained `services/viewpoint/utility` model when `weights/utility_v1.npz`
ships, else `features.hand_utility`: information-weighted legible coverage, crease
crispness, anti-flattening, revolve-axis-stare penalty, redundancy dedupe). Hidden targets
get x-ray tiles (translucent occluders, opaque target, dashed hidden edges) or section tiles
(`doc_render_view view.section`: hatch cap, cut outline, ghosted removed half). Tiles carry
SID face tags + a caption legend whose SIDs tools accept verbatim; the old
`cad_viewpoint.ideal_view_dirs`/`score_view`/`_legibility_score` lattice is deleted.
**Drawings.** HLR provenance gives every projected edge its model body/edge/SID so
`add_dimension` hits the exact instance (radius join removed); every drawing view was
projected from the opposite side of contracts/doc_schema.md (front upside-down, top seen
from below) — fixed at the source. **Engine.** AICM shm names are pid-scoped
(`p<pid>_<doc>_r<rev>`): concurrent engines were unlinking each other's live meshes.
**Learning (LLM-free by the owner's decision).** Labels: `training/viewpoint/datasets`
(`chudware.geomlabel/1`, geometry-matched faces; MFCAD MIT, an OCP port of the MIT MFCAD++
generator with curved features, GSL; MFCAD++'s zip is Cloudflare-blocked and the HF mirror
gated; Fusion 360 Gallery is non-commercial). Judge: a from-scratch cross-fitted Observer CNN
(`training/viewpoint/observer`) that must read labelled facts (counts, opening side,
through/blind, type) from exactly the agent's grids — its P(correct) is the usefulness label.
Utility: DeepSets over face tokens predicting it; `training/viewpoint/campaign.sh` runs render
→ trials → observer folds → labels → utility → convergence report → A/B, resumably.
Gotchas: never relink `core/build-linux/bin/libchudware.so` mid-campaign (the render index
keys on the library); `RenderPool` has a stall watchdog because a pool whose workers vanished
hung the first campaign for 2 h. Windows: MSVC builds the whole windows-occt tree (native-build
Windows row now uses the shipping preset); remaining Windows/macOS ctest reds are
`mesher_advanced` (hours-long) and `sim_cache`/`fea_contact`, outside this work.
**Observer must read pixels (2026-09-29 evening).** The first observer report showed ~90%
of its accuracy came from the query vector (blind .530 vs full .577): queries carried
centroids/axes/location flags. Query schema v2 (`observer/queries.py`, `requery` migration,
qvec-only probe in the report) removed the leak, after which the CNN learned nothing from
pixels (full ≈ blind ≈ .28). Fix: dense per-pixel supervision (`observer/dense.py`) —
CANON class + feature/depth edge masks rasterised from the cached grids' reconstructed
cameras (no RGB re-render; a silhouette-registration check refuses misaligned masks, which
caught a trial fixed-camera index offset) and a joint decoder head. Held-out probe (788
parts, 8 epochs): full .524 vs blind .385; type .529 vs .133; per-pixel mIoU .42. The box's
GPU occasionally hard-resets (hardware), so `campaign.sh service` installs a reboot-surviving
user unit with per-run stage done markers; fold training resumes per epoch.
**Readable grids (2026-09-29 night).** A visual audit of 13 real parts × whole/visible/hidden
modes (`scratch/planner_qa/`) found 29/39 grids with flattened, edge-on, axis-stare or
cap-obscured tiles and SID chips painted over the target in every target grid. Fixes:
`shot_planner` prefers true revolve/cylinder axes, applies one readability ladder (screen
footprint, visible thickness, legible area, cap/axis-stare) to ordinary, section and x-ray
candidates but relaxes it rather than ever returning <4 tiles, and dedupes rotational/mirror
symmetric cameras; diagonal sections are appended inside `plan_shots` only, so the public
candidate enumeration that stored training indices (`observer/dense.py`) replay is unchanged.
`_annotate_render` anchors every chip on its face's winning pixels (x-ray uses the target
pass), one chip per face, never on the target mask. `render_part` now passes boundary
conditions to the planner: study fixtures/loads (queries, SIDs, GUI selectors, thermal
faces) and assembly mate faces as `bc`, the target's adjacent faces as `context`. Sim
contacts are transient body-pair interfaces with no persisted face refs, so they are not a
BC source yet. Result: profile criterion 36/39 (was 10/39), tags 38/39 (was 13/39), 4 tiles
39/39, plan latency −10%.
**Campaign results and ship decision (2026-09-30).** Full-data observer (5-fold cross-fit +
independent judge, 10 epochs, dense heads): held-out fold 0 full .489 vs blind .267 vs
shuffled .261 (majority .256), type .639 vs .074; learning curve .352→.419→.441→.484 over
1/8→1 of 3,827 train parts, still rising (more parts would pay); seed std .006; mIoU .52.
Utility (DeepSets, 263k labelled pairs, two rounds): CONVERGED (last doubling −0.38% val
log-loss, seed std .0005, shuffled control at prior) and beats calibrated hand_utility
offline (test log-loss Δ .096 [.084, .111]). Online A/B, 548 held-out test parts,
independent observer P(correct): hand_greedy@4 .519, model_greedy@4 .512, random@4 .488,
fixed4 .443, iso@1 .382. A ranking-objective + hand-residual retrain (within-(part,query)
conditional logit, selected on val only, one test A/B) still lost: model−hand −.004
[−.008, −.0003]. **Not shipped**; the planner stays on `hand_utility`, which measures +.137
over a single ISO view and +.077 over a fixed 4-view set. The learned-utility path
(`weights/utility_v1.npz`, residual-on-hand inference) stays wired and gated by `ab --ship`.
Gotcha: the repo conftest's simsuite resguard sets `RLIMIT_AS`, which breaks torch CPU
backward in the observer tests on a busy box — run them with `SIMSUITE_RLIMIT=0` under a
cgroup cap.

## 2026-09-29 — extruded parts no longer show their sketches through the solid

An agent's keychain (sketch → extrude, raised emblem sketched with `params.offset` 3.5 →
extrude) showed both profile sketches over the finished part, drawn with no depth test, so
the solid looked see-through and "not merged". There were two causes. (1) The viewport's ghost
pass (`SketchMode::refreshGhosts`) counted a sketch as consumed only for a region selector
`{"kind":"region","sketch":fid}`; the engine's list form `refs.profile:["f11"]` (what
`add_features` writes) kept it drawn. Consumption is now one header-only rule,
`host/src/core/sketch_consumers.h` (`core::consumedSketches`: profile lists/ids/regions,
`*sketch*` keys, sweep `path`/`guides`, loft `sections`, never pattern `features`), shared with
the FeatureManager's sketch nesting (`test_sketch_consumers`). (2) The engine's
`resolve_sketch_plane_doc` ignored the sketch's `offset` that `compile_document` applies, so
every session-side frame (ghost/overview, live sketch overlay, region picker, convert) sat on
the unshifted plane; it now shifts by the evaluated offset (`test_sketch_on_face` "offset
sketch"). Staged with `./build.sh stage`; the app picks it up on restart.

## 2026-09-30 — 6-axis redesign turn failures: selector injection, sketch edits, sections, import copies

Triage of every failed call in the two latest 6-axis turns (c237 redesign, 73 failures incl.
nested subagents; c239 slicing). Fixed: (1) `workspace_status({})` failed in BOTH turns because
`chudcode_tools._document_args` injected the live GUI run id into a declared-but-omitted
`run_id`; tools now declare model-facing selectors with `tool_registry.model_selectors(...)`
(workspace_*, dag_*, buildgraph_status, list_artifacts, piping/reveng/mfg run selectors,
graph/taxonomy `node_id`) and omitted selectors stay omitted; ledger/mission tools keep
context injection. (2) `edit_feature` replacing sketch entities/constraints skipped the
creation normalizer (construction default, c<N>/e<N> ids, `fixed`→`fix`, rectangle/polygon
expansion, scalar `refs.profile`); creation and edit now share
`normalize_inline_sketch_entities` (`core/doc/session.cpp`), a `fix` on an expanded compound
pins every emitted segment and other relations on a compound fail by name;
`reorder_feature` resolves same-batch `$N` in `id`/`before`; `sketch_convert` SID refs use the
SID-aware resolver (was `measure ref kind ''`). No cache-key bump: stored documents compile
identically (these inputs previously failed validation). (3) `inspect_section` reported
"open contour" on valid solids whose plane hits coplanar steps (degree-3 vertices); sections
are now `BRepAlgoAPI_Common(plane face, solid)` per solid; the open-surface path reports
`open_locations`. A retired measure SID stays a refusal but names `live_selectors`.
(4) describe_part on the wrist took 38–84 s: `doc_face_points` re-described every face per
sampled selector; descriptors + bbox diag are cached per (revision, exact shape). (5) c239's
`import_part(parts/redesign/base.step+)` minted a root `base_revD.step+` at the terminal sweep:
detached native opens now carry `doc_list.native_source_path` (contracts/worker_rpc.md),
are `reproducible_import` while unedited, and save back in place after edits. Also
`read_native_file` names only the unknown sections with `did_you_mean`. Deferred: the
process-wide `daemon_bridge` client serializes all subagents' engine RPCs; a pool is unsafe
because hostd ties rw attachment/CAS to the connection (`doc_registry.cpp` postCall/preCall) —
needs a logical-client identity seam. Chudcode-owned, report-only: unloaded `xd://` reads say
"No such tool" without an `enable_tools` hint; Wayland `computer.screenshot` lacks pipewire.
The stray `~/Desktop/6-axis/base_revD.step+` from c239 was left in place.

## 2026-10-01 — nightly release runtime provisioning and deterministic contact admission

Nightly capability checks now use `scripts/pin_chudcode.py ensure` to verify an
explicit installation or fetch a digest-keyed runtime cache from the private
CAD repository's `chudcode-runtime-v<locked version>` release, authenticated by
the existing read-only job token. The mirror never changes the authoritative
lock digest; the API token is not forwarded to GitHub's signed release CDN,
and format/hash checks precede native version execution. Linux cold fetch and
offline cache reuse were exercised; the macOS mirror's hash and Mach-O target
were checked on Linux (native execution remains a macOS CI check). The Linux
nightly's actual failure was an incidental error-text assertion in
`core/tests/test_mesher_advanced.cpp`: a legitimate 3.1 GiB direct-solve
estimate exceeded the runner's 3 GiB study budget. The bore regression now
replays deterministic 3 GiB refusal and 8 GiB delivery cases and checks
structured admission evidence plus the original contact-resolution invariants.
`docs/reference/CI_RUNNERS.md` documents the mirror and Windows build/pin
prerequisite; Windows remains fail-closed until real Windows bytes are built
and pinned. No desktop release was published during these local checks.
Follow-up from the headless rerun: hostd idle-exited (120 s) under the worker's parked MCP socket
and its teardown aborted — `~Server` checked `joinable()` then `join()`ed while the woken client
loop `detach()`ed itself (std::system_error → SIGABRT core). Threads are now moved out under
`clientsMtx_` (and started under it); the tail detaches only a joinable thread. A clean idle exit
or completed soft drain now pushes `{"event":"daemon.exiting","reason":"idle"|"drained"}`, and
`DaemonClient.call` reconnects on such an announced close instead of failing the next call as a
lost session (unannounced closes — crash, kill, hard drain — stay loud; never replayed).

## 2026-10-01 — linux-gate builds host test binaries before CTest

`scripts/gate.sh` now builds the native host default target set rather than only
`Chudware`: the latter produced a valid GUI and CTest configuration but none of
the host test executables on clean CI checkouts, so the following linux-gate
CTest step reported missing binaries (`test_img_tex` first, then `test_updater`
and the rest). Updated `.github/workflows/linux-gate.yml` comments to describe
the build/run boundary. The first real default-target smoke also exposed
`test_splat_renderer`'s missing core include directory for
`device/numeric_parse.h`; corrected that target in `host/CMakeLists.txt`.
The real host build stage ran with `CHUDWARE_GATE_STRICT=1`, Release, and CEF
enabled in a fresh scratch tree (reused dependency source downloads; four build
jobs), then host CTest passed all 30 tests, including the splat and HTTPS updater
tests. The live `host/build-linux` tree and open application sessions were not
touched. Run 36793532370 separately stopped before host CTest because training
pytest collection exited 2; its step log does not contain the collection traceback.

## 2026-10-01 — nightly release unblocked on Linux / Windows / macOS

`nightly-release` had failed every leg for 5 days. Chudcode runtime: legs now fetch the pinned
binary from this repo's `chudcode-runtime-v<ver>` release mirror with the job token
(`pin_chudcode.py ensure --github-mirror-repository`, sha from `CHUDCODE_ARTIFACT.lock`), so no
runner pre-install or PAT; Windows `chud.exe` is built natively on the Ryzen runner by
`chudcode-runtime-windows-build.yml` (source = mirrored archive of the locked commit; smoke +
RPC v2 verified, then pinned). PowerShell 5.1 gotchas hit there: `bash` is a WSL shim (no
bash composite actions), native args lose embedded `"`, `ConvertTo-Json` is not canonical.
Platform fixes surfaced once the legs compiled: libc++ has no floating `from_chars`
(splat PLY parse → `device::parseFiniteDouble`); MSVC needs a static constexpr in a captureless
lambda and resolves symbols in unreferenced COMDATs (`test_agent_job_routing` stubs); contact
criteria were compared by pointer (`sim::criterion_is` now); `test_worker_long_run` is
developer-build only (production clients ignore the fake hostd override by design); native
relocation compared ancestor spellings (`C:\WINDOWS` vs `C:\Windows`) and refused its own temp
dir — paths are long-name/drive-normalized and ancestors compared by `fs::equivalent`.
It also refused every stored Windows absolute dependency (`C:\...`) as foreign-platform syntax;
`native_reference` now classifies paths by host platform (drive-relative/root-only still refused).
Windows and macOS release builds have NO CHOLMOD (scalar SimplicialLDLT, ~50x slower on big
contact solves): the bore contract's success case runs a reduced same-radius model there
(`CHUDWARE_WITH_CHOLMOD` define on the test), and `mesher_advanced` has TIMEOUT 3600. Shipping
CHOLMOD on those platforms is an open owner decision (its Supernodal module is GPL).
`linux-gate` built only the `Chudware` target, so its host ctest step found no binaries; gate.sh
now builds the host default targets. Runner contention: `livingroom-pc` is the only
`cc-linux-build` runner, so linux-gate, the Stripe gate of deploy-prod and the nightly Linux leg
serialize on it.
macOS follow-up: the LEB-region quality pass stopped after a fixed 5 passes even with cells under
its own 0.05 floor, and processed boundary faces in `unordered_map` order (libc++ ≠ libstdc++),
so the pin-in-bore region shipped jacobian_min 0.0479 on macOS. `core/sim/mesher.cpp` now walks
faces in canonical sorted order and iterates to convergence (floors met, or no repair and no
improvement of the deficient-quality multiset); `chudware-simcache/7`.

## 2026-10-01 — responsive AI workspace, bounded authoring and consistent Explorer operations

Implemented three independently owned UI slices in parallel, then built and exercised the
integrated native application. `host/src/ui/ai/ai_workspace.cpp` retains the existing
three-column layout when chat, navigation and a useful compartment fit; smaller logical
widths use a full-width Chat/Workspace switch and a selector for all six compartments.
The existing conversation, draft and selected-compartment state is shared across layouts;
run-follow processing remains active while the compact Chat pane hides the utility view.
`host/src/ui/ai/rules_authoring.cpp` uses the shared command-dialog placement helper every
frame, keeps its close row fixed, and scrolls authoring content instead of clipping input,
preview or actions after resize. Hints and errors wrap; Enter remains a newline and
Ctrl+Enter submits. `host/src/ui/file_explorer.cpp` gives the Files tree an explicit height
based on the outer pane, uses path-stable tree IDs across label elision, reveals a new folder
through its ancestors before focusing inline rename, and keeps reveal pending through
Loading listings. Successful renames remap tracked Explorer paths at folder boundaries.
Folder paste refuses self/descendant destinations, and copy collisions preserve the entire
dotted folder name rather than treating its final component as a file extension.

Verification: native default-target build succeeded; host CTest passed all 30 tests.
Isolated Xvfb application interaction reproduced the original cramped 640x480 workspace
and offscreen authoring popup, then exercised all six compact compartments, draft retention
across Chat/Workspace and 640x480/800x600/1340x838 resize, the wide layout, and the compact
layout at 2x scale. Actual rule creation persisted its pack and keywords; multiline input,
Back, Create another, duplicate-create errors and reachable close controls were exercised.
Explorer interaction verified expansion across pane resize, new-folder reveal/rename,
creation and clipboard paste after parent rename, refusal of self/descendant paste, and
`Rev 1.2 (copy)` / `Rev 1.2 (copy 2)` with byte-identical copied payloads. These were native
runtime checks; browser asynchronous-listing behavior was not visually exercised.

The broader gate additionally passed the native core/host builds, scratch WASM bundle build,
all 111 core CTests, hostd smoke, web parity and UI style checks. Its system-Python audit
and packaging invocations lacked `mcp` and `pytest`, respectively; running those checks with
the provisioned service interpreter passed all nine source gates and all 22 packaging tests.
The full gate is **not green**: manual regeneration reported drift, the Python sweep recorded
17 failures across agent tool guidance, DFAM, MCP intake/hostd hold, worker streaming and DVC
tests, and the sensor-node golden demo reached the gate's 3600-second deadline after nine
passing legs through thermal. No global PASS is claimed, and those non-UI failures were not
changed or suppressed by this UI work.

## 2026-10-01 — real slicing pipeline, deterministic DFAM checks, G-code floating-extrusion proof

Trigger: an agent sliced the 17-part 6-axis arm for an Ender 3 with no slicer integration —
it extracted a Fedora PrusaSlicer RPM into /tmp, hand-wrote ~100 slicer keys (sheathless
snug supports at 3 mm spacing, invented start G-code, 0..220 bed), reused design-time
`build_dir` vectors that stood tall parts upright, and "verified" only bounds and
temperatures. The prints failed. The new `verify_gcode` on those 17 files fails 5 of them on
mid-air extrusion (overhang perimeters floating 12–18 mm, a 52 mm unbacked bridge on
upper_arm) and warns on 3 more.

New: `services/dfam/` (numpy/scipy only) — `PrintMesh` (engine tessellation joined to face
sids/owner features), uniform-grid Möller–Trumbore ray caster, depth peeling, signed-winding
voxels; checks `min_wall` (area-sampled, knife-edge wedges warn instead of failing),
`overhang` (>1 mm² needing support fails, bed-contact/one-layer strip/bridges/small horizontal
holes excluded), `bridges` (two-sided anchors; >limit warn, >2×limit fail), `support`
(envelope volume, landing on model, trapped support ≥10 mm³ behind a <3 mm opening fails),
`small_holes`, `internal_voids` (sealed cavities, floating bodies), `bed_adhesion`,
`build_volume` (searches yaw), a deterministic material/time estimate, and
`optimize_orientation` scored by absolute terms (support/part volume, time excess,
overhang/surface, contact/tip stability, strength, preserved faces) — min-max scaling picked
an edge-balanced pose for forearm_lower. `services/manufacturing/slicer/{prusaslicer,
vendor_bundle,gcode_check}.py`: stock-PrusaSlicer discovery (CHUDWARE_SLICER_BIN → user/system
flatpak with `--command=prusa-slicer` → standard paths), private datadir, flatpak filesystem
grants, bounded process group; vendor INI bundles resolved at runtime (inherits, compatibility
condition parser, "ender 3" ranks the plain Ender-3 above V2/Pro/S1/BLTouch/Max); G-code
checks incl. role-aware floating extrusion (hard: perimeters/support unanchored >2 mm fail;
anchored: overhang perimeters / one-ended bridges >10 mm fail; two-ended bridge or support
spans are backed, >25 mm warn; infill/interface spans informational; support may start
within 1.5 mm above lower strata). `scripts/provision_slicer.py --check|--install`.
`services/mcp/print_tools.py` (family `cam`): `slicer_status`, `list_print_profiles`,
`optimize_print_orientation`, `slice_part`, `verify_gcode`. `slice_part` takes vendor presets,
goal settings, measured orientation, organic supports when needed (and support under bridges
the analysis measured past 2× the limit), `overhangs=1`, brim on tip risk, writes
gcode + 3MF + `.print.json` manifest, and sets `print_ready` only when the G-code check passes
and no non-support printability row fails. `dfm_check` FDM now runs the DFAM rows (legacy
overhang keys kept; `printer=`, `checks=`, `detail=`); `warn` rows pass the verdict.
Exposure: `print` category pack, main-chat core tools, tool guidance, `cad_guide("printing")`
cheatsheet, general_agent 3D PRINTING block, read-risk entries, regenerated GUI tool tables.

Verification: dfam/slicer unit suites 105 passed; `test_print_tools.py` (real engine + real
flatpak slicer) 7 passed; `test_intake_ledger.py` 24 passed after updating dfm assertions that
pinned the old implementation (sub-1 mm² ledge area, 6-axis orientation list, horizontal
cylinder now excludes the first-layer strip). Live: all 17 arm STEP parts imported and run
through dfm_check + slice_part (Ender-3, PLA 220/60): 39.9 h / 326 g vs the old 46.9 h /
361 g, every G-code passes the floating scan (old: 5 fail), 9/17 print_ready; the other 8
are blocked by real geometry (0.001–0.86 mm walls, support trapped in servo pockets) that
dfm_check names by face. Gotchas: PrusaSlicer 2.9.6 headless flatpak has no `--arrange` and
segfaults on bare `--merge` (use `--dont-arrange --merge`); vendor Creality presets ship
`overhangs=0` and `dont_support_bridges=1`; `;HEIGHT:` after `;TYPE:` is extrudate height,
not plane pitch. Deferred: SLA/SLS process profiles in `core/dfm` (DFAM already supports them),
per-project remembered printer, GUI surfacing of print jobs.

## 2026-10-03 — algorithmic multiphysics topology optimization

Added native `core/sim/topopt_*` problem preparation, reusable sparse physics, analytic/adjoint sensitivities, constrained SIMP/RAMP MMA updates, genuinely bidirectional BESO, and reaction-diffusion level-set evolution. The explicit capability matrix supports SIMP/RAMP across structural, conduction, modal, prestressed buckling, Stokes-Brinkman flow, thermomechanical and CHT physics; BESO/level set support structural and conduction volume-budget studies. Shared FEA preparation preserves existing fixture/load semantics. Passive regions, partial fixtures, weighted load cases, symmetry/extrusion ties, response limits and additive-manufacturing intent use one study contract. Final relaxed fields and separately clipped/reanalysed geometry are distinct; manifold/intersection/nesting/passive checks gate STL, while hashed VTU/AICM/history/checkpoint artifacts retain evidence. `sim_run_topology` and `topology_capabilities` route natively, with independent simulation/agent lanes, bounded progress queues and live cancellation. `host/src/ui/sim_topology*`, `sim_manager`, viewport and Topology ribbon expose setup, face picking, run history, comparison and export without replacing source CAD. `services/mcp/topology_tools.py` and `services/sim/topology.py` share those studies through durable compiled/source jobs; all polling paths retain failed/cancelled outcomes exactly once, preserve explicit nonconvergence, and recover authentic partial checkpoints. Catalogs, mode policy, packaging, RPC contract, canonical `cad_guide` prose and generated manual/tool tables were updated. Limits are explicit: tet4, linear/small-strain structural response, steady one-way coupling, flow Re <= 1 enforced against inlet and solved MINI velocity, finite Brinkman leakage, filter/overhang intent rather than a thickness/support guarantee, mesh rather than parametric reconstruction, and no resume input despite retained optimizer state. CHT uses conservative constant-fluid-capacity transport. Topology never claims print readiness; slicing plus passing G-code validation remains mandatory.

Focused verification: corrected numerical/geometry/transport regressions; all 18 native method/physics combinations; 8 sustained structural/thermal runs; 15 independent scikit-fem optimized/reconstructed states (objective relative error below 8e-12, one disconnected reconstruction honestly refused); SIMP/RAMP two-load mass minimization converged with stress limits and active displacement limits; 149 model/lifecycle tests; real desktop SIMP/BESO solves and save/reopen retaining two runs and 21 artifacts; real registered model-tool background completion/cancellation, measured mesh DFAM and editable-document round trips; private hostd progress streaming, responsive control and durable native cancellation. Existing user changes were preserved; runtime probes used isolated documents and daemon instances.

Interactive desktop verification also reopened a saved study through a private native daemon and exported a 2,610,122-byte VTU and a 578,684-byte validated STL: both SHA-256 hashes match the recorded solver artifacts, and source files remain unchanged. This caught and fixed the artifact row intercepting its Export button (`SetNextItemAllowOverlap`); the invalid BESO surface remains withheld. Result comparisons use stacked values for the narrow rail. Audits and generated-manual checks require the provisioned service interpreter; system Python lacks `mcp` and produces an incomplete catalog.

Final desktop checks made baseline/final metrics, constraint limits and manufacturing requested/measured values stack vertically in the narrow result rail. Reopening identical saved geometry under three native document IDs reproduced false source-staleness; `topology::geometryKey` now excludes runtime identity, display name and studies without copying retained history. The regression still invalidates dimensional, feature-identity, unit and material changes. The real desktop scenario reran SIMP/BESO and verified all retained source keys against the independently reopened document (two runs, 21 files, no missing artifacts). Source audits pass all nine gates with the service interpreter; core CTest passes 122/122, and Linux packaging passes 22/22 with Python 3.14 (Python 3.11's tarfile cannot read the package's Zstandard stream).

Final affected model/job regression sweep: 176 passed across `test_cad_sim_tools`, `test_topology_tools`, `test_compiled_sim_jobs` and `sim/test_jobs`. A second interactive desktop reopen into native document `doc-cpp00000002` retained the result overlay and enabled artifact exports; clicking Export produced the same 2,610,122-byte VTU and recorded SHA-256. Verification processes used private HOME/runtime/socket paths and never controlled the user's daemon.

The full repository gate is **not wholly green**: it built native/desktop/WASM, passed core CTest, hostd and web-parity smoke, and completed the service/tools/training sweep, but its 3,600-second command deadline expired during the unrelated FIN25 sensor-node demo. The sweep recorded one live-preview event timeout (`test_ro_gui_sees_mcp_edits_and_can_upgrade`; all three live-preview tests passed afterward in an isolated native daemon) and two flywheel DVC round-trip failures at `dvc init`. The initial audit/manual/packaging interpreter failures were resolved by the separately passing provisioned-interpreter checks above. No failure was suppressed or reclassified as a full-gate pass.

## 2026-10-03 — Explorer workspace files before separate recents

`host/src/ui/file_explorer.cpp` now keeps the active workspace directory tree in Files and places recent files in an independent, initially collapsed Recent card below it. Collapsing Files no longer hides Recent; the tree reserves space for the Recent header. Directory listings include all non-hidden files, with folders first and case-insensitive name ordering, rather than filtering to CAD/ECAD extensions and sorting by modification time. Unsupported file types retain file-management actions but do not advertise a CAD Open action. Existing inline-rename, folder-reveal and clipboard changes were preserved.

Verification: `./build.sh` and `python3 scripts/gui_lint.py --rule all` passed. An isolated GLFW/ImGui smoke rendered the real Explorer at 405×810 and exercised header clicks: CAD, Markdown, CSV and folders appeared in Files; external recents stayed outside the tree; the collapsed Recent header remained visible; Recent expanded independently while Files stayed collapsed; switching working directories replaced cached rows. Captures: `scratch/explorer-files-smoke.png`, `scratch/explorer-recent-smoke.png`. The successful smoke opted out of daemon mode and hard-exited after assertions to avoid linked runtime teardown hanging; temporary source, object, binary and fixture directories were removed.

## 2026-10-03 — transactional chat message editing and runtime rewind

Message editing now retains the original transcript while stopping the existing run,
forks the runtime session before the selected user message, waits for any geometry
rollback, then parks the replaced tail and dispatches one replacement turn. The
transaction belongs to its conversation; switching views or receiving stale callbacks
cannot truncate a different chat or a new running reply. Cancellation and RPC refusal
keep the original transcript and session link.

`chudcode_rewind_session` resolves repeated prompts on the runtime's active entry
branch, excludes abandoned siblings, preserves the parent history, and retires the
exact branched process before a queued turn can acquire it. A saved session can be
resumed solely for rewind without making a provider call. Worker contract and
integration guidance document the new method.

Verification: `./build.sh`; native chat-session helper suite; Python runtime/worker
regressions (83 passed, one skipped); GUI lint and all nine source-audit gates.
Real-runtime smoke on a copy of the affected session verified branching before the
edited message, unchanged source history, and process retirement. An isolated native
smoke reproduced the old loss of the live chat tail and confirmed its preservation;
another exercised rewind refusal, rollback failure, cancellation, background commit,
branch preservation, and stale callbacks.

## 2026-10-03 — exact sent-image identity in Copilot

`ChatMsg::imagePaths` now retains the exact image paths from the model attachment payload on fresh sends and mid-run steering. `copilot/chat_log.cpp` renders those paths directly instead of looking up a basename across dated paste-cache directories; `copilot/conversations.cpp` preserves the ordered paths in raw archives, session messages, inactive conversations and rewind branches. Legacy messages without recorded paths keep their attachment markers but never show a guessed image. The model already received the correct full-path image; the visible mix-up came from repeated names such as `paste-0001.png` on different dates.

Verification: `./build.sh` and all nine `scripts/check.py` gates passed. A throwaway native GLFW/ImGui/OpenGL smoke exercised the real composer attachment helpers, steering transcript, persistence/load and sent-image renderer: same-name old red and intended green images rendered the intended green pixels, with ordered external images; reload, inactive sessions, rewind branches, raw archives, legacy markers and malformed metadata passed. The harness hard-exited after GL teardown to avoid unrelated engine-runtime teardown. A debugger launch initially missed isolation overrides and touched two real cached images and the session index; image content was recovered from retained runtime blobs and the index rebuilt from all 109 untouched conversation transcripts. Subsequent smoke execution enforced isolated data/session roots before any write.

## 2026-10-03 — unbounded online turns and selected runtime compaction

Removed the implicit two-hour online-turn budget from GUI jobs, hostd, native online handoff and the Python host. Missing budgets now wait until completion, cancellation or process loss; explicit positive budgets retain abort/reporting grace. `run_mission.py --timeout-s 0` is unbounded. Regression coverage advances a controlled clock through six hours, explicit budget expiry, cancellation and process death; an unconfirmed abort retires its connection instead of leaving unseen work alive.

Settings now govern the online runtime: Summary selects only `soft`, Shake only `shake`, Snapcompact only `snapcompact`, Off disables automatic compaction, and Automatic retains runtime policy. Pruning preferences accompany the selection in immutable content-keyed overlays. Because the current runtime has no acknowledged live method-setting RPC, a changed policy refreshes only an idle connection and resumes its exact saved history, preserving explicit auto/retry switches; its transient eval scratch state resets. Manual compaction applies the current choice, refuses unsupported requests, and labels only the committed entry's actual method. Synchronous session RPCs allow 90 seconds, below the worker's 120-second boundary; a timed-out manual compact retires its connection while preserving saved history.

Files: `services/agent/{config,online_turn,chudcode_host,worker_rpc,session_cmds}.py`, `host/src/core/jobs.*`, `host/src/ui/{settings_modal.cpp,copilot/composer.cpp}`, `core/hostd/{server,worker_bridge}.cpp`, `core/hostd/worker_bridge.h`, `services/worker/worker_client.*`, `core/agent/run.cpp`, `scripts/run_mission.py`, and focused regressions. Verification: final focused suite 124 passed, one skipped; 10 hostd worker-bridge tests and native agent-job routing passed. An isolated real `openai-codex/gpt-6.1-sol` conversation compacted 39,755 tokens with method `soft` and subsequently recalled all four required facts. An isolated live GUI rendered all five compaction choices and persisted Summary → Automatic → Summary as `auto` then `summary`. No live user session was stopped during the fix.

Full isolated `scripts/gate.sh`: PASS — source audits, OCCT core and GUI builds, WASM build, all core CTests, hostd lifecycle/race/stop/recovery smoke, web parity, Linux packaging, services/tools/training suites (10,403 passed reported), golden demo and GUI captures. Manual regeneration reported advisory drift with complete 379/379 coverage; opt-in benchmarks and AppImage smoke were not run.

## 2026-10-03 — authoritative printer-media inventory and managed revisions

Repeated plain-copy deliveries left obsolete G-code selectable on printers that do not display timestamps. `services/manufacturing/print_media.py` now inventories actual media directories/files, current/modified/missing/superseded jobs, duplicate hashes, unknown printables and interrupted publication state. Pagination retains a whole-media snapshot token. Missing mount roots are never created; symlinks are not traversed.

`print_media_inventory` and `publish_print_media` are available in the main agent tool set and print category. Publishing requires explicitly idle media, a fresh inventory token, the complete project job set, print-ready slicer manifests and matching G-code hashes. Current jobs occupy `PRINT/<PROJECT>/R00101_PART.gcode`-style names; revision and part identity are front-loaded for truncated printer menus. Replaced jobs and explicitly reviewed legacy paths are verified into non-printable ZIP archives; unrelated files remain visible in the returned warnings rather than being silently deleted. Stable part numbers, idempotent retries, non-printable staging, exclusive locking, journaled rollback/recovery and post-publish readback prevent successful deliveries from leaving competing managed revisions. Recovery validates project/release scope and every path before touching files; a reproduced recovery symlink escape is covered by regression tests.

The general-agent role, printing guide, CAM guidance and successful slicing next-steps now direct agents through inventory/publish instead of file-copy tools. Ask/Plan retain inventory but refuse publication through the shared append-only mode contract. The generated Settings tool tables and integration/development skills are synchronized. `scripts/print_media.py` exposes the same inventory/publish API for explicit operator use. This is the supported delivery workflow, not a claim that arbitrary shell copies are technically impossible.

Verification: 110 focused publishing, printing and mode/parity tests passed; all nine `scripts/check.py` gates and `./build.sh` passed. A real MCP-call smoke used the eight verified robot-arm source files: retired ten recorded legacy paths, preserved unrelated G-code, verified archived bytes, published a second revision with one current set, refused a stale snapshot and non-idle publication, and confirmed identical retry idempotency. The CLI correctly refused the disconnected `PRO GAMER D` mount without creating it. Physical USB reconciliation remains pending reconnection of idle media; the project has verified job inputs and a hash-based legacy reconciliation plan, not a claim that the absent drive was cleaned.

## 2026-10-03 — multi-part plates and "Complete individual objects"

`slice_part` now slices several parts as one plate: `parts=[{doc_id|path, copies, orientation, build_dir, name, preserve_faces, strength_dir}]` orients and analyses each part on its own, applies plate-wide supports/brim/settings, and writes one G-code, one `.3mf` review project and one `chudware.print_job/1` manifest whose `plate` block lists every object (part, copy, position, height, print order, G-code label, mesh hash) and every part's orientation/printability. `complete_individual_objects=True` sets PrusaSlicer's `complete_objects=1` for plates or copies.

Probing stock PrusaSlicer 2.9.6 showed its CLI performs no sequential clearance validation: it exported objects 10 mm apart against the Ender-3's 55 mm `extruder_clearance_radius`, and printed a 30 mm object before shorter ones under the 25 mm `extruder_clearance_height`. The new deterministic planner (`services/manufacturing/slicer/plate.py`) therefore owns layout: hull gaps of clearance radius + both brims + 2 mm (6 mm arrange gap layer-by-layer), bed-polygon containment, ascending-height print order, and at most one object above the gantry clearance, printed last. Refusals (`too_tall_for_sequential`, `plate_does_not_fit`, `clearance_unknown`) name the objects. Meshes are written at absolute bed coordinates; `slice_mesh(center_xy=None)` keeps them (`--dont-arrange [--merge]`), and PrusaSlicer prints them in argument order. `complete_objects` via `overrides` is refused.

`gcode_check.verify_gcode` adds `sequential_clearance` for `complete_objects = 1` files (found by a tail pre-scan, with a re-parse fallback): objects come from PrusaSlicer's `; printing object` labels (or Z restarts), and every motion after an object's last extrusion and below its top must stay outside the clearance radius of its footprint hull and keep the gantry above it; interleaved objects fail. Layer-by-layer files report it as not applicable. `publish_print_media` copies plate membership into the media manifest and lists it in `README.txt` under the plate's file.

Verification: planner, synthetic G-code and real-engine/real-slicer plate tests plus the existing print, publishing and mode suites passed (141); `scripts/check.py` 9/9 and `./build.sh`. Real MCP smoke: a 4-object/3-part Ender-3 plate printed one at a time (gaps 57 mm, tall pin last) was print-ready with `sequential_clearance` pass; two objects above 25 mm were refused by name with no G-code; a layer-by-layer plate of the same parts was print-ready; publishing the sequential plate to a scratch media root produced `R00101_SMALL_PARTS_PLATE.gcode` with its members in README/manifest. Nothing was physically printed, and the check models the hotend as the preset's clearance cylinder plus a full-width gantry, not the printer's real hotend shape. Pre-existing and unchanged: `slice_part(printer="prusa mk4")` without `filament_preset` fails in `ProfileCatalog.default_filament` with "Unknown or ambiguous filament preset: AmazonBasics PLA".

## 2026-10-03 — interactive ask_user: the desktop chat answers the agent's questions

Online `ask_user` used to return suggested defaults instantly (`MissionDispatch.ask_user`,
`blocking=False`), so the agent asked its real questions as prose at the end of a turn (the
plane chat: "the question tool returned blank default answers"). The GUI composer now sends
`interactive_questions: true`; `chudcode_host.run_turn` binds `chudcode_questions.TurnQuestions`
as `context["ask_human"]`, `chudcode_tools.dispatch_host_tool` injects it into targets that
declare `ask_human` (hidden from schemas via `_HOST_INJECTED_PARAMS` / `tool_schemas._SKIP_PARAMS`),
and `MissionDispatch._ask_human` blocks on the user through the interaction core (typed
finalization, run-ledger records, no id-keyed replay). Feed events `agent.question` /
`agent.answer` (`chudcode_events._question_payload` keeps option structure that `_bounded`
would flatten). Answers return over a new control action `answer` (Python ControlServer +
`worker_rpc.chudcode_control`, `WorkerClient::callAgentAnswer`, hostd `agent_answer` →
`WorkerBridge::controlActiveRun`, never the busy agent lane); a user steer while a batch is
pending resolves it as `user_reply`; Stop/turn end resolve it as cancelled. The watchdog holds
while waiting. `ask_user` now also survives Ask/Plan (`apply_preference_tools` stripped it —
Plan is where questions belong). GUI: `ui/copilot/ask_answers.{h,cpp}` (GL-free model, RAW
answer building, persistence; `test_ask_answers`), `drawAskCards` in `cards.cpp`, `feedAskEvent`
in `feed.cpp`, answer jobs on the composer's control channel; batches persist with the chat and
restore as Expired. Verification: `test_chudcode_questions.py` (form answer, chat reply, Stop,
headless, no replay), related suites (603 passed), `test_worker_control`, `test_ask_answers`,
`./build.sh`, `./build.sh core`, and real gpt-6.1-sol turns through `_agent_admin_turn` that
asked, received `PETG` + `0.125 in` (canonical 3.175 mm) over the control action, and in a
second run took a chat reply instead.

## 2026-10-03 — copilot chat: magic-keyword gradients and a calmer chat surface

Chudcode's magic keywords (ultrathink, orchestrate, workflowz, jevify, ultragoon, autobot,
tokenmaxxing) now render like omp: `ui/copilot/magic_keywords.{h,cpp}` mirrors omp's matcher
(standalone lowercase prose; never in inline code, fences or tags; `test_magic_keywords`), and
`chatPaintMagicKeywords` paints a per-character hue sweep on the composer's own glyphs
(animated only while focused with a keyword present) and a static sweep in sent bubbles. The
chat itself was restyled: right-aligned mode-tinted user bubbles with a hover-revealed action
row, borderless assistant prose, thin edged iteration rows, tinted error cards, fade-in for new
messages, a sweep on the Working/Thinking labels, a rounded composer with a focus ring and a
round send/stop disc, and one card style for question/clarify/plan-approval cards. An open
ask_user batch shows "Waiting for your answers" instead of a stale tool phase, a proposal that
repeats its prompt is hidden, and the answered summary is a light edged list. `ui_theme` gained
`magicKeywordInk`, `metrics.h` the `cpChat*` bubble rhythm; `CHUDWARE_CHAT_PROMPT` pre-fills the composer for shots.
Verification: `./build.sh`, `test_magic_keywords`, `test_ask_answers`, gui_lint color/scale/
dupe/glyph/theme clean, light/dark/narrow/AI-workspace captures. The working-line shimmer and a
live question form were not captured headlessly (they need a running turn).

## 2026-10-04 — visible compaction and provider stalls; c245 tool-failure fixes

Run c245 (GPT-6.1-Sol, 272k window) looked frozen for 10–28 min at a time. Evidence
(`~/.chudcode/logs/chud.2026-10-04.<pid>.log`, session JSONL): compaction was NOT the stall —
the one real compaction applied an armed speculative summary in 20 ms; the long silences were
provider calls, two of them recovered Codex streams (a second assistant `start`). The feed
showed a never-closing "threshold" row, a separate "context-full" row and "Agent · start".
`services/agent/chudcode_events.py`: `_compaction` pairs unkeyed start/end frames through
`RpcFrameState.compaction_id` into one "Auto-compacting context" → "Context compacted (was
N k tokens)" sub_run; `_response_restarted` names stream restarts; `irc_message` becomes
"Message from <agent>"; `tool_stream_update` emits nothing; turn/message start read "Waiting
for model". GUI (`feed.cpp`, `cards.cpp`, `chat_log.cpp`, `app_state.h` GUI-only
`ActivityItem::startedTs`/`ChatMsg::lastEventTs`): live sub_runs shimmer with elapsed time,
terminal sub_runs no longer leave "X (done)" as the status, and the status line returns with
the silence duration after 5 s of no streaming/running row. Tool fixes from the same run:
`cad_doc_tools.py` remaps a constraint on an expanded polyline (fix → per segment; others get
a segment-listing error), natural-sorts the id list, and refuses `rename` without a feature id
with the real part-naming path; `cad_asm_tools.py` resolves relative component paths
project-root first (listing every candidate on a miss) and accepts face/edge sids (bare,
`{sid}`, part- or assembly-frame) in asm_mate refs. Not fixed (outside this repo or policy):
Chudcode's "No such tool: xd://<menu tool>" should point at `enable_tools`; the eval kernel
lacks `shapely`/`fitz`; `research_web_search` has only DDG configured (`provider_blocked`).

## 2026-10-04 — desktop caption controls match native window conventions

`host/src/ui/title_bar.cpp` now reserves and draws three desktop caption buttons:
minimize, maximize/restore, and close. Removed the extra exclusive-fullscreen button,
its glyph/toggle helper, and unused `WinBtn::BTN_FULL` / `AppState::isFullscreen` state
from `host/src/core/app_state.h`; refreshed `title_bar.h`'s desktop/browser contract.
The WASM app bar still delegates window controls to browser chrome. Verification:
native `Chudware` target built, theme/glyph lint clean, and an isolated Xvfb GUI smoke
exited successfully with all three buttons visible and no fourth button or unused gap.

## 2026-10-04 — evidence-backed project deslop pass

Reviewed code/build tooling, all 11 repository skills and 18 discoverable runtime skills,
current internal documentation/task records, and accessible Claude project memory. Removed
proven obsolete host/platform/worker/parser paths, hidden provider substitution, duplicated
digest/calendar helpers and implementation-only tests; preserved persisted/wire contracts,
historical engineering evidence, design data, resource policies, user sessions and unrelated
dirty work. Online CAD helpers now use private, tool-free compiled runtime completions with
the owning actor's selected model/credentials and cancellation; Hunter explicitly accepted
that RPC v2 cannot enforce exact helper output-token caps. Normal conversations remain
uncapped. Policy/configuration/lease failures refuse execution rather than enabling it.
Durable training-state writes fsync before atomic replacement and do not reset corrupt
resume data. Manual/catalog/DAG generation uses the current registries; historical claims
are distinguished from operational authority. Memory contains 110 classified topic notes,
each indexed exactly once, including Hunter's new bus-fidelity policy; auth/vault/mnemopi and
decision stores were not rewritten.

Observed baseline verification: source gate, core/host/WASM builds and all 123 CTest targets
passed; packaging had 22 passing tests. The Python sweep initially failed three incidental
agent assertions; after correction, the complete agent unit passed with 3,519 tests and the
other 28 already-passing units remained unchanged, totaling 10,500 observed Python passes.
This is not a claim that the original failing gate invocation returned success. The FIN25
demo passed 13 legs. Actual compiled helper/RPC loopback completed and cancelled/reaped
private processes, without a live provider/authentication claim. Independent digest
verification covered 54 SHA and five HMAC vectors; Release watchdog negative control failed
as intended. Production policy fault injections refused all three failure cases; interrupted
atomic JSON-state publication preserved the last successful checkpoint and later advanced.
The native sketch corpus executed all 18 fixtures (10 agreements, eight historical
divergences, no execution errors); expected results were not rewritten to hide divergences.
All 12 migrated hydraulic/piping/impedance/MNA V&V cases passed. B2 passed ten actual legs
including compiled firmware/Renode fault handling; its dry thermal handoff is not a solved
thermal analysis.

Integration smoke exposed stale engine selection and user-project restoration in the gate's
screenshots. The corrected stage selected `core/build-linux/bin/libchudware.so` and actual
private-HOME/private-working-directory native captures passed. Seeded real Parts Library
queue state rendered two active downloads and zero for all-terminal state, not a real vendor
download claim. Empty GUI scan roots now exit 2 and preserve baselines; nine audit tests
passed. Flight-computer PDN/mount setup and sensor firmware were repaired, but its rejected
I2C stream exposed missing simulation capability rather than a passing sensor loop. Hunter
authorized implementing I2C and expanded the request to CAN, UART/serial and SPI with
virtual latency and electrical loading/impedance. Those new models require separate
end-to-end evidence; this baseline entry does not claim their completion.

Limits: no live-provider/login, Windows/macOS, AppImage, openEMS, physical hardware/vendor
download, GPU-training or real-power-loss proof. Graphify refresh was AST-incremental, not a
new semantic extraction. No user daemon was restarted and no unrelated work was discarded.


## 2026-10-05 — bounded STM32F4 bus cutover and exercised flight mission

Implemented I2C, TTL UART, master SPI and classical bxCAN in one shared Renode runtime,
reached through the existing Firmware > Emulate and native/MCP `fw_sim_run` surfaces.
No additional simulation framework or target family was introduced. Clocks, link loading,
receiver thresholds, delays, source edges and lossless-line segments are explicit inputs;
unsupported target/variant requests refuse instead of inventing hardware behavior.

Actual compiled firmware exercised the complete B1 flight mission: 11 legs passed;
GNSS passed all four predicates and MPU6050/BMP280/LIS3MDL passed all six. Both partitions
used the same ELF and a three-second virtual horizon. Initial gyro was 1876 raw,
barometric pressure was 100129 Pa, and the final GNSS fix was 1100 decimeters altitude,
2865 centidegrees course, with zero checksum/framing accounting errors. GNSS excitation
starts one millisecond after reset so firmware can enable the receiver before the first
start edge; all eleven sentence pairs and 200 ms cadence remain intact. IRQ-serviced
bounded serial buffers and elapsed-time deadlines replaced instruction-count polling.

Correction smoke retained hardware-visible failures: delayed I2C ACK caused NACK,
STOP-only retry established phase recovery before any peripheral reset, and slower legal
CCR programming recovered checked data. Missing SCL bias timed out; weak/capacitive SDA
caused arbitration loss. Both documented POS/ACK orderings read the identical two-byte
sample and all fourteen burst bytes. SPI's healthy five-nanosecond-edge link passed,
whereas the same ELF on the twenty-nanosecond-edge link reported bad data and actual
indeterminate MISO. CAN's declared external electrical shunt drove TEC to 256/bus-off,
then 128 observed recessive sequences recovered transmission; isolated ACK loss stopped
at TEC 128 rather than falsely declaring bus-off. Unsolicited peer traffic did not block
three real loopback TXOK interrupts and receive frames. ONEBIT UART received electrically
indeterminate words with `errors=0`; completion evidence retained their unresolved cause
rather than inventing NF or claiming that zero hardware flags meant healthy data.

Independent common-runtime probes compared 78 voltage observations with native SPICE MNA
(maximum difference 0.0002366103 V), measured the RC threshold crossing, and exercised
matched/open/short transmission-line arrivals, reflections and historical reads. Quiet
ports no longer skip waves in flight. UART assertion evidence is selected before its own
4096-entry cap, so unrelated I2C volume cannot remove valid mission messages. Real Renode
clock observation exposed scientific-notation `RunFor` intervals being misparsed;
intervals now use decimal seconds at nanosecond formatting precision.

Production firmware UI, in private state and working directory, ran real `fw_sim_run`
against the compiled electrical UART probe and displayed its actual assertion name,
host elapsed time and native Engineering result. No canned result supplied that proof.
A long private-state Unix socket path was corrected in the throwaway harness, not hidden
by altering production error handling.

B1 PASS is a benchmark execution verdict, not board signoff: this run retained eight DRC
and 93 DFM findings, unknown current draw for ten components, and an unavailable openEMS
full-wave antenna result. These are reduced linear RC/lossless-line models, not calibrated
hardware, full RCC/PLL, sensor-conversion, nonlinear-transceiver, EMC or subnanosecond
accuracy claims. Windows/macOS and live-provider/hardware coverage remain unexercised.

Late consumer proof used the same native surface, not empty assertion-list success:
CAN mailbox 0 lost arbitration at 186510 ns with TEC zero, then retried and received
ACK at 564510 ns with TEC/REC zero. Slow MOSI produced an actual peer transaction
rejection at 2980 ns with `threshold_indeterminate`, without a fabricated peer word
or committed partial register transaction. The final native firmware behavior suite
passed all 35 cases and 8956 assertions, including a real 50-microsecond versus
40-millisecond execution boundary. Actual production Bus events and selected-event
detail rendered the native result in an isolated desktop capture.

Regenerated twelve manual pages with 379/379 ribbon-command coverage and the live
offline catalog with 519 tools, 21 families, 514 descriptions and 519 guidance entries.
Local AST-only graph refresh completed without a shrink override or provider call:
86134 nodes and 197048 edges. These are code/structure freshness counts, not a new
semantic extraction. Protected publication distinguishes recorded cleanup hunks from
initial and later user edits, including concurrent CI/ARM and shared-memory reader work.

The first strict final gate passed source/core/host/WASM/CTest/hostd/web-parity and ten
Python units but did not finish green: system `python3` lacked manual/packaging dependencies,
one firmware-authoring assertion exposed raw-read final-newline loss, and the MCP process
ended without a pytest summary during provisioning-dependent reconstruction tests.
The launch now explicitly selects the provisioned interpreter and fresh daemon binary.
A real pytest exit-7 negative control proved the narrow gate guard preserves a fatal verdict
while continuing to a later real unit; before the guard the sweep aborted.

Raw UTF-8 reads now retain physical line terminators across bounded offset/range pages,
including CRLF and files without a final newline; numbered/document behavior remains.
Filesystem and firmware MCP suites passed all 65 tests, including actual authored code
cross-compilation. Two concurrent real private daemons inherited the same gate key but
received distinct private identities/sockets and independently owned exactly one document;
their environment bindings restored and owned processes closed. Daemon discovery now uses
the existing resolver and exact listener ownership instead of adopting arbitrary new sockets.
Missing-backend scan tests use deterministic capability/backend fault injection rather than
leaking installed native SfM into another test; six tests passed, one full-backend test skipped.
The original no-summary process termination cause remains unconfirmed; the source hazards
and isolation defects are corrected, not presented as a proven kernel/GPU fault.

The existing charger caller had relied on ignored UART baud and a 30-second native default
despite declaring 120 seconds. It now programs reset-HSI baud, passes the declared deadline,
keeps deadline failure fatal, and emits a latched fault transition once rather than flooding
bounded evidence with repeated state diagnostics. Actual rebuilt firmware passed both unchanged
FAULT predicates in 4.89 seconds with intact evidence. This safety-loop proof does not claim
that its earlier failing full benchmark passed. The cleanup-only App.cpp publication snapshot
also compiled with production Release flags while excluding the user's protected source hunks.

The corrected strict run completed the entire MCP sweep: 3309 passed, 32 skipped and
one expected failure. Its remaining failures were outside MCP: Python 3.11's `tarfile`
cannot directly read the packager's Zstandard output, and private HOME hid the installed
DVC package behind an otherwise discoverable CLI launcher. The package comparison now
uses the existing `zstd` executable before inspecting real tar metadata; all 22 package
tests pass under the provisioned interpreter. Keeping the installed Python user-base
fixed, while retaining private HOME/data, passed both actual DVC push/pull round trips.
No DVC behavior or test verdict was replaced with a mock, skip or weakened assertion.

That run reached nine passing sensor-demo legs, including built firmware and a solved
133.3956-degree-C thermal result, but the verification launch's 3600-second deadline
stopped it before the final demo, charger and screenshot stages. A new complete strict
run has no launch deadline and still enables the full charger benchmark. The interrupted
invocation is not recorded as a full-gate PASS.

The final incremental local AST refresh completed at 86142 nodes, 197072 edges and
2594 communities without a force/shrink override or semantic-provider call. Curated
graph instructions and hooks were retained; no installer rewrote them. Generated
structure is current, not a claim of newly re-extracted semantic documentation.

The complete uncapped strict Linux gate finished PASS in 4145.74 seconds: source/audit,
manual regeneration, core OCCT/native host/WASM builds, core CTest, hostd lifecycle,
web parity, 22 package tests and all 30 Python units. Python totals were 10732 collected,
10524 passed, 207 skipped and one expected failure; available stages did not use an
environment SKIP. Remaining gate SKIPs were disabled opt-in benchmarks or AppImage.

Detailed demo reports passed all 12 sensor-node legs and all 10 charger legs; the gate's
13/11 PASS-marker counts also include each overall-result marker. The sensor-node FEA
heatsink solve met its target at 69.7 degrees C. Its 70 DRC findings, 19 ERC warnings and
unknown power-budget data remain advisory findings, not erased by the execution PASS.
The charger retained 131 DRC findings and a dry, unsolved thermal handoff; rebuilt firmware
passed the real safety-loop FAULT predicates in 4.74 seconds. That is no thermal or board
certification. Both production headless GUI captures were inspected, including the real
Copilot slash-command fill; the separately exercised firmware Engineering/Bus-event UI
used the native electrical probe result. No live-provider, hardware or Windows/macOS
verification is claimed.

Selective-publication verification also excluded the concurrent user startup change
in `host/src/app.cpp` and its coupled `AppState::rtReady` deletion. The original field
is retained only in the index projection alongside cleanup-owned state changes; neither
working-tree file was rewritten. All 162 production host-source units compiled and the
real GUI linked against that projection. Dependency evidence showed all 57 state-header
consumers used the projected header, not a mixed working-tree ABI. Both isolated real GUI
scenarios then exited zero and their host/Copilot captures were inspected. The first relocated
launch lacked the worker module path and exited 141; supplying the real repository Python
module path corrected the harness without suppressing the error or changing production code.

After the concurrent user edit, another guarded local AST update completed at 86141 nodes,
197071 edges and 2562 communities, without semantic-provider calls or a shrink override.
Publication uses 494 explicit whole-file paths and six index-only mixed-file projections;
initial and later user edits remain outside the cleanup commit.

## 2026-10-05 — MSVC-safe firmware embedding and measured Darwin ARM deadlines

`core/cmake/EmbedFirmwareBuses.cmake` now emits constexpr character-byte arrays
instead of oversized raw string literals. The five maintained bus sources retain
their exact bytes, symbol names, array types, static storage and trailing NUL;
`bus_models.cpp` requires no additional runtime concatenation or consumer change. A real
MSVC 19.44 / toolset 14.44.35207 consumer compiled and executed on the Windows
builder, checking all five arrays byte-for-byte against the original files
(18,236 / 27,810 / 31,208 / 26,189 / 48,761 bytes) and their NUL terminators.
The concurrently published firmware feature remains enabled.

The Intel Mac's single-worker cold OCCT ARM SDK reached only 1,241 of 5,476
actions in the former 30-minute limit; its cold native core build took 69 minutes.
Darwin-specific caps now allow 200 minutes for OCCT, 230 for the whole SDK,
120 for the core build and 330 for the job (327-minute process-group deadline).
Other SDK builds remain capped at 30 minutes; Linux ARM and native-row budgets
are unchanged. SDK exports, consumer validation, compiler-width caps and test
coverage are unchanged. Native rows have no ARM dependency, and this Darwin
lane checks arm64 compile/link architecture only, not native ARM execution or
signing. These caps correct observed failures; full workflow qualification is
reported by the corresponding Actions run, not inferred from the new limits.

The dependency-free workflow policy parser now resolves document-local block
anchors and aliases, so the native/ARM shared-step workflow is inspected rather
than rejected as unexpected indentation. The real-tree policy command passed
with zero errors (two pre-existing advisories). Three behavior regressions cover
rejection of an aliased metered runner, acceptance of an aliased private runner,
and rejection of an alias leaking from a previous document; all passed.
The policy workflow runs these regressions and watches their source path.

## 2026-10-05 — full portable QEMU suite with measured meshing headroom

Run `37289792754` passed all three native rows, including 123 native Mac core
tests and the Windows engine-dependent UI scenario. ARM Linux compiled and
linked the full engine against its validated aarch64 SDK and passed 116 tests;
the mesh test then exceeded its 1,800-second limit, and the whole suite stopped
at 3,600 seconds. Native mesh and advanced-mesher took 279 and 546 seconds.
The Linux ARM suite now gets 180 minutes total and at most 120 per test,
within a 240-minute job (237-minute deadline). Explicit native test timeouts
receive eightfold emulation headroom capped at 7,200 seconds, only for Linux
aarch64 cross-tests; native timeouts and existing serialization are unchanged.
An actual CMake configure/CTest metadata smoke proved native 600/3,600/90,000
second allowances remain unchanged while ARM resolves them to 4,800/7,200/7,200.
No portable test or meshing assertion is excluded. SDK recipes remain unchanged,
so this correction does not invalidate the genuine qualified SDK cache.

## 2026-10-05 — independent lightweight Mac gate capacity

The macOS fast-gate row now uses `cc-mac-chudware-gate-intel` as the separate
hidden UID 423 `ccci-chudware-gate` account. Its 0700 home, runner root,
work/temp/cache state and system daemon are separate from the native/ARM
builder's `ccci-chudware` account; only host tooling is shared read-only.
The CloudCastle manager's `--product Chudware --gate` mode cannot purge
accounts, run legacy cleanup or install Rust. The runner registers on its
bootstrap label; runner-smoke declares and checks its actual identity before
promotion. Native/ARM builds and operator signing keep their existing lanes.
The actual Mac manager qualified native C/C++, Objective-C, Python venv and
CMake/CTest consumers and sealed sibling/operator homes without restarting
the active builder. This adds no native Apple Silicon or signing proof.

The completed cold Darwin ARM SDK and full 623-action core build in run
`37289792754` passed on the private Intel account; the resulting engine is
thin arm64 Mach-O. The native Linux row's advisory clang-tidy command was
not analyzing sources: LLVM 18 rejected `--quiet` and `-source-filter`.
It now uses the supported `-quiet` and positional source regex arguments,
without changing the existing advisory policy or source coverage.

## 2026-10-05 — persisted build state, piping/hydraulics/industrial design, internal-flow CFD

**Build state in the file.** `doc_save` now writes per-feature compile checkpoints
(`cache.ckpt`, contracts/stepplus.md §9) beside `cache.json/aicm/brep`. A valid
cache makes `doc_open` hydrate `compiled` and `compile_cache` (`warm:true`), so the
first activate/pick/save after reopen runs 0 features and the first edit only the
tail. Before this change a cache-hit open was cold, and the first op re-ran the whole
tree: `claw_gear_drive.step+` took 2.5 s on its first `doc_activate`, and large parts
took minutes. Skips are now reported (`cache.skipped_reason`, `cache_miss_reason`)
instead of swallowed. An oversized entry is a cache miss, never a failed open.
`seed_cached_scratch` shares `compiled`, so autosave snapshots never recompile.
The GUI open/activate timeouts follow the engine's long tier
(`core::kLongEngineCallTimeoutMs`), and the tree shows elapsed time plus whether the
open was a cache hit or why it missed. The engine key is not bumped. Files saved
before this change open warm from `cache.brep`, but the first geometric query
still compiles once, until they are saved again.

**Fluid/process design.** New native RPCs, each a study the GUI and agents share:
- `piping_route`: orthogonal A* around doc bodies.
- `piping_spec_check`: B31.3 eq. 3a, B16.5 P-T ratings, support span.
- `piping_bom`: spools and welds.
- `vessel_size`: ASME VIII-1.
- `duct_size`: equal friction.
- `sim_run_pipe_flow`: D3Q19 internal flow with a mixing-length RANS model and a
  Spalding wall function (lattice coefficient 0.6, fitted to Colebrook).
  Straight pipes land within about 10% of Colebrook at Re 1e4–1e6. Bends come out
  1.5–1.85× the Rennels K at 16 cells. The result reports `one_d_estimate_pa`
  (friction plus fitting K) and `cfd_over_one_d`.
- `hydraulic_simulate`: lumped-parameter implicit transient that replaces the
  dead `hydraulic_fmu_simulate` stub.
- `hydraulic_size_motor`.
- `pump_drive_size`: IEC/NEMA ladders and frames, IE2–IE4.

The code tables were audited against B31.3-2024, B16.5-2025 and II-D 2023
(CodeTableAudit). The GUI gains a Simulate › Piping tab and a Hydraulics tab;
Hydraulics shows its schematic and plots in the central area. Agents use the
`piping-design` and `hydraulic-design` skills. Shots: `CHUDWARE_SHOT=piping`,
`hydraulics`.

Gotchas:
- Engine envelopes carry a design-code string in `code`; a protocol error is
  `code` without `ok`.
- `services/piping` bore and 3-D sweeps let an end segment use its full take-out.
  The old 0.49 clamp mis-swept the last elbow of every route.

Deferred:
- AsyncRpc has one FIFO lane, so a multi-minute cold open still queues other async
  RPCs.
- Bends are over-predicted on stepped voxel walls.
## 2026-10-05 — ARM FMA parity repair without weakening mesh contracts

The revised full run `37326030572` passed all three native rows and the complete
Darwin arm64 compile/link row. QEMU passed 119 core tests, but advanced meshing
failed six native-speed assertions and three genuine STEP-bar size/element-count
contracts; the enforced three-hour suite deadline then stopped the remaining
serial fixtures. The QEMU step now supplies the existing wall-scale value 8 and
allows 210 minutes within the unchanged 237-minute job deadline. Native ceilings,
per-test caps, serialization and all functional checks remain unchanged.

The numerical failures came from the pinned Polyhedral-FEA ray-parity
determinant: ARM GCC contracts it into FMA, and the independently anchored
expressions could both reject a shared face-diagonal ray. The source-only
`cad-fill-progress` revision `274503cca6a8fc29bdb7d41cd58cbaf7bc72f68c`
canonically orders edge endpoints and negates the reverse direction, preserving
watertight parity with FMA still enabled. No global floating-point flags,
tolerances, mesh expectations, allocations or size special cases were added.
Actual ARM/QEMU proof: the retained classifier regression failed three of 48
assertions before and passed all 48 after; the real STEP-bar consumer passed all
47 original assertions. The upstream standalone product's separate release
history and the user's local branch/index were not changed.

## 2026-10-05 — retain strict-gate Python failure diagnostics

The completed strict Linux gate `37331563747` passed native core/host/WASM builds,
core tests, daemon/parity/packaging, the sensor-node demo and GUI shots, but six
Python tests failed across agent, MCP, simulation and tools units. Its wrapper
printed only counts with `--tb=no`; runner-temp cleanup then removed both failure
identities and their diagnostics. No retained artifacts or pytest failure cache
were available.

The per-unit sweep now records short tracebacks and copies failed-unit output
into unmetered CI logs, including collection errors. Passing units remain quiet;
test enumeration, suite separation, failure verdicts and skip policy are unchanged.
The actual stage was exercised with real pytest: the original hid an assertion
failure's identity, the revised stage emitted its name/source/assertion details,
collection errors remained fatal with diagnostics, and all-passing units stayed
green without failure blocks. Temporary smoke fixtures were removed.


## 2026-10-06 — Simulation GUI overhaul onto one standard

User-reported ImGui "conflicting ID" errors (Hydraulics: two `Name` fields both resolving
to `PushID(label)`+`##v`) led to a full simulation-section overhaul.
- `sim_manager.cpp` (13.7k lines) is split into `host/src/ui/sim/sim_manager_*.cpp` by
  concern, with shared helpers in `sim_manager_internal.h`.
- New kit vocabulary:
  - `widgets/kit_form` (explicit-id fields, pane-wide label column/unit lane, unit
    spellings, number formatting);
  - `kit_plot` (`linePlot`/`barPlot`, chip flow, `removableChip`, result cards, adaptive
    `metricTable`);
  - `kit_panel` (`panelBegin` scaffold, `panelHeader`, `formTabs`, `stepper`,
    `actionFooter`, `removableCard`, `canvasFrame`, `groupedPicker`).
- Shared panel helpers: `sim/sim_common` (JSON/RPC/`StudyRecordStore`) and
  `sim/sim_pump_drive`.
- Every study editor, Piping, Hydraulics, EM/Electronics/Firmware/Handoff, topology
  settings/BCs/results, the Evaluate dialogs and the legend use them.
- The Simulate ribbon follows Study · Setup · Boundaries · Mesh · Run · Results · Reference;
  "New Study" opens one grouped picker on every tab.
- The Thermal tab is Thermal FEA (the desktop flow backend has no OpenFOAM).
- 33 new icons were added via `icon_pipeline.ensure_cells`.
- The Simulate left column is its own `SimulationManager##LeftPane` window, with a wider
  default.
- Framing fits the model in `ui::viewportClearRect()`. The legend reserves its own width,
  and the renderer LUT and legend share `result_ramp.h`.
- Gate: `IMGUI_ENABLE_TEST_ENGINE` is defined on the app's imgui sources; `ui_audit.cpp`
  hooks every ItemAdd.
  - `CHUDWARE_SHOT=simaudit` covers 78 states and requires 0 conflicts and 0 overflows.
  - `CHUDWARE_IDAUDIT=1` gates any scenario.
- The `fea`/`flow` shots had never completed: there was no frame-5 gate and no engine-idle
  wait. They now print PASS and exit non-zero on failure.

Deferred: the glass refraction wedge in the legend pill (ui_theme glassPanel); the Firmware
UART log is still a raw child with a pill scrollbar (no kit log widget).

## 2026-10-06 — c247 tool reliability and topology-to-CAD continuation

Investigated the exact c247 trace and ME-490/Showcase/Topology_Bending artifacts.
The main trace recorded four tool failures: a document rename sent to feature-only
`add_features`, daemon-mode `show_in_app` reporting a failed legacy publication,
a 900-second job wait inside an eval cell capped at 600 seconds, and a later
`NameError` after SIGINT killed/replaced the Python session.

- `cad_doc_tools.py` reports an existing daemon document as live without a file
  handoff or stealing the active tab. Feature rename requires an id; native-file
  naming guidance points to `relocate_native_files`, not a fake document rename.
  Job waits are standalone calls; interrupted cells recover results via `get_job`.
  Tool guidance, context packs and generated tool-panel descriptions were updated.
- The canonical Chudcode repository (`Repositories/omp`) preserves the Python
  session on SIGINT during both synchronous work and top-level async waits.
  Cancellation wakes the selector and retains prior globals; it does not pretend
  the interrupted assignment completed or cancel an independent simulation job.
  Regressions cover both cases. The matching native addon and runtime binary built;
  the compiled binary passed `--smoke-test`. Release pins/global installs were not changed.
- Topology Results has **Continue in CAD** for a valid, available, non-stale
  reconstructed surface. It imports a separate solid, activates a new document,
  clears the stress overlay/legend, and returns to Model / Feature Manager.
  Source geometry and retained studies are unchanged. Invalid reconstructed geometry
  is prominently reported even when the optimizer converged and was feasible.
- Closed, consistently wound single-shell mesh import now shares edges directly
  and combines coplanar triangles into planar faces. BRepCheck gates the direct
  lane; open, disconnected or otherwise unsupported meshes retain the sewing lane.
  Curved areas remain faceted: no recovered sketches, NURBS or parametric base history.
  The geometry-cache epoch advances to 7; existing baked BREP imports are not rewritten.
- Updated `cad_guide` simulation workflow, generated manual and GUI workspace skill.

Verification: real GUI activation from the retained final 3D run, source-tab return,
and a separate daemon edit/save/reopen of that same beam. The imported solid has
14,290 faces (26,496 source triangles); its left cap resolves as one 960 mm² face.
Adding a 10×16×60 mm mounting pad and a 3 mm-radius, 17 mm-long axial cut preserves
one sound/watertight solid and changes volume from 72,759.746816 to 81,879.083358 mm³.
All three native features and the final volume survive `.step+` reopen. Original
project/result/surface SHA-256 hashes and source document tree are unchanged.
Focused checks: five native suites, 83 Python tests plus 15 subtests, five rename
cases, and 12 Chudcode runner cases (three existing skips). Native/GUI builds used
the supported local `CHUDWARE_POLYMESH_ROOT` override to preserve the user's dirty
dependency checkout; no dependency reset/stash/checkout was performed.

Limits: the two extrusion-tied results genuinely reconstruct 21 and 18 bodies;
optimization feasibility is not geometric validity. The final 3D run reconstructs
one valid body. Fine FEA remeshing still exceeded bounded probes on both import
lanes; this change does not claim that meshing stall is resolved.

Broad gate evidence: source audits, manual freshness, native/GUI/WASM builds, all
native suites, hostd lifecycle, web parity and Linux packaging passed. Its Python
sweep exposed an obsolete test requiring a triangulated box to reconstruct zero
features, plus the hermeticity test invoking it. Removed the implementation/wording
pin rather than changing it to 100%; hermeticity now invokes the existing analytic
shaft reconstruction case. Both affected files pass together (17 tests). A real
MCP smoke independently recovers the closed mesh box as a 10 mm extrude with a
6,000 mm³ volume and 100% geometric match; mesh notes distinguish planar recovery
from curved facets. The full gate timed out at 3,600 seconds in the sensor-node
demo, after the Python sweep, so no complete green gate is claimed.

## 2026-10-07 — Interactive, fully customizable simulation setup

- Simulation setup uses document-scoped drag/drop in addition to the existing
  precise forms: model faces into boundary wells, compatible boundary tools onto
  model faces, motion joints into drivers, circuit/emulation tracks into setups,
  hydraulic components onto the schematic, and piping endpoints/ports/presets
  into their respective forms. Repeated same-role face drops deduplicate rather
  than toggling or multiplying loads. Ordinary selection, blank-space navigation,
  Explorer file drops and the user's open sessions remain intact.
- Coverage includes structural/static/modal/buckling/contact/fatigue/frequency
  response and mesh, steady/transient thermal FEA, all topology physics boundary
  editors, external flow and thermal CFD, motion, electromagnetics, circuit/PCB
  thermal/heatsink setup, firmware emulation in both workspaces, hydraulics,
  and piping/network/internal-flow/vessel/duct setup.
- Exact native editors retain unsupported/unknown metadata and inactive modes
  without pretending that unknown fields extend a solver. Apply validates and
  commits atomically; Revert reloads current data. Dirty drafts refuse stale
  commits; pristine drafts refresh. Managed identities/result evidence remain
  read-only. Native run requests merge authored supported parameters rather
  than reconstructing a reduced subset. Single-edited mesh aliases synchronize;
  genuinely conflicting aliases are refused.
- Face/tool and palette payloads capture their source context once; document,
  revision, panel/study generation and busy-state fences prevent stale delivery.
  A face-only GPU ID-pass mode avoids edge/vertex priority stealing a face drop
  and participates in the existing render-cache key.
- Exact global capability/account/catalog polling no longer interrupts a setup
  gesture: `AsyncRpc::interactionBusy()` maintains a separate conservative counter.
  Document reads, mutations, solves and unknown RPCs remain blocking; scheduling,
  refresh and teardown retain the original all-work `busy()` contract. A real
  queued `doc_tree` refused a setup edit atomically; an actual `fw_projects` call
  between mouse press/release did not cancel native-editor Apply.
  Firmware toolbar/rail controls now adapt to measured content bounds. Document
  tab lanes use logical as well as drawing clips and reveal the active tab/close
  control without accepting clicks beneath Copilot or pinned overflow controls.
- Native face-query resolution handles prose, query objects, raw SIDs, sampled
  points and terminal compound sets. Compound children retain independent
  geometry, union nodes/facets without phantom cross-child faces, and apply one
  authored force once. Thermal flux/convection use explicit validated exterior
  facets when supplied. Firmware shares one native scenario/event editor;
  hydraulic and piping exact definitions preserve authored zeros/nulls/aliases
  according to their native contracts.
- Updated canonical `cad_guide` simulation instructions, generated manual and
  GUI workspace ownership map. Toolkit audit hooks observe actual controls,
  disclosure state, text-only errors and clipping only during opt-in QA. Native
  multiline fields use readable stacked layout when the pane is too narrow.

Native verification: five core simulation suites and the host topology CPU
suite passed; static/thermal Python suites passed 101 tests, including compound
boundary behavior. EM/heatsink checks passed 41 tests plus 47 subtests (one
optional-backend skip). Real native EM runs exercised analytical/FDFD paths
and explicit excitation fidelity. A compiled STM32F4 program ran in Renode
against two edited GPIO schedules and produced the expected final output states.

The final input-driven `simaudit` completed 114 states with zero ID conflicts and
zero overflows, including expanded native editors, firmware metadata and motion
playback. Its three explicit exclusions remain schema-reserved Torque, custom
material creation, and the separate solver-result matrix; it runs static and
motion solves, not every solver. Existing async-RPC and long-worker suites also
passed. Native/desktop and private-staged WASM builds passed; GUI lint was clean,
all nine standards gates passed, and the generated manual had 509/509 coverage
with no drift. User sessions and the dirty Polyhedral-FEA checkout were preserved.

The input-driven native-editor round trips also completed eight actual solves:
modal sampled points, prose, query objects and stable face IDs; static prose,
query objects and duplicate-leaf compound sets; and steady thermal convection.
Edited mesh aliases matched actual element order; compound sets matched the
reference solution without multiplying the authored force. The thermal slab
retained 160 W/m·K conductivity, 10 W/m²·K film coefficient and 75°C ambient,
producing 20°C minimum and 20.2055°C maximum against its one-dimensional analytic
solution. The same real Apply gesture refused document-busy edits atomically
and survived project discovery queued between mouse press and release.

The same real-input run placed hydraulic components on the schematic, connected
compatible ports and persisted native parameters. All seven invalid fluid
drafts were refused without changing the saved circuit. A zero-length return
hose retained its exact value and the native ODE solve produced 23.2 L/min at
zero pressure difference, matching the pressure-dependent leakage model.
The structural handoff retained explicit zero load-pressure and tolerance.
Seven piping-fluid alias and gas-temperature variants persisted and solved;
a held port drag kept its immutable origin and refused stale-generation delivery.

Post-solve setup transitions now restore the editable model and face-pick
buffers together. Full scalar/displacement result loads preserve the part's
CPU/GPU model cache; cached model restoration invalidates queued visible-load
generations without copying already-bound shaded arrays. Same-document setup
edits/deletion restore the model before clearing result visibility. The actual
face-source probe recovered six B-rep faces after the eight native solves;
dragging one into a piping endpoint persisted its selector. Vessel preset and
firmware stimulus drops also completed with native parameter checks.

The complete isolated `siminteractive` passed with 185 checkpoint evaluations
(71 distinct assertion names), 16 real drag/drop deliveries, no skips and zero
ID conflicts. Native RC probe copying is duplicate-safe; its schematic palette
uses the compiled circuit translator through the native-only worker route, with
no nonexistent Python-method fallback. Conversion failures expose the engine
error. Two additional transient thermal runs proved native schedules/frame times
and changed final maximum temperature from 20.0834°C to 20.2533°C when duty was
increased. Flow-direction, topology-face, EM-standard and real motion-driver
reorder checks retained their exact native parameters. Setup checks for circuit,
EM, topology, motion and 3D CFD are not claimed as numerical solver execution.

Limits: optional OpenFOAM/OpenEMS availability is reported and gates the relevant
Run action; no substitute physics is silently used. External-flow LBM and contact
proxies retain their existing qualitative limits. Renode 1.16.1 cannot install
the requested timestamped GPIO hook, so timestamped assertions are refused;
final-state assertions were exercised. Firmware bus summaries remain ideal
functional models, not calibrated electrical/latency simulation. These changes
improve setup and preserve native capabilities; they do not claim new solvers.

Handoff (2026-10-07 — native simulation scenes, pipe drawing and hydraulic layout): `host/src/ui/sim/sim_scene.*` now dispatches cached shaded/line batches and viewport tools to `pipe_scene.cpp` / `hydraulic_scene.cpp`; `viewport_panel.cpp` renders them through `Overlays::drawSimulationLitTriangles` even without `st.hasDocMesh`, fixing invisible routed pipes on empty parts. Renderer auxiliary bounds, auxiliary-aware camera clamps, first-source luminance bootstrap and corrected blur-downsample UVs preserve framing, zoom and glass contrast. `pipe_mesh.*` / `pipe_sketch.*` add shaded bends/fittings, working-plane drawing, risers, snapping, typed lengths and cancellable queued finishes; `piping_panel.cpp` adds fitting placement, deterministic K(Re), CFD-derived K and pressure/velocity colouring. Native `core/piping/edit.cpp` / `solve.cpp` and MCP `piping_edit`, `piping_fitting_k`, `piping_fitting_k_cfd` share the §9 contract and CFD start/status/cancel pipeline (`services/piping/{fitting_cfd,worker_rpc}.py`). Hydraulics 3D Layout persists component positions as `params.layout3d`, shared with MCP `hydraulic_layout_3d`, and renders pressure-coloured hoses. `core/sim/flow_run.cpp` packages pressure/velocity and streamline/slice AICM overlays (`overlay:false` opts out); voxelization/LBM/pipe-flow performance work retains approximately one-second progress heartbeats. Flow fidelity is Quick preview 58/667 (new-study default), Standard 96/2000 or Accurate 144/3000 with ETA and face-normal inlet direction; `sim_run_adaptive` routes natively in `services/worker/{worker_client,engine_backend}.cpp`. EM/Electronics prioritize typed forms, collapsed Advanced native parameters and outcome plots. Real-input `pipedraw` / `hydraulics3d` shots join pointer/audit registration; `piping` pixel-checks route visibility. Session measurements: quick CFD-K for an NPS 2 LR 90° elbow (two Re points, 12 cells) ≈9.5 s; flow-shot solve ≈1.9 s (not general runtime guarantees). Manuals, GUI skills, hydraulic-design skill and a proportionate website paragraph document the human/agent workflows. Build gotcha: core reconfigure currently fails PINNED FETCH because the uncommitted `POLYMESH_COMMIT` bump to `274503c` conflicts with local modifications in `third_party/polymesh/src` at `ef464dc`; this session built with `CHUDWARE_POLYMESH_ROOT=$PWD/third_party/polymesh/src` (LOCAL SOURCE), not a validated public-pin build. Deferred: none known beyond that.

Handoff (2026-10-08 — civil / site / utility infrastructure, `aicad.civil/1`): new pure-Python engine package `services/civil/` (contract `contracts/civil_schema.md`, SI metres) — `schema`/`criteria` (GLUMRB Ten States 2014 + Water Works 2018 profile, exact customary conversions), `crs` (pyproj, new pinned dep 3.7.2, MIT), `gis_io` (GeoJSON RFC 7946 + stdlib-sqlite GeoPackage), `landxml` (1.2 surfaces/alignments/pipe networks, US survey foot), `ifc_export` (dependency-free IFC4X3_ADD2 alignments + distribution systems; parsed by IfcOpenShell 0.9 out of tree), `terrain` (constrained TIN by Sloan flips, contours, exact cut/fill, stage–storage, pad grading), `dem` (priority-flood, D8, watersheds), `alignment` (clothoids, PI layout, vertical curves, AASHTO SSD/K, sections, AEA/prismoidal, mass haul), `water` (Todini–Pilati GGA, EPS, PDA, valves, fire flow; Net1/loop/PDA/PRV fixtures from EPANET 2.2 via WNTR), `epanet_inp`, `gravity` (partial flow, Manning, HGL screening with the HEC-22 §9.1.6.6 caveat, design sizing, standard step, jumps; SWMM 5.2 steady fixture), `swmm_inp`, `hydrology` (rational, CN, TR-55 Tc, NRCS UH, SCS storms, NOAA PFDS IDF, modified Puls), `culvert` (HDS-5 Table A.1 inlet/outlet control), `buried` (prism/Marston, live load, Iowa, buckling, flotation, thrust, DIPRA restraint), `supports` (spans/reactions from `aicad.piping/1`), `planning` (QTO, user price-book cost, CPM/4D, MSPDI, requirement evidence), `piping_link` (fixed-point civil↔piping tie-in coupling, simhandoff couplings `civil_to_piping`/`piping_to_civil`/`civil_to_fea`). 30 `civil_*` MCP tools in `services/mcp/civil_{tools,hydro_tools,link_tools}.py` over shared `civil_common.py` persist one `civil_site` study per model (results dropped on model change; renames keep the study id); registered as family `sim`, pack `civil`, Plan mode, `cad_guide('civil')` (`civil_guide.py`), skill `civil-design`, manual `guide-civil.md`. 700 tests (`services/civil`, `services/mcp/test_civil_*`). Runtime smoke against in-process libchudware: water solve → `civil_piping_couple` with the native `piping_solve` converged in 6 iterations with a passing verify envelope, thrust → FEA handoff, `.step+` save/reopen → `civil_load` restored model + results. Deferred: no GUI panel (the concurrent UI owner can consume `civil_site` studies via `doc.studies_changed`); `piping_tools.network_from_handoff` still accepts only `hydraulic_to_piping`, so `piping_solve(source_handoff=civil_to_piping)` refuses (the coupling tool uses the solver directly); coupling is steady DDA only; HGL is screening, not dynamic-wave routing.

Handoff (2026-10-08 — single public Polyhedral-FEA line, stable pipe-sketch framing): Polyhedral-FEA is one public repository. `Hunter-124/Polyhedral-FEA` `master` now merges `cad-fill-progress` (ARM FMA watertight predicates, 274503c) and the private fork's history (9554579..9737e10, content already published squashed in ef464dc; the variant test "adapt stops before the predicted next-pass growth" lives only in that history), plus `09924b7` making polymesh subproject-safe (`PROJECT_SOURCE_DIR`; CPack only when top-level — as a CAD subproject it looked for `core/LICENSE` and failed configure). `Polyhedral-FEA-private` is archived on GitHub (reversible); the local clone and bare mirror now point at the public repo. `core/cmake/ProvisionPolymesh.cmake` pins `9f6595e` and the configure log says PINNED FETCH; the previous configure failure was the uncommitted pin bump colliding with byte-identical uncommitted copies of 274503c in `third_party/polymesh/src` (discarded after an identity check). CAD core CTest (123 tests, `tet10_thin_wall` excluded for its solve budget) passed against this source. Pipe sketch: transient scene batches set `ui::simscene::{Lit,Line}Batch::bounds=false`, framing grows in 25 % bands, never shrinks while a tool is armed, compensates zoom exactly and uses reciprocal wheel steps, so cursor motion no longer rescales the view; `DrawSettings::autoPlane` follows the camera-facing axis plane through the last point, Tab cycles XY→XZ→YZ→Auto mid-run, and three-axis ray–line inference gives risers without Shift (`host/src/ui/sim/pipe_sketch.*`, `viewport_panel.cpp`, `renderer.*`, `piping_panel.*`).

Handoff (2026-10-08 — desktop startup, primary/background IPC separation): `host/src/main.cpp`, `app.cpp`, `core/startup_trace.h` and `ui/doc_tabs.cpp` present the native shell before worker/daemon startup and poll readiness asynchronously after recovery bootstrap before offering the disposable starter; deliberate New/open activity cancels that offer. `services/ipc/ipc_client.*` routes exact document-free queries from non-primary GUI threads through per-thread `gui-background` sockets; document/unknown methods keep the primary identity, and side EOF/timeouts do not invalidate it. `services/worker/engine_backend.cpp` makes the 200-ping diagnostic opt-in (`CHUDWARE_PING_BENCH=1`); `ui/copilot/settings.cpp`, `composer.cpp`, `provider_usage.cpp`, `settings_modal.cpp` and `firmware/fw_workspace.cpp` defer automatic discovery until startup settles and share subscription-status polling. Before-state on the real Wayland display under strace: a 3.2 s main-thread mutex stall (2.0–5.2 s; another launch stalled 0.16 s), repeated GUI model/firmware discovery and 200 startup pings at 146 us/call; Xvfb showed the starter at about 4 s. These are baseline measurements, not an after-change speedup claim. Deferred: integrated build/runtime checks and cold-launch trace comparison (`CHUDWARE_STARTUP_TRACE=1`); `test_worker_long_run` adds background-query nonblocking, EOF/timeout isolation and disconnect-wakeup cases. Gotchas: background sockets avoid the unconditional GUI pin but recent/in-flight work still pins; shots, web readiness and crash recovery retain their existing paths. No user daemon/session should be stopped to obtain a cold launch.

Handoff (2026-10-08 — Hydraulics workflow and native templates): `host/src/ui/hydraulic_panel.cpp/.h` now owns shell/state/RPC/mutations/undo; `sim/hydraulic_forms.cpp`, `hydraulic_canvas.cpp`, `hydraulic_internal.h` (`ui::hydint`) and `hydraulic_canvas_state.h` (`ui::hydcanvas::State`) split forms, canvas and shared state, registered in `host/CMakeLists.txt`. Before-state was staged port selection followed by an explicit Connect action and setup-only ribbon simulation; no pre-change interaction timing was recorded. Circuit · Design · Simulate · Results (Outcome · BOM · To Piping) now separates editing/setup/outcomes; Start adopts native templates, the canvas has a draggable/click-placement palette, click/drag port gestures call `connectPorts` immediately, and ribbon Simulate runs when ready or reveals its blocker. `core/hydraulic/templates.*`, `hydraulic.cpp`, `core/CMakeLists.txt`, `services/mcp/hydraulic_tools.py`, the hydraulic contract/agent skill, `worker_client.cpp` and `tool_categories.py` expose one blank/power-unit/cylinder/motor catalog with editable suggested simulation setup to GUI and agents. `commitCircuitMutation` owns bounded circuit/layout undo; `app.cpp` suppresses global CAD undo while Simulate/Hydraulics owns it, and `hydraulic_scene.cpp` handles owner-aware 3D keys. `scenarios.cpp` adds the template run and `hydraulics_09_template.png`; native/MCP tests cover templates. Deferred: integrated checks and manual regeneration (`python3 -m tools.manual_gen`) from `services/mcp/cad_guide.py`, not edits to generated pages. Gotchas: only the 3D layout card is view-gated; selected component editors retain 3D fields/Place in 3D. Layout edits preserve simulation; circuit edits invalidate it. Templates are starting designs, not manufactured component CAD.

## 2026-10-08 — Professional 2D engineering drawings (ISO 128/129/5457/7200, ASME Y14)

Before: drawings had a bare 10 mm border, an unframed key/value title-block list (PNG drew a separate screen-space panel), outline-only arrowheads, blue Helvetica text, no centre lines/marks, a uniform grid layout that left views unaligned and duplicated the same overall dimension in every view, iso views full of hidden lines, landscape-only paper, and stacked diameter leaders. Now `core/doc/annot_draw.cpp` compiles a v2 primitive contract (thin/thick/dashed/chain classes, filled arrows, Helvetica-metric text, stroked ⌀⌴⌵↧) plus `compile_sheet_frame` (ISO 5457 frame, zones, centring marks, ISO 7200 title block with the ISO 5456-2 projection symbol, cell-fitted values) and `compile_view_decorations` (ISO 128-40 cutting planes, detail circles, SECTION/DETAIL captions); `drawing_get` emits `sheet.frame` + `view.draw`. `core/doc/drawing_session.cpp` adds the `drawing_arrange` layout engine (first/third-angle lattice, one ISO 5455 scale fitted to the area free of title block/tables, shared projection lines, derived views beside their parent), sheet-level non-redundant auto-dimensioning (each extent once, round outlines by ⌀, centre marks, side-view axis centre lines from the B-rep cylinders, bolt circles), portrait sizes (`A4P`), assembly BOM-on-title-block + balloons, general-arrangement mode for >4 bodies (no hidden lines, envelope dims), and a source-revision-keyed shape + HLR projection cache (skid drawing_make no longer re-runs HLR per step). `export2d/pdfvec/svgvec/dxf` + `drawing_render_png` share one pool (`core/doc/sheet_ink.h`): true ISO line weights, WinAnsi PDF text, DXF R12 layers VISIBLE/HIDDEN/CENTER/DIMENSIONS/HATCH/BORDER/TABLES with SOLID arrows, anti-aliased 5 px/mm PNG with a stroke font. Host `drawing_panel.cpp` renders the same contract, clips to the viewport clear rect, and gains a Sheet Properties popup (size/portrait/projection/scale/units/arrange/title block/export); `makeDrawing()` is one `drawing_make`. MCP: `make_drawing(projection=, orientation="portrait")`, `arrange_drawing`, extended title-block vocabulary; manual `guide-drawings.md`. Pins: `test_drawing_gdt` (layout per projection, non-redundant dims/centre lines/bolt circle, frame fit, ISO 129 geometry, section/detail identification), `services/mcp/test_cad_drawing_tools.py`, `CHUDWARE_SHOT=drawing` (self-checking PASS/CHECK). Deferred: agent-side registration of `arrange_drawing` (services/agent tool_categories/tool_guidance + panel snapshot), feature-level dimensioning beyond envelope/diameters.

## 2026-10-09 — Facilities demo qualification and GUI runtime closeout

`benchmarks/artifact_validity` now uses the ordinary single-turn mission executor and
grades one declared saved artifact with fixed mandatory checks, independent facilities
oracles and isolated workspaces. Live GPT facilities trials exposed and repaired
study-only save, vessel/duct persistence and skill-tool loading failures. These are
product QA trials, not training-data collection.

Copilot now shows persistent tasks, a bounded scrolling agent roster and inline
coordination messages. Transcript/control styling, settings navigation and pane grips
share the kit/theme vocabulary. Ended and restored runs settle unfinished agent/task
states and freeze elapsed time at the last event. Scripted screenshots use private
session/draft stores and skip legacy history import; explicit session overrides remain
available. Simulation units share one SI/US conversion implementation. The viewport's
auxiliary radius is document-scoped so a large facility cannot leave a new empty part
unusable; `pipedraw` exercises that transition before real port/plane input.

Drawing verification covers projected sheets and PDF/DXF/SVG export, but automatic
dimensioning is not a substitute for engineering detailing. Pressure-vessel sizing is
preliminary internal-pressure sizing, not code certification. Thermal CFD requires an
installed OpenFOAM backend. The demo script names these limits and separates quick live
vessel/duct prompts from the longer HPU/piping prompts.

Final workspace closeout restores the most recently active surviving document when
closing tabs, sizes collapsed columns against the current workspace band, and makes
empty ECAD boards actionable. PLM/Explorer share project discovery and fence stale
graph responses; Versions previews export the selected immutable commit rather than
HEAD. Parts performs real initial searches and coalesces filter changes. Onboarding,
Manufacture, EM frequency controls, Motion playback and topology summaries use the
existing kit layout and document units. Native and WASM builds passed; source audits
passed all nine gates, host CTest passed 31/31, and PLM worker tests passed 28/28.
Runtime captures covered pipe drawing after a large-document transition, the demo's
15 views, chat, modeling/edit/save/reopen, sketch3D, Evaluate, drawing exports,
workspaces, Parts, PLM and multi-document state. The simulation audit covered 117
states with zero ID conflicts or overflow findings; its declared solver/torque
skips are not claimed as verified capabilities.

## 2026-10-09 — Haiku 5.5 agent compatibility

Added Claude Haiku 5.5 to the offline Anthropic catalog and shipped model rankings,
including subscription and router capability resolution. Existing provider tests
now cover adaptive thinking and omitted sampling parameters for Haiku 5.5.

The published Chudcode 0.4.1 binary fails Haiku 5.5 registration with overlapping
budget/adaptive thinking rules. Runtime commit
`0300151940e68e3ecf30244d2c6abc071554683a`, tagged `chudcode-v0.4.2`, fixes that
overlap and supplies the native seed, tokenizer, effort levels and pricing tiers.
Its three-platform release workflow is pending; the artifact lock and installed
runtime deliberately remain at 0.4.1 until verified artifacts are available.

Verification: 135 focused runtime tests passed; the catalog lint, formatting and
type checks passed; Chudware's focused agent suite passed 223 tests and 21
subtests; artifact pin checks passed 15 tests. The actual source CLI discovers
and selects Haiku 5.5. A real completion returned Anthropic HTTP 429 with a
retry-after delay, so successful generation and compiled CAD-tool execution are
not claimed. No existing user sessions or host daemons were stopped.

## 2026-10-09 — Working-directory authority and saved demo studies

`AppState::workspaceDir` is now the sole top-left, PLM and chat project authority;
opening or selecting outside-directory documents cannot override it.
`copilot/conversations.cpp` reconciles history startup/reload against the selected
root and validates remembered-session ownership, while parking running chats on
root switches. `layout.cpp` keeps Simulate, Manufacture and Electronics rails
independent of Model's saved roll-up state; Simulation Manager reports list-load
errors with an explicit retry instead of a blank pane or repeated frame retries.
The `projectchat` runtime scenario passed startup, root-switch, remembered-map and
real external-file checks. `demoreview` reopened a copy of the pristine facilities
part with Model collapsed, found five studies through the normal tree path and
captured sixteen saved-result views. The original and backup stayed byte-identical.
Linux CI now refreshes the service runtime, obtains the private typed SDK from the
source archive pinned by the canonical runtime release, installs its DVC gate
dependency and uses that interpreter for manual generation; configure failures
retain full logs. A clean isolated wheel install proved the archived SDK's 0.3.0 pin.
Focused service suites passed 1,758 tests; host CTest passed 31/31 and all nine
source gates passed. Full remote Linux gate and production promotion remain pending
at this handoff; no successful OpenFOAM execution or live provider turn is claimed.

### Linux gate qualification follow-up

The refreshed CI runtime passed core/native/WASM builds, core CTest, hostd smoke,
manual generation, DVC and the facilities-adjacent service suites. Its remaining
failures were a parity test's overlong Unix socket directory and an absent ngspice
executable, not missing engine outputs. The registry test now uses a private short
runtime directory and always tears down its owned daemon; the dedicated Linux gate
provisions real ngspice rather than weakening backend assertions. Seven focused
tests passed, and direct runtime smoke connected the default-key private daemon
and computed a 6.6667 V divider through ngspice. Full remote requalification remains
pending; the workspace/demo repair itself is already promoted and locally verified.

The WSL builder has no unattended sudo. Ngspice provisioning therefore extracts
the real distro package and its dependency closure into job-local storage, scopes
library lookup to its launcher and relocates the shipped code-model initialization.
The exact CI step ran as an unprivileged Ubuntu user and solved the 6.6667 V divider.

### Gateway CI qualification follow-up

Remote run 37975808162 passed the strict core/service gate and host GUI CTest;
its remaining failure was the gateway suite. Both fake-daemon fixtures now use
a shared, private short AF_UNIX path rather than pytest's deeply nested paths.
The gateway test venv installs the existing six-package MCP import surface from
the canonical manifest. Billing/BYOK contracts patch only `save_touched_docs`,
not the entire `cad_common` module, preserving live registry imports.
The complete gateway suite passed locally with real PostgreSQL under a deep
CI-style TMPDIR: 849 passed, 29 skipped. Workflow policy reported zero errors.
Remote requalification remains pending; these changes do not alter the desktop
demo binary. Its fallback now contains sixteen verified metric views and a
64-second still-view walkthrough, with the source model and backup unchanged.

## 2026-10-09 — Repository housekeeping and development-gate coverage

`scripts/gate.sh` now uses its selected runtime interpreter for the entire
packaging suite and includes script and asset regressions in the per-unit sweep.
`scripts/check.py` also runs the existing shared-UI manifest/asset verifier.
Removed duplicate source-text assertions in the icon and release suites; ribbon
icon coverage remains in `gui_lint`, and release handoff identity/tamper tests
now build a real canonical payload manifest. Six generated public-site `.astro`
files were removed from the index while all nine local cache files, including
the pre-existing edit, remained byte-identical. A source-only isolated site
build regenerated its types and built 39 pages. Nightly release concurrency
queues instead of cancelling in-flight platform legs; Linux runner provisioning
explicitly targets `cc-linux-build`. All workflow policy checks report zero
errors and advisories; no remote workflow was dispatched.

The undefined-name audit exposed real missing imports in gateway workspace
copy/delete and MTConnect JSON serialization, unresolved runtime annotations,
an obsolete machining-registry call and retired assembly-verification code.
Gateway file and directory copying reproduced `NameError` before the import
fix; behavioral regressions now preserve source bytes through copy/delete.
Machining aliases resolve through native material-key and CAM-id RPCs instead
of a swallowed deleted-oracle lookup. The assembly verifier now proves native
save/close/reopen component placements, volume, centres of mass and world bounds
and removes its temporary files. The contact audit binds the loaded engine's
error class, retaining honest missing-method SKIPs and real error failures.
Malformed lint directives in three flywheel benches were also removed.

Runtime proof: a live isolated gateway backed by real PostgreSQL returned
HTTP 200 for both file and directory copy/delete with source bytes intact;
native assembly reopening preserved its geometry and placements; native
two-box contact carried the 4000 N load into the support with 16 active pairs
and a 4000.000000000458 N reaction. Monitoring JSON and runtime annotations
resolved successfully. Focused service suites passed 158 tests, workspace
isolation passed 47, and the actual packaging gate stage passed 52 using the
selected interpreter. The complete gateway suite passed 851 tests with 29
existing skips, using the same LangGraph/LangSmith versions as the development
environment; its isolated virtualenv was removed afterward. A separate sandbox
with newer LangSmith and its HTTPX2 dependency timed out in the full websocket
suite, although the chat module passed alone. That dependency upgrade is not
qualified by this session, and no application dependency pins were changed.
The machine-local compilation database is now ignored. Existing user sessions
and unrelated changes were left intact.

Integrated qualification: `CHUDWARE_GATE_STRICT=1` completed with PASS and no
environment-level skips. Native OCCT core, host GUI/test targets and the WASM
client built successfully; core CTest, isolated hostd lifecycle/recovery and web
parity passed. The Python sweep passed 11,499 tests across 33 units, covering
509/509 service test files. Packaging passed all 52 tests, and the sensor-node
golden demo passed all 13 checks. Native GUI startup and Copilot input screenshots
were rendered and inspected; this is startup/control smoke, not a claim of
full interactive modeling coverage. Separately, host CTest passed 31/31.
Heavy V&V/modeling/E2E/performance and AppImage stages remain explicit opt-ins
and were not enabled. Obsolete historical gate failure counts were removed
after qualification; only comments changed after the integrated gate run.

## Copilot controls and project-scoped Sub-agent hub

- Split the composer footer into adjacent model, effort, attachment, voice and
  context cells with four theme-aware vertical dividers. Moved New chat to a
  second Copilot header row beside a permanently labelled Sessions control.
  Removed duplicate session creation in the shared Sessions panel; narrow header
  spacing keeps task/artifact controls separate.
- Added AI → Sub-agents with Flat / By parent organization, a project-wide roster
  and optional persisted Current session only isolation. Registry agents and
  stored task traces share the existing docked inspector. Selection includes the
  owning conversation, so reused child IDs cannot alias across sessions. Inspecting
  another session's agent does not switch the active chat.
- Scoped transcript/steer/pause/terminate requests to project and owner session,
  cleared selection/drafts on scope changes and fenced stale async responses.
  Backend scope refusals precede reads or mutations. Explicit nested project roots
  no longer expand to an enclosing repository; trace-only agents stay read-only.
  Missing token/cost usage is not fabricated.

Verification: native build succeeded; all-rule GUI lint was clean for the changed
GUI files; the registry/RPC suite passed 44 tests. Rendered and inspected light,
dark and minimum-width Copilot plus dark/wide and light/compact AI hub surfaces;
ID audits reported zero conflicts. In an isolated native GUI with fixture sessions
and real registry records, exercised project/session filtering, duplicate child
IDs, foreign-chat isolation, explicit project switching, transcript inspection,
steer and pause; only the selected owner received the control mutations. Top New
chat opened a fresh chat while retaining the prior session. The projectchat
scenario passed its root/session/run-parking checks. No provider turn was launched;
the user's open application and sessions were not restarted or interrupted.

## 2026-10-09 — Planar outward normals honour left-handed plane frames; `axis_point` read-back

Root cause of revolved bodies reporting normal `[1,0,0]` for every planar face (the x=0 end
included): `BRepPrimAPI_MakeRevol` builds some planar faces on a LEFT-handed `gp_Ax3`
(`Position().Direct() == false`), whose surface normal is `-Axis().Direction()`. The repo-wide
idiom "`Plane().Axis().Direction()`, flipped when REVERSED" ignored handedness. New header-only
`core/geom/face_normal.h` (`surface_outward_sign`, `plane_outward_normal`) is now the single
source for doc_query (`picks.cpp`), `describe_faces`/pick selectors (`selectors.cpp`), hole drill
direction, weld and text face frames (`document.cpp` — a hole on a revolved shaft's x=0 end used
to drill outward into air), the sketch-overlay plane and faceset normal (`session.cpp`), convert
entities (`recon.cpp`), sheet-metal face normals and the FEA contact preflight's plane/cylinder
normal envelopes (`fea_run.cpp`). Cache epoch `chudware-geomcache/8`. picks.cpp's CAP
PREFERENCE block is kept (it still decides cap-vs-curved side queries); only its rationale
changed. Cylinder/cone matches add `axis_point` (centroid projected onto the axis; contract
selectors.md, readback-key allowlists in picks.cpp + cad_common.py). Gotcha: draft's neutral
pull (`document.cpp`, raw unsigned plane axis) and mirror planes are sign-agnostic/unchanged.
Also fixed: doc_query `hint_index` was the match's position in the FILTERED result, so the
`body`/`topo` join keys derived from it pointed at the wrong face for any filtered query
(`{"surf":"cylinder"}` reported topo 1). It is now the sub-shape's index in the whole shape's
unique map, the meaning the topo stamping, hint fallback and measure picks already assumed.
`face_sid_inventory` drops `axis_point` (derived from centroid+axis) so saved face-finish
fingerprints keep matching; finishes recorded on a left-handed-frame face carry the old
`normal`/`side` and go inert (the documented stale-record behaviour), so repaint those.
Tests: test_selectors `B9b`, test_picks readback keys, test_cad_tools
`test_revolved_shaft_*`, `test_hole_on_revolved_end_face_drills_into_material`,
`test_cylinder_faces_report_axis_point_on_the_axis` (filtered-query topo via doc_resolve_pick).

## 2026-10-09 — Tee per-path K, strict fitting params, ASTM pipe/vessel grades in the registry

Benchmark F2 showed `update_fitting` silently storing `K_run`/`k_branch` on a tee while solve
ignored them, and `set_material('ASTM A106 Grade B'|'SA-516-70')` refusing as an "Unknown
appearance material". Tees now take `params.k_model_run` / `params.k_model_branch` (any
k_model kind; straight-through charged on `run_ids[1]`, branch on `branch_run`, precedence
path model > `k_model` > Crane/default) in `fitting_K` (`core/piping/solve.cpp`), so solve,
report, `run_loss_1d` and `piping_fitting_k` (new `path:"run"|"branch"`, tees always return
`paths.run/branch`) share it. `piping_edit` add/update_fitting refuse unknown params keys with
`unknown_param`, naming every offending key and the accepted set (`fitting_param_keys`,
`core/piping/schema.cpp`); null deletions and unchanged re-sent values pass so GUI
whole-params updates of older docs keep working. Registry gains standard-minimum rows for
A106-B, A53-B, SA-516-70, A312 TP304/TP316 with aliases; `_resolve_appearance` now resolves
every registry row (own finish → family word → category look) and its refusal names the
registry. Tests: test_piping `tee:`/`edit: unknown fitting params`/`fitting_k: tee previews`,
test_piping_design_tools `test_tee_per_path_constant_k_*`, test_material_resolve
`test_piping_and_vessel_grades_*`, `test_set_material_accepts_astm_*`. Pending: contract
append to `contracts/piping_schema.md` §9 (integrator-owned); no GUI per-path K editor yet.

## 2026-10-09 — Shell: query lists resolve per element; every offset strategy validated

Hard-benchmark parts hit two shell defects. (1) `refs.remove:[{"of":"face","side":"top"},
{"of":"face","side":"bottom"}]` dangled "no candidates of matching kind" (a list went to the
topology scorer; only a one-element list was unwrapped) and, with `"kind":"face"` added,
leaked `[json.exception.out_of_range.403] key 'centroid' not found` from `score_candidate`.
`resolve_face_picks` (`core/doc/picks.cpp`) now resolves every centroid-less query element
through `resolve_geom_query` (a readback carrying a centroid stays a pick; duplicate faces
select once), and `resolve()` (`selectors.cpp`) names a non-object or centroid-less selector
instead of throwing. (2) The "shell is not supported on lofted / swept / revolved" refusal
was not preemptive: `geom::shell_solid` returned the FIRST non-null strategy result and the
caller refused when it was open. It now walks the whole join/tolerance ladder, accepting a
result only when BRepCheck-valid, watertight and volume-moved (a valid unchanged result is
still returned so the "thickness too large" guard names it), and fills a `diag` with the
`BRepOffset_Error` per strategy; the doc-level open-shell guard is gone (unreachable). A
spline-revolve cup shells. A rectangle-to-circle loft duct genuinely cannot: every join type /
tolerance fails `BRepOffset_MixedConnectivity` (its corner seams go from a 90° crease to G1),
and `MakeOffsetShape` / `PerformBySimple` / a slab+pipe erosion boolean fallback were tried
and failed or produced wrong volumes, so the refusal stays, now naming the error and locating
the crease-to-tangent edges. Cache epoch `chudware-geomcache/9`. Tests:
test_modeling_repertoire `test_a_shell_opened_top_and_bottom_is_a_tube[*]`,
`test_a_revolved_freeform_cup_shells_when_the_offset_validates`,
`test_a_rect_to_circle_loft_duct_refuses_shell_by_name`.

## 2026-10-09 — Geometry-query `near` ranks by distance to the geometry

gpt-6-luna H5 chamfered a bolt hole instead of the Ø110 rim: `{"of":"edge","side":"+z",
"surf":"circle","near":[55,0,12],"count":1}` names a point ON the rim, but
`resolve_geom_query` sorted by centroid distance (rim centroid = axis point, 55 mm; +X hole
edge centroid (40,0,12), 15 mm). `rank_near` (`core/doc/picks.cpp`) now keys on
min(BRepExtrema point→trimmed sub-shape distance, centroid distance) — the centroid term keeps
every internal "centroid as near" re-resolution (`session.cpp` selector/measure/face-by-sid,
`cad_asm_tools` fixture faces, recon edges) exact — with ties (max(1e-4 mm, 1e-6×key)) going to
the nearer centroid, then MapShapes order; a bbox lower bound skips exact distances that cannot
reach the `count`-th centroid distance. It is the only `near` implementation (selectors.cpp,
Python and assembly code all call it). Contract `contracts/selectors.md` §7, cache epoch
`chudware-geomcache/10` (`contracts/stepplus.md` §12). Tests: test_picks `geom query near: …`,
test_hard_tasks `H5-5`.

## 2026-10-09 — GPT-6.1-sol / GPT-6-luna artifact benchmark campaign

`benchmarks/artifact_validity` gained an M tier (`tasks/modeling.py`: revolve shaft, bolt
flange, filleted L-bracket, counterbored plate, hex standoff) and an H tier (`tasks/hard.py`:
swept tube, loft duct, beam-sized cantilever + FEA study, hinge assembly with mates and
interference sweep, flange redesign from fixture, filleted shelled enclosure). Baselines
(tags `base1010-*`, `baseM-*`, `baseH-*`; confirmations `fix1`, `final-*`; runs git-ignored)
surfaced the fixes logged above (hole world points, left-handed plane normals + `axis_point`,
doc_query topo join keys, torus readback, eval_thickness offset, shell list/strategy ladder,
`near`, piping per-path tee K + `unknown_param`, pipe/vessel grade materials, `sim_results`
for `job-*` drivers, `new_assembly(fresh=True)`, turn-budget notice + pre-expiry steer) plus
three harness defects fixed here: (1) `tool_registry._FamilyView.tool` copies a
`description=` kwarg onto the guarded callable's `__doc__` — the `check_geometry_health`
aliases had none, so a prompt that auto-loaded their category died in `set_host_tools`
before any model call; (2) `hydraulic_simulate` stores `series_path` project-relative
(`portable_series_path`, resolved by `hydraulic_load`), so a moved project keeps its
full-resolution run (every F1 trial was false-failed by the absolute path); (3) the reply's
`max_points` no longer decimates the persisted run (the study always keeps the ≤400-point
compaction). To benchmark against a frozen engine while editing core, copy
`core/build-occt-linux/bin` and run the runner with `CHUDWARE_LIBCHUDWARE=<copy>/libchudware.so
--hostd-bin <copy>/chudware-hostd --allow-stale-engine`. Open: rectangle→circle loft ducts
cannot be shelled by OCCT (`BRepOffset_MixedConnectivity`, refusal names the edges); the
`spice_sweep` ctest flaked once under `-j6`.

## 2026-10-10 — Sectional shell: rectangle→circle loft ducts hollow

`shell` on a rectangle→circle loft (with or without collars) used to refuse: OCCT's
`BRepOffset_Analyse` marks an edge between two B-spline faces whose dihedral crosses 0.1 rad
along it as `ChFiDS_Mixed`, and `BRepOffset_MakeOffset` stops with `MixedConnectivity` for every
join, tolerance, face split (tested: splitting the seams at the threshold, flagging the upper part
G1, wrapping the faces in zero offset surfaces — all invalid or `UnknownError`). New
`core/geom/shell_sections.{h,cpp}` (`geom::shell_by_sections`) is the inward fallback
`geom::shell_solid` tries after the offset ladder fails. It never offsets a face: the opened faces
fix an axis, the body splits into height bands at vertex levels, and each band's cavity is lofted
through sections computed from point-to-skin distance (a Newton march along each slice's inward
normal, with per-face/edge persistent `BRepExtrema_ExtPF/ExtPC`; ownership of the nearest entity
rejects marched points that belong to a neighbouring side, and corners come from a 2-D Newton on
the two sides' distances, or the bisector point near tangency). Bands of planes/cylinders along the
axis get exact analytic inner planes/cylinders and go first, so junction sections are shared
through per-body-vertex/edge caches; freeform bands keep a 4-step ruled transition at junctions
(the true kink sits t·tan(θ/2) off the level). The cavity is sewn, cut (`cut_unify`), and the result
must be BRepCheck-valid, one watertight solid, with every sampled inner-wall point t ± 2% from the
kept skin; a failed exact-prism attempt retries with sampled junctions, and only the accepted cut's
history reaches the feature scope. Loft duct (H2 geometry): 2.7 s, wall 1.978–2.000 mm at
t = 2 (worst at the collar junction corners, ≤0.0001 mm inside the loft), max edge tolerance 1e-7.
Outward shells, opened non-end faces and steps across the axis still refuse with the kernel
reason plus the sectional one. Cache epoch `chudware-geomcache/11` (`contracts/stepplus.md` §13);
cheatsheet `shell.md` updated. Tests: `test_modeling_repertoire`
`test_a_rect_to_circle_loft_duct_shells_open_at_both_ends`,
`test_an_outward_shell_of_the_loft_duct_still_refuses_by_name`; `test_hard_tasks` H2 cases built
with one `shell` feature pass all eight H2 grader checks.
Same session: `eval_feature_attachment` (`core/doc/session.cpp`) compared every additive feature's
base face against the FINAL part, so any later shell or cut that hollowed the material under it
read as an overhang (the luna smoke's shelled duct: "Top collar OVERHANGS its support: 89 %"; a
box + boss + shell: 55 %). Samples whose inner side is no longer the feature's kept material
(`common(part, tool)`) are now skipped and the base area is scaled to the surviving share.
Test: test_interference `eval_feature_attachment: a boss over a later shell's cavity is attached`.

## 2026-10-10 — 6-axis interview trace: tool defects fixed

Source: the 6-axis robot-arm session (`~/Documents/Chudware/Sessions/2026-10-10T15-38-28-774Z_*`)
and its four filed bug reports. Fixes, one per defect class:
`sketch_edit_entity` takes the `entity:{id,…}` patch and refuses ignored, empty, type-changing or
unknown-field patches by name (`core/doc/session.cpp`); profile region picks never snap to a
surviving region (`resolve_profile_faces`, selectors §8, cache epoch `geomcache/12`);
`sketch_add_constraint` `fix` adopts the solved coordinates first, and non-convergence names
the conflicting constraints and authored gaps (`core/sketch/sketch.cpp`); `centroid` alone is
a valid region selector; unknown extrude params (`reverse`) are refused on the op path only
(`validate.cpp unknown_feature_params`). `feature_attachment` measures the FOOT with a
mirror-invariant tie-break; an in-place `transform` re-places the moved body's ToolRecs
(`document.cpp`), so overlap/attachment read the as-built pose. Relative component sources
resolve against the assembly file folder, else the `doc_new` project folder, never the cwd
(`assembly.cpp`, `component_source_base`; `describe_assembly`/`read_native_file` add
`source_resolved`). `verify_battery` treats a doc with components/world solids as geometry
and marks an ambiguous battery-chosen measure probe `not_evaluated`. DFAM ranks fail before
warn before truncation (`regions_omitted`) and locates inaccessible-support clusters;
`doc_eval_thickness` counts only exit faces met at > 5° (no rim grazing minima). Topology
`reanalysis.vtu` carries point `von_mises[@case]`; `render_animation` cell-averages old files.
`slice_part` declares `context_doc_unless("parts")`; the CEF client names a missing or exiting
helper at once; `research_web_search` binds `limit` to `max_results`; sub-1e-7 mm endpoint gaps
are not `near_coincident`. Deferred: the extrude-only known-key table (other feature types not
audited); a boss sketched on a bottom plane and extruded through the plate can still read as an
overhang (ToolRec lacks the extrude direction); `xd://` reads of unmounted tools still return
Chudcode's "No such tool" (harness side). Gotcha: old `cache.ckpt` files keep stale ToolRecs
until the transform recompiles; the `/12` epoch forces that on open. Tests:
`sixaxis_regressions`, `sketch_convert`, `interference`, `insert_component`, `topopt_run`,
`services/dfam/test_dfam.py`, `services/mcp/test_verify_battery.py`, `test_animation_tools.py`.

## 2026-10-10 — Failed tool calls after the 6-axis fixes

Survey of every failed tool row in conversations c213–c250 (164 of ~6,100 calls). Most
failures were already fixed by the entry above, are modeling errors with teaching replies,
or are external (HTTP 401/login walls). The one failure after the post-fix worker restart
was eval `KeyError: 'features'`: `get_part(summary=True)` returned top-level `features`,
while the full form nests it under `tree` (c237 and c243 hit the same split). Both forms now
return `{ok, doc_id, revision, tree:{features}}`; summary adds `summary`/`feature_count`,
full adds `owner_map` (`cad_doc_tools.get_part`, panel tables regenerated). The deferred
unmounted-`xd://` case is mitigated only in the prompt: `general_agent.yaml` says a MORE TOOLS
name fails "No such tool" until `enable_tools` loads it. Chudcode 0.4.1 has no host hook for
unknown devices. Not changed: eval code that `json.loads` a shortened string field
(`fs_read` of a >46 KB file) still sees a cut string; the result's `_truncated.full_result`
names the complete JSON. Python edits take effect when the services worker restarts.

Second pass (same run, after the restart): three `add_features` mate batches failed.
A flat `{op:"asm_add_mate", type, align, refs, value}` failed "requires a 'mate' object".
Refs `{component, query}` failed "selector must be an object". A number `value` failed
"dimension must be an object". `asm_mate` accepted all three forms. Now
`_normalize_asm_mate_ops` wraps the flat op, changes a number value to `{value, expr}`, and
resolves each `{component, query|sid}` ref with `cad_asm_tools.resolve_mate_ref`. That
resolver is the old `asm_mate` loop, moved to module level with its source-document
helpers. The engine text "a selector slot accepts a geometry QUERY object" is still not
true for mate refs (C++ not changed). Test: `test_asm_add_mate_accepts_the_live_flat_and_query_shapes`.

## 2026-10-10 — preserve mechanical transmission during native motion

`core/doc/session.cpp` no longer pre-poses scalar drives when active gear,
rack-pinion or screw mates use incremental warm-start coordinates. Pre-posing
the drive erased the increment and left the passive gear stationary despite a
converged solve. The previous solved pose now seeds the coupled native solve.
`core/tests/test_simulate_motion.cpp` checks actual opposite gear rotation in
both directions from one driver. The full motion executable passed 12 cases
and 1,927 assertions. An actual saved four-bar gripper also passed native
opening and closing sweeps after native incremental initialization of the
open pose. A loaded services worker retains its old library after a rebuild;
restart the app before recomputing coupled studies. No firmware was changed.

## 2026-10-10 — Failed tool calls, third pass (engine, hostd, Chudcode)

Each failed-card class from c237–c250 now has a root fix. Engine: `validate.cpp` gives a
mate-ref message (`mate_ref_forms`) in place of the generic selector grammar. `picks.cpp`
`canon_surf` accepts `cyl`, `sph`, `arc` and similar words from one table that also builds
the refusal text. `mate.cpp` and `assembly.cpp` name each unresolved ref (index, component,
sid). `doc_eval_section_props` sections a closed shell as a solid. A plane that cuts a body
with free edges is refused as `body_not_closed_solid` with the body id, not a wrong area.
`eval_is_closed_solid` now uses the `check_geometry_health` free-edge rule, so an imprint
edge does not mark a body open. `sketch3d_section_at` no longer turns section errors into
area 0. hostd: a draining daemon reads the first request and answers `hello` with a
`daemon_draining` error (reason, pid, active clients). Before, it closed the socket unread,
and the client got ECONNRESET. `--status` reports the drain. `daemon_bridge`, `core_bridge`
and the GUI `engine_backend` refuse by name and never fall back to an in-process engine.
Tool contracts: `check_interference` returns `ok:true` for a complete check, and the verdict
is `clear`. `interface_check` returns `ok:true`, and the verdict is `status`. All
consumers moved (battery, dag verify, design review, guardrails, reviewer, req evidence).
`get_part` summary keeps long lists as lists and records `truncated_lists`. `fs_read` pages
by `byte_offset`/`next_byte_offset` in 32 KB pages. A host-cut string carries a
`…[cut: …]` marker. Image tools give eval callers one mapping
(`{ok, caption, images, saved_path}`). `workspace_status()` reports the turn workspace
(`scope:"turn"`). `chudcode_host.publish_host_tools` sends `loadable_tools` (the catalog
minus the mounted names). Chudcode 0.4.3 adds the `xd://<name> is not loaded. Call
enable_tools…` error. It also pauses the eval cell budget during host-tool calls and prints
truthful timeout text. A cell that runs on a replacement kernel gets a notice. The lock pins
0.4.3, and `requirements.txt` pins `chudcode-rpc==0.3.1` (`LoadableHostTools`); the GUI
worker's system Python needs that client too. The 600 s eval cap came from the user config
(`~/.chudcode/agent/config.yml` `tools.maxTimeout: 600`), not from Chudware. Not changed:
third-party errors in agent eval code (matplotlib `set_box_aspect` on a read-only trimesh
array, a missing `rtree` module). Verified with ctest `picks`, `document`, `mate`,
`mate_corpus`, `insert_component`, `export_assembly`, `test_worker_long_run`, the hostd and
daemon_bridge pytest, `scripts/check.py`, and two headless `run_mission.py` turns on
Chudcode HEAD.

Release (2026-10-11): Chudcode `chudcode-v0.4.3` (commit 2eaba2f4) is published. Its
release CI no longer uses Actions artifacts: the account storage quota had failed every
upload (0.4.2 never published). Files now cross runners as assets of the tag's draft
release (`scripts/ci-release-transport.ts`). The release ships no Windows CLI, because the
Linux cross-compiled exe segfaults in Bun 1.4.0. The Windows lock row comes from
`chudcode-runtime-windows-build.yml`; that workflow now installs Bun itself, because
setup-bun failed with EPERM on the runner. The mirror `chudcode-runtime-v0.4.3` holds the
Linux/macOS bytes and the exact-commit source archive. The deploy host's
`CHUDWARE_CHUD_BIN` holds 0.4.3; the 0.4.1 copy is kept beside it as `chud-0.4.1.backup`.

## 2026-10-10 — use geometric axes for partial-cylinder interfaces

`services/mcp/interface_check_tools.py` now uses `doc_query.axis_point`, not
the area centroid of a cylindrical surface, for bore/boss axis comparisons
and seam merging. A semicylinder centroid is displaced from its geometric
axis; the actual MG996R capsule mounts previously produced spurious 2.0823 mm
shift instructions. `test_interface_check.py` adds native D-shaft/socket
cases at zero and 0.75 mm offset. The targeted suite passed 16 tests.
Read-only native verification of the saved arm also confirmed all twelve
nominal M3 centers lie within capsule travel, with 0.2 mm radial clearance.
Circular-hole matching still does not infer a complete capsule from its
endcaps; the raw automatic findings remain separate from that exact check.

The full gate's source audit, manual regeneration and host build passed.
Its core build failed on `services/ipc/ipc_client.cpp` declaration/definition
mismatches and two-argument `hello` calls against a three-argument header.
The command then timed out during WASM smoke. Those IPC files were not
edited by this repair. `graphify update .` completed AST extraction but
timed out before a successful graph publication was confirmed.
The active desktop/worker has not been restarted; the rebuilt native
motion library was verified in an explicitly isolated process.

After intervening working-tree changes, the inspected IPC declarations and
definitions match. `cmake --build core/build-linux --target chudware-hostd -j 2`
completed successfully. This target-only result does not supersede the
failed whole-repository gate or mean the running desktop loaded the new
library. The interview package records the unreleased runtime and physical
fit limits separately from the isolated native verification.

## 2026-10-11 — DWG import hardening + DWG/DXF export

Native DWG import never worked with a real LibreDWG: `dwg_to_temp_dxf` reserves the output
file and `dwg2dxf` refuses to overwrite without `-y` (the argv test used a fake converter).
Fixed, and the shared DXF reader (`core/doc/formats.cpp`) now expands BLOCKS/INSERT (incl.
MINSERT, mirrored/non-uniform scale), honours entity OCS (ARC/CIRCLE/LWPOLYLINE/2D POLYLINE/
INSERT/ELLIPSE), imports 3DFACE/POLYFACE/polygon meshes as one mesh body per layer, and
reports ACIS 3DSOLID/BODY/REGION/SURFACE by name (warning, or the import error when nothing
else remains). Export: `export_file` writes `dxf` (R12, solids as 3DFACE with hidden interior
edges, 2D bodies as LINE/ARC/CIRCLE) and `dwg`; `export_drawing`/`export_flat_dxf` take
`dwg`. New `core/doc/dwg.{h,cpp}`: LibreDWG tool lookup (PATH, then
`<appdata>/Chudware/tools/libredwg/bin`, mirrored by `services/formats/libredwg.py`), a
timed literal-argv runner, and `write_dwg`, which drives `dwgadd` and refuses unless the
file re-reads with every entity. `scripts/provision_libredwg.py` builds the SHA-pinned
LibreDWG 0.14 locally (Windows: upstream win64 zip, untested here). Gotchas, all measured on
0.14: `dxf2dwg` writes handle-0 garbage (unused); `dwgadd` exits 0 without writing on >5000
points per command, sscanf-rescans the whole script (NUL-terminate each line), segfaults in
`polyline_pface`/`polyline_3d` past ~1000 objects (stale object pointer after realloc — only
single-object entities are emitted), cannot set linetypes, 3DFACE edge flags, TEXT alignment
points or a new layer's linetype (layers carry no linetype; AutoCAD acceptance unverified),
and leaves TEXT width factor 0 (set to 1). GUI: File > Export DXF/DWG (tessellation dialog),
import/insert filters list DXF/DWG, Drawing ribbon `dwg_export_dwg`, flat-pattern DWG.
`.sto` was asked about and is not implemented (no CAD-standard format).

## 2026-10-11 — Mid-run messages: wake waits, no ghost turn after Stop, delivery captions

c252 "froze": the user's message arrived as a steer the moment the agent called
`write xd://wait_job {timeout_s:900}` on an FEA job. Chudcode only injects a steer at a tool
boundary and a host tool is not interruptible, so it sat 2m41s until the user pressed Stop; the
abort kept the queued steer and Chudcode's settle drain started a fresh model call with it that
no Chudware listener observed (`message user steering:true` after `stopReason:aborted`). Fix:
`chudcode_host` tracks user steers (`_Run.undelivered`) until their user `message_start`
streams, exposes `user_steer_pending` on the run context, and `dispatch_host_tool` injects a
`wake` probe into tools that declare it (`wait_job` returns `woken_by:"user_message"`, job
untouched); the steer then lands at the normal boundary — append-only, real tool result, no
aborted response, reasoning items and prompt-cache prefix intact (real codex smoke:
cacheRead held across the steer). Stop now `remove_queued_message`s unread steers before
`abort()`; withdrawn steers and ones stranded at the terminal open the session's next prompt.
`agent_steer.steer_id` round-trips to `agent.steer {delivered|carried}`; the GUI keeps
`ChatMsg::steerId/steerState` and draws a caption under the bubble (queued / read / unread ·
sends next time / not delivered); `CHUDWARE_SHOT=steerstatus`. Not changed: a steer during
real work (synchronous solve/build) still waits for that tool — Chudcode semantics. Carried
steers live in the worker process; a worker restart drops them while the caption still says
"sends next time". Also seen in c252, not addressed here: the first request of each new user
turn re-read only 121 088 of ~185 k cached tokens (later requests in the turn hit fully).

Same session, ask_user cards: `drawAssistantTurn` drew every question batch after the whole
timeline and reply, so an answered card in c252 (asked at row 24 of 63) trailed the rest of
the run. `feedAskEvent` now appends a rowKind-20 marker (stage `__ask__:<batch_id>`) to the
asking turn and `drawAgentTimeline` draws the card there (full width, off the spine);
`drawAskCards` draws only unmarked batches. Load anchors pre-marker batches after their
`ask_user` tool row (`anchorLegacyAsks`); persistence keeps rk 20 like plan markers.
`CHUDWARE_SHOT=steerstatus` asserts both paths.

## 2026-10-11 — Native AutoCAD R2000 DWG writer replaces LibreDWG `dwgadd`

`write_dwg` no longer drives `dwgadd` (exit-0-without-writing, crashes past ~1000 objects,
no linetypes / 3DFACE edge flags / TEXT alignment). `core/doc/dwg_write.{h,cpp}` encodes the
whole AC1015 file from the public ODA specification: file header with six section locators,
AuxHeader, preview, header variables (INSUNITS from the document, EXTMIN/EXTMAX computed incl.
SAT vertices, limits, HANDSEED), classes (DICTIONARYWDFLT, PLACEHOLDER, LAYOUT), every table
control and the records AutoCAD expects (layer 0 + document layers, style Standard, linetypes
ByBlock/ByLayer/Continuous/DASHED/CENTER, VPORT *Active, APPID ACAD, DIMSTYLE Standard,
*Model_Space/*Paper_Space with BLOCK/ENDBLK), the named object dictionary (ACAD_GROUP,
ACAD_LAYOUT Model/Layout1, ACAD_MLINESTYLE Standard, ACAD_PLOTSETTINGS, ACAD_PLOTSTYLENAME
Normal), per-object CRCs, the paged object map, ObjFreeSpace, second header and template.
Entities: LWPOLYLINE (closed, constant width, per-entity linetype; split at 20000 vertices,
LibreDWG's reader limit), ARC/CIRCLE, LINE, 3DFACE with edge visibility, SOLID, TEXT with its
alignment point, and 3DSOLID/BODY carrying the document's SAT 4.00 text inline (version-1
modeler data, CR LF lines, 4096-byte blocks). Writing needs no LibreDWG; `dwg2dxf`, when
installed, still re-reads the staged file and must return every entity before the rename.
`scripts/provision_libredwg.py` now provisions `dwg2dxf` only. Verified with the ODA File
Converter (audit on) to ACAD2018 DXF and ACAD2018 DWG→DXF: all entities by type, layers,
colours and linetypes back, handles preserved; the 3DSOLID SAT text is byte-identical through
LibreDWG and differs through ODA only where ODA re-serialises every body (save-date header,
last-digit floats — the same lines it changes re-exporting its own R2000 DWG). Not verified in
AutoCAD itself. Tests: `core/tests/test_dwg_write.cpp`.

## 2026-10-11 — AutoCAD solids: ACIS read/write, native DWG data storage, R2000 DXF

AutoCAD 2013+ DWGs (the user's AC1032 `SteelTubeWidget.dwg`) imported only their construction
lines: their 3DSOLID/SURFACE geometry is SAB in the `AcDb:AcDsPrototype_1b` data storage, which
LibreDWG drops (it also drops SURFACE classes, and its DXF output splits inline SAT at every
4096-byte DWG block and 255 characters, corrupting R2000–R2010 solids). New native pieces:
`core/doc/acis_read.cpp` (SAT 1.x–2xxxx and SAB/ASM → OCCT: analytic, NURBS and procedural
surfaces via stored approximations or rebuilt rolling-ball blends, ASM `exact_int_cur`),
`core/doc/acis_write.cpp` (SAT 400/700, analytic records whenever geometry is exactly analytic,
self-checked parameters), `core/doc/dwg_native.cpp` (AC1012–AC1032 containers incl. R2007,
AcDs records by handle, object map + per-version entity headers for type/owner/layer, inline
SAT/SAB), and an R2000 branch in `core/doc/dxf.cpp` used only when a document carries ACIS.
`core/doc/formats.cpp`: DXF/DWG import builds one exact body per ACIS entity (inline SAT, DXF
ACDSDATA, or native DWG data by handle — the handle-keyed data always wins), places block
instances, applies $INSUNITS and records it as the import's `source_units`; entities dwg2dxf
dropped are re-created from the native owner/layer; without dwg2dxf a DWG still imports its
model-space solids. `sat` is a new import/export format. `export_file` dxf/dwg write solids as
ACIS 3DSOLID/BODY, tilted arcs keep an OCS extrusion, `units` defaults to `source_units`, and
dxf/dwg/sat exports are re-read (solid count, validity, volume). The STEP round-trip check now
compares solids (a profile's remnants split into many bodies), and an import body made only of
curves/points is accepted as reference geometry instead of tripping the empty-body backstop (a
STEP with construction wires reopened empty). Embedded DXF/DWG/SAT imports keep their baked body
grouping; cache epoch `geomcache/13`. Verified: brother's DWG → 4 bodies (solids 1.541e6 and
2.784e5 mm³) in-process, in the GUI (`CHUDWARE_SHOT=dwgopen`) and over a private web stack; the
DWG/DXF/SAT revisions re-read identically, and ODA File Converter (validation oracle only)
re-saves them as AutoCAD 2018 DWG/DXF that re-import with identical solids. Not opened in
AutoCAD itself. Web: gateway grants docs minted by `import_file`, deploy provisions dwg2dxf.
Tests: `test_acis_read`, `test_acis_write`, `test_dwg_native`, `test_dxf_r2000`, `test_dwg_write`,
`test_import_roundtrip` (ACIS round trips, inch export, STEP with free wires).

## 2026-10-11 — Subagent task names and inline visual evidence

`services/agent/chudcode_events.py` now separates the Chudcode assigned task handle from
the generic agent type and late description; `host/src/ui/ai/agent_dag.cpp` preserves that
name through lifecycle/spawn updates and DAG persistence, including nested task handles.
Anonymous legacy activity is not assigned a guessed identity. The same mapper extracts
actual tool-returned image paths/captions from direct and nested CAD results, including
failed inspection diagnostics. The Copilot feed, activity timeline and internal detail
parser keep those images inline at completion, outside collapsed drawers and in grouped-call
order; expanded drawers do not duplicate images, and legacy Session files thumbnails
appear only when their tool evidence is absent. `ui_common.cpp` copies captions and
`conversations.cpp` preserves complete bounded tool trailers on compact persistence.
Show renders inline still governs display; numeric-only geometry checks create no
synthetic renders. Added the `chat_evidence` mapped-event screenshot scenario in
`app.cpp`. Verified: application build, 89 Python tests plus 10 subtests, five GUI lint
rules, native `render_part`/`inspect_part` image results replayed into actual collapsed,
grouped, simplified and expanded chat UI, and assigned names/nested parents retained
through DAG save/restore. Smoke uses private short HOME/appdata paths to avoid AF_UNIX
path-length limits and explicit separate Xvfb display numbers (concurrent auto-number
launches collided); no user session or daemon was stopped.

## 2026-10-12 — Six-axis recovery: save roots, tool results and rejected geometry

The c255 trace exposed separate persistence, tool-envelope and geometry failures.
`cad_common` now resolves explicit relative save/export destinations against the selected
project, not the worker cwd or an ancestor repository; absolute paths retain credential-path
checks. `chudcode_tools` carries successful guide/skill text in `details` as well as content,
including bounded full-result spills. The general-agent policy separates motor-case centering
from shaft coaxiality and requires stopped/released part ownership and frozen saved handoffs
before assembly verification. Native dress operations reject invalid B-reps, zero-area faces
and self-intersections before replacing the preceding body (cache epoch `geomcache/14`).
Atomic failures expose `rejected_tree`; regeneration diagnosis uses that candidate and its
native feature errors rather than the restored live tree. The live verifier replays canonical
feature envelopes with sparse-ID remapping through supported operations and closes its
temporary documents. Verification: 203 targeted Python tests and 19 subtests; five native
document cases with 1,624 assertions; private native save/export tests from two worker cwd
values with credential-symlink refusal and unchanged rollback state. The reduced wrist
R4 fillet committed two zero-area/self-intersecting faces before the fix; afterward it
rejects the feature and preserves the sound original volume. Real Chudcode guide/skill
calls and two parallel GPT workers returned usable text details without `KeyError: inner`.
No user daemon was stopped, no session history was rewritten, and these software checks
do not qualify the robot's physical fit or printed strength.
The subsequent real nested-workspace smoke also exposed `online_turn.resolve_workspace`
widening the selection before any tools ran. It now retains the exact canonical directory.
The same resumed Chudcode workflow now reports the selected nested root through
`workspace_status` (previously the repository root); all 34 worker RPC cases pass.
Reopening the actual robot with the new guard separately exposed wrist feature f74's
existing 4 mm chamfer zero-area face, demonstrating why cached historical pass reports
cannot substitute for rebuilding and verifying the saved model after this correction.

The actual recovery then exposed two native delivery defects. `core/doc/formats.cpp`
now refines overlapping tangent-cusp boundary chords on their native curves before
face meshing, synchronizing parameters across located partner edges and all incident
pcurves. STL omits only native degenerate-edge pole facets with zero boundary chain,
then checks noncollapse and closed, oppositely directed edge incidence in the actual
ASCII/binary representation. It does not heal the source or weld nearby points.
`session.cpp` evaluates self-intersection in a constituent face's proper rigid frame,
including nested import wrappers; world metrics and genuine folded/scaled/sheared
defects remain unchanged. Detached assembly cache validation now hashes the authored
file before session-local path/name rewriting; dependency changes still invalidate it.
Added reduced geometry/boundary regressions in `test_formats_registry`,
`test_interference` (including the two-face `placed_fillet_skin.brep` witness), and
`test_appearance`. The document, formats, appearance and interference ctest suites pass.
Private real-base exports pass exact raw-facet checks in binary, ASCII and fine binary:
15,644/15,644/89,054 facets, zero degenerates, one closed oriented surface, unchanged
source hash. Independent reopening reports cache hits on all five saved deliverables
and all 27 assembly bodies sound with every self-intersection check completed.
These are exporter/checker/persistence changes, not a further feature-geometry epoch.
The full Linux gate passed (11,670 Python tests, desktop/WASM builds, daemon lifecycle,
web parity, packaging and GUI-shot smokes). Its advisory generated-manual drift was
resolved through `tools.manual_gen`; coverage is 516/516 with a clean drift check.
After the delivery fixes, the complete gate-tree native rebuild and all 130 ctest
suites passed again; final source audits pass all ten gates. Code graph refreshed.

The final real slicing pass exposed a further float32 placement defect: resting the fine
wrist mesh on the bed merged a 1.9e-6 mm vertex pair and PrusaSlicer silently removed two
facets. `services/mcp/print_tools.py` now centres single-part input with a grid-aligned
shift, refuses newly merged/zero-area geometry before slicing, and checks requested 3MF
repair evidence. DFAM support reach now uses the same Euclidean tool shape as its clearance
erosion; trapped-volume status is per cluster/cavity, and tangent raw-mesh bores remain
recognisable. Existing narrow/sealed-cavity failures remain covered. The manufacturing
union passed 150 tests initially; the one new bore fixture incorrectly included its flared
mouth in the straight-bore assertion, corrected to its actual axial bounds and passed.
All four actual native source parts then sliced with `print_ready=true`, zero slicer
repairs, unchanged source hashes and exact closed consistently wound input meshes.
The resumed Chudware session then regenerated the actual four-job delivery packet through
native document slicing and independent G-code verification. Parent acceptance checked all
28 output hashes, all five frozen source identities, all delivered/input STL topologies and
zero repair counters in each 3MF. All four jobs are ready with recorded warnings; no physical
fit/strength/servo-duty claim or media write. Final source audits and generated-manual drift
checks pass; the live shared daemon was left untouched.

## 2026-10-12 — AutoCAD drawings: construction curves stay curves beside 3D content

Opening `SteelTubeWidget.dwg` showed a lid on an open tube: the DXF lane turned every closed
2D loop into a planar face, so the AutoCAD edge CIRCLEs of the tube (and its revolve profile)
imported as filled discs over the opening. `read_dxf` (`core/doc/formats.cpp`) now keeps all
curves as wires when the drawing has 3D content (ACIS bodies, meshes or ACIS entities); purely
2D drawings keep the closed-loop profile faces. Export writes loose faces as one-face ACIS BODY
sheets when the document has solids/shells, so they still re-import as faces. The ACIS surface
itself was already right (outer wall, bottom, rim annulus, inner wall; open top). Test:
`test_import_roundtrip` cube/disc/circle round trip for DXF and DWG.

## 2026-10-12 — Fillet joins two bodies that share only an edge

A ring resting on a tube rim shares one edge with four faces (two per solid); `add` keeps both
solids in one result, but BRepFilletAPI needs two faces per edge, so a fillet there failed as
"too large" and the part stayed two physical solids. `geom::fillet_edges` (explicit edges,
`core/geom/dress.cpp`) now treats an edge whose faces span two solids as a contact edge. It
classifies the four sectors around the edge midpoint. Each empty sector under 180° gets the
exact rolling-ball cross-section, swept by a prism (line) or a revolution (circle). The sweep
is fused in with UnifySameDomain; both histories are recorded. Other picks are mapped onto the
joined shape through those histories, then blended as before. The fillet is exact only when the
faces run straight from the edge, so the code checks five stations along the edge. It refuses
curved faces or an oversize radius by name (`fillet_edges(..., why)`, surfaced by
`document.cpp`). An earlier prototype used "fill the other corner, then BRepFilletAPI" and was
not robust at the edge ends. Variable fillets, chamfers and the dir/all-edges overloads do not
join. Cache epoch `geomcache/15`; contracts `doc_schema.md` §"Fillet on an edge shared by two
bodies", `stepplus.md` §16. Test: `test_document` "a fillet on an edge shared by two bodies
joins them into one solid".

## 2026-10-12 — STL export: folded BRepMesh fins at vanishing-fillet poles

The 6-axis `base` and `turntable` (sound, single solids) refused STL/slicing with "not a closed
consistently oriented source solid". Cause: a fillet that runs out where its two support faces
become tangent is a rational B-spline face with a degenerate edge (pole). BRepMesh inserts a
node column toward the pole and links BOTH UV ends of the degenerate edge to it, so each
column edge X-Y carries `(P1,X,Y)` and `(P2,Y,X)`: in 3D one facet plus its reverse (a
double-covered spike), and pole edges are used 4 times. `stl_face_facets`
(`core/doc/formats.cpp`) now omits such pairs, identified by topology only (one corner each on
the same degenerate-edge polygon, same exact 3D point, same UV edge, opposite winding). The
pair's chain is zero, so this welds and heals nothing; nodes never move and the validator
still requires every serialized edge twice, once each way. The writer and
`validate_stl_solid` share one per-face facet list. Test: `test_formats_registry` "STL exports
a vanishing fillet whose pole BRepMesh folds…" (inside-corner R2/R3 floor fillets, R0.5 on
their crossing; it requires the raw fold first).

## 2026-10-12 — Rebuilt build state is saved back into the file; Explorer redesign

Drag-drop, Explorer and "Open Part" from an assembly all reach `doc_open` through
`st.requestOpenPath`; the full re-resolve came from every file saved before the latest
engine-key bump (`chudware-geomcache/15`) missing its cache, and nothing ever rewrote it.
`doc_open` now writes the build it just produced back into the file
(`write_back_build_state`, `core/doc/session.cpp`; reported as `cache.writeback`), and an
assembly load of a stale self-contained component does the same for the component
(`compile_component_with_build_state`, `AsmComponentStatus.build_state`). The write goes
through `stepplus_write_build_state` (`core/doc/stepplus.cpp`): it copies every non-build-state
ZIP member as its compressed stream and replaces only `cache.*`/`geometry.step`, refusing on a
changed size+mtime stamp or document hash. `persist_build_state` is the extracted `doc_save`
cache builder shared by both. Assembly fingerprints carry per-component `doc_hash` so a
component rewrite that keeps its document no longer invalidates the assembly display; AI-VC
`file_fingerprint` adds `authored_sha256` so rollback preflight ignores derived members.
Measured on the six-axis arm: assembly cold open 35 s → 0.24 s on the next fresh process; a
lazy full assembly rebuild after a cache-hit open 1.4 s. Contract: `contracts/stepplus.md` §17;
tests: `test_appearance` "stale cache saves the build", "never replaces a file that changed",
"rebuilt component saves its build"; `test_ai_vc::test_rollback_preflight_ignores_rewritten_build_state`.
Open toast says "build saved to the file" (DocMirror `lastOpenedWriteback`). Same session:
`host/src/ui/file_explorer.cpp` rewritten — tree is the primary surface (no root node, one
header line + branch chip), recursive filter (Ctrl+F), compact toolbar (new folder, refresh,
collapse all, reveal active doc), natural sort, right-aligned change/rev badges, open-document
markers, keyboard navigation, collapsed Recent/Changes strips at the bottom; shot hooks
`CHUDWARE_EXPLORER_SHOT_{EXPAND,SELECT,QUERY,STRIPS}` under `CHUDWARE_SHOT=explorer`.
Deferred: no `collapse_all` icon exists yet (Collapse all uses `body_folder`).

## 2026-10-12 — c259: unify corrupted cached checkpoints; agent auto-wrote the user's open file

The in-app agent (6-axis `base_revD`, "slip-in" slot) spent its turn bisecting phantom regen
failures. Root cause: `ShapeUpgrade_UnifySameDomain` edits its input's edges in place even in
safe-input mode (temporary pcurves, `BRep_Builder::Range`), and a NonDestructive boolean shares
every untouched TShape with its argument, i.e. a cached compile checkpoint. A full-height box cut
came back as 3 shells (+44 cm^3, BRepCheck-invalid, `ok:true`), and deleting it restored a
corrupted prefix (101 895 mm^3 instead of 92 847, invalid). `_project_save` then wrote that state,
with `cache.ckpt`, into the user's open `base.step+` after every batch and cleared its dirty
flag. Fixes: `geom::unify_same_domain` and the `dress.cpp` joining-fillet unify run on a
`BRepBuilderAPI_Copy`, with the copy's history recorded. With this fix the same cut is valid
(87 828 mm^3) and delete restores 92 847. A subtractive feature whose exact volume grows fails by
name (`require_cut_removed_material`, `core/doc/document.cpp`, all cut sites). Cache epoch
`chudware-geomcache/16` (`contracts/stepplus.md` §18) forces corrupted saved checkpoints to
rebuild. `_project_save` skips `origin_kind=gui` docs. A run of `delete_feature` ops applies
dependents first, so `[sketch, its extrude]` deletes in either order. `set_rollback` refuses keys
other than `after`, and the Python normalizer maps `id`/`feature_id`/`feature`/`target` to
`after`. Tests: `test_document` "a delete batch naming a sketch before its extrude deletes both";
`test_project_save::test_add_features_never_writes_the_file_of_a_doc_the_user_opened`;
`test_add_features_shape::test_set_rollback_reads_a_target_spelling_as_after`. There is no
minimal synthetic repro of the unify mutation: a plain revolve+bore+tunnel does not trigger it.
Verified on copies of the real part, old vs new `libchudware`. Deferred: compile ignores
`rollback_after` (the bar is cosmetic; `rolled_back` is hardcoded false; backburnered by the
owner in favor of git-style PLM); `plm_rollback` returns `ok:true` without touching the live
doc; `undo`/`redo` are advertised to the main chat but always denied for agents; a GUI
sketch/drag-cancel `doc_revert` can discard agent edits made during the session (issue #55).

## 2026-10-12 — Mass uses the material applied to the model

`eval_mass` without a density now weighs each body with its applied material: the
`body_materials` override, else the document material, at its registry density
(`chudware::materials::density`). For assemblies it uses each component body's source-part
material, carried as `AsmBodyOut.body_material_keys` / `LoadedComponent.body_material_keys` through
`load_bodies`; a nested sub-assembly body keeps a key only when its bodies agree. Steel 7.85 applies
only to a body with no material, or with a key that has no registry density (`unresolved_key`).
Mixed totals weight mass, COM and inertia per body (`GProp_GProps::Add(props, density)`). Every
`material` block names its `source`: explicit | body_material | document_material |
component_material | default, with `per_body`/`mixed` at the top when bodies differ.
`measure_part` passes a density only when one is given (`density_g_cm3=`/`material=`). The Mass
Properties panel sends one only for a custom override and displays the density the engine
reports. The BOM panel and the verify battery inherit applied materials. New parts default to
`alu_6061`, so an untouched part now weighs as aluminum. Test: `test_document` "eval_mass weighs each
body with its applied material"; Python pins updated (`test_modeling_repertoire`,
`test_material_resolve`, `test_eval_tool_dialect`).

## 2026-10-12 — Agent edit lock (interim #55) and real-time parallel agent editing

**Lock.** `AppState::docLockedByAgent` (host/src/core/app_state.*) drives the lock.
`engine_events.cpp` stamps `DocTab::lastForeignWriteAt` from `doc.changed`/`doc.opened` with a
non-`gui` `source_kind`. A doc stays locked while any run live at that write continues, or for
`kAgentLockGraceS` (8 s); `last_writer_detached` clears the lock once no run is active. Gated
paths:
- `CommandRouter::authorize` (Mutate commands) and ribbon greying (`setRibbonAgentLockReason`, fed per frame from app.cpp).
- Feature tree mutations and edit entries.
- Sketch, 3D-sketch and PropertyManager entry (`openGuest` now takes `AppState&`).
- Viewport drags, appearance and hide.
- Mass/Sim material, equations, AsmPanels mate/pattern/insert.
- `docUndo`/`docRedo` (`agent_edit_locked`) and app.cpp document keys.

Open sessions on a doc that becomes locked keep-exit; locked cancel paths never `doc_revert`.
`userMidEdit` now also covers the 3D sketch. app.cpp draws a reserved banner with Stop
(`requestStopRunFor`, copilot.h). Shot: `CHUDWARE_SHOT=agentlock` (16 self-checks, including a real
second-client MCP write).

**Parallel agents.**
- `daemon_bridge` opens one hostd connection per calling thread (thread-local, closed on thread exit). Every connection sends a per-process `writer_group`. hostd (`doc_registry`) treats a group as one writer for rw counting, CAS revision, `_foreign_edits` and holds. Doc lifetime stays per connection.
- hostd no longer drops events on a busy connection: it waits 2 s, then disconnects for resync.
- `doc_list`/`unique_doc_name`/`resume_open_doc` serve busy sessions from a per-session summary snapshot (`busy:true`). They no longer wait behind another doc's compile.
- Rejected batches publish only the committed tessellation.
- `core_bridge` retries implicit-CAS `stale_revision` once and `busy_preview` for up to 30 s.
- The current part is per agent (`cad_common.agent_doc_context`, bound in `chudcode_tools.dispatch_host_tool`). A sub-agent's doc-less call targets its own part before the turn doc.

Measured through a private hostd: 4 sub-agents, each building its own part, took 15 s wall
against 11.5 s for one agent alone. All revisions reached a subscribed observer. Two agents
co-editing one part landed 8/8 edits. Tests: `test_hostd_registry` writer-group cases (53 pass);
`test_document` "doc_list and document minting stay responsive…" and "doc_apply_ops rejection
preserves the committed revision AICM"; `test_multiagent_overlap`, `test_current_part`,
`test_chudcode_tools` and `test_daemon_bridge` additions. Pre-existing failure:
`test_multiagent_overlap::test_autosave_skips_unchanged_docs` fails under the suite's private
hostd, because Python autosave is a no-op in daemon mode, and passes with
`CHUDWARE_ENGINE_DAEMON=0`.

Deferred: the real fix for #55 (a `doc_revert` that refuses or merges across foreign revisions).
Two conversations still cannot run turns in parallel (worker lane mutex).

## 2026-10-12 — Concurrent chat turns and same-part editing across chats

**Turns.**
- hostd `WorkerBridge::dispatch` replaces the single agent worker with a pool: the primary plus `agent-turn` extras, up to `CHUDWARE_AGENT_WORKERS`, default 4. Each worker runs one turn at a time.
- Conversation ownership keeps a conversation's live Chudcode runtime in one worker. Moving a conversation first asks its busy owner to `release` it over the control socket.
- A real-chud smoke showed why the release is needed. Without it, a stale runtime's disposal wrote `session_exit` at its old leaf, and the next resume forked the history (turn 3 chained from turn 1).
- Fixed a latent bug: worker children were forked on hostd connection threads, so Linux PDEATHSIG killed every worker when the client that triggered the spawn disconnected. The next turn then paid a respawn and lost its live sessions. Forks now run on one daemon-lifetime thread, as `ChildSupervisor` already does.
- GUI: daemon `agent_admin_turn` leases its own run connection (`engine_backend` pool, at most 8) and takes no client mutex. Control goes through hostd by `run_id`.
- Worker: lane `agent-turn` (no bench reconcile/shutdown). Control sockets are per pid. `ChudcodeSessionHost.release_session`.

**Shared parts.**
- `core_bridge` sends implicit batches with `base_revision: -1`; the engine DocLock serializes them.
- `doc_checkpoint` returns its revision. `doc_revert expect_revision` and `core_bridge.revert_own_batch` stop a rollback from wiping another writer's commit.
- Recon probes delete only their own feature.
- Assembly tools dropped the CAS read they took just before applying.
- `doc_list.other_writer` keeps one chat's end-of-turn save sweep off another chat's in-progress agent doc.

**Evidence.**
- Private hostd with the real Python worker and Chudcode: two chats' turns overlapped for 6.4 s of about 7 s, in two workers (`agent` and `agent-turn`). The follow-up turn reused its worker's live session (2.2 s, no respawn).
- Session-migration smoke: A1, A2 and A3 ran on different workers and form one linear history.
- Two separate agent processes ran `add_features` on one part: 8/8 batches landed interleaved at revisions 1..8, and invalid batches were refused normally.
- Tests:
  - `test_hostd_worker_bridge`: concurrency, same-conversation serialization, release accept/refuse.
  - `test_same_doc_writers`, `test_document` guarded revert, `test_worker_long_run` (online turns overlap, native runs do not).
  - `test_chudcode_host` and `test_chudcode_control` release tests, `test_terminal_save_sweep`.

## 2026-10-12 — Inline pasted images in chat

Image paste now inserts a numbered reference at the composer caret, replacing its selection;
sent bubbles render the picture between the surrounding prose instead of showing attached-image
filename lines plus a trailing thumbnail strip. `inline_image_refs.h`, `composer.cpp`,
`chat_log.cpp` and `image_lightbox.*` share exact path rosters and aspect-preserving previews.
`app_state.h`, `conversations.cpp`, `cards.cpp` and `copilot_internal.h` preserve stable draft
slots, queued attachment snapshots and staged edit/cancel state. Sending/steering omits deleted
references, orders images by first reference and compacts numbering; legacy records use only
their recorded paths, never a basename lookup. `CHUDWARE_SHOT=chat_inline` in `scenarios.cpp`
uses actual clipboard/keyboard/mouse input: 15 checks passed for caret/selection, deletion,
image-only payloads, reorder, drafts/history and edit/cancel/viewer behavior, with zero ImGui ID
conflicts. Portrait/landscape pasted bytes matched the fixtures; composer, transcript, legacy
and viewer captures were inspected. Desktop build and theme/color/glyph/dupe/scale lints passed.
The smoke uses private HOME/XDG paths, in-process engine and isolated Wayland/X11 clipboard
environment; no live provider turn or browser/WASM runtime was exercised.

## 2026-10-13 — Concurrent-agent connection lifetime and responsive chat

- `daemon_bridge.TurnConnections`, `chudcode_host.py` and `chudcode_tools.py` retain CAD
  sockets for the owning turn instead of closing them with each transient RPC tool thread.
  Concurrent calls lease independent sockets; retirement never interrupts an in-flight
  mutation. Genuine disconnect/lock-release notifications remain visible.
- `general_agent.yaml` now describes a shared project registry with explicit part IDs and
  agreed feature scopes, not mandatory private parts per sub-agent. Existing atomic,
  sequential same-part batch application and geometry validation are unchanged.
- `conversations.cpp`, `chat_message_snapshot.h` and `chat_persistence.h` reuse exact
  unchanged snapshots and move routine desktop durable writes to one ordered, coalescing
  writer. Explicit saves/reloads and shutdown remain barriers; failures retain retry state,
  indexes follow transcript durability, and deletions cannot be resurrected by older saves.
- `activity_timeline.cpp` and `chat_log.cpp` cache parsed tool rows and stable heights,
  window settled offscreen rows/group members, and invalidate for content/layout changes.
- `composer.cpp` now owns and joins its control thread. The old detached thread was
  reproduced hanging in `pthread_cond_destroy` at process exit. GUI shutdown drains accepted
  answer/steer jobs and their results before final persistence and worker/AppState teardown.

Evidence: the untouched live session spent 4.99 s of an 8 s trace in 1,658 fsync calls,
rewriting 119 distinct session/index files. An isolated production-object smoke with 118
sessions and 400 tool rows rewrote only the edited transcript: median UI snapshot 5.47 ms,
median timeline draw 0.231 ms; final text persisted and normal shutdown completed. Storage
trace placed 256 transcript/index/draft fsync calls on the writer thread; the one main-thread
fsync was outside the autosave series (the harness explicitly changes a saved UI preference).
These are component timings, not whole-application FPS claims.

Verification: desktop build and changed-Copilot GUI lint passed; 259 Python tests passed,
one skipped, plus 19 subtests. Private native-engine smokes ran 3 agents/12 edits on separate
parts with zero mid-turn disconnects and proper turn-end release, then 3 agents/12 sequential
revisions on one shared part with all features preserved and explicit IDs overriding each
agent's unrelated current-part slot. Failure injection exercised file-fsync, rename and
directory-fsync retry, unchanged writes, coalesced deletion and shutdown drain. Real GUI
`chat_evidence` replay (182 events), `projectchat` and `steerstatus` scenarios passed; captures
were inspected. Live GUI, daemon and agent runs were not restarted or modified. Desktop
runtime only; no provider/network or browser/WASM performance claim.

## 2026-10-13 — ESPCAM message receipts and question submission

ESPCAM c261 proved that a question consumed a typed reply while its user bubble said
“Not delivered”; the next form failed with “worker control endpoint unavailable for active
run.” `core/hostd/worker_bridge.cpp` and `services/worker/worker_client.*` now retain
generation-owned control credentials after individual failures and keep socket I/O outside
hostd's control-state lock. `services/agent/chudcode_{host,questions,events}.py` correlates
text-only question replies to authoritative delivery events, retains unresolved correlations
after send failure, and requires matching text/images for image delivery. Copilot
`feed.cpp`, `composer.cpp`, `conversations.cpp`, `copilot.h` and the state-header contract
make confirmed delivery monotonic, remove blanket Stop/unknown-frame delivery guesses, and
recover only exact unique image-free replies within the original saved turn. Regression
coverage lives in the three Chudcode suites, native worker control/long-run tests, hostd
worker bridge tests and the existing `steerstatus` scenario. Verification: 128 Python
delivery/question/event tests passed (one skipped), all 25 hostd tests and both native
control targets passed; nine GUI scenario checks passed and receipt/question captures were
inspected. A real provider→hostd→worker turn survived an oversized control refusal, accepted
a typed PETG reply with a delivery event, accepted a subsequent Ender 3 form answer and
reported both correctly. GUI and daemon built and staged; the initial unrestricted GUI
build was killed (exit 137), then the bounded `-j 2` build passed. All five GUI lint rules
passed. Contracts and the integration skill document the endpoint lifetime and evidence
rules. Recovery preserved the original GUI/runtime/child histories, gracefully retired
only c261's already-stopped stranded runtime, and its runtime session doctor reported
healthy with zero findings. No JSONL surgery or forced daemon shutdown.
The rebuilt desktop reopened c261 in ESPCAM and submitted exactly one continuation through
its composer, preserving the original session ID/file and Claude Opus 5.5 / High settings.
The old Command-strip message now persists as delivered. The live GUI showed the resumed
model incorporating the saved under-eave/PETG/Ender 3 answers; the application remains open.

## Copilot responsive header and question layouts

`panel.cpp` and the header helpers in `cards.cpp` now use compact flat navigation with measured
widths, one row when everything fits and a second row for Sessions/New chat otherwise. Tasks
has a name rather than an idle `0/0`, and header glyphs use the existing DPI-crisp SDF path.
Question choices wrap within full-row hit areas; Other has its own full-width input. Unit and
file controls stack by available width, geometry labels/actions wrap/stack, and pending/resolved
headings wrap. `chat_log.cpp`/`copilot.h`, `panel.cpp`, `ai_workspace.cpp` and `agent_dag.cpp`
replace the floating latest-reply overlay with one reserved footer and budget short windows
around the composer; the trace tree also respects the remaining timeline/footer budget.
`host/CMakeLists.txt` enables semantic question-item annotations. The `copilot_ui` scenario in
`scenarios.cpp/.h` exercised real clicks and text entry in five isolated configurations:
200/340/520dp panes, 1x/1.5x/2x DPI, light/dark themes and the AI workspace. All 156 checks
and 58 layout audits passed, with zero ID conflicts or horizontal overflows; captures were
inspected. Build and six GUI lint rules passed. Matrix runs need distinct session directories
and displays: the default screenshot store is shared by scenario name and cleared on startup.
Answer transport remains unchanged; live provider dispatch and browser/WASM runtime were not
exercised. Existing user GUI/daemon sessions were not stopped or restarted by this work.

## 2026-10-13 — Flat printer-media delivery and delete-only retirement

`services/manufacturing/print_media.py` now publishes stable raw part filenames at the drive root by default, or in one top-level project folder with `layout="project"`. Hidden root metadata replaces the `PRINT/<PROJECT>` and `CHUDWARE` trees; existing managed sets migrate even when their job bytes are unchanged. Deprecated managed G-code, reviewed legacy printables and owned obsolete archives are permanently deleted after current files and metadata pass hash readback. No new toolpath ZIP is created on media or host; temporary staging retains rollback capability only until verified commit, including crash recovery. The MCP wrapper, human CLI, agent printing guidance and development skill use this contract; filesystem regressions cover layout, deletion, migration, collisions, rollback and recovery. Runtime proof used stock PrusaSlicer on the desktop 6-axis `base.step+` and `forearm_lower.step+`, Ender-3 v1/PLA/direct-drive settings and zero brim. Both passed print-readiness and zero-repair checks; the forearm required measured +Y orientation and snug/sheath supports and retains support/adhesion warnings. A real MCP root-to-project smoke left no ZIPs; actual `PRO GAMER D` readback proved root-level `base.gcode` and `forearm_lower.gcode`, deleted 22 obsolete robot printables and the old robot archive, removed the obsolete managed trees, and preserved/reported unrelated OS-trash files. Source model hashes stayed unchanged during slicing; no physical print/fit test or user-session restart.

Final scoped verification: 142 publishing, real-engine/slicer MCP and agent-mode tests passed; actual MCP root/project delivery, physical-drive SHA-256 readback and the human inventory CLI were exercised. Throwaway slicing/pose/delivery scripts were removed.


## 2026-10-13 — Security P0 browser containment and identity authority

`Webservice/gateway/engine_bridge.py` now accepts one strictly parsed/canonical request per frame under a version-1 default-deny contract (`hello`/`ping` only), including administrators; it hides global recovery metadata, drops global events/unsolicited responses, bounds pending requests, and removes raw forwarding, path-key heuristics and credential-injection code. Browser CAD/AI execution is deliberately unavailable pending tenant-owned sinks and isolated workers; native desktop/MCP transports are unchanged. `authn.py`/`oauth.py` separate provider ownership proof from password verification, invalidate preregistration capabilities, preserve independently verified passwords, and remove password login on OAuth-only admin bootstrap. Account-row locking covers session issuance and credential transitions, including `portal_api.py`; `accounts.py` revokes sessions transactionally on suspension/denial, and `billing.py` no longer changes operator account status. Changed contract tests remove obsolete forwarding expectations; disposable-PostgreSQL regressions exercise callback, dormant-session, billing, rollback and concurrency behavior. The gateway suite passed 814 tests with 29 skips; separate actual production-mode ASGI/native-hostd smoke evidence is retained in `reports/security/2026-10-12/p0-remediation-evidence.json`, alongside unchanged vulnerable-state evidence. No production service, user daemon, real credential, provider API, signing operation or physical device was touched. External release remains blocked by staging/signing/ownership/isolation/parser/dependency/operational gates; historical compromise requires operator review and revocation, not merely deployment of this patch.


## 2026-10-13 — Tenant cells, release authenticity and audit hardening wave

Browser CAD/AI moves from the P0 `hello`/`ping` containment to OS-isolated tenant cells (contract in `contracts/worker_rpc.md` cell addendum, `contracts/cell_methods.json`, `contracts/stepplus.md` §19). Gateway (`cells.py`, `engine_bridge.py`, migration 0022) resolves `(account_uid, workspace)` → one socket-activated `chudware-cell@<hex>` (`Webservice/deploy/cell/`: launcher, nested `chudware-actor-sandbox`, CONNECT egress broker on 127.0.0.1:18080, `chudware-worker` runpy bootstrap) and sends a principal hello; hostd `--cell` adopts fd 3, enforces per-method rights, per-actor lanes/listeners, owner-checked controls (managers: cancel only via `ctx.control_delegate`), revocation, attached-only events, and refuses escaping file/embedded references. Workers bind envelope ctx, mint opaque handles, return typed `not_found`, keep BYOK identity as (workspace, actor, provider) and never mutate process env per turn. Cache provenance HMAC (`cache.provenance`) keeps verified warm reopen at zero features executed; foreign caches restore immediately as `cache_trust:"unverified"` and refresh in the background. Release side: gateway authenticates payload/SBOM/provenance/platform-test signatures and identities from private snapshots at CI/manual publish and promotion, Windows/macOS stable promotion requires an owner-authorized native publisher byproduct, rollback restores only operator-authorized (root-owned record) private snapshots (`deployment_release.py`, manifest schema 7 with `cell_runtime_sha256`), Chudcode verify/stage execute private snapshots, managed Git sources use `--no-replace-objects`. Hardening: central `services/net/safe_http.py`, PLM git/zip/manifest/relay fixes, SPICE name remapping + directive grammar, PDF escaping, firmware without semihosting and with Renode `@/abs` encoding, bounded manufacturing state, parser `.inc` regressions under ASan/UBSan, identity/commerce fixes (migration 0023, mandatory `CHUDWARE_MAIL_CONTEXT_ENCRYPTION_KEY`), dependency pins with `reports/security/2026-10-12/dependency-dispositions.md`. Gotchas: systemd `PrivateNetwork=yes` on user units implies a user namespace that maps root-owned ancestors to nobody, so ownership-checking tests (firmware storage, Box mirror) must not run under it; AF_UNIX paths >107 bytes break deep pytest tmp roots; bwrap has no `--preserve-fds`. Deferred/blocked: authenticated runtime wheel lock (cell payload staging fails closed), root-provisioned disk quotas, installed-unit/cgroup evidence on the production host, provider broker for pooled/subscription web turns, Windows/macOS native verification runs.

## 2026-10-13 — Operational recovery and mixed-suite lifetime follow-up

`Webservice/deploy/backup_state.py` now streams actual PostgreSQL dumps into age, archives configured data/workspace/bucket/cell roots with mount manifests and credential separation, scopes retention to owned environment/family artifacts, and restores only into explicit isolated targets without retained plaintext dumps or broad sudo fallback. `backup.sh`, `update.sh`, the backup unit and RUNBOOK agree; a disposable real pg_dump→age→pg_restore drill recovered all four roots and cached-part bytes while leaving source rows unchanged. `terminal.py` revalidates immediately before outbound PTY/session/exit payloads and closes silent revoked endpoints within one second without killing another attachment; entitlement provisioning uses bounded memory-only stdin and encrypted-only atomic output. Mixed-suite fixtures now isolate HOME/XDG before collection, preserve dynamic path attributes, stop selecting the stale slim engine at collection, and give spawned daemons private state roots. The broad sweep exposed a genuine `doc_view_metrics` SIGABRT: partial vector-of-thread construction unwound joinable threads. `session.cpp` now honors the solver governor and cancels/joins safely, returning `metrics_failed` without partial success; the constrained native consumer then recovers exactly. Python 3.11 wheel authentication also stopped discarding authenticated vendored `.dist-info/INSTALLER` bytes as if they were top-level pip-generated metadata; real offline-install/tamper regressions pass on 3.11 and 3.14. Durable scopes and remaining release prerequisites are in `reports/security/2026-10-12/release-hardening-remediation-evidence.json`; production/CI/provider qualification and historical incident closure are not inferred from these source/scratch results.

## 2026-10-13 — Release qualification tooling and grouped-writer continuity

`packaging/qualify_native_release.py` adds read-only prerequisite inspection and genuine-target-OS artifact qualification using the existing publisher policy, retained CTest records, and evidence/signing-request formats; it never signs or promotes. `platform_publishers.py` now bounds native verifier subprocess output and duration. Central qualification/finalizer/evidence/signing coverage passes 183 cases with 25 native-only skips; a real Linux CLI invocation refuses an unsigned Windows fixture without creating an output directory. Real native host/publisher qualification remains outstanding. The MCP animation metadata path no longer re-handshakes away `writer_group`; recovery refreshes metadata through `DaemonClient.refresh_server_info()`. A 781-case whole-collection focused run and separate private-daemon smoke preserve own-group edits while reporting genuine outsider edits. The complete prior-wave regression sweep remains separate.

## 2026-10-13 — Offline prerequisite tooling and real admin promotion surface

Runtime preparation now distinguishes unapproved wheel observations from independently reviewed exact-target inputs; the explicit isolated SDK builder consumes reviewed source/base/backend identities, never acting as a provisioning fallback. Central regression: 64 passed; wheelhouse validation separately passes 30 cases, including a real file growing during bounded hashing. Two actual offline builds from pinned private SDK commit `2eaba2f4` produced identical `8ae5f2a8…` wheel hashes, still unapproved for release. Read-only CI evidence collection passes 14 behavioral cases; its actual conformant synthetic-export CLI still exits 1 with three authority gates unavailable. P3 host evidence collection passes 65 cases and its actual no-packet CLI reports all eight gates unavailable. These tools do not establish deployed authority. A real isolated Chromium session against the disposable staging gateway visually verified native publisher-required rows without promotion controls and exercised click/confirmation to publish only the inert local Linux stable fixture. No genuine native signing, production release or CAD/provider qualification is implied. Full audit reconciliation additionally exposed desktop shell/sign-out/thumbnail/trace defects and finding-level proof gaps; these require their own exercised disposition rather than prior aggregate suite counts.

## 2026-10-14 — Quota authority and desktop residual proof

Cell admission now requires a root-protected complete owner quota plan/proof over workspace, state and bucket roots, with enforced XFS/ext4 project limits and boot/mount/device/inode binding. It refuses unsafe aliases and migrations and mounts checked descriptors. Quota/runtime regression passed 71 cases. A separate actual XFS loop-image ioctl/write probe showed initial-namespace owners can clear inheritance, while the nested cell user namespace refuses that mutation and retains the hard byte limit; the namespace is part of the security boundary, not optional packaging.

Native desktop compilation and 37 CTests passed after shell-free external opening, generation-fenced account completion, bounded thumbnail inflation, bounded opt-in CEF trace sinks and checked AICM counts. Actual isolated native screenshots confirmed sign-out clears both live and stored credentials, preserves read-only access and reserves a nonoverlapping banner row; settings and the canned capture surface rendered with zero ID conflicts. This build had CEF disabled and preceded the Windows named-pipe cutover, so neither the live CEF callback nor Windows OS isolation is certified by it. The broad Python sweep stopped after 8,698 passes and five newly exposed fixture-isolation failures; source corrections and later broker/cell changes still require final verification.

## 2026-10-14 — Actual residual-path and dependency observations

The native, desktop and WebAssembly build chain succeeded. Direct C ABI smoke leaves drawings and output files unchanged across 20 Ask/Plan/plan-only requests on both native agent entrypoints, while eight Agent/default controls still create real drawings or matching G-code. After correcting the fixture's separate design/Ask mock descriptors, 12 explicit-approved-plan scenarios on both native entrypoints completed design execution and created the expected editable 30×20×10 box instead of entering the drawing/CAM shortcuts. The native activity export reported one dirty document after each edit and zero after closing all documents. CAM refuses seven hostile archive forms without consuming session IDs or damaging an existing job, then preserves a valid human edit through save/reopen; CAD graph construction admits the valid recipe and rejects oversized document metadata. Actual pathological `fs_grep` returns a typed deadline refusal in about two seconds without blocking an independent read, and a normal search still returns its expected line.

Actual OSV queries on 2026-10-14 returned no advisory matches or pagination for 54 exact public declaration versions and 171 unique public versions installed in the disposable upgraded gateway environment. All 17 gateway pins match installed versions under PEP 440 normalization. This is neither private/runtime-image approval nor native, browser, OS or advisory-exploit qualification. The original vulnerable audit JSON remains unchanged. The provider-cutover gateway sweep still exposed lifecycle/fixture failures after 1,303 passes; those corrective edits and the new native/worker/gateway maintenance census require an integrated gate before release.

## 2026-10-14 — Bounded Python import and verified acquisition paths

The shared Python DXF traversal now budgets nested INSERT/MINSERT work before expansion. An actual nested 2×2 fixture imports four edges, while a 32767×32767 array is refused before materializing instances. The slicer keeps its existing subprocess deadline after both output pipes close; an actual child that closes its pipes then sleeps returns `slicer_timeout` and is reaped. These are operation bounds, not agent lifetime limits.

Real namespace-only DNS/TLS acquisition exercised the unmodified production resolver and pinned-IP connector against private fixture servers. Trusted matching certificates succeed; wrong-hostname and untrusted-CA certificates fail. Redirects retain the new hostname's SNI/verification, and a private-address redirect is refused. Static CAD-page acquisition succeeds; a JavaScript-only shell returns `no_cad_link` without invoking the removed unrestricted rendered-browser fallback. No shared browser profile or live provider was used.

An actual local Git flow rejects a credentialed PLM link without committing, opens legacy unsafe metadata with redacted diagnostics, and refuses unapproved sync. Explicit clean-remote approval appends a repair without changing historical bytes; a subsequent checkpoint and real bare-repository sync succeed. This is forward repair, not credential revocation or historical incident closure.

## 2026-10-14 — Integrated native and desktop regression gate

The current native, WebAssembly, desktop and CEF builds succeed. The CEF C++20 build exposed a `std::u8string`/`std::string` conversion in trace-directory setup; retaining the original UTF-8 parent path fixes that incompatibility without re-encoding. Central CTest passed all 136 native and 37 desktop executables, including approved-mode handling and native cache/dirty-document census coverage. These Linux results do not certify Windows IPC, macOS signing, live firmware tools, or the still-pending integrated Python/gateway/cell drain proof.

The real CEF Parts Library browser, not the screenshot-only helper, executed a local page's console callbacks inside an owned network/PID namespace. Default tracing created no trace file and did not echo the page's private sentinel to stderr. Explicit opt-in produced metadata-only console events in a mode-0600 file. After seeding that file to 1 MiB minus 128 bytes, two real reloads reached 1,048,575 bytes and then stayed there while the page continued completing. Both actual surfaces were visually inspected; all proof processes and the private daemon were isolated from user sessions.

## 2026-10-14 — Actual portable SolidWorks and printer-lane paths

The portable SolidWorks importer decoded a real compressed tetrahedron fixture to four triangles and 166.6666666667 mm³, and preserved a 20 mm assembly-instance translation. A raw-deflate stream with 4 MiB of output but a one-byte declared size was rejected at the inflater boundary; peak traced Python allocation during that refusal was 53,185 bytes (not a native-allocation measurement). The actual native manufacturing lane also exchanged requests with the real Python Moonraker adapter against an isolated loopback fixture: an IP-literal endpoint with no `address_class` connected, returned two complete telemetry snapshots, and retained the `transport` authority ceiling. No physical device was operated.

## 2026-10-14 — Named remediation evidence and honest acceptance closeout

`reports/security/2026-10-12/release-hardening-remediation-evidence.json` now adds 104 finding/companion mappings and 1,044 exact class/test/status records from the complete, untruncated parent XML index plus completed newer quota and native-final artifacts, with original numbered and phase acceptance requirements reconciled separately. The original vulnerable audit/reviews, dependency and secret-scan reports remain unchanged. Native executable passes are distinguished from registered source cases, compiled-out platform branches and optional live-tool legs. Actual wasm32 `loadAicmBytes` rejects wrapped BODY/envelope/PTRDIFF inputs and decodes the real golden; actual capture trickle closes in 0.201000345 seconds without partial files, followed by authenticated PNG HTTP 200. Latest completed full Python remains 13,074 passed with one historical capture-fixture expectation failure; the corrected module and focused follow-ups do not make the still-running broad rerun green. Quota `results-j.xml` has 99 passed/one empty-profiler-report fixture assertion failure: both actual SDK census cases and provider revocation pass, but corrected viewer/editor/manager admission and the final combined quota wave remain unverified. The newest build, actual delayed account HTTP/GUI completion, chat artifact callbacks and browser picker/Copilot edit/save/reopen are pending. `services/reveng/test_tenancy.py` adds exported/imported bundle rejection, CRC/content and desktop custom-destination regressions, while `Webservice/deploy/tests/test_reveng_cell_boundary.py::test_num4_owned_bundle_and_foreign_destination_boundary` adds the real personal/shared-team gateway/native export consumer scenario; neither new proof was executed by its worker, and actor-private `/tmp` plus explicit desktop destinations remain supported. Exact parent todo recommendations identify local closure candidates without changing the todo or implying release readiness; native Windows/macOS, reviewed complete private runtime inputs, genuine CI/installed-host authority, historical rotation/exposure, off-host recovery and live mail/DNS qualification remain separate prerequisites. No checks, builds, formatting, live services, production/customer/device operations or signing were performed by this evidence worker.

## 2026-10-14 — Epoch retirement and real delayed-account completion

Native authority transitions now serialize publication with retirement: control hello does not mutate actor authority, newer GUI epochs retire only older actor/workspace generations, and repeated updates cannot close fresh sessions or kill fresh workers. Real two-tenant provider revocation/reconnect and delayed-control replay pass. The embedded worker regression harness now supplies the required workspace and epoch cutoff; all 261 native-cell/policy regressions pass after the broad run exposed its two stale callers.

The production account panel was exercised with a real device-signed refresh held by an owned loopback HTTP fixture until sign-out. After the released 503 and actual worker reaping, normal account tick preserves signed-out/read-only state and banner, erased session credentials, device identity, security events and monotonic refresh-sequence bytes. The harness inherits the original network-namespace descriptor to verify isolation without reading privileged `/proc/1` state, and preserves the provisioned Python interpreter path instead of resolving away its virtual environment. The inspected screenshot keeps the notice above, not over, Copilot. This proves stale error completion through the production path; stale successful refresh/login candidates remain covered by the existing mailbox regressions, without inventing entitlement authority.

## 2026-10-14 — Real tenant boundary closeout and deployment shortcut retirement

The complete disposable XFS/cell gate passes all 104 cases. Real personal/shared-team SDK turns survive another actor's refused legacy credential mutations and resume the same session. Viewer/editor/manager admission and actual profiler state transitions pass without admitting dangerous operations. NUM-4 now exercises real native actor mounts and Python consumers: valid bundles preserve the owned session/mesh across workspace, actor/home, user-selected symlink and private-tmp destinations; malformed/absent/foreign handles cannot mutate destinations, and outside sentinels remain unchanged. Exact archive-member-set assertions were removed rather than repinned when a legitimate internal lock file appeared; CRC, manifest, mesh and foreign-data invariants remain.

The unsafe admin rebuild/redeploy routes, buttons and shell shortcuts are removed. The System panel is a read-only operator deployment monitor, not a second build/restart authority. The gateway/deployment gate passes 1,443 cases with 63 explicit skips; quota-required cases pass in the separate real-cell run. Qualified update/rollback retain exact-commit, dirty-tree, maintenance-census and independent first-cutover recovery requirements. No live service was restarted.

Runbook and canonical integration guidance now distinguish optional local/native daemon ceilings from tenant-cell ceilings, Windows private named pipes from obsolete loopback control, and actor-scoped catalog helpers from desktop's shared transient client. The release guide explicitly states that artifact signatures do not bind `min_supported`; no floor increase or signing authority is authorized. All 145 redacted historical-secret detections retain owner-review-required disposition: pathnames, fixture usage and deletion do not prove false positivity or revocation. Installed-host, CI/image, native-platform, private-runtime, rotation/exposure and off-host recovery authority remain external prerequisites.

## 2026-10-14 — Browser edit/save/reopen and actual desktop consumer proof

PropertyManager now reads canonical numeric dimensions as well as expression-bearing values. DocMirror notifications distinguish an actual same-document revision transition from unchanged refresh/activation/snapshot traffic, so read-only Ask no longer dirties the GUI. The owned native scenario passes 42 checks across 40/30/20 dimensions, inch conversion, drag, independent native/local edits, undo/redo, ordinary save, warm reopen and expression preservation. Linux, WASM and CEF builds complete.

Actual Chromium exercises the tenant canvas and scoped provider path: Claude 4.1 Opus Ask streams the local synthetic response while the part remains clean; human editing produces 50/30/20, saves, closes and reopens with verified cached state and zero feature execution. Actual admin UI permits only the qualified Linux fixture promotion, retains Windows/macOS publisher refusal, and exposes read-only deployment monitoring; retired rebuild/redeploy POSTs return 404. Owned browser and gateway/cell fixtures are stopped without touching user sessions. Synthetic identities and development authentication are not production qualification.

The real CEF download CLI accepts exact benign bytes and refuses both declared and streamed 256 MiB responses under its 200 MiB ceiling, preserving existing destination bytes. Each request uses a fresh process/cache; sender progress is not a memory measurement or same-process recovery proof. The final chat artifact open/reveal callbacks compile against the shell-free APIs already exercised by the real Linux argv recorder; native Windows/macOS launch behavior is not attested.

## 2026-10-14 — Final native gate and deterministic TCP teardown coverage

The final Linux gate passes 136 core and 37 desktop executables. One earlier round-trip run returned an error before payload, but its socket reason was not captured; 1,000 focused and 100 complete instrumented pre-change runs did not reproduce it. A teardown scheduling race is an inference, not an established production defect. The fixture now waits for the client's receipt acknowledgment instead of closing after 50 ms, and aggregates TCP stream fragments instead of requiring a single event. A separate deterministic case closes the peer before polling, verifies an expired poll leaves data untouched, then requires the complete final payload before EOF and refuses subsequent writes. Half-close/reset coverage remains. All 13 transport cases and 122 assertions pass in the final gate, as do 100 complete post-change transport runs; no production error handling was suppressed.

## 2026-10-14 — Completed local regression and release prerequisite ledger

The final full `services scripts tools` sweep completes with 13,087 passed, 194 skipped, one expected failure and 206 passed subtests in 2,091.72 seconds. All 29 focused reverse-engineering tenancy/export cases also pass. Together with the completed 1,443-case gateway/deployment gate, 104-case real quota/cell gate, 136 core and 37 desktop executables, and actual browser/native/CEF/account consumer observations, this closes the reachable local verification work. Skips and synthetic trust never count as unavailable platform or production proof.

The evidence ledger reconciles all 26 original numbered/phase acceptance clauses and 104 finding/companion mappings while preserving historical failures. Release qualification remains blocked on genuine native-platform/publisher, approved private-runtime/bootstrap, CI/image/secret migration, installed host/edge, live mail/provider, historical credential/consumer/exposure and off-host recovery authority. All 145 historical secret detections remain owner-review-required. Owned fixtures and temporary smoke drivers are cleaned up; no user session, customer system, device, signing service or CI runner was operated.

## 2026-10-15 — Native Windows IPC, workers and cached document persistence

Genuine Windows 11/MSVC execution exposed daemon pipe polling latency and an indefinite read that shutdown could not wake. Separate overlapped read/write events, sticky cancellation and cancel-and-drain now preserve partial frames and enforce the existing 64 MiB ceiling. Native regression covers duplex traffic, blocked read/write cancellation and boundary-sized frames. Three quiet matched native-VM pairs measured median per-run daemon round trips of 33.109495 ms before versus 0.077285 ms after, control round trips of 4.80254 versus 0.733935 ms, and control idle CPU of 906.25 versus 15.625 ms per eight seconds. These are transport measurements, not GPU or desktop-rendering benchmarks; the original single-pair evidence remains retained.

Native workers use restricted inherited handles and suspended creation followed by kill-on-close Job assignment. Detached console creation avoids the hidden `conhost.exe` child that `CREATE_NO_WINDOW` still allocates; an actual native probe demonstrated that helper preventing an idle census. Control requests use event waits and absolute deadlines; response disconnect waits for the remaining request trailer to drain rather than losing a successful reply. Private IPC objects, firmware directories and cache/save stages explicitly use TokenUser ownership, including elevated launches. Python discovery reuses typed SID lookup, and Python activity now has a real Toolhelp/FILETIME census. Unknown descendants and uncontained SDK work still prevent a trusted idle verdict.

The real engine exposed saved archives deleted by staged-file cleanup and unterminated Windows rename buffers publishing unintended filenames or failing with error 123. Both normal saves and cache-only writeback now preserve publication and supply a UTF-16 terminator. Archive reads, saves and relocation keep explicit UTF-8/native boundaries, close readers before replacement, and tolerate unrelated NTFS sibling names that cannot be encoded as UTF-8. Native save/restart/reopen restores verified checkpoints with zero feature execution and no pending verification rebuild; a tail edit reuses the earlier two checkpoints. Firmware metadata replacement closes the checked destination handle before rename. Native CppCore diagnostics preserve Unicode paths instead of narrowing wchar_t values. MSVC portability fixes cover Windows macro collisions, C++17 template syntax and static storage for the ribbon's captureless-lambda constant. Release/LTO links are serialized by default without disabling parallel compilation. Unchanged CMake configuration no longer rewrites generated miniz or embedded firmware-source headers; the latter previously recompiled `bus_models.cpp` and relinked the engine despite identical source bytes.

The Windows stdout integrity fixture now yields within a frame without scheduling once per character, waits for producer completion before decoding, and verifies payload integrity. It no longer treats unfinished writers under native-build load as malformed completed frames. Native run records, preserved failures and qualification limits live in `reports/security/2026-10-12/windows-native-evidence.zip` and the remediation evidence ledger. Portable developer tools and diagnostic runtime sources do not authorize signed/customer-runtime release; approved private-runtime inputs, positive publisher fixtures and installed/GPU qualification remain separate prerequisites. Original user sessions and the original Windows VM are untouched.

Actual native desktop captures use an isolated per-application llvmpipe driver because the owned guest has no WGL-capable GPU driver. Visual inspection—not PNG existence—exposed the M1 scenario dropping the restored mesh after reopen. The scenario now adopts the cached mesh and rejects failed mesh loading or a non-warm round trip instead of forcing a rebuild. Its former timing-equality printout is diagnostic timing only, not a cache correctness assertion. A separate native probe reproduced GUI tab matching treating an absolute Unicode path as nonexistent and throwing while normalizing a relative path under a Unicode working directory. Matching now uses explicit UTF-8/native conversion; M1 also checks that an absolute spelling reuses the selected document saved under a relative spelling.

The completed native Release/LTO gate passes all 137 core executables and 34 host executables, with the host's explicit Windows `test_external_open` skip retained. The complete native Python gate passes 161 cases with 39 explicit platform/authority skips. Actual Unicode full-save and cache-only reopen take 4.56 and 4.30 ms on the three-feature fixture, restore verified checkpoints, execute zero features and schedule no verification rebuild; both files also restore under the same account's genuinely unelevated token. Actual worker/control RPCs and clean shutdown pass. A post-suite first daemon connection measures 412 ms, then 129–137 ms on restarts; earlier 13.5–16.4 second starts remain unexplained and are not claimed as a demonstrated cold-start speedup.

The corrected native M1 capture visibly contains the restored filleted/shelled model and its three-feature tree; its production open-path check reuses the selected tab for an absolute Unicode spelling. Missing-engine M1 exits unsuccessfully without a capture, while the engine-free static chat scenario still renders. These are real software-rendered desktop observations, not hardware GPU, authenticated editing, provider, CEF or installed-release qualification.

The final native unchanged-configure check preserves all three generated-header timestamps and hashes. The core runs only its build-ID refresh, and the host reports `ninja: no work to do`; neither recompiles nor relinks. The firmware header remains byte-identical to the original embedded source data.

## 2026-10-15 — Measured performance audit and exact hot-path cutover

[The performance audit](review/PERF_AUDIT_2026-10-15.md) records private, sequential Linux/NVIDIA before/after evidence and retained profiler commands for the SHA-NI/scalar SHA path, cached AICM publication without a temporary mesh copy, direct face descriptors, bounded PCB scans with monotonic contour traversal, linear Python IPC framing, memoized single-block GL picks, and corrected compact-transfer CUDA LBM. Verified warm-open median falls 65.470 → 36.083 ms; all 442 selectors remain exact; dense fill falls 7936.428 → 113.815 ms with byte-identical output; the 128×64×64/200-step GPU fixture is bit-identical to CPU at 0.273 versus 2.033 s median, with Nsight D2H bytes reduced 197.86×. CUDA remains optional/default OFF, geometry/cache/solver contracts remain intact, old/new saved caches reopen verified with zero feature execution, and actual GUI pick/M1 captures were visually inspected without touching user sessions or staging dist. Independent review reproduced and corrected import-time discovery mutation, false warm-open success after real cache loss, valid throughput after export failure, and positive MLUPS after a real governor stop; scoped execution now restores discovery, validates saved-cache hydration and exports before crediting throughput, and emits null MLUPS for failed solves. Linux RSS diagnosis additionally proved `getrusage` inherited launcher peaks; new captures use current-image `VmHWM`, historical baseline peaks are marked unavailable without changing timings, and no memory speedup is inferred. Post-review CLI proofs cover the real failure paths, 442 exact selectors and 16×8×8 CPU/GPU parity; hashes match Python at 15 sizes including 64 MiB. Scoped validation passes all eight selected CTest modules (including the full 38-case CUDA-enabled simulation suite), 70 combined tool/sampling/daemon pytest cases, and the final 30-case RSS-aware performance-test run. The complete repository gate passed native/host/WASM builds, all 137 core CTest executables, daemon/web/packaging checks and GUI smokes, but exited 1 on five Python environment failures: three overlong Unix socket paths and two DVC user-package discovery failures under private HOME. Complete affected-suite reruns with short pytest paths and explicit PYTHONUSERBASE passed 4,017 MCP tests and 525 tooling tests, without weakening assertions or changing application code for those failures. The original monolithic gate is not relabeled green; its diagnostics and correction commands are retained in the report. Raw scratch captures are ephemeral; durable sources and the machine/thread-specific schema-2 after baseline are linked in the report. FEA meshing, DFAM ray walls, dense DRC and patterned tessellation remain measured bottlenecks; PLM/CAM/sketch/hydraulic/gateway findings are static-only, and other platforms are unqualified by this pass.

## 2026-10-15 — Six-axis c275 tooling, knowledge delivery and recoverable waits

Audited the latest Desktop six-axis trace without changing its session or project files. `core/doc/session.cpp` now reuses immutable motion geometry/exact-relative-pose pair evidence and avoids redundant solid containment only for proven-disjoint threshold checks; native motion/interference regressions retain tiny-gap, containment, cavity, multi-solid and invalid-geometry coverage. Replaying the recorded 17-frame, 0.5-mm J3 sweep on copies of the current 27-component project completes in 309.31 s (the trace timed out at 900 s), with all 351 pairs classified per frame and unchanged source hashes. The opposite sweep completes in 305.39 s through an intentionally one-second host timeout and retry, executing native work exactly once and recovering its full result. `agent_turn.py`, `chudcode_tools.py` and `chudcode_host.py` own/retire recoveries per run, preserve creating-call identity and settle late validation refunds once. `worker_rpc.py`/`workspace_tools.py` capture selected roots before activation; live fresh-thread initial/refreshed bindings now find the real selected-directory STEP+ and classify its document as project-local. Online descriptions retain curated notes, effective timeout policy and project/built-in skill pointers even through eval; the simulate skill, guide and role helper document complete frame evidence and reject inner failed results before indexing. Agent discovery no longer advertises denied undo/redo. `process_isolation.*`, native dry-run dispatch and advisor preflight bound the entire candidate process group: the copied forearm probe returns an honest construction failure in 9.38 s, while an injected stall returns `budget_exceeded`, `still_running:false` in 10.10 s with the source state unchanged. Blend diagnostics no longer infer radius feasibility from a null kernel result; thickness reports its actual minimum ray and sampling limits without hiding the 0.00219-mm local taper. `verify/battery.py` uses unique opposing planar-face references rather than ambiguous generic selectors; the real forearm measurement passes. `cad_common.py` and native study mutations preserve detached study-only sources, deliver to an unused project filename, and disambiguate legacy duplicate study IDs without rewriting unrelated studies; a private-daemon import/study/terminal-save/reopen preserves source and occupied destination bytes and round-trips the results. Verification: five native suites, 786 Python tests plus 26 subtests (two skips), all ten source-audit gates, and private runtime smokes. Engine/daemon artifacts were atomically staged and their hashes matched the verified build; no user daemon was stopped or restarted. Contracts and both integration/development skills record the new invariants. Remaining model contacts, sub-0.5-mm fits and genuine overhang/taper findings are not suppressed or certified safe; this is tooling repair, not a model redesign or live-provider qualification.

## 2026-10-15 — User override for not-print-ready printer media delivery

The in-app agent refused to put a failing slice (`arm_upper`, trapped-support printability fail) on the user's flashdrive: `publish_print_media` hard-refused `not_print_ready`, and the prompts forbade any other route, so the agent first refused and then offered an unmanaged hand copy. Jobs now accept `user_override` (the user's explicit words, 1–500 chars); a not-ready job publishes only with it, through the same hash-verified, revisioned, stale-deleting transaction. `_failed_checks` mirrors slice_part's gate (gcode_check fails, printability fails except overhang/bridges when supports are on, unproven slicer mesh repairs); the override and its failed checks land in the media manifest (validated on read), README, inventory job rows and a `published_not_print_ready` warning on every publish/unchanged result. Hash checks are never overridden. Prompts (`general_agent.yaml`, printing cheatsheet, cam guidance, `slice_part` next steps, publish docstring, `scripts/print_media.py`) tell agents to ask once via the question tool, then publish with the override — never refuse or copy. Regenerating the tool tables also picked up four pre-existing description drifts (`cam_save`, `fs_grep`, `simulate_motion`, `wall_thickness`). Verification: `test_print_media.py` override test plus print/mode/guidance/role suites (187 passed), `scripts/check.py` 10/10, and a CLI publish of the real `arm_upper.print.json` to a scratch drive (README and warning name `printability.support`; bytes hash-identical).

## 2026-10-16 — Sketcher overhaul: relation set, model references, Fully Define, Convert performance

Audit found the 2D solver silently dropping wrong-type relations (parallel on arcs fed garbage residuals), no way to relate sketch geometry to model vertices/edges/pierce points or datum axes, no Fully Define, and Convert Entities costing ~11 s per click on a 341-edge part (`op_sketch_convert` ran two UNCACHED full `compile_document`s; every later edit in a doc with external refs re-described all faces/edges per ref, ~585 ms, and `sim_bbox_diag` stripped triangulation with `BRepTools::Clean`). Engine (`core/sketch/sketch.cpp`, `core/doc/validate.cpp`): intersection, arc midpoint, point concentric, ellipse/slot/arc_slot equal, `equal_curve_length`, full-ellipse and spline/arc endpoint (G1) tangency, `smooth` (G2), point_on spline, line/circle symmetric, `[line,line]` distance, `arc_length`, virtual `axis.h`/`axis.v`; `SketchSolution::skipped` → `skipped_constraints`; junction-form tangency for curves joined by coincident (a dimensioned slot is DOF 0, was 4 spurious DOF); normalized collinear/point-on-line and angular parallel/perpendicular (collinear used to collapse a free line to zero length against a converted model edge — caught by the `sketch` scenario). 3D (`sketch3d.cpp`): tangent, equal, midpoint, collinear, concentric, coradial, on/parallel/normal to plane, plane symmetry, point_on spline (GeomAPI_Interpolate-exact evaluator), GUI vertex references. `core/sketch/sketch_define.cpp`: Fully Define (relations, then dimensions incl. spline points, never moving geometry; 200 entities < 0.5 s), op `sketch_fully_define`. `core/doc/session.cpp`/`recon.cpp`: apply context gives op handlers the session CompileCache; session-owned external projection cache (descriptors per model state, per-ref memo); vertex, pierce, datum axis/origin and `sketch_entity` refs; `sketch_project_ref` through the shared resolver; composite rectangle/polygon/polyline relations. Measured (private engine, same part): convert face 11 109 → 373 ms, convert edge 11 665 → 127–215 ms, sketch edit after converts 580 → 75 ms; plain edits unchanged at ~58 ms. GUI (`sketch_mode.cpp`, new `sketch_relations_panel.cpp`, `viewport_panel.cpp`): complete Add Relation table, model vertex/edge/face references projected + related in one batch (Pierce, On Edge, Collinear…), new glyphs incl. point_on, Display/Delete Relations panel, Fully Define button, batched Convert reusing resolved selectors, id-pass refresh after mesh reload (model hover no longer dies after the first Convert/Smart Dim click), dirty-flagged regions and cached badge layout. AI (`services/mcp`, `services/agent`): `add_features` appends `sketch_fully_define` in the same batch (opt-out `auto_define:false`), `check_sketch` reports `pinned_only` and never calls a fix-pinned sketch fully defined, advisor/battery check every sketch, prompts teach relations/dimensions/origin/axis refs/external references instead of `"constraints":[]`; new MCP tool `sketch_fully_define`. Cache epoch `geomcache/17` (contracts/stepplus.md §20); contracts doc_schema "Sketch relation set v2…", doc_ops §31, sketch3d_gui_contract §9. Verified: core ctest 137/137, sketch/agent pytest, `CHUDWARE_SHOT=sketch` steps 3–8 (collinear to model edge lands on y=40, pierce (100,40), vertex (100,0), Fully Define DOF 0), SW parity verdicts identical before/after. Deferred: 3D edge/face external refs (vertex only), silhouette edges of curved faces, headless sketch camera framing (pre-existing: captures sit far too close, so judge overlays by printed coordinates).

## 2026-10-16 — Topology optimization for 3D-printed parts (GUI + agent parity)

Printing with topology results needed six numbers typed by hand, a raw build-direction vector, a jagged marching-tet STL, no DFAM in GUI runs and no slicer handoff. Shared preset data `services/sim/topology_print_processes.json` (fdm_0.4/fdm_0.6/resin/powder; GUI Apply overwrites offered keys, agent `print_process` fills unset keys; `overhang_penalty` 2 measured on two clamped beams: 23-32 % less print-surface support area for ≤6 % compliance). Study `build_plate` (bed face) → native derives build direction with named refusals (`core/sim/topopt_model.cpp`). `min_member_mm` filter radius gets a 2×median-element floor. Native print surface `surface_print.stl` (`core/sim/topopt_surface.cpp` pinned Taubin smoothing, local rollback of folding micro-triangles, BVH tri-tri self-intersection check with exact shared-vertex incidence) drives the overlay, which gains an `overhang` ("Needs support") scalar plus surface support-area evidence. GUI (`sim_topology*`, `sim_manager_topology.cpp`): volume-fraction gate, preset/bed-face card, print surface first for Export/Continue in CAD, async `topology_dfam` worker (`services/sim/topology_worker_rpc.py`, gui-background, cell `allow/run/actor`) merging DFAM rows into the run. Agent (`services/sim/topology.py`, `topology_tools.py`): early refusals before save/export, process-aware DFAM on the print surface, `print_handoff` with `slice_part`/`import_part` arguments. Contract: worker_rpc.md 2026-10-16 addendum. Verified: topopt ctest 10/10, `test_sim_topology`, `test_topology_tools.py` 68, `CHUDWARE_SHOT=topology` ok, private-hostd agent run + stock PrusaSlicer slice of the print surface. Gotchas: where the thresholded surface grazes a part face it leaves sub-layer slivers; DFAM calls the raw one a knife edge (min_wall warn) and, once smoothing flattens it, a thin skin (min_wall fail) — same sliver, stricter verdict. `slice_part(printer="prusa mk4", material="PLA")` currently fails filament resolution ("AmazonBasics PLA"), unrelated to topology.
