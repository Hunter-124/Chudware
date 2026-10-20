## Yield: findings

**Read-only source audit completed. No files changed; no builds, tests, scanners, payloads, signing commands, or network requests executed.** Findings below are confirmed by source; proposed reproductions remain unexecuted.

### RS-1 — High: Windows finalization continues after failed payload/build/signature checks and prepares attacker-supplied installers for release signing

**CWE:** CWE-252, CWE-754, CWE-347  
**Locations:**
- `packaging/finalize_release.py:174-195` — generated PowerShell invokes builders and `signtool verify` without checking `$LASTEXITCODE`.
- `packaging/finalize_release.py:362-366` — `platform-evidence` labels any existing artifact `"verified": true`.
- `packaging/finalize_release.py:244-255,259-280` — completion trusts that evidence and creates an artifact-signing request.
- `services/installer/build_release.py:477-480,624-631` — existing installer causes an error returned as process exit code 1.
- `services/installer/build_msi.py:161-164,213-215` — equivalent existing-MSI rejection.
- `packaging/finalize_release.py:51-61,102-144` — extraction and handoff validation do not prohibit additional top-level installer artifacts.

**Attacker prerequisite:** Ability to substitute/add files in a prepared handoff archive or its staging directory, without possessing the release-signing key. The owner subsequently follows the generated Windows finalization workflow. This is a build/handoff compromise, not an unauthenticated Internet endpoint.

**Trust-edge failure:**
1. The attacker retains expected handoff identity, locks, and signed payload material but adds arbitrary bytes under the final names:
   - `Chudware-Setup-<version>-x86_64.exe`
   - `Chudware-<version>-x86_64.msi`
2. The legitimate builders refuse to replace those existing artifacts and exit nonzero. Payload-verification failures likewise return nonzero.
3. The generated script only sets `$ErrorActionPreference = 'Stop'`. On Windows PowerShell, that does not make an ordinary native-process nonzero exit terminate the script.
4. Failed `signtool.exe verify` commands are also ignored.
5. `platform-evidence` produces affirmative evidence for the retained bytes; `complete` produces their Ed25519 signing requests.

Thus the workflow can present attacker-supplied installers as successfully finalized, including stable-channel Authenticode evidence, for the owner’s subsequent release signing.

**Customer impact:** Malicious installers can cross the intended untrusted-build-to-offline-signing boundary. Actual publication still requires the owner to sign the generated requests; this is not automatic theft of the offline key.

**Nearby controls considered:** Archive traversal/link rejection, handoff identity checks, locked-input hashes, signed-payload verification, and builders’ refusal to overwrite outputs are real controls. The unchecked native return codes defeat their enforcement in this orchestration path. The stable-channel presence check for `CHUDWARE_SIGN_IDENTITY` does not establish successful signing.

**Minimal remediation:** Check and terminate on every native command’s nonzero exit in the generated PowerShell, including builders, verifiers, and completion commands. Do not generate positive platform evidence after verification failure. Reject unexpected pre-existing final artifacts in prepared handoffs.

**Safe isolated reproduction:**
- In a temporary directory, reuse `_prepared()` from `packaging/test_finalize_release.py:34-48`; create a text-only artifact with the canonical `.exe` name. Never execute it.
- Call `write_platform_evidence()` for that file and then `_stable_platform_evidence()`: source predicts acceptance of the unsigned text file. `complete()` can demonstrate creation of its signing request using only temporary fixtures, without signing anything.
- Separately demonstrate the native-exit behavior in an isolated Windows PowerShell session:
  ```powershell
  $ErrorActionPreference = 'Stop'
  & $env:ComSpec /d /c exit 7
  if ($LASTEXITCODE -eq 7) { 'continued after failed native command' }
  ```
- For an orchestration-level fixture, shadow builder/verifier commands with inert failure-returning fixtures and assert that no evidence/request should be created. Do not invoke real signing tools or production handoffs.

