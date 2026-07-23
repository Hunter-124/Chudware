# PLM v1 — OSS Survey (git binding + prior CAD-PDM art)

Status: SURVEY COMPLETE — feeds `contracts/plm.md` (aicad.plm/1, T-R1). This
document is the OSS-first (master plan §3.6, CONTRIBUTING license guardrails)
evidence base for the version-control storage engine (T-R2). The decided
direction — snapshot-based on the T-H2 canonical serialization, per-project
hidden git vault at `.aicadvault/` — is fixed in
[docs/design/PLM_VERSION_CONTROL_PLAN.md](../design/PLM_VERSION_CONTROL_PLAN.md); this survey
only chooses the *git binding* and records prior-art lessons.

All licenses below were verified against upstream (2026-07-03), not recalled.

## 1. The decision: how do we talk to git?

The vault is a real git repository the user never sees (plan §3.1). Three ways
to drive it, weighed against the CONTRIBUTING license guardrails
(MIT/BSD/Apache in-process OK; GPL subprocess-only; AGPL never) and the stated
priority: **lightweight + cross-platform (Fedora + Windows) + zero-provisioning**.

| Binding | License | In-process legal? | Cross-platform / provisioning | Many-small-files perf | Feature completeness |
|---|---|---|---|---|---|
| **dulwich** (pure-Python git) | **Apache-2.0 OR GPL-2.0-or-later** (dual; SPDX `Apache-2.0 OR GPL-2.0-or-later`) | **YES** — take the Apache-2.0 arm, fully in-process-clean under the guardrails | **Best.** Pure Python, `pip install`, no compiled wheel, no binary on PATH. Identical on Fedora + Windows. Zero runtime provisioning. | **Weakest** — pure-Python pack/loose-object I/O; slowest of the three on large trees | Read/write objects, refs, packs, commit, local clone, HTTP/SSH transport. **No LFS, no native file-locking.** |
| **pygit2** (libgit2 bindings) | **GPLv2 WITH linking exception** (the exception explicitly permits linking the compiled library into proprietary programs "without any restriction") | **Only via the exception.** Plain "GPL linking" is a NEVER row in the guardrail table; the linking exception is what clears it — a call the ratifier should make, not a subagent (flagged NEEDS RATIFICATION). | Needs a **compiled wheel per platform** (libgit2 C lib). Manylinux wheel on Fedora is fine; Windows wheel exists but adds a native-dep provisioning surface. | **Best** — native libgit2 speed. | Full libgit2 surface. No built-in LFS; locking is app-level. |
| **subprocess `git`** | GPLv2 (the git binary) | N/A — never linked. **Clean via mere-aggregation** (same house precedent as ngspice / QEMU / pandoc / esptool: subprocess-only, PATH-discovered, never bundled). | Needs `git` **on PATH**. Ubiquitous on Fedora; on Windows requires Git-for-Windows to be installed (a provisioning assumption). | **Middle** — process-spawn overhead per op, but native pack perf once running. | Full git incl. optional **git-lfs** and **git-lfs file locking** if the user has them. |

### 1.1 Do we even need LFS / native locking? (scoping the "feature completeness" column)

The task framing lists "LFS, locking, atomic ops" as a discriminator, but the
**decided design removes the dependency**:

- **No LFS in v1.** Checkpoints store only canonical `document.json` + tiny
  `thumbnail.png` (plan §3.2) — small, highly deltifiable text/PNG. Large binary
  `geometry.step` is written **only at releases** and is derived/recomputable, so
  even there we are not accumulating unbounded binary churn. Git's own delta
  compression on canonical JSON is the whole point (plan §3.1). LFS is therefore
  **out of scope for v1**; if release-geometry volume ever forces it, LFS is a
  later additive layer that only the subprocess binding could reach.
- **No git-lfs locking.** Team concurrency v1 is **advisory locks + no-merge**
  (plan §3.3). We implement our **own** lock manifest + compare-and-swap on a
  dedicated git ref (contract §6) — this is portable across all three bindings
  and does not depend on git-lfs's locking server. So "locking feature
  completeness" does **not** discriminate the binding.
- **Atomic ops** we need reduce to: atomic ref update (compare-and-swap push)
  and atomic working-tree assembly. All three bindings expose non-fast-forward
  push rejection, which is our CAS primitive (§6.3).

Net: the real discriminators are **(1) license cleanliness, (2) zero-provisioning
cross-platform parity, (3) many-small-files perf** — in that priority order per
the plan.

### 1.2 The many-small-files perf hit — confronted head-on

The exploded-container shadow tree (`items/<id>/document.json` + `meta.json` +
`thumbnail.png` per item, contract §4) maximizes exactly the workload where
dulwich is weakest: many small loose objects per commit. This is dulwich's real
liability and must not be hand-waved:

- **Mitigation is architectural, not binding-specific.** A commit is whole-tree
  but **unchanged items reuse their existing blobs** (content-addressed) — a
  typical explicit save touches one item, so only ~3 small blobs are hashed and
  written per commit. The pathological case (first import of a 200-item project)
  is a one-time cost, not the steady state.
- **Periodic repack.** dulwich can pack loose objects; T-R2 schedules an
  occasional `repack` (idle-time or every N commits) to keep the object store
  from bloating. If a system `git` is present, `git gc` is dramatically faster —
  see the hybrid below.

