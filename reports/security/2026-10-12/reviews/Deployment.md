## yield: findings

Read-only source review; **no commands, checks, payloads, deployments, or network requests executed**. Findings below describe current source behavior, not verified live deployment state.

### D1 — High: operational credentials are passed through process arguments
**CWE-214 — Invocation of Process Using Visible Sensitive Information**

**Locations**
- `Webservice/gateway/deploy_ops.py:189-206`
- `Webservice/deploy/update.sh:474-499`
- `Webservice/deploy/install_webd.sh:382,434-435`
- `Webservice/deploy/rollback.sh:321,371-372`
- `Webservice/deploy/verify_public.sh:85-88`
- `Webservice/deploy/monitor.py:437`

**Execution path:** `_launch_detached()` copies every nonempty `CHUDWARE_*` environment value into `systemd-run --setenv=NAME=value` arguments. This includes configured session, provider-encryption, billing, OAuth, and managed-provider secrets. The deployment and rollback health probes also pass freshly minted session cookies directly to `curl`; their account selection prefers active administrators. `verify_public.sh` passes the operator password as a JSON command argument. The observability installer passes the Grafana administrator password to Podman in argv.

A lower-privileged local account able to inspect process command lines during these operations can obtain secrets without permission to read the mode-0600 environment files. A stolen health session authenticates as the selected account until expiration; it is not restricted to health endpoints.

**Nearby controls:** PostgreSQL clients correctly use private `PGPASSFILE` files rather than password-bearing DSNs in argv. Cloudflared’s canonical unit similarly avoids token arguments. Short session lifetimes narrow, but do not eliminate, the exposure.

**Customer impact:** administrator-session theft, provider/billing credential disclosure, and access to customer data under the stolen account. This requires local process visibility; it is **not** an unauthenticated remote exploit. Live `/proc` visibility restrictions are unverified.

**Minimal remediation:** pass only required, nonsensitive environment values to deploy jobs; use protected environment/credential files or inherited environment rather than `--setenv` values. Supply curl cookies and JSON through private files or stdin. Use a protected Podman environment file for the Grafana password.

**Safe reproduction:** in a temporary fixture, patch `deploy_ops.subprocess.run` to capture argv and return success; set only `CHUDWARE_WEB_SESSION_SECRET=fixture-marker`, patch repository/data paths, and invoke `_launch_detached()` with a harmless fixture argv. Assert the marker appears in captured argv. Separately extract the curl command construction into an isolated function and substitute a stub curl that records arguments containing a fake session marker. Do not invoke the deployment scripts end to end.

---

### D2 — High: staging inherits production’s personal CAD workspace and shared daemon
**CWE-863 — Incorrect Authorization**

**Locations**
- `Webservice/deploy/chudware-gateway-staging.service:10-19,44-45`
- `Webservice/deploy/install_webd.sh:19-20,526-529`
- `Webservice/gateway/config.py:527-543,586-588`
- `Webservice/gateway/limits.py:109-128`
- `Webservice/gateway/workspace.py:103-123,205-211`
- `Webservice/gateway/engine_bridge.py:958-974,999-1002`

**Execution path:** both gateways load the same `~/chudware-web/webd.env`. Staging overrides its bucket root, but not `CHUDWARE_WORKSPACE_ROOT` or `CHUDWARE_WEBD_SOCKET`. Configuration consequently selects the production workspace and daemon. The personal directory name derives solely from normalized email, without an environment namespace.

An attacker controlling an active staging account with the same email as a production account can request that production account’s existing personal workspace files through staging. A staging administrator can additionally select `scope=shared` and reach the entire shared workspace root. For an entitled staging identity, the engine bridge admits the same filesystem paths and forwards them to the same daemon.

**Nearby controls:** staging has its own database, session secret, bucket root, and read-only home mount. These do not prevent reading production files. The gateway’s read-only mount also does not constrain writes made by the separate shared daemon.

**Customer impact:** production CAD confidentiality and, through the daemon, integrity become dependent on staging account security.

