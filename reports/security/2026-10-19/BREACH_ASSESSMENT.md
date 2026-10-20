# Chudware breach assessment — 2026-10-19

Trigger: owner reported suspicious visitors in traffic analytics.
Scope: live production (`livingroom-pc`, `app.chudware.com` gateway, PostgreSQL `chudware_prod`),
Cloudflare account/edge, GitHub repository access, the developer workstation (`hunter-pc`), the
`cloudcastle-prod` host that holds a root agent on production, plus a source/dependency refresh.
All production work was read-only; nothing was changed, restarted, suspended, or rotated.

## Verdict

**No evidence of a breach.** Every privileged action seen in the available logs traces to the owner's
networks. The suspicious traffic is automated internet scanning, and every probe was refused with
401/403/404.

**Production is still exposed to a known critical flaw.** Production runs commit `92a4ebb7`
(2026-10-12), the *vulnerable* baseline of the 2026-10-12 audit. All RB-01..RB-12 remediations
exist only as uncommitted changes in the local working tree. Anyone can self-register through
Google OAuth and is active immediately. Such an account can open `/engine`, where the deployed
bridge forwards `agent_spawn`/`run_model` (RB-01). That gives arbitrary execution as `liveuser`,
and `liveuser` has NOPASSWD sudo (`/etc/sudoers.d/wheel-nopasswd`), so root on the host. This host
also runs the mail server, GitHub self-hosted runners, the Cloudflare tunnel, and holds the Stripe,
OAuth, and DB secrets.

## What was checked and observed

| Area | Observation | Assessment |
|---|---|---|
| Prod SSH | 87 accepted logins since 2026-10-16 (journal start), all from `100.83.229.76` (hunter-pc on the tailnet) with the `other-pc-to-livingroom-pc` key. 0 failures. `btmp`: none since 2026-10-01. Password and keyboard-interactive auth are off; root login is refused. | Clean |
| Prod accounts and keys | Only `root` has UID 0. Login shells: root, liveuser, postgres, plasma-setup, `ccci-auto-livingroom` (CloudCastle CI). `liveuser` has 5 authorized keys, root has 3; all are named and match the owner's machines or CI. | Clean. Verify the `dev` and `chudware-ci-windows-runner` keys are still needed. |
| Prod persistence | No user crontabs. `cron.d` has only `0hourly`, and `cron.daily` has only `brave-origin`. Timers are all recognizable (chudware-*, cloudcastle-*, stalwart-backup, ddns, dnf). No `/etc/ld.so.preload`. No processes running from deleted, `/tmp`, or `/dev/shm` binaries. | Clean |
| Prod binary integrity | `rpm -V` reports no changes for openssh, sudo, pam, shadow-utils, coreutils, procps-ng, util-linux, systemd, bash, iproute, findutils, or curl. Recently changed files in `/usr/bin` are package updates plus CloudCastle's own binaries. | Clean |
| Prod network | Outbound connections go only to GitHub Actions, Tailscale, Cloudflare, and the CloudCastle agent → `70.225.75.56`. Gateway and Postgres listen on loopback only. Internet-facing: mail (25/465/587/993/8443 via podman). | Clean. The mail stack adds attack surface on the same host. |
| Prod checkout | `~/Repositories/CAD` is clean at `92a4ebb7`, matching `origin/master`. Last deploy rc=0 on 2026-10-12. | Not tampered with, but outdated (see verdict) |
| Gateway DB | 14 accounts. 1 admin (owner); every admin session IP is home (`136.56.68.102`, `2605:a601:812e:6300::/64`) or the pre-2026-09-07 home ISP (`174.110.99.16`). No orgs, marketplace orders, provider keys, invitations, or security actions. No active account lacks both a password and an OAuth identity, so no pre-hijack population. | Clean |
| Third-party signups | 8 unknown Gmail OAuth signups (TR, UA, DE, BE, …). 2 were already suspended by the owner. Each made 1–2 sign-ins and downloaded the installer or viewed the portal. Only `aezelt1@…` (BE, 2026-09-21 13:27–13:30Z) opened `/app` and `/engine`, once. It created an empty workspace directory; no agent processes or files appeared at that time. The account is suspended. | No evidence of abuse. The engine RPCs for that minute were not logged (see limits). |
| Admin API | All 493 successful `/api/admin/*` responses in the 30 days came from `136.56.68.102`. `/admin/terminal` was used twice, from home (the terminal is now disabled). | Clean |
| Admin password change on 2026-10-14 16:35 | `credentials.updated_at` matches a new admin session from home IPv6 (Windows Chrome). No reset token or reset email exists, so this was a successful login that triggered an Argon2 rehash. | Owner login, not a takeover |
| Edge traffic (30 d) | 45k requests to chudware.com (Pages) and 5.8k to app.chudware.com. The top sources are known scanner ranges (45.148.10.0/24 NL, GCP Belgium 34.x/35.x, 2a06:98c0::/32). They probed `/.env*`, `/.git-credentials`, `/terraform.tfstate`, `/privatekey.key`, `/wp-admin/install.php`, Windmill `../../proc/self/environ`, and Langflow `build_public_tmp`. The 2026-10-11 spike (8.5k) was one GCP scanner. Every probe got 401/403/404 (864×404, 330×401); none returned 2xx. | Benign background noise. This is the "sus" traffic. |
| Cloudflare config | DNS, the tunnel ingress (`app.chudware.com`→:8300, scootware→:8320, catch-all 404), and tunnel connector origins (home IPs only) all match `DEPLOYMENT.md`. There is 1 account member (owner) and 1 account token. | Clean |
| Cloudflare audit log (2026-07-15 → now, 333 entries) | Every user and token write action came from owner IPs (174.110.103.69 → 174.110.99.16 → 136.56.68.102) or Cloudflare-internal dashboard IPs. | No foreign use. Read-only API calls are not audited. |
| GitHub | `Hunter-124/CAD` is private. Write collaborators: `CharlesEdmonds` and `iamnotyobabydaddy` (account created 2026-07-26, profile "Michael", "Mossad"; no recorded activity). Former pushers `Gissa777` and `MooseinaTree` were last active 2026-07-05 and are no longer collaborators. All workflow runs in the last 100 were triggered by Hunter-124. | No abuse seen. Confirm you know `iamnotyobabydaddy` (see P1). |
| cloudcastle-prod (root agent on prod) | Accepted SSH only from `192.168.1.40` (garrisongamingpc key) and `136.56.68.102` (owner key). 7,567 failed attempts are normal internet scanning. Password auth is off. | Clean. It is a root trust path into production. |
| hunter-pc | sshd uses keys only; the journal has no accepted or failed logins. Ollama is bound to `0.0.0.0:11434` without auth. The firewall zone opens 1025–65535 tcp/udp. | No compromise. Ollama is reachable from the LAN and tailnet. |