**Existing-test blind spot:** `packaging/test_finalize_release.py::test_unsigned_stable_windows_rejected_but_beta_truthful` tests *missing* evidence, not evidence emitted after a failed native verifier.

---

### RS-2 — High: PR-controlled code runs on persistent release-signing runner state

**CWE:** CWE-501; CWE-522 for resulting signing-secret exposure  
**Locations:**
- `.github/workflows/fast-gate.yml:34-36,51,67-75` — PR trigger and self-hosted Linux/Windows runner selection.
- `.github/workflows/fast-gate.yml:180-183,283,302` — checkout followed by execution of checked-out `scripts/check.py`.
- `.github/workflows/ci-workflow-policy.yml:40-44,62,96-99,212-217,259-261` — another PR-triggered execution path on `cc-linux-build`.
- `.github/workflows/nightly-release.yml:225,257-266` — trusted release job uses that same Linux runner label and a persistent interpreter under `$HOME/chudware-ci-tools/release-python`.
- `.github/workflows/nightly-release.yml:396-398,447-450` — later execution with `CHUDWARE_RELEASE_CI_KEY` in the environment.
- `core/security/release_keys.h:29-40` — packaged clients unconditionally trust the CI release key.
- `docs/reference/CI_RUNNERS.md` — fleet/routing documentation also identifies persistent runner reuse; Windows primary/package labels share a host.

**Attacker prerequisite:** A contributor capable of getting a code-bearing PR or branch workflow scheduled in this **private repository**, including whatever approval is required by repository policy. Anonymous fork execution is not established.

**Trust-edge failure:** The PR job can modify runner-user-writable state outside the checkout—for example, the release virtual environment, executable search paths, or user-owned tool caches. A later trusted release uses that state while holding signing/publication secrets. Checkout cleanup, read-only `GITHUB_TOKEN`, and `persist-credentials: false` do not reset `$HOME` or authenticate interpreter startup files.

A modified Python `.pth` startup hook in the persistent release interpreter is a direct route to code execution inside the later secret-bearing signing invocation. No persistence process needs to survive job cleanup.

**Customer impact:** Theft/use of the CI signing key, modification of release artifacts, and access to other secrets subsequently delivered to the same runner account. Since `release-ci-v1` is accepted by packaged clients and signs paired stable identities, the impact is not confined to disposable test builds.

**Nearby controls considered:** Pinned checkout action, read-only permissions, false persisted credentials, PR-specific concurrency, and separation from the production serving host reduce other risks. The macOS lightweight gate explicitly uses a separate account; I do **not** claim its account is shared with the signer.

**Deployment uncertainty:** Actual fork approvals, runner-group restrictions, filesystem ACLs, and any out-of-band destruction/reimaging between jobs were unavailable. A documented, enforced fresh-machine boundary could invalidate the persistence route; the checked-in workflows instead explicitly reuse persistent home-directory tooling.

**Minimal remediation:** Run PR-controlled code on disposable workers with no state shared with release/signing jobs. Keep signing on separately controlled, fresh or integrity-verified infrastructure; do not share writable interpreters/tool caches across that boundary.

**Safe isolated reproduction:** In an offline disposable VM or temporary fixture HOME, create a throwaway Python venv without installing packages. Simulate the PR job placing a one-line `.pth` startup hook in that venv that writes only a marker containing a dummy environment value. Simulate the trusted signing job by starting the same interpreter with `FIXTURE_SECRET=not-a-secret`. A marker demonstrates the cross-job boundary failure. Never run this on an actual CI worker or use a real secret.

---

### RS-3 — High: Linux installer never verifies the “signed release manifest”

**CWE:** CWE-347, CWE-494  
**Locations:**
- `packaging/linux/install.sh:351-363` — extracts only SHA-256, size, and URL from the purported signed manifest.
- `packaging/linux/install.sh:440-465` — verifies only the downloaded file’s hash and length against those unauthenticated values.
- `packaging/linux/install.sh:490-520` — privileged package installation or installation of an executable AppImage.
- `Webservice/gateway/tests/test_install_script.py:114-120,330-344` — a manifest with signature `"c2ln"` is expected to install successfully.
- `Webservice/gateway/releases.py:668-714,1265-1294` — latest metadata and artifact bytes are served from stored release rows/files without client-independent reauthentication on these reads.

