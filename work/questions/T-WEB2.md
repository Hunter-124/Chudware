# T-WEB2 — chudware-web finish-line consult (auth keys + PLM bootstrap + v1 DoD)

Driver: web-service session. Hunter delegated decisions to the advisor.
Context (as of this consult, 2026-07-14): T-WEB1 shipped and was live on the
since-retired `cad` hostname on `scootware.us`, behind CF Access; per-user
containers; chat; AI agent w/ remote providers. That hostname no longer exists in
DNS or the tunnel; the service now runs at `app.chudware.com` with no Cloudflare
Access application in front of it at all. Hunter's directive: "handle
authentication however you see fit — restrict to a few people, not random
webscrapers, protected by cryptography/keys."

## Q1 Auth: add gateway-native signed invite keys?
Current: CF Access (email allowlist; OTP + optional Google) → RS256 JWT verified
at gateway (JWKS, iss/aud/exp). Already crypto-enforced and scraper-proof.
Friction: every friend does an email-OTP dance; Hunter must edit BOTH the Access
policy and users.yaml.
Proposal: keep CF Access at the edge, ADD gateway-minted invite links:
`/join?k=<token>` where token = HMAC-SHA256(itsdangerous-style, secret in
~/chudware-web-data/secrets/gateway_secret) over {email, exp(default 30d)}; visit
sets a signed HttpOnly cookie (same signing); middleware accepts CF-JWT OR
valid key cookie, both still requiring users.yaml membership. Admin mints via
CLI (`python -m gateway.invite alice@x`) or admin-only /api/invites.
Alternative considered: switching Access policy to include-everyone + relying
only on gateway keys (weaker: exposes gateway HTML+login surface to scrapers,
loses the free bot-wall). Second alternative: CF Access service tokens
(machine-to-machine headers — awkward for humans in browsers).
Ratify proposal? Any change to token format/lifetime/storage?

## Q2 PLM bootstrap in session lifecycle (advisor T-WEB1 §A3 mandate, unbuilt glue)
Built: services/plm/remote.py (seed/clone/sync/lock), /plm mount, author env.
Missing: nothing seeds a shared project or clones into a session HOME.
Proposal:
1. Host-side (gateway setup or CLI `python -m gateway.plm_seed <name> [--from dir]`):
   create ~/chudware-web-data/plm/<name>.git bare via plm remote.seed_remote from
   a template project (empty vault or a sample part from samples/).
2. Session entrypoint: for each *.git in /plm, if ~/Projects/<name> absent →
   remote.clone_from_remote into it (venv python, CHUDWARE_REPO on path); if
   present → remote.sync(pull-only fast-forward) at START. No sync on stop
   (user saves are explicit; a stop-time push could publish half-work — but
   T-WEB1 Q3.5 said auto-sync on start/end; re-rule with the CAS/variant
   safety in mind).
3. GUI: user opens ~/Projects/<name>, works, uses plm tools; publishing =
   plm_sync (agent/MCP or PLM panel if wired).
Ratify or amend (esp. stop-time sync)?

## Q3 v1 definition-of-done — what MUST land now vs stays deferred?
Candidates: (a) selkies/WebCodecs backend [defer?]; (b) warm seat pool [defer?];
(c) /transfer live verification (unit-tested only); (d) Google IdP manual step
[Hunter]; (e) Entra phase-2 [defer]; (f) locks in status() [defer];
(g) monitor.timer live-fire test of MemAvailable action [cheap, do?];
(h) anything you consider demo-embarrassing still open.

## ANSWER — (advisor fills in)

**Advisor:** Fable / AdvisorFable2 (2026-07-14). Evidence: `Webservice/gateway/{auth,sessions,config,app}.py`, `Webservice/image/entrypoint.sh`, `services/plm/remote.py`, `contracts/plm.md` §6, `Webservice/DESIGN.md`, `work/questions/T-WEB1.md`.

### Q1 Invite keys — **REJECT (drop keys)**

