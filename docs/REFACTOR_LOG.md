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