**Attacker prerequisite:** Control of both release metadata and artifact responses/storage, but not the release private key—for example, compromise of release-serving storage plus database metadata, or a response-serving endpoint. An on-path attacker additionally needs a compromised TLS trust path or use of the installer’s HTTP base-URL option. Merely replacing artifact bytes while leaving metadata unchanged is insufficient.

**Trust-edge failure:** The installer never consumes or verifies the manifest’s `signature` or pinned `key_id`. An attacker supplies matching catalogue/manifest hashes for their replacement artifact; all existing checks pass. Native package installation can execute maintainer scripts with root privileges. AppImage installation places executable attacker bytes into the requested prefix.

**Customer impact:** Installation of software not authorized by the release signing authority; root execution through malicious native-package installation, or user execution through the installed AppImage.

**Important limitation:** The usual `curl | sh` bootstrap already trusts the origin serving the script. This finding does **not** claim signatures protect a script already replaced by that same attacker. The broken boundary is a trusted/local bootstrap accepting unsigned artifact metadata, despite describing it as signed.

**Nearby controls considered:** HTTPS by default, catalogue/hash agreement, download-size checks, temporary-directory cleanup, SHA-256 enforcement, and atomic AppImage rename. These detect corruption or artifact-only substitution, not metadata-plus-artifact substitution. RPM behavior may supply additional distro-specific checks; the Debian and AppImage paths do not provide the missing Ed25519 verification.

**Minimal remediation:** Verify the artifact’s pinned-key Ed25519 signature before any installation, using the existing domain-separated preimage:
`chudware-artifact-v1\0key_id\0version\0platform\0arch\0sha256`.
Reject missing, malformed, and unknown-key signatures.

**Safe isolated reproduction:** Use the existing `_Gateway` and `run_install()` helpers in `Webservice/gateway/tests/test_install_script.py`. Start only their loopback fixture, use a temporary HOME/prefix, select `--format appimage`, and supply the module’s synthetic token. Its existing fixture signature `"c2ln"` is not an Ed25519 signature, yet the script installs the inert fixture bytes. Inspect the file only; do not execute it. Stop the fixture in `finally`.

The exact existing reproduction test is:
```text
Webservice/gateway/tests/test_install_script.py::test_a_real_appimage_install_lands_in_the_prefix_with_a_desktop_entry
```
Its current success expectation demonstrates the missing authenticity check; it is not evidence that authentication works.

---

### RS-4 — High, conditional: native SDK provisioning disables server authentication on supported older CMake versions

**CWE:** CWE-295, CWE-494  
**Deduplication:** RuntimeArtifact’s SC-2 covers mutable/unhashed bootstrap downloads. Merge the shared download-hash remediation there if desired. The additional defect here is the **concrete on-path TLS attack on supported pre-3.31 CMake**, rather than requiring takeover of an upstream publisher.

**Locations:**
- `core/cmake/ProvisionOCCT.cmake:33-57` — `file(DOWNLOAD)` without `TLS_VERIFY` or `EXPECTED_HASH`, extraction, then `find_package(OpenCASCADE REQUIRED)`.
- `core/cmake/ProvisionLib3mf.cmake:47-62` — same unauthenticated provisioning pattern.
- `CMakeLists.txt:10`, `core/CMakeLists.txt:1` — minimum CMake 3.20.
- `CMakePresets.json:3-6` — minimum declaration also permits versions below 3.31.
- `.github/workflows/nightly-release.yml:1185-1191` — Windows release build reaches CMake provisioning.
- Locally installed CMake documentation, `/usr/share/cmake/Help/variable/CMAKE_TLS_VERIFY.rst:5-13`, explicitly states the default changed from off to on in 3.31.