**Pick ONE:** Keep **strict CF Access** (no path bypass, no service-token for humans). **Do not ship** gateway-minted `/join?k=` keys or a second auth cookie. `users.yaml` stays the sole app allowlist after JWT.

**Why keys fail under the live topology**
1. **Access sits in front of everything.** Tunnel → Access → `127.0.0.1:8300`. A friend still hits Google/OTP **before** any `/join` or cookie can run. Keys do **not** remove the OTP dance for first visits.
2. **After Access, keys are redundant.** `AuthMiddleware` already accepts `Cf-Access-Jwt-Assertion` / `CF_Authorization`, maps `email`, then **requires** `users.yaml` (`auth.py` PUBLIC_PATHS = only `/healthz`). A second signed cookie that still requires `users.yaml` adds no capability the JWT path lacks.
3. **Relaxing Access for `/join` is the only way keys become load-bearing** — and that is the rejected alternative: scrapers hit gateway HTML/login surface; free bot-wall is lost; URL tokens leak via Referer, history, chat paste, access logs. Not worth it for N≈6.
4. **Cookie theft / middleware risk.** Parsing another cookie is *structurally* easy (middleware already parses cookies; ASGI covers HTTP+WS), but OR-ing “CF-JWT **or** invite cookie” creates a durable second trust root that must never be weaker than Access (Secure; HttpOnly; SameSite=Lax; short TTL; no token in query after redeem). Unnecessary attack surface for zero friction win.
5. **Dual-edit friction is real — fix the operator path, not auth.** Hunter still maintains Access policy **and** `users.yaml`. Keys do not collapse that. **Do:** one operator command that reads `users.yaml` and updates the Access include-email policy via existing `cf_provision.py` / Zero Trust API (emails already a first-class input there). That is the friction fix.

**Token format if anyone reopens this later (not v1):** never leave long-lived secrets in `?k=`; one-time redeem → HttpOnly cookie; HMAC over `{email, exp, nonce}` with secret under `~/chudware-web-data/secrets/` (mode 0600); default exp **7d** not 30d; single-use nonce store. Still only useful if Access policy is deliberately opened — which we refuse for v1.

### Q2 PLM bootstrap — **RATIFY with amendments**

**Ratify direction:** host bare remotes under `~/chudware-web-data/plm/<name>.git`; session private clones under `~/Projects/<name>`; `/plm` shared mount stays **bare-only** (already `:U` rw in `sessions.py` `_podman_run`). Concurrent dulwich CAS on bare refs is the contract model (`remote._cas_push` expected-old; §6.3) — **safe enough for v1** as long as nothing puts a working tree on `/plm`. No shared working-tree mount (T-WEB1 still holds).

**Amendments (binding):**

1. **Seed must not be a tip-less empty bare for demos.**  
   `seed_remote` *can* `Repo.init_bare` + symbolic HEAD only. But `clone_from_remote` only calls `add_remote(... origin ...)` when `tip is not None`. Empty bare → clone “succeeds”, **no origin in manifest**, later `sync` → `not_found`.  
   **DoD for seed:** host CLI `python -m gateway.plm_seed <name> [--from dir]` must (a) `plm_init`/vault a template (empty manifest commit **or** sample from `samples/`), (b) `seed_remote` so remote has a real `refs/heads/master` tip, (c) land at `settings.plm_dir / f"{name}.git"`. Prefer a **Demo** project with one sample part over a void vault.

2. **Session entrypoint bootstrap (missing today).** `entrypoint.sh` exports `CHUDWARE_PLM_DIR` but never clones. Add after HOME mkdir, before Chudware:  
   for each `/plm/*.git` → name=`basename .git`; dest=`$HOME/Projects/$name`; if no `$dest/.aicadvault` → `clone_from_remote`; elif present → `sync` **pull path only** (see §3). Use image venv: `"$CHUDWARE_PYTHON" -c 'from services.plm import remote; …'` with `CHUDWARE_REPO` on `PYTHONPATH`. Failures log + continue (seat still boots).