**Prerequisites:** access to staging plus a matching active account, or staging-admin credentials for the shared-root path. Public staging accessibility is not assumed; D5 explains the provisioning exposure.

**Minimal remediation:** explicitly give staging an independent workspace root and daemon socket. Keep staging account/document capabilities environment-specific.

**Safe reproduction:** before importing configuration modules, set `CHUDWARE_WEBD_ENV` to a temporary fixture containing a temporary workspace/socket. Construct production and staging settings with distinct data and bucket roots, but otherwise the canonical unit’s configuration. Assert their `user_workspace_root(user_slug("fixture@example.invalid"))` values and sockets are identical. Place one harmless text file in that shared fixture directory and exercise only the path-resolution/download helper with a fake staging request. No daemon is needed.

---

### D3 — High: staging-admin credentials authorize production operational actions and logs
**CWE-862 — Missing Authorization**

**Locations**
- `Webservice/gateway/app.py:499`
- `Webservice/gateway/deploy_ops.py:38-42,333-349`
- `Webservice/deploy/redeploy.sh:40-47`
- `Webservice/gateway/app.py:366-379`

**Execution path:** the deploy router is registered unconditionally in both gateway environments. Its authorization checks only whether the current environment’s account has `role="admin"`. `/api/admin/deploy/redeploy` launches `redeploy.sh`, which unconditionally restarts **production** `chudware-gateway` and probes port 8300. Changing `CHUDWARE_WEB_DATA` only relocates its logs/status.

Likewise, `/api/admin/logs` always requests the production `chudware-gateway` journal, even when called through staging.

**Nearby controls:** administrator checks prevent standard-user access. Flocks serialize deploy jobs, but do not authorize a staging principal against production. Journal redaction hides recognized secret shapes, not customer emails, paths, and other operational metadata.

**Customer impact:** compromised staging-admin credentials can interrupt production connections and read production operational/customer metadata without production credentials. These are remote gateway endpoints, not merely authorized desktop same-user tools.

**Minimal remediation:** bind operations and journal selection to an explicit deployment environment; deny production actions from staging. Do not let a staging role imply production authority.

**Safe reproduction:** directly call the route with a fake staging-admin request, temporary log root, `_read_status` returning idle, and `_launch_detached` patched to capture argv. Inspect the launched fixture script using a stub `systemctl` that records its arguments; assert it targets `chudware-gateway`, not staging. Patch the journal subprocess similarly and assert the production unit is selected. No real systemd call.

---

### D4 — Medium: revoked administrators can continue receiving terminal output
**CWE-613 — Insufficient Session Expiration**

**Locations**
- `Webservice/gateway/terminal.py:889-923,935-978`
- `Webservice/gateway/auth.py:611-619`
- Existing regression: `Webservice/gateway/tests/test_gateway.py:358-374`

**Execution path:** terminal authorization is revalidated only after receiving an inbound WebSocket message. The PTY-to-WebSocket pump sends output without checking whether the session remains valid or the account remains an administrator.

An attacker who established a terminal connection before session revocation, suspension, or demotion can stop sending input and continue reading output from the existing shell/session until the connection ends or its receive timeout fires. The default idle timeout is 30 minutes; output authorization is not immediate.

**Nearby controls:** terminal defaults off; handshake and inbound input require admin authorization; sessions have ownership checks; output is throttled. The existing demotion test sends another input frame, so it does not cover passive disclosure.

**Customer impact:** continued disclosure of privileged terminal output after access is revoked. Requires the terminal to have been explicitly enabled and an already-authorized connection.

**Minimal remediation:** revalidate before sending output and/or close terminal sockets on revocation through a bounded revocation watcher.

**Safe reproduction:** use a fake WebSocket, mocked PTY reader producing two harmless markers, and mocked spawn/kill helpers. Allow handshake, revoke the fake credential before the second output marker, and send no inbound frames. Assert the second marker is currently delivered. Do not start a real shell or tmux server.

---

### D5 — Medium: the normal Cloudflare provisioner republishes supposedly private staging
**CWE-668 — Exposure of Resource to Wrong Sphere**