## Findings requiring action

| ID | Sev | Finding | Evidence |
|---|---|---|---|
| BA-01 | **Critical** | Production runs the pre-remediation gateway and hostd. Any self-registered account gets arbitrary process execution through `/engine`, which leads to root through NOPASSWD sudo. | `deploy-status.json` release `92a4ebb7`. Deployed `engine_bridge.py:1031-1057` only denies `agent_admin_turn`, `doc_file_operations`, and non-admin `agent_provider_*`. Signup→active is immediate (2026-10-15 OAuth signup became active). |
| BA-02 | **High** | The Cloudflare API token `old-cell-14db` that `Cloudflare-info.txt` declares compromised is **still active**. It has near-all-permissions scope, including *Account API Tokens Write* and *Registrar Domains Admin*. It was committed in `84963165` (2026-07-14), which is reachable from `origin/master`, and `Webservice/DESIGN.md:95` records that the repo was public at that time. Its last use before this audit was 2026-10-11T04:52Z, and that use cannot be attributed. The R2 key pair derived from it is also still in tracked `Webservice/gateway/tests/test_secrets_supplychain.py:90-91`. | `/accounts/{id}/tokens/verify` → active. The token detail lists its policies. |
| BA-03 | High | The production gateway venv has 19 known advisories: PyJWT 2.13.0 (×14), cryptography 49.0.0, urllib3 2.7.0. The working tree pins fixed versions. | `pip-audit` of the prod `pip freeze` |
| BA-04 | Medium | Gateway access logs are disabled (`--no-access-log`), engine RPC methods are not logged, and the system journal keeps only about 5 days (300 MB cap). Gateway logs before 2026-10-14 20:48 no longer exist. Because of this, `/engine` activity from third-party sessions (e.g. 2026-09-21) cannot be reconstructed. | Unit `ExecStart`, `journalctl --list-boots`, `journald.conf.d/10-size-cap.conf` |
| BA-05 | Medium | Unknown write collaborator `iamnotyobabydaddy` on the private repo. A push to `master` auto-deploys to production through the `livingroom-pc` runner (liveuser has NOPASSWD sudo). Write access also exposes the history secrets (BA-02). | `gh api repos/Hunter-124/CAD/collaborators` |
| BA-06 | Low | The production data root contains test-fixture workspaces (`student~40ua.edu.attacker.example`, etc., created 2026-10-01 13:54), so tests ran against the prod path. A world-readable root-owned `pre-deploy-20260811-131431.dump` sits in `~/chudware-web-data` (the parent directory is 0700). | `ls` of `CAD_Build/users`, `~/chudware-web-data` |
| BA-07 | Low | `_dmarc.chudware.com` has `p=none`, which allows spoofed mail from the domain. | Cloudflare DNS |

