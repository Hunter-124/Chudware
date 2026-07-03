# License debt register — cleanroom-before-SaaS

**Policy (Erik, 2026-07-03, binding):** early *personal-use* builds of AI-CAD may relax
license compliance — vendoring, bundling, or runtime-fetching license-encumbered material
is acceptable — **provided every such shortcut gets a row here**. Before any SaaS,
commercial, or centralized-server deployment, every open row must be resolved
(clean-roomed, re-licensed, subprocess-isolated, or removed). This register is the
single source of truth for that debt; merging a shortcut without a row here violates
the policy.

`contracts/licenses.md` still records what each dependency's license *is*; this file
records where we are knowingly out of compliance and what must happen before
commercialization.

| # | Component / material | Issue | Required action before SaaS | Added | Status |
|---|----------------------|-------|-----------------------------|-------|--------|
| 1 | T-D4 `LiveViewerProvider` (building-code runtime fetch from upcodes.com / codes.iccsafe.org) | Fetches copyrighted IBC/ASCE/ICC text at runtime on the end user's machine. Ruled acceptable for personal use because the *user* initiates the fetch, not our servers. | Legal review; likely restrict to user-supplied corpus (`UserCorpusProvider`) or licensed API in any centralized deployment | 2026-07-03 | open (provider not yet implemented) |

_No other known violations as of 2026-07-03 — everything shipped so far followed the
old strict guardrails (clean-room tables, GPL-via-subprocess, no AGPL)._