**Locations**
- `Webservice/deploy/cf_provision.py:37-39,143-157`
- Conflicting commitments:
  - `Webservice/deploy/DEPLOYMENT.md:55-63`
  - `Webservice/deploy/RUNBOOK.md:143-146`
  - `Webservice/deploy/PUBLIC_ROLLOUT.md:177-189`

**Execution path:** `cf_provision.py` unconditionally adds a public staging hostname routed to port 8310 and creates its proxied DNS record. Its full tunnel-configuration PUT also omits the foreign-service ingress entries that current deployment documentation explicitly says must be preserved.

Running the documented provisioning step therefore exposes staging to Internet requests and can remove unrelated shared-tunnel routes. Authentication remains present; this is exposure of a deliberately private service, not proof of an authentication bypass. D2/D3 materially increase its consequences.

**Minimal remediation:** keep staging unpublished by default; require a deliberate staging-ingress option. Preserve explicitly owned foreign ingress rather than replacing the shared tunnel configuration with a narrower list.

**Safe reproduction:** reuse the offline fake-API structure in `Webservice/deploy/tests/test_cf_provision.py`, supply an existing tunnel configuration containing a harmless foreign-host fixture, and capture the configuration PUT and DNS writes. Assert staging is published and the foreign entry disappears. No provider call.

---

## yield: confirmed operational defects and policy gaps

These are source-backed control deficiencies, separated from directly exploitable findings.

1. **Staging deployment disables production retention.**  
   `update.sh:406-407,422-423` disables both environments’ security/release timers, then enables only the selected environment. `setup.sh:140-141` does the same for security retention. The services already use distinct databases. Deploying staging after production therefore disables production’s scheduled purge and release collection.  
   **Impact:** expired detailed security telemetry can remain past its intended expiry, and release files can accumulate. `migrations/0011_security.sql:106-128` establishes the 30-day detailed-batch expiry; `security_retention.py:33-47` performs the purge.  
   **Fix:** operate only on the selected environment’s timers.  
   **Safe check:** mock `systemctl` and evaluate extracted retention functions in a temporary shell fixture; assert production timers stay enabled during staging selection. Existing unit tests currently assert the problematic disable-both behavior.

2. **Pre-deploy dumps are plaintext and unbounded.**  
   `update.sh:342-360` writes custom-format `.dump` files directly under `DATA_ROOT/backups`, without age encryption. Mode 0700/umask 077 protects ordinary account access, but not offline/storage access. No reviewed retention path collects these files; `backup.sh:171-173` collects only nightly `.age` families in another directory. This contradicts `RUNBOOK.md:99-101`’s prohibition on parallel plaintext backups.  
   **Fix:** encrypt rollback dumps and define bounded retention.  
   **Safe check:** extract `backup_database` into a fixture, stub `pg_dump` to write a nonsecret marker, and inspect the resulting fixture artifact; never use a real DSN.

3. **Nightly backup coverage does not match current customer storage roots.**  
   `backup.sh:25,109` archives only the production data root. Current default personal CAD and bucket roots are elsewhere (`config.py:535-543`; `install_webd.sh:19-20`). Staging’s non-database state is also outside that archive. Consequently the runbook claim that this archive contains the per-account workspace/bucket trees is not true for the canonical defaults.  
   **Fix:** enumerate configured roots in an explicit backup/restore manifest, including environment and mount boundaries.  
   **Safe check:** create temporary data/workspace/bucket directories with distinct text markers; archive only the fixture data root using the script’s tar selection; verify external markers are absent.

4. **Backup excludes do not cover the new institutional credential directory.**  
   `backup.sh:86-91` excludes `./secrets`, but the university unit stores its Box configuration at `university/secrets/rclone.conf` (`chudware-university-box.service:9`). That nested non-`.env` file is inside the archived data root. The archive remains age-encrypted, so this is **not** a plaintext leak; it violates the stated separation between data backups and credentials.  
   **Fix:** explicitly exclude credential-bearing nested directories and maintain a credential inventory.  
   **Safe check:** archive a temporary `university/secrets/rclone.conf` containing only `fixture-marker` using the exact exclude list, then inspect archive membership.

