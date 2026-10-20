# Release signing and notarization

Chudware has two independent trust layers:

1. **Platform signing** makes Windows SmartScreen and macOS Gatekeeper recognize the publisher.
2. **Release signing** creates the Ed25519 `.sig` consumed by the in-app updater. It signs the 64 lowercase ASCII characters of the artifact's SHA-256 digest, not the 32 raw digest bytes. The `.sig` file and release-manifest `signature` field use standard RFC 4648 base64.

Unsigned packages remain buildable for internal testing. Every packager emits exactly:

```text
WARNING: unsigned build — SmartScreen/Gatekeeper will warn users
```

An unsigned package is not a production trust signal. Publish it only through a controlled test channel and verify its SHA-256 and Ed25519 signature.

## CI custody prerequisite

Do not enable signing based on workflow YAML, a fresh virtualenv, or separate
runner users. [CI runner trust-boundary requirements](reference/CI_RUNNERS.md#execution-trust-boundary-supplychain-rs-2)
are a release gate: authenticated restricted runner-group/workflow/ref
configuration, one-job disposable branch workers, clean protected OS images,
and externally checked interpreter/native-tool/SDK identities are required.
No signing credentials may be delivered before that evidence is approved.

The checked-in boundary is `chudware-untrusted-ephemeral` versus
`chudware-trusted-protected`. All platform and CI release credentials belong
only to the protected fleet's `chudware-release-protected` environment, with
protected source refs and independent approval. Remove signing, R2 and gateway
credentials from repository/organization secret scopes visible to branches;
changing a workflow label does not revoke those secrets. Rebuild or quarantine
previously shared Windows/Linux signing state before restoring credential
access. A profile/Python/tool cache writable by a PR must never be mounted or
reused by a signing job.

Protected images must export the externally qualified absolute
`CHUDWARE_CI_BASE_PYTHON` and reviewed offline `CHUDWARE_WHEELHOUSE`.
Nightly and reusable release builders provision a job-local interpreter from
the authenticated complete wheel snapshot and check all applicable
`packaging/requirements-release.txt` compiler inputs before use. Never repair
a missing compiler by publicly syncing pip packages into a signing interpreter
or accepting an old home-directory venv by its import capabilities alone.
The separately trusted base inspects candidate executable/launcher/pyvenv.cfg
and installed package bytes offline before any candidate Python launch;
bootstrap removes inherited Python/loader injection environment variables.

Stable Windows/macOS prepared handoffs additionally retain the paired files
selected by `CHUDWARE_PLATFORM_PUBLISHER_POLICY` and
`CHUDWARE_PLATFORM_PUBLISHER_POLICY_SIGNATURE` as
`platform-publishers.v1.json` / `.sig`, with both hashes in `locked_inputs`.
The finalizer authenticates this policy against the fixed offline owner root
and verifies the actual certificate/Apple team and bundle identity. A
`CHUDWARE_SIGN_IDENTITY` subject selector is not publisher authority. These
nonsecret, owner-authenticated files and real certificate/team inputs must be
supplied operationally; source does not invent trusted publisher identities.

Nightly Windows/macOS builds emit a signed native-publisher byproduct in SLSA.
When the owner policy pair and platform signing/notarization inputs exist, the
native evidence writer verifies the actual artifact before a positive report
is attached. An attempted native verification failure stops the job; it cannot
be downgraded to a negative report. Missing native inputs instead produce an
explicit signed `verified:false` report: beta testing remains possible, but
stable promotion must reject it. This is distinct from missing release/test
evidence, which is always a failure.

Tagged `release.yml` calls the protected reusable `release-prepare.yml`.
Authorize the defining reusable builder and nested Stripe workflow at each
exact approved tag, not merely the caller or a wildcard. It prepares bounded
offline-owner signing requests; it never receives the offline owner's private
key. Nightly signing uses its separate CI key only after the same operational
runner gate. Native build/test jobs stay disposable even for release tags;
their outputs are not downloaded into protected signing jobs.

## Windows: what to buy

Buy a currently trusted **Authenticode code-signing certificate** for the legal entity that publishes Chudware. An OV certificate is sufficient for cryptographic publisher identity; an EV certificate can improve reputation bootstrapping but does not remove every SmartScreen reputation check. The product needs Authenticode/code-signing use, SHA-256 support, and RFC 3161 timestamping—not a TLS/SSL certificate.

Current CA/Browser Forum rules require newly issued public code-signing private keys to be held in approved hardware or a compliant cloud-signing service. Choose one of:

- a CA-provided USB hardware token usable by `signtool.exe` through the Windows certificate store, or
- a CA/cloud HSM service that exposes a `signtool.exe`-compatible certificate/provider on the self-hosted Windows runner.

Do not export or commit a PFX merely to simplify CI. Install the certificate only for the clean, qualified `cc-win-package` account in `chudware-trusted-protected`, in `Cert:\CurrentUser\My`, including the private-key provider and chain. Ensure `signtool.exe` from the Windows 10/11 SDK is installed. Never expose that store/provider to a disposable worker.

Set these GitHub Actions secrets/environment values:

| Value | Where it goes | Meaning |
|---|---|---|
| `CHUDWARE_SIGN_IDENTITY` | `chudware-release-protected` environment secret for the Windows packaging job | Exact certificate subject passed to `signtool sign /n`, for example `Chudware, Inc.` |
| `SIGNTOOL` | Optional runner environment (not a secret) | Full path to `signtool.exe` when it is not on `PATH` |

`services/installer/build_release.py` invokes Inno Setup, then signs and verifies `Chudware-Setup-<version>-x86_64.exe` with SHA-256 and DigiCert's RFC 3161 timestamp service. Buying or rotating the certificate changes runner provisioning/secrets, not packaging code.

## macOS: what to buy

Enroll the publishing legal entity in the **Apple Developer Program** and issue a **Developer ID Application** certificate. Do not buy/use a Mac App Distribution or Mac Installer Distribution certificate: Chudware ships outside the Mac App Store as a signed `.app` inside a zip.

Install the Developer ID Application certificate and private key only in the keychain of a clean, qualified protected Mac packaging runner, never the OS/home/tooling used by disposable test workers. Unlock that keychain for non-interactive signing and grant `/usr/bin/codesign` access to the key. Set:

| Value | Where it goes | Meaning |
|---|---|---|
| `CHUDWARE_SIGN_IDENTITY` | `chudware-release-protected` environment secret | Full `codesign` identity, normally `Developer ID Application: Legal Entity (TEAMID)` |
| `CHUDWARE_NOTARIZE_PROFILE` | `chudware-release-protected` environment secret | Name of a `notarytool` keychain profile stored only on the protected runner |

Create the notarization profile once per runner. Either use an App Store Connect API key with Developer access, or an Apple ID plus team ID and app-specific password. For the Apple-ID route:

```bash
xcrun notarytool store-credentials chudware-notary \
  --apple-id RELEASES@EXAMPLE.COM \
  --team-id TEAMID \
  --password APP_SPECIFIC_PASSWORD
```

Enter `chudware-notary` as `CHUDWARE_NOTARIZE_PROFILE`; never put the Apple password itself in that variable. `packaging/macos/build_app.sh` applies hardened-runtime signing, submits the zip with `notarytool --wait`, staples the accepted ticket to the app, and recreates the canonical zip.

## Ed25519 release key: what to create

Generate an offline Ed25519 owner release key dedicated to software updates. This is not an Authenticode or Apple certificate and cannot be purchased from a CA. Keep the owner private key encrypted and offline; never put it in Actions. The automatic nightly key is a separate CI identity and must be stored only in the protected release environment, after the runner boundary is qualified.

One generation method is:

```bash
openssl genpkey -algorithm ED25519 -out chudware-release-ed25519.pem
openssl pkey -in chudware-release-ed25519.pem -pubout -out chudware-release-ed25519.pub.pem
```

Set:

| Value | Where it goes | Meaning |
|---|---|---|
| Offline owner release key | Encrypted offline owner custody, not GitHub Actions | Used by `packaging/sign_release.py` for the bounded requests produced by `release-prepare.yml` |
| `CHUDWARE_RELEASE_CI_KEY` | `chudware-release-protected` environment secret | Separate automatic nightly CI signing identity; not the offline owner key |
| Release public key | Desktop updater trust store/build configuration | Public half corresponding to the private key; it is safe to distribute but changes require an updater trust-key rotation |

The offline owner signs verified request bytes using `packaging/sign_release.py`. Manual `packaging/publish.py upload` tooling accepts its signing key through the operator's controlled process environment; it is not a reason to place the owner key in repository secrets. Private keys must never reach R2, the gateway, or branch workers.

## R2 and gateway publication values

These are transport/authentication secrets, not signing identities:

| Value | Where it goes |
|---|---|
| `R2_ENDPOINT` | Release job environment; account-specific S3 endpoint |
| `R2_ACCESS_KEY_ID` | `chudware-release-protected` environment secret, scoped to `chudware-releases` |
| `R2_SECRET_ACCESS_KEY` | Matching protected release environment secret |
| `CHUDWARE_GATEWAY_URL` | Release registration job environment, for example the production gateway origin |
| `CHUDWARE_GATEWAY_RELEASE_TOKEN` | `chudware-release-protected` environment secret accepted by release publication endpoints |

R2 is not enabled for the account yet. Publication intentionally fails with an actionable message until R2 is enabled, the `chudware-releases` bucket exists, and the three R2 values are configured.

## Rotation and verification

- Renew platform certificates before expiry; timestamped older Windows releases remain verifiable after certificate expiry.
- Rotate an R2 or gateway credential without rebuilding artifacts.
- Treat Ed25519 private-key rotation as a product trust migration: ship the new public key through an update signed by the old key before using the new private key.
- Verify Windows with `signtool verify /pa /v <installer>`.
- Verify macOS with `codesign --verify --deep --strict --verbose=2 Chudware.app` and `spctl --assess --type execute --verbose=4 Chudware.app`.
- Verify a release sidecar by hashing the artifact, then verifying the base64-decoded `.sig` over the lowercase ASCII digest with the pinned Ed25519 public key.
