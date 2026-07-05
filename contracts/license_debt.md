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
| 2 | Gemma-family GGUF weights, user-initiated download. **T-W26:** Gemma 3 4B QAT (`bartowski/google_gemma-3-4b-it-qat-GGUF`) via `model_manager.provision("gemma3-4b-qat")`. **T-W29 (2026-07-05, this row extended, not duplicated):** Gemma 4 E4B-it (`unsloth/gemma-4-E4B-it-GGUF`, file `gemma-4-E4B-it-Q4_K_M.gguf`) downloaded to `~/.local/share/AI-CAD/models/gemma4-e4b/` for the tokenmaxx bench. DiffusionGemma (`google/diffusiongemma-26B-A4B-it`, Apache-2.0) was **surveyed but NOT downloaded** — 26B-A4B MoE min quant is 16 GB, does not fit the 12 GB 3080 Ti, needs the draft `llama-diffusion-cli` (PR #24423); recorded NO-GO, so no new debt from it. | Same Google Gemma Terms of Use (use-restricted, NOT Apache-2.0) applies to every Gemma weight above. Legal review before any hosted/SaaS deployment. Options: (a) drop Gemma from default registry, opt-in with ToU acknowledgement; (b) Qwen3-only default (Apache-2.0); (c) commercial license with Google. Note: DiffusionGemma itself is Apache-2.0 (no ToU debt) if ever bundled, but carries a draft-runtime dependency to clear. | 2026-07-04 (2 extended 2026-07-05) | open |
| 3 | T-X2 `services/browser/adapters/grabcad.py` — GrabCAD Community model search/download via the user's own authenticated browser session (Hunter directive 2026-07-04 supersedes the "no scraping" posture for this mechanism specifically: the browser acts as the user's own browser under the user's own login, not an anonymous scraper). Each model carries its own uploader-set license (CC or GrabCAD's community terms); this adapter does not verify per-model licensing before download. | Legal review of GrabCAD's ToS for automated (Playwright-driven) access even when user-authenticated; per-model license surfacing to the user before reuse, in any centralized/SaaS deployment | 2026-07-04 | open (adapter regexes unverified against a live logged-in session — see T-X6) |
| 4 | T-X2 `services/browser/adapters/traceparts.py` — TraceParts supplier-catalog search/download via the user's own authenticated browser session. Per-supplier terms vary: some catalogs are open manufacturer downloads, others require accepting a per-download reseller/distribution agreement. This adapter detects the per-download terms click-through and refuses to auto-accept it (surfaces `terms_confirmation_required` to the user instead), but has not catalogued which suppliers require it. | Legal review of TraceParts' ToS for automated access; per-supplier terms audit once T-X6's real integration tests run against actual supplier catalogs; this is also the browser-session alternative to the parked paid-API plan in `work/backlog/T-BL12.md` — reconcile which mechanism ships if both are ever pursued | 2026-07-04 | open (terms-wall markers unverified against a live logged-in session — see T-X6) |

_No other known violations as of 2026-07-03 — everything shipped so far followed the
old strict guardrails (clean-room tables, GPL-via-subprocess, no AGPL)._