5. **Signing-key provisioning uses a plaintext temporary file despite its prose guarantee.**  
   `provision_entitlement_credential.sh:36-43,54` copies raw signing bytes into a mode-0600 temporary file before encryption. Its header/help and test name claim plaintext is not persisted. Whether the temporary file is disk-backed, tmpfs-backed, encrypted, or included in storage snapshots is unverified.  
   **Fix:** stream validated bytes to `systemd-creds` via an appropriate memory-only input, or explicitly constrain/document temporary storage.  
   **Safe check:** copy the helper into a fixture, supply 32 nonsecret marker bytes, and replace `systemd-creds` with a stub that records input-file existence/content before cleanup.

6. **The launch privilege policy has changed, but the runbook has not.**  
   Current `harden_host.sh:28,81-91` includes removal of service-user wheel membership in `launch`. `RUNBOOK.md:62-64,119-125` still says that control is excluded and passwordless root is an accepted continuing state. This is a contradictory operational instruction, **not evidence of current live sudo privileges**. Removing wheel also conflicts with `backup.sh:100-106`’s general passwordless-sudo fallback for unreadable files. A least-privilege backup authority must be provisioned and proven.

7. **Restore instructions create a plaintext dump with unspecified permissions/cleanup.**  
   `backup.sh:19-21` and `RUNBOOK.md:588-593` decrypt into a fixed `/tmp/prod.dump` without an explicit private umask or cleanup.  
   **Fix:** document a private temporary restore directory, umask 077, cleanup, target isolation, and a tested streaming alternative where supported. Do not follow these instructions against production during this audit.

---

## yield: historical leaked credentials and closure evidence

**Redacted source-path-only reporting:**

- Historical exposure: `Webservice/Cloudflare-info.txt`
- Current source also retains historical credential literals in: `Webservice/gateway/tests/test_secrets_supplychain.py`

The integrator’s redacted history projection contains three detections for the historical Cloudflare-information path. Deployment documentation independently records Cloudflare API-token and R2-key compromise. Those are credible historical exposures, not merely unexplained detector hits.

**No reviewed source establishes revocation/rotation closure.** `DESIGN.md`, `DEPLOYMENT.md`, and `RUNBOOK.md` still require rotation. `PUBLIC_ROLLOUT.md` contains dated claims that credentials were still live, but those claims **do not prove present validity**. I did not open the credential file, inspect credential values for validity, contact providers, or inspect secret history content.

The administrator-hardening JWT/key detector hits are visibly test fixtures. The historical entitlement and vendored-xterm detections remain unclassified: the metadata alone is insufficient, and those historical contents were not opened.

**Precise missing incident evidence:**
- provider-side revocation records for the disclosed API token and R2 credential pair;
- replacement scope/custody records, without secret values;
- rotation/revocation of derived tunnel credentials if the compromised token could obtain them;
- inventory of dependent scripts, CI, object-storage clients, and deployed consumers updated to replacement credentials;
- incident access-log review and customer-impact determination;
- disposition of remaining literal copies and history, without mistaking deletion/history rewriting for credential revocation.

---

## yield: PUBLIC_ROLLOUT historical closure checklist

These classifications concern current reviewed source, not live enforcement or a blanket security certification.