Source-only findings in the undeployed working tree (anonymous oversized-body DoS, IPv6 /64
rate-limit rotation, register-sets-password on passwordless accounts, Argon2 on the event loop,
4 cell methods added since acceptance) are in [`gateway-delta-review.md`](gateway-delta-review.md).
Fix them before deploying. Repository and dependency exposure is in
[`supply-chain-secrets.md`](supply-chain-secrets.md); the working-tree pins have zero open advisories.

## Remediation order

1. **P0, today:**
   - Contain BA-01 before anything else. Either deploy the remediated gateway and hostd, or, if that release is not ready, disable `/engine` for non-admin users and close self-signup until it is.
   - Suspend the unknown active accounts you do not recognize from `/admin` → Accounts (`POST /api/admin/accounts/{email}/suspend`). The deployed code does not revoke sessions on suspension (RB-04), so do not reinstate those accounts before the remediated build ships.
   - Remove NOPASSWD sudo for `liveuser`, or move the service to a dedicated unprivileged user. That reduces the impact of any gateway or hostd escape.
2. **P0:** rotate BA-02.
   - Create a scoped replacement: DNS and Email Routing for `cf_mail.py`/`cf_provision.py`, and a separate Pages-deploy token.
   - Update the GitHub secret `CLOUDFLARE_API_TOKEN` and `Webservice/Cloudflare-info.txt` on both hosts.
   - Revoke `old-cell-14db`. No running service on production reads it: the only file copies are `Cloudflare-info.txt` and the test literal. The `site-build` workflow secret may be the same value, so update it first.
   - Remove the literals from `test_secrets_supplychain.py`.
3. **P1:**
   - Confirm or remove `iamnotyobabydaddy` (BA-05).
   - Rebuild the prod venv from the pinned `requirements.txt` (BA-03).
   - Enable a persistent gateway access log and log engine RPC methods per account, without parameters. Raise journald retention to at least 30 days (BA-04).
4. **P2:**
   - Bind Ollama on hunter-pc to loopback or the tailnet.
   - Delete the fixture workspaces and the stray dump on production.
   - Set DMARC to `quarantine`.
   - Add Cloudflare WAF rules for the scanner paths (`/.env*`, `/.git*`, `*.php`) and a rate limit on `/api/auth/*`.

## Limits

- Gateway logs only go back to 2026-10-14 20:48, the system journal to 2026-10-14, and prod sshd to 2026-10-16. Cloudflare HTTP analytics cover 30 days (sampled), and the Cloudflare audit log covers 2026-07-15 onward. Earlier activity, such as the 2026-09-07 third-party session, is visible only as DB rows.
- `/engine` traffic records the connection, not the RPC methods, so "no abuse" for the single third-party engine session rests on side effects: no agent logs, no files, no new processes or persistence.
- Read-only Cloudflare API use and R2 access are not in the audit log. R2 is not enabled on the account, so the R2 keys could not have reached any data.
- The mail stack (Stalwart/Roundcube, scootware.us) and the CloudCastle and Kinocasino services sharing the host were checked only as host processes, listeners, and persistence; their application logs were not reviewed.
- No exploit was run against production. BA-01 rests on the deployed source plus the 2026-10-12 runtime reproduction of the same code path.
