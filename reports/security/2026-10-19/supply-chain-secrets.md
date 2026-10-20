# Dependency and secret-exposure refresh

Scope: read-only source/dependency assessment on 2026-10-19, compared with `reports/security/2026-10-12/SECURITY_AUDIT.md` and `dependency-dispositions.md`. No repository edits, builds, tests, deployments, production-host access, Cloudflare calls, credential validation or secret rotation were performed. No secret values are reproduced here.

## Repo visibility

Authenticated, read-only `gh repo view <repo> --json visibility,isPrivate` returned:

| Referenced repository | Current visibility |
|---|---|
| [Hunter-124/CAD](https://github.com/Hunter-124/CAD) | PRIVATE, `isPrivate: true` |
| [Hunter-124/Polyhedral-FEA](https://github.com/Hunter-124/Polyhedral-FEA) | PUBLIC, `isPrivate: false` |
| [Hunter-124/Chudware](https://github.com/Hunter-124/Chudware) | PRIVATE, `isPrivate: true` |
| [Hunter-124/chudcode](https://github.com/Hunter-124/chudcode) | PRIVATE, `isPrivate: true` |
| [Hunter-124/omp](https://github.com/Hunter-124/omp) | PRIVATE, `isPrivate: true` |

These additional names were extracted from tracked references, rather than guessed. Current private visibility does not establish whether a repository was public previously, who has access, or whether anyone copied its contents.

The Polyhedral checkout is `third_party/polymesh/src`, whose Git origin identifies `Hunter-124/Polyhedral-FEA.git`. Neither Gitleaks nor TruffleHog was available on PATH or in the checked common/retained tooling locations. As requested for that case, an obvious-credential-pattern scan was performed on the checkout, not its history. The initial broad grep returned only minified Plotly/Mapbox API-parameter/error-message contexts in `third_party/polymesh/src/bench/advisor/dashboard.html:2756`, `:2761`, `:3257`; a subsequent metadata-only scan of 1,634 text files, excluding Git databases, found no private-key headers, AWS/GitHub/provider-token shapes or quoted alphanumeric credential assignments. Exact comparisons against the historical Cloudflare/R2 literals also found no checkout matches. **This is not a clean public-history attestation**; the scanner-unavailable history condition was not satisfied.

## Secrets in history (reachable from public remote?)

### Still open: RB-11 historical Cloudflare/R2 exposure, with literal retention in tests

**Severity: High, conditional on credential validity/scope.** Confirmed credential-byte retention; current validity was deliberately not tested. Attacker prerequisite today: repository read access or a preexisting copy of the affected source/history, **not an anonymous web visitor or an ordinary Chudware account alone**. If still valid, the retained R2 credential pair can authorize object-storage operations within its issuer-defined scope; that scope and access/rotation records remain unknown. This is the existing RB-11 issue, not a newly discovered independent incident.

Evidence:

- `git log --all --format='%H %ad' --date=iso-strict -- Webservice/Cloudflare-info.txt` identifies addition commit `84963165c57c08780fab0062ed6492c19036cff4` and deletion commit `ac1b912f4b3ef35616d762c1802cb499c1b7c153`, both dated 2026-07-14.
- `git branch -r --contains <commit>` lists `origin/master` for **both** commits. Thus deleting the file did not remove the earlier blob from branch history.
- The known redacted scanner locations are `Webservice/Cloudflare-info.txt:4-5`, `:10-11` and `:16-17` in the addition commit (`reports/security/2026-10-12/secret-scan-locations.json`).
- Local `origin/master` is `92a4ebb71ce307fbf68babc736945c6dfe968cc2`. The read-only GitHub `git/ref/heads/master` API returned the **same** SHA, so this conclusion is not based merely on a stale tracking ref. GitHub's compare API reports the addition commit as master’s merge base, with master ahead by 1,377 commits and behind by zero.
- **Current tracked source still embeds the historical R2 access-key identifier and secret at `Webservice/gateway/tests/test_secrets_supplychain.py:90-91`.** An in-memory exact-byte comparison against the historical credential file confirmed both constants, without emitting their values. Both locations also match at GitHub's current master tip. The negative regression test itself therefore preserves the credential pair.
- Other exact historical-literal matches in `Webservice/deploy/DEPLOYMENT.md:21-22,28,91`, `Webservice/deploy/RUNBOOK.md:228,515` and `Webservice/deploy/cf_provision.py:34` were 32-character identifiers, not evidence of additional retained secret tokens. Do not inflate these into independent secret findings.

**Public reachability answer:** reachable from the current **private** CAD remote’s master history and, for the R2 pair, its current tracked files; **not established as anonymously retrievable from any currently public remote**. No matching literals were found in the public Polyhedral checkout, and its full history was not scanned.

The previous incident-closure requirements remain open: issuer-authenticated revocation/rotation, derived tunnel-credential disposition, affected consumer inventory and access-log review (`SECURITY_AUDIT.md:251-258`; `release-hardening-remediation-evidence.json:5253-5255`). Removing test literals would reduce continuing disclosure but would not revoke credentials or erase historical copies.

### Requested fresh history scanner

Neither `gitleaks` nor `trufflehog` was available. Consequently `gitleaks git --log-opts='--since=2026-10-11'` was **not run**; no fresh comprehensive history-detector result is claimed. There is one commit in the local `--all --since=2026-10-11` range. The old 145 Gitleaks detections remain prior-audit candidates, not newly confirmed active credentials. Exact historical-literal and ancestry checks above were performed independently of that old scan.

## Secrets in working tree

### Confirmed retained historical material

The tracked test at `Webservice/gateway/tests/test_secrets_supplychain.py:90-91` retains the historical R2 credential pair, as described above. This is the only confirmed credential retention from this refresh; it is not evidence that the pair remains usable.

### Coverage and limits

Because Gitleaks was unavailable, its requested filesystem scan could not be run. A metadata-only fallback scan instead covered **4,822 Git-enumerated paths: 4,638 tracked plus 184 untracked-but-not-ignored**, using `git ls-files --cached --others --exclude-standard -z`. Existing regular nonsymlink text files were checked for private-key headers, AWS/GitHub/provider-token shapes, quoted credential assignments, and exact historical Cloudflare/R2 literals. Ignored credential files were not content-scanned; binary and symlink targets were not part of the text-pattern scan. No raw matches were printed.

Fourteen broad-pattern candidates occurred in test/document fixtures, with their surrounding test functions inspected. Locations: `Webservice/deploy/tests/test_gateway_preflight.py:72,125`; `test_process_credentials.py:511`; `test_two_tenant_cells.py:1140,1309`; `test_monitor_status_publish.py:135`; `Webservice/gateway/tests/test_admin_hardening.py:263,269`; `test_mailer.py:315,540`; `test_provider_keys.py:25`; `docs/omp-integration/research/verify-bridge-callable.md:73`; `services/manufacturing/printers/test_adapters.py:386`; `tools/flywheel/test_export.py:623`. These are not confirmed live secrets and are not reported as new vulnerabilities. Pattern checks are narrower than a maintained entropy/rule scanner and do not establish absence of secrets.

### Ignored and untracked credential locations

`git check-ignore -v` confirmed:

- `Webservice/secrets/` and a child path: `.gitignore:131`.
- `.env`: `.gitignore:10`.
- `Webservice/Cloudflare-info.txt`: `.gitignore:130`.
- `Webservice/gateway/.env` and `Webservice/prod.env`: `.gitignore:135`.

`git ls-files -- Webservice/secrets .env Webservice/Cloudflare-info.txt` returned **no paths**. These locations are therefore not currently tracked, despite the historical file exposure. Metadata only: the local Cloudflare file is mode `0600`; `.env` is mode `0644`; the secrets directory is mode `0755`. `.env` contents and parent-directory effective access were not inspected, so world-readable secret contents or an Internet disclosure are **not** established by that mode observation.

### Credential-looking tracked filenames

The requested case-insensitive filename expression (`secret|\.env|token|cred|key|\.pem`) matched the following **40 tracked paths**. These are filenames, not a list of leaked credentials:

```text
Webservice/deploy/SETUP_CREDENTIALS.md
Webservice/deploy/provision_entitlement_credential.sh
Webservice/deploy/tests/test_entitlement_credential.py
Webservice/gateway/config.example.env
Webservice/gateway/credits.py
Webservice/gateway/migrations/0005_credits.sql
Webservice/gateway/migrations/0010_provider_keys.sql
Webservice/gateway/provider_keys.py
Webservice/gateway/tests/test_credits.py
Webservice/gateway/tests/test_provider_keys.py
Webservice/gateway/tests/test_secrets_supplychain.py
Webservice/public-site/src/content/docs/keyboard-reference.mdx
benchmarks/tokenmaxx/REPORT.md
benchmarks/tokenmaxx/bench.py
benchmarks/tokenmaxx/results/.gitignore
benchmarks/tokenmaxx/results/gemma3-4b-qat.json
benchmarks/tokenmaxx/results/gemma4-e4b.json
benchmarks/tokenmaxx/results/matrix.json
benchmarks/tokenmaxx/results/qwen3-4b.json
benchmarks/tokenmaxx/results/transformers_backend_seam.json
core/security/release_keys.h
core/util/secret_store.cpp
core/util/secret_store.h
docs/omp-integration/research/omp-dap-keywords-mcp.md
docs/simulation/ACCREDITATION_PROGRAM.md
host/src/ui/copilot/magic_keywords.cpp
host/src/ui/copilot/magic_keywords.h
host/src/ui/web_provider_keys.cpp
host/src/ui/web_provider_keys.h
host/tests/test_magic_keywords.cpp
services/agent/providers/token_usage.py
services/agent/recipes/shaft_with_keyseat.yaml
services/browser/adapters/digikey_web.py
services/browser/adapters/test_digikey_parser.py
services/plm/keywords.py
services/plm/test_keywords.py
services/sim/credibility.py
services/sim/test_credibility.py
tools/flywheel/.env.argilla.example
tools/flywheel/.env.mlflow.example
```

## Dependency advisories (new/open, reachability)

### Refresh results

| Input and command | Observed result | Internet-facing reachability |
|---|---|---|
| `Webservice/gateway/requirements.txt`; `pip-audit -r Webservice/gateway/requirements.txt --format=json --progress-spinner=off -o /tmp/chudware-gateway-pip-audit-20261019.json` | **44 resolved packages, zero known advisories, no skipped-package records.** The manifest has 17 exact direct declarations; no separate source lock was found under the gateway. | Gateway packages include public request parsing/authentication and HTTP/storage transports, but no new advisory-specific vulnerable path was identified. |
| `Webservice/public-site/package-lock.json`; `npm audit --package-lock-only --ignore-scripts --json` in the site directory | **Zero vulnerabilities**, 464 total dependencies reported by npm. Both development and production graph were audited; no omit filter was used. | Site config is static output (`Webservice/public-site/astro.config.mjs:4-7`), not an Astro SSR gateway. No npm advisory currently requires production reachability disposition. |

The first pip-audit invocation attempted `--disable-pip` without `--no-deps`; the tool refused it before auditing. The successful invocation above used pip-audit's disposable resolver and did not install into or modify the shared gateway venv. No install scripts, site build or tests were run.

Input SHA-256 values:

- Gateway requirements: `6d3fe446803c95083653a12a864eab5ef33fc9ac3f8aff3ef744f7caffffb110` (matches the previous post-cutover declaration digest).
- Public-site lock: `d0b31e99f2fcf167ce7e6d59b17b07569bf4074ce6f6d3469fc2176a06d85513`.

**New or still-affected selected-package advisories: none reported by the refreshed Python/npm audits.** The old vulnerable-version tables are not re-reported as current findings. This says nothing about the production host’s installed versions or unlisted/private/native dependencies.

### Existing conditional exception retained, not a new affected-version finding

[GHSA-gvp8-978c-rx2q / CVE-2026-103001](https://github.com/advisories/GHSA-gvp8-978c-rx2q), PyJWT options-dict mutation: the refreshed [GitHub advisory API](https://api.github.com/advisories/GHSA-gvp8-978c-rx2q) still lists **Medium**, affected versions `>=2.11.0, <=2.13.0`, with `first_patched_version: null`. Gateway selects `PyJWT[crypto]==2.15.0` (`requirements.txt:14`), outside that published range. The authentication seam is Internet reachable when Cloudflare Access is configured, but the actual bypass requires reuse of the same mutable options mapping between unverified and verified decodes. `auth.py:554-565` constructs separate fresh options dictionaries; Google OAuth does likewise at `oauth.py:336-342`, and Microsoft’s options are separately constructed at `oauth.py:378-394`. Attacker prerequisite if the condition were reintroduced: an anonymous submitter possessing a signed token with claims that should be rejected; impact would be claim-verification bypass, not signature forgery. **No presently exploitable application condition was established**, and the old conditional mitigation/no-release-fix-attestation disposition remains unchanged.

Existing provenance/release prerequisite also remains open: the approved complete private runtime lock/wheelhouse and production-image parity were not established (`dependency-dispositions.md:16-20`; `SECURITY_AUDIT.md:57-61`). Public advisory cleanliness is not a substitute for these controls.