**Attacker prerequisite:** An on-path attacker during first SDK provisioning, using an affected pre-3.31 CMake installation with no externally configured TLS-verification override. The checked-in build configuration does not set that override. Actual deployed CMake versions were not inspected.

**Trust-edge failure:** HTTPS is not authenticated under the affected default. A replacement OCCT ZIP is extracted and its `OpenCASCADEConfig.cmake` is executed by `find_package()` during configure. There is no expected digest to reject the substituted archive. Supplied libraries can also enter packaged output.

**Customer impact:** Build-user code execution, compromise of release inputs, and distribution of substituted native libraries.

**Nearby controls considered:** Fixed version strings and expected archive layout do not authenticate archive contents. Cached archives are also accepted without a committed digest.

**Minimal remediation:** Explicitly require `TLS_VERIFY ON` and committed `EXPECTED_HASH` values; verify cached archives before reuse as well. Do not rely on the CMake version’s default.

**Safe isolated reproduction:** Avoid all downloads. In a temporary `OCCT_ROOT`, place a fixture `occt-combined.zip` containing the expected `opencascade-7.9.3-vc14-64/cmake/OpenCASCADEConfig.cmake`; its sole action should write a marker inside that same temporary directory. Run a script-mode wrapper with `WIN32=TRUE` and the temporary cached archive against the provisioning module. The marker demonstrates unauthenticated archive-to-CMake execution. The TLS-default portion is established by the versioned CMake documentation; reproducing an interception is unnecessary.

## Yield: overlapping evidence, policy gaps, and rejected candidates

### Supplementary evidence for RuntimeArtifact SC-2 — do not count twice
- `.github/workflows/runner-provision.yml:69-79`: downloads mutable `innounp-2.zip`, extracts it, and immediately invokes the contained executable without hash/signature verification. That executable prepares persistent Inno Setup tooling used for releases.
- `.github/workflows/chudcode-runtime-windows-build.yml:107-121,136-139`: downloaded Bun/rustup bootstrap execution.
- `scripts/fetch_cef_windows.ps1:28-42`: downloaded CEF archive accepted using existence/size checks, then extracted.
- `.github/workflows/runner-provision.yml:196-219`: analogous macOS CEF provisioning.

These require upstream/download-response control absent the separate CMake TLS issue; ordinary HTTPS network presence alone is not sufficient.

### Documentation defect, not a cryptographic implementation bypass
`docs/SIGNING.md:6,63-80,105-109` describes signing only the ASCII digest, a legacy signing-secret name, and upload-time signing. Current code uses domain-separated identity-bound requests and verifies detached signatures. Following the document can generate signatures rejected by current verifiers and misdirect key custody. Update the document; do not weaken the implementation to match it.

### Policy question, not ranked as an independent vulnerability
The updater uses `min_supported` to activate mandatory-update UI before artifact authentication (`host/src/update/updater.cpp:925-935`; UI at `host/src/app.cpp:8400-8429`). That field is outside the artifact signature. Whether the online gateway is intentionally authoritative for retirement policy needs an explicit decision; I did not assume the offline signing key must authorize that policy.

### Rejected as insufficiently grounded
- Verify-then-open/rollback mutation concerns confined to writable same-user updater state: no established lower-privilege-to-higher-privilege execution path.
- Developer-only trust keys and developer-build bypasses: compile-time guarded, with packaged build checks.
- Intentional unsigned preview/beta platform packages: not inherently a bypass.
- Generic action-pinning advice: reviewed workflow action references were pinned; no concrete expression-injection path survived source tracing.
- Historical rollout/signing claims: not treated as current deployment proof.

## Yield: positive controls

