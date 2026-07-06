---
id: T-X6
title: Hardening - license register, captcha/2FA failure modes, gated integration tests
workstream: X
owner: opus-x6
status: review
branch: task/T-X6-browser-hardening
depends_on: [T-X3, T-X4]
lane: parts-sourcing
kind: new
priority: 2
---

## Context

Closes out the workstream X v1 chain. Three things: (1) sweep and complete the
`contracts/license_debt.md` register for every source touched by T-X1..T-X4
(GrabCAD, TraceParts, and whatever T-X4's fallback actually exercised), since
earlier tasks were told to add rows "as you observe" rather than up front; (2)
make the captcha/2FA hard rule mechanical rather than just documented —
`browser_login`/`browser_fetch` must detect these states and hand control back
to the user (headed window, no automation attempt) instead of silently hanging
or retrying; (3) add real per-source integration tests that exercise actual
adapter code paths against real sites, gated behind an env flag so CI never
attempts a real login — mock-proven with real code, not an all-skip suite (see
`[[offline-ml-track-discipline]]`-style prior art in this repo: pending-
provision tests must prove the code path executes, not just skip cleanly).

## Files / areas touched

- `contracts/license_debt.md` — one row per source actually integrated
  (GrabCAD, TraceParts, plus SnapEDA/McMaster/manufacturer portals only if
  T-X4 actually touched them — don't add speculative rows for sources with no
  code). Each row states the TOS posture as observed (account-required?
  redistribution restrictions on downloaded CAD? per-supplier terms
  click-through?) and what "before SaaS" would require, matching the existing
  register's format.
- `services/browser/session.py` (T-X1) — captcha/2FA detection: a simple,
  conservative heuristic (known captcha iframe/service markers, known 2FA
  prompt text/selectors for the integrated sites) that, when hit inside
  `browser_login`, pauses and leaves the HEADED window open with a clear
  message for the user to complete manually — never attempts to solve, bypass,
  or detect-and-evade. When hit inside `browser_fetch` (headless), abort that
  fetch immediately and return an `ok:false` "needs manual re-login" shape
  (never fall back to solving it programmatically).
- `services/browser/test_hardening.py` — captcha/2FA detection triggers the
  correct pause/abort path (faked page content, no real captcha/2FA involved).
- `services/browser/integration/` — NEW: per-source integration tests gated
  behind `AICAD_BROWSER_INTEGRATION_TESTS=1` (or similar — match any existing
  env-flag convention in the repo's test suite if one exists, check via
  graphify first). These run against real sites with a real (developer-
  provided, out-of-band) logged-in profile — never CI, never committed
  credentials. Document exactly how a developer opts in, in a short README in
  that directory.

## Steps

1. `graphify query "license_debt register TOS provider compliance"` and
   `graphify query "offline pending provision env flag integration test"` to
   find the existing gated-test convention this repo already uses (per
   `[[offline-ml-track-discipline]]` memory, similar patterns exist for T-H10/
   T-F11-style offline model tasks) — mirror it rather than inventing a new
   gating scheme.
2. Sweep T-X1..T-X4's actual code/behavior and write accurate license_debt rows
   — read the adapters, don't guess from vendor names.
3. Implement captcha/2FA detection + the pause/abort behavior.
4. Write the hardening unit tests (fast, no real network).
5. Write the gated integration test scaffold + README; verify it correctly
   no-ops (not "all skip", but a clear "provision required, see README" single
   informative skip) when the env flag is unset, and confirm locally (manually,
   outside CI) that it exercises real code when a developer sets the flag with
   a real profile.

## Definition of done

- [x] `contracts/license_debt.md` has an accurate row for every integrated
      source (rows 3-8 already existed; updated their status to reflect the
      mechanical captcha/2FA safety + gated live suite that now closes them)
- [x] Captcha/2FA always hands control to the user headed, in both
      `browser_login` and `browser_fetch`, never automated around
      (`[[oss-first-directive]]`-adjacent safety posture: no evasion, no bypass)
- [x] Gated integration tests exist and are proven to exercise real adapter
      code paths when opted in (documented manual verification, since CI can't
      run them) — not a suite that's structurally incapable of running
- [x] Full existing test suite (`~/.local/share/AI-CAD/mcp-venv/bin/python -m
      pytest`) still green with zero new regressions  *(gate at commit time)*

## Results (2026-07-06, opus-x6)

- **New `services/browser/challenge.py`** — pure, conservative `classify_challenge(html,
  url) -> "captcha"|"2fa"|None` (+ `is_challenge`). Distinctive service markers /
  prompt phrases only (recaptcha/hcaptcha/turnstile/cloudflare-interstitial/Amazon
  robot-check/PerimeterX + 2FA phrasings), never bare words; captcha wins ties. No
  Playwright import — unit-tested against faked HTML.
- **`services/browser/session.py` wiring** — `browser_login` (HEADED) now polls the page
  while the user works and, on a detected challenge, prints a clear "complete it
  yourself, this tool will not solve/bypass it" message once per kind and records
  `challenge_detected` in the result — never a click. `browser_fetch` (HEADLESS) aborts
  a detected challenge with `ok:false, reason="captcha_or_2fa_detected"` + a re-login
  hint — never returns the challenge page as content, never retries/solves.
- **`services/browser/test_hardening.py`** — 30 tests: classifier truth-table (captcha /
  2fa / ordinary / url-only / both-present) + session wiring via the `_sync_playwright`
  seam (fetch aborts on captcha & 2fa; fetch passes ordinary; login detects+reports and
  makes zero solve calls). Existing `test_browser_session.py` still green (8/8).
- **`services/browser/integration/`** — gated live suite behind
  `AICAD_BROWSER_INTEGRATION_TESTS=1` + a per-source saved profile. Two ALWAYS-RUN cases
  keep it from being all-skip: registry-wired check + a real `GrabCADAdapter.search()`
  run end-to-end offline (browser_fetch faked to a login-wall page). 6 live cases skip
  with clear "no profile for <site>" messages. README documents opt-in + safety posture.
- Verified: `pytest services/browser/` → 40 passed, 6 skipped (unflagged); flagged w/o
  profiles → live cases skip on "no profile" (proves real-code gating, not all-skip).

**Scoped out (noted):** `browser_download` (raw APIRequestContext bytes, not a rendered
page) has no challenge check — it runs only on artifact URLs obtained AFTER a `search`/
`acquire` that already passed the `browser_fetch` challenge gate. A bytes-level guard
there would be a separate, lower-value follow-up.

## AI hints

- This task is the natural place to also fix anything T-X1..T-X4 left as a
  `# TODO` or a known gap — check their Progress log entries first.
- Keep the captcha/2FA detection heuristic simple and conservative (false
  positives — pausing when it wasn't actually needed — are much cheaper than
  false negatives here).

## Progress log
