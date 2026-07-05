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
| 2 | T-W26 Gemma 3 4B QAT GGUF weights (`bartowski/google_gemma-3-4b-it-qat-GGUF`) — user-initiated download via `model_manager.provision("gemma3-4b-qat")` | Google Gemma Terms of Use (use-restricted, NOT Apache-2.0). Prohibits certain commercial deployments and redistribution of the weights. The *user* initiates the download to their own machine; we do not host or redistribute weights. Acceptable for personal use under policy above. | Legal review before any hosted/SaaS deployment. Options: (a) drop Gemma from default registry and make it opt-in with ToU acknowledgement; (b) switch to Qwen3-only default (Apache-2.0); (c) negotiate a commercial license with Google. | 2026-07-04 | open |
| 3 | T-X2 `services/browser/adapters/grabcad.py` — GrabCAD Community model search/download via the user's own authenticated browser session (Hunter directive 2026-07-04 supersedes the "no scraping" posture for this mechanism specifically: the browser acts as the user's own browser under the user's own login, not an anonymous scraper). Each model carries its own uploader-set license (CC or GrabCAD's community terms); this adapter does not verify per-model licensing before download. | Legal review of GrabCAD's ToS for automated (Playwright-driven) access even when user-authenticated; per-model license surfacing to the user before reuse, in any centralized/SaaS deployment | 2026-07-04 | open (adapter regexes unverified against a live logged-in session — see T-X6) |
| 4 | T-X2 `services/browser/adapters/traceparts.py` — TraceParts supplier-catalog search/download via the user's own authenticated browser session. Per-supplier terms vary: some catalogs are open manufacturer downloads, others require accepting a per-download reseller/distribution agreement. This adapter detects the per-download terms click-through and refuses to auto-accept it (surfaces `terms_confirmation_required` to the user instead), but has not catalogued which suppliers require it. | Legal review of TraceParts' ToS for automated access; per-supplier terms audit once T-X6's real integration tests run against actual supplier catalogs; this is also the browser-session alternative to the parked paid-API plan in `work/backlog/T-BL12.md` — reconcile which mechanism ships if both are ever pursued | 2026-07-04 | open (terms-wall markers unverified against a live logged-in session — see T-X6) |

_No other known violations as of 2026-07-03 — everything shipped so far followed the
old strict guardrails (clean-room tables, GPL-via-subprocess, no AGPL)._