| §1 historical row | Current classification and evidence |
|---|---|
| Stream-proxy `urljoin` SSRF | **Deleted surface.** No `/stream/` route registration in current gateway Python inventory/search. |
| Shared workspace/bucket | **Fix present, qualified.** Account/team root authorization and resolved containment exist: `workspace.py:103-159`, `bucket.py:105-157`. Environment isolation remains broken: D2. |
| Unrestricted shared-daemon bridge | **Controls present; full closure not certified by this scope.** Identity rewrite/path and document checks exist; `engine_bridge.py:191-235,970-1045`. Integrator’s engine review must establish complete method/sink coverage. |
| Host keys copied into session containers | **Container surface deleted; replacement gate present.** Host model-probe reads gated at `engine_bridge.py:540`; ambient-turn fallback blocked at `1105-1117`. Full provider credential closure belongs to its reviewer. |
| Admin terminal shell | **Partially fixed/open.** Default-off, owner checks, caps and output throttling present; D4 remains. Persistent shells intentionally retain same-user shell authority. |
| Forwarded identity headers to stream upstream | **Deleted surface.** Retired stream proxy absent. |
| Missing CSP | **Fixed in reviewed source.** `app.py:135-159` sets CSP/HSTS/Permissions-Policy/COOP. Special WASM CSP/runtime behavior not exercised. |
| Unbounded uploads | **Guards present.** Declared and streamed caps plus partial cleanup: `workspace.py:224-263`, `bucket.py:242-287`. |
| Unpinned dependencies | **Direct pins fixed.** Current `requirements.txt` uses exact pins. Transitive integrity, installed versions, and current advisories require integrator evidence. |
| YAML allowlist/admin emails | **Authorization source removed.** No current file-backed users loader; roles resolve from account store. This does not erase historical personal data. |
| 48-bit session identifiers | **Original session-manager surface deleted.** Terminal IDs currently use `token_urlsafe(16)` at `terminal.py:321-323`, i.e. 128 random bits. |
| `/healthzFOO` public prefix | **Fixed.** `auth.py:125-135` uses exact/slash-boundary matching. |
| Anonymous health telemetry | **Fixed.** `app.py:455-461` returns only `{ok: true}` anonymously. |
| Admin static assets available to standard users | **Fixed.** `app.py:524-550` checks admin role. |
| Chat query-email identity fallback | **Fixed.** `chat.py:325-338` requires middleware user state. |
| Arbitrary admin project paths | **Fix present.** Resolved project-root checks: `app.py:187-204`; configured roots in `config.py:566-584`. Default admin root remains the service account’s home. |
| Missing rate limits/quotas | **Guards present, not full resource isolation.** `limits.py`, upload routes, and atomic engine-bridge reservations enforce controls. Current filesystem quota helper measures the selected root, not a combined workspace-plus-bucket total. Native/remote writers are outside process-local quota locks. |
| Raw journal tokens | **Recognized-shape redaction fixed.** `app.py:343-379`; production-journal selection from staging remains D3. Deployment-log route returns unredacted deploy output at `deploy_ops.py:369-393`; no concrete bearer leak into those logs established. |
| Release filename header injection | **Fixed.** `releases.py:140,258-262` limits basename characters and rejects leading dots/separators. |

### §3/§4 operational obligations
- Plan/storage and pooled-credit enforcement now have current source guards (`limits.py:184-204`, `credits.py:338-375`, `engine_bridge.py:1140-1189`); those are not proof of all-path closure.
- **Shared daemon remains:** `DESIGN.md` and `chudware-hostd.service` retain one shared process. Its unit has no memory/CPU/task ceilings or gateway-like sandbox directives. Shared-process availability and host blast radius remain architectural risks; no parser exploit is asserted here.
- Live wheel/sudo status, SSH password authentication, LAN firewall exposure, outbound pivot-key removal, update timer state, and secret rotation are **unverified**.
- Old assertions that all backups are plaintext are superseded by age pipelines in nightly `backup.sh`; the separate pre-deploy plaintext path remains.
- Current cloudflared unit avoids token argv. Old “token visible in ps” statements are historical for that canonical unit.
- Historical deployed commits, test-pass counts, Cloudflare topology observations, OAuth/mail status, and “currently live” statements are not current deployment evidence.

---

## yield: live controls and unresolved prerequisites