## 2. Verdict

**Primary binding: `dulwich`, using its Apache-2.0 license arm.**
**Optional accelerator: subprocess `git` when a system git is detected on PATH**
(a "fast path" for clone/fetch/push/gc/repack against remotes and for repacking
large local stores), always with the pure-dulwich path as the guaranteed
fallback. **pygit2 is NOT selected for v1.**

Rationale (one paragraph): The plan's #1 axis is *ease of use / zero-config /
lightweight*, and the #2 is *cross-platform parity*. dulwich wins both outright:
it is `pip install`-only, has no compiled wheel and no binary-on-PATH
assumption, behaves identically on Fedora and Windows, and — via its Apache-2.0
arm — is the **only** candidate that is unambiguously in-process-clean under the
existing guardrails with no exception-interpretation call required. Its lone
weakness is many-small-files throughput, which the design already blunts
(one-item-per-save commits reuse blobs; periodic repack) and which the optional
subprocess-git accelerator erases entirely on machines that have git — giving us
libgit2-class remote/gc performance **without** taking on pygit2's per-platform
native-wheel provisioning burden or its GPL-linking-exception ambiguity. pygit2
buys raw speed we don't need at the cost of the two axes we most care about, so
it is declined for v1 (revisitable if profiling ever proves dulwich+subprocess
insufficient — a version-bump concern, not a v1 one).

**License posture for the contract / licenses.md (T-R2 must add the rows):**

| dependency | license | posture |
|---|---|---|
| **dulwich** | Apache-2.0 OR GPL-2.0-or-later | **in-process, take the Apache-2.0 arm** (permissive, GPL-clean, commercial-safe). A hard `requirements.txt` dep of `services/plm/`. |
| **git** (optional accelerator) | GPL-2.0+ | **subprocess-only**, PATH-discovered, never bundled, feature-detected — absent git degrades to pure-dulwich. Same mere-aggregation isolation as ngspice/QEMU/pandoc/esptool. |

> Because T-R1 is docs-only, no `contracts/licenses.md` row is added in this
> change. **T-R2 (the first code task that imports dulwich) MUST add both rows
> above in the same commit** per the licenses.md "adding a dependency needs a
> row in the same change" rule. This is flagged so no silent license debt is
> created.

## 3. Prior art (how others do CAD/data version control + checkout locks)

| System | What it is | Relevant lesson for us |
|---|---|---|
| **Ondsel / FreeCAD PDM** ("lens" + Ondsel ES) | A hosted service + FreeCAD addon giving cloud storage, sharing, and lightweight versioning of FreeCAD documents. Ondsel Inc. wound down (2024); the open pieces live on. | Confirms the market gap and the "don't make CAD users learn git" thesis. Cautionary tale on the **SaaS-first** dependency — validates our local-first-with-optional-remote seam (plan §3.3) and the Pillar-12 gate on any hosted remote. |
| **git-lfs file locking** | LFS adds server-coordinated **exclusive** locks (`git lfs lock <path>`) so binary files that can't be merged aren't edited concurrently; lock state lives on the LFS server, enforced by pre-push hooks. | Directly validates **advisory/exclusive locking for un-mergeable binaries** as the right team-v1 primitive. We adopt the *concept* (lock-before-edit on un-mergeable items) but **not the implementation** — our locks live in a plain git ref updated by CAS (§6), so we need no LFS server. |
| **DVC (Data Version Control)** | Apache-2.0. Versions large data/model files by storing lightweight pointer files in git and the blobs in a separate content-addressed remote (S3/NAS/local). | Validates the **"small pointer in git, big blob elsewhere / only when needed"** pattern — mirrors our "canonical JSON in every snapshot, geometry.step only at releases." DVC itself is already a GPL-clean, in-process-safe dep in this repo (flywheel row in licenses.md) — evidence the pattern is house-approved. We do **not** need DVC for v1 (git's own delta compression suffices for our small canonical JSON), but it is the fallback if release-geometry volume explodes. |
| **Onshape** (proprietary, reference only) | Cloud-native CAD with automatic, always-on document history, named versions, and branch/merge. | The **UX bar** for "history just happens": our three-nouns model (Project/History/Checkpoint+Release) is the local-first, git-backed answer to Onshape's automatic history without requiring their cloud or their branch/merge complexity (explicit non-goal, plan §6). |
| **SolidWorks PDM** (proprietary, reference only) | Vault + check-in/check-out with exclusive locks, revision schemes, lifecycle states, where-used. | Source of the **vocabulary users already know** (revision letters, lifecycle, where-used, BOM freeze) — we adopt the *concepts* (contract §2–§5) while hiding the vault/check-out mechanics behind auto-history + advisory locks so nobody administers a vault (plan principle #4). |

Sources:
- [libgit2/pygit2 license (GPLv2 WITH linking exception)](https://github.com/libgit2/libgit2)
- [pygit2 "Why GPL?" discussion — same license as libgit2, GPL with linkage exception](https://github.com/libgit2/pygit2/discussions/1085)
- [dulwich (dual Apache-2.0 OR GPL-2.0-or-later)](https://github.com/jelmer/dulwich)