3. **Stop-time sync — RE-RULE: NO auto-push on stop.**  
   - **Start:** pull/FF only (fetch + materialize if behind; never push).  
   - **Stop / idle reaper / gateway stop:** **do not** call `plm_sync` push.  
   - **Publish:** explicit user/agent `plm_sync` (MCP/panel).  
   **Why (CAS does not make auto-push safe socially):** `sync` only moves **vault commits** (not dirty working `.step+`), so “half-typed geometry” is not the issue — **accidental/incomplete explicit saves** are. Stop fires from reaper, crash, or Access logout; auto-pushing those commits pollutes the shared timeline. Copy-as-variant (§6.4) prevents silent data loss on diverge but **does not** prevent “Bracket (Alice's edits)” spam from unrehearsed stop pushes. T-WEB1 §Q3.5 auto-sync-on-end is **overruled**.

4. **GUI:** open `~/Projects/<name>`; no merge UI; locks remain advisory CAS on `refs/aicad/locks` (implemented) — no locks-in-status UI required for this glue.

### Q3 v1 DoD — ordered

#### MUST-DO-NOW (≤6)
1. **PLM seed CLI + one Demo bare** with real tip under `~/chudware-web-data/plm/` — empty seat is demo-fail (T-WEB1 §A3).
2. **Entrypoint clone + start-only pull** for `/plm/*.git` → `~/Projects/<name>` — the unbuilt glue; no stop push.
3. **`/transfer` live browser smoke** once (upload STEP → open in seat → download) — unit tests exist; path is demo-critical and unproven live.
4. **Operator dual-list path** — script/RUNBOOK step: `users.yaml` emails → CF Access policy update (reuse `cf_provision.py`); documents that invite-keys are rejected.
5. **`chudware-monitor.timer` live-fire** — enable, one real sample, confirm JSONL + optional `REFUSE_NEW` dry path; host must not melt unattended.
6. **Stream honesty for demo** — ship/verify **novnc** path as default working stream (`entrypoint` already falls back; selkies pip is legacy/broken on Fedora). Portal/iframe must not assume WebCodecs Selkies.

#### DEFERRED (one-line each)
- **(a) Selkies/WebCodecs backend** — Fedora image records selkies fail; novnc is the verified seat; revisit if pixelflux/Fedora story lands.
- **(b) Warm seat pool** — pre-pulled image + healthcheck-before-iframe is enough; pool adds idle RAM cost on 32 GiB host.
- **(d) Google IdP manual** — Hunter dashboard/OAuth client only; not a code gate.
- **(e) Entra phase-2** — blocked on UA IT app registration; OTP+Google remains phase-1 truth.
- **(f) locks in `status()` / locks UI** — `plm_lock` CAS exists; Hub badges not demo-blocking for N small.
- **Invite keys / Access path bypass** — rejected Q1.
- **Stop-time auto `plm_sync` push** — rejected Q2.
- **SSH/HTTPS remotes, merge UI, chudware-cloud** — out of `aicad.plm/1` folder subset.

### Top risks
1. **Empty-bare seed + clone** ships a vault without `origin` → first sync fails; always seed a committed tip.
2. **Auto-push on stop** (if someone “restores” T-WEB1) pollutes shared history with unrehearsed saves despite CAS safety.
3. **Selkies-as-default marketing** vs novnc reality — demo embarrassment if portal expects WebCodecs and gets x11vnc only.

### Verdict table
| Q | Verdict |
|---|--------|
| Q1 invite keys | **REJECT** — strict Access + `users.yaml`; fix dual-edit via provision script |
| Q2 PLM bootstrap | **RATIFY + amend** — seed w/ tip; start pull-only; **no** stop push; `/plm:U` bare OK |
| Q3 DoD | **6 must-dos** above; Selkies/warm/Entra/locks-UI/keys deferred |