1. Confirm installed units/drop-ins match reviewed templates; effective loopback binding and proxy trust remain unverified. Canonical uvicorn trusts forwarded headers only from `127.0.0.1`; Cloudflare/cloudflared header handling and any additional local proxies were not inspected live.
2. Establish installed `CHUDWARE_WEB_DEV=0`, terminal disabled, and host-provider sharing disabled. Canonical prod/staging units fail closed on dev bypass.
3. Verify service UID privileges, sudoers, inherited groups, and user-manager authority. **Do not claim current root compromise solely from stale wheel documentation.**
4. Verify firewall/listener exposure, including intentional foreign mail services and the actual active zone.
5. Establish independent staging filesystem/daemon authority and bind all operations to their intended environment.
6. Obtain backup success/freshness, off-host copy, bucket-private access policy, age identity custody, and lifecycle evidence. R2’s historical disabled state is not proof of current state. The canonical backup unit has no environment-file wiring for R2 credentials.
7. Perform isolated recovery drills covering both databases, configured external personal roots, institutional mount/cache, secrets restored independently, and schema-compatible rollback. Source release rollback does not undo database migrations.
8. Confirm both environments’ retention timers remain active concurrently; set policy for plaintext rollback snapshots and logs containing email/path/model-use metadata.
9. Confirm transactional mail actually delivers through the configured TLS backend. Current console mail **suppresses message bodies** (`mailer.py:277-285`), contrary to old rollout/setup claims that reset links are logged.
10. Establish Cloudflare rotation closure as listed above, correct tunnel ownership/preservation, and intentionally unpublished staging.
11. Supply current CI branch protections, runner separation, executed promotion gates, dependency advisory results, and live parity-watch configuration. Repository documentation is not evidence that these controls ran.
12. For institutional storage, establish separate Box OAuth custody, private Unix RC socket/directory permissions, mount authenticity, drained upload queues, and remote durability. Source Unix-only RC with no auth is a same-user filesystem boundary, not a demonstrated public endpoint.

---

## yield: reviewed paths, limits, and suggested checks

### Reviewed
- All named target modules and `Webservice/DESIGN.md`.
- Entire `PUBLIC_ROLLOUT.md`; security-relevant sections throughout `DEPLOYMENT.md`, `RUNBOOK.md`, and `SETUP_CREDENTIALS.md`.
- Deployment entrypoints: `setup.sh`, `update.sh`, `install_webd.sh`, `rollback.sh`, `rebuild.sh`, `redeploy.sh`, `harden_host.sh`.
- Backup, monitor/observability, security/release retention, spool reaper, static rehash, SQLite migration, entitlement provisioning, university provisioning, Cloudflare tunnel/mail provisioning; selected Stripe transport/safety/output sections.
- All deployment service/timer templates.
- Supporting source sections in `auth.py`, `app.py`, `workspace.py`, `bucket.py`, `limits.py`, `chat.py`, `engine_bridge.py`, `credits.py`, `mailer.py`, `releases.py`, and security expiry migration.
- Existing deploy/admin-hardening/security-retention/provisioning test source and the integrator’s redacted historical-detection projection.

### Limits
No production access, git commands, secret-file reads, payload execution, scans, builds, tests, formatters, or network activity. No installed service/runtime verification. Stripe reconciliation internals, complete engine/provider authorization, native/parser security, public-site implementation, and historical blob contents were not independently audited by this slice.

### Existing checks for the integrator
Use isolated fixture environments only:
- `Webservice/deploy/tests/test_cf_provision.py`
- `Webservice/deploy/tests/test_monitor_status_publish.py`
- `Webservice/deploy/tests/test_entitlement_credential.py`
- `Webservice/deploy/tests/test_provision_stripe.py`
- `Webservice/deploy/test_security_retention.py`
- `Webservice/deploy/test_security_retention_units.py`
- `Webservice/gateway/tests/test_admin_hardening.py`
- `Webservice/gateway/tests/test_deploy_exit_codes.py`
- Selected fixture-only tests in `test_secrets_supplychain.py`; **exclude its worktree credential-file test**, which reads the actual credential file.
- Add the bounded reproductions above: existing tests currently affirm staging publication and disable-both retention behavior, and terminal demotion coverage checks only the next input frame.

**Coverage summary:** deployment/auth-bypass/proxy/terminal/ops, backup custody and restore coverage, retention, logging metadata, historical exposure, and every rollout §1 row were reviewed. Six operational-control contradictions remain alongside five source-backed exposure findings; none establishes the present live host or credential state.