- Payload manifests are canonicalized, signature-checked against a dedicated release keyring, path-validated, and hashed; native integrity verification includes held-file-handle paths.
- Windows payload staging rejects runtime symlinks and missing required runtime closure; package builders verify signed payloads and snapshots.
- Updater code verifies artifact identity, size, digest and signature, checks inner payload metadata, and maintains staged transaction/rollback state.
- `packaging/publish.py` verifies detached artifact, payload, and evidence signatures; its download command recomputes the artifact digest and verifies its signature.
- Offline signing uses domain-separated requests; CI signing supports checking the expected public key.
- Safe handoff extraction rejects traversal and links.
- macOS packaging uses `set -euo pipefail`, nested/outer code-signature verification, notarization submission, stapling validation, and Gatekeeper assessment.
- AppImage tooling includes a pinned hash; several other provisioning paths pin immutable commits or hashes.
- Workflow token permissions and checkout credential persistence are substantially more restrictive than unrestricted defaults, though they do not solve RS-2.

## Yield: reviewed paths and limits

### Reviewed subareas
1. **All 13 workflow files:** `fast-gate`, `ci-workflow-policy`, `linux-gate`, `native-build`, `nightly-release`, `release`, `runner-provision`, `runner-smoke`, `chudcode-runtime-windows-build`, `deploy-prod`, `site-build`, `stripe-sandbox`, `prod-external-watch`.
2. **Signing/publication:** `packaging/{sign_release,ci_sign,publish,finalize_release,generate_release_evidence}.py`; related evidence/signing tests; `packaging/ci_gateway.sh`; protected-asset and sidecar build paths.
3. **Platform packaging:** Linux bootstrap/AppImage/native-package scripts; Windows publication script, Inno/WiX definitions and sidecar staging; macOS app builder and `Info.plist`.
4. **Installer:** `services/installer/{payload_manifest,build_release,build_msi,provision,register_mcp,setup_mcp_clients,setup_file_assoc}.py`; install/uninstall PowerShell; relevant installer tests and provisioning helpers.
5. **Native security:** every `core/security` implementation/header subarea—integrity, keyring, runtime guard, containment, event queue.
6. **Updater dependencies:** `host/src/update/updater.cpp`, platform apply implementations, Windows update helper; targeted updater/security tests and mandatory-update consumers.
7. **Build/provisioning dependencies:** root/core/host CMake configuration, presets; OCCT/lib3mf/PolyMesh/openNURBS/CEF provisioning; `scripts/pin_chudcode.py`, CEF fetcher, ARM SDK builder and Windows runtime builder; relevant Renode/developer provisioning portions.
8. **Documentation and active consumers:** `docs/SIGNING.md`, CI runner reference, gateway installer serving/tests, and relevant release publication/serving controls.

Review depth varied: direct trust-edge implementations were read in detail; peripheral definitions were inventoried and searched with relevant ranges inspected.

### Explicit limits
- No live repository-policy, runner ACL/reimage, certificate, key-custody, bucket, CDN, or production deployment evidence.
- No dependency advisory scan; owned by parent.
- No credential files/values inspected.
- No exhaustive cryptographic-library audit or binary/platform signature validation.
- No live updater, installer, customer-device, or manufacturing-safety exercise.
- Concurrent edits can move line numbers; locations reflect source read during this audit.

### Suggested existing checks for integrator discretion — not run
- `packaging/test_finalize_release.py::test_safe_extract_rejects_traversal`
- `packaging/test_finalize_release.py::test_unsigned_stable_windows_rejected_but_beta_truthful` — retain, but insufficient for RS-1.
- `packaging/test_release_signing.py::test_publish_rejects_wrong_key_and_digest`
- `packaging/test_release_signing.py::test_artifact_request_must_carry_the_full_verifier_identity`
- `packaging/test_release_signing.py::test_ci_signer_refuses_a_seed_that_is_not_the_expected_release_key`
- `services/installer/test_build_release_closure.py::test_windows_stage_requires_verified_chudcode_artifact`
- The isolated Linux bootstrap test named under RS-3.

**Coverage summary:** Four source-supported trust-boundary defects identified, including one TLS-specific addition overlapping SC-2. The Windows finalizer failure propagation is the strongest offline-signing-boundary defect; shared PR/release runner state and the Linux bootstrap’s absent signature verification are separate root causes. No verification was exercised.
