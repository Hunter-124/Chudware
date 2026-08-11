# Release signing and notarization

Chudware has two independent trust layers:

1. **Platform signing** makes Windows SmartScreen and macOS Gatekeeper recognize the publisher.
2. **Release signing** creates the Ed25519 `.sig` consumed by the in-app updater. It signs the 64 lowercase ASCII characters of the artifact's SHA-256 digest, not the 32 raw digest bytes. The `.sig` file and release-manifest `signature` field use standard RFC 4648 base64.

Unsigned packages remain buildable for internal testing. Every packager emits exactly:

```text
WARNING: unsigned build — SmartScreen/Gatekeeper will warn users
```

An unsigned package is not a production trust signal. Publish it only through a controlled test channel and verify its SHA-256 and Ed25519 signature.

## Windows: what to buy

Buy a currently trusted **Authenticode code-signing certificate** for the legal entity that publishes Chudware. An OV certificate is sufficient for cryptographic publisher identity; an EV certificate can improve reputation bootstrapping but does not remove every SmartScreen reputation check. The product needs Authenticode/code-signing use, SHA-256 support, and RFC 3161 timestamping—not a TLS/SSL certificate.

Current CA/Browser Forum rules require newly issued public code-signing private keys to be held in approved hardware or a compliant cloud-signing service. Choose one of:

- a CA-provided USB hardware token usable by `signtool.exe` through the Windows certificate store, or
- a CA/cloud HSM service that exposes a `signtool.exe`-compatible certificate/provider on the self-hosted Windows runner.

Do not export or commit a PFX merely to simplify CI. Install the certificate for the dedicated `cc-win-package` runner account in `Cert:\CurrentUser\My`, including the private-key provider and chain. Ensure `signtool.exe` from the Windows 10/11 SDK is installed.

Set these GitHub Actions secrets/environment values:

| Value | Where it goes | Meaning |
|---|---|---|
| `CHUDWARE_SIGN_IDENTITY` | GitHub Actions secret/environment for the Windows packaging job | Exact certificate subject passed to `signtool sign /n`, for example `Chudware, Inc.` |
| `SIGNTOOL` | Optional runner environment (not a secret) | Full path to `signtool.exe` when it is not on `PATH` |

`services/installer/build_release.py` invokes Inno Setup, then signs and verifies `Chudware-Setup-<version>-x86_64.exe` with SHA-256 and DigiCert's RFC 3161 timestamp service. Buying or rotating the certificate changes runner provisioning/secrets, not packaging code.

## macOS: what to buy

Enroll the publishing legal entity in the **Apple Developer Program** and issue a **Developer ID Application** certificate. Do not buy/use a Mac App Distribution or Mac Installer Distribution certificate: Chudware ships outside the Mac App Store as a signed `.app` inside a zip.

Install the Developer ID Application certificate and private key in the login keychain of each dedicated self-hosted Mac packaging runner. Unlock that keychain for non-interactive signing and grant `/usr/bin/codesign` access to the key. Set:

| Value | Where it goes | Meaning |
|---|---|---|
| `CHUDWARE_SIGN_IDENTITY` | GitHub Actions secret/environment for each macOS packaging job | Full `codesign` identity, normally `Developer ID Application: Legal Entity (TEAMID)` |
| `CHUDWARE_NOTARIZE_PROFILE` | GitHub Actions secret/environment for each macOS packaging job | Name of a `notarytool` keychain profile stored on that runner |

Create the notarization profile once per runner. Either use an App Store Connect API key with Developer access, or an Apple ID plus team ID and app-specific password. For the Apple-ID route:

```bash
xcrun notarytool store-credentials chudware-notary \
  --apple-id RELEASES@EXAMPLE.COM \
  --team-id TEAMID \
  --password APP_SPECIFIC_PASSWORD
```

Enter `chudware-notary` as `CHUDWARE_NOTARIZE_PROFILE`; never put the Apple password itself in that variable. `packaging/macos/build_app.sh` applies hardened-runtime signing, submits the zip with `notarytool --wait`, staples the accepted ticket to the app, and recreates the canonical zip.

## Ed25519 release key: what to create

Generate one offline Ed25519 release key dedicated to software updates. This is not an Authenticode or Apple certificate and cannot be purchased from a CA. Keep the private key only in the protected GitHub Actions secret used by release jobs; keep an encrypted offline recovery copy with restricted access.

One generation method is:

```bash
openssl genpkey -algorithm ED25519 -out chudware-release-ed25519.pem
openssl pkey -in chudware-release-ed25519.pem -pubout -out chudware-release-ed25519.pub.pem
```

Set:

| Value | Where it goes | Meaning |
|---|---|---|
| `CHUDWARE_RELEASE_SIGNING_KEY` | GitHub Actions secret for package-upload jobs | The PEM private key, base64-encoded PEM, or base64 encoding of a 32-byte Ed25519 seed |
| Release public key | Desktop updater trust store/build configuration | Public half corresponding to the private key; it is safe to distribute but changes require an updater trust-key rotation |

`packaging/publish.py upload` writes `<artifact>.sha256` and `<artifact>.sig` and uploads all three objects. It never sends the private key to R2 or the gateway.

## R2 and gateway publication values

These are transport/authentication secrets, not signing identities:

| Value | Where it goes |
|---|---|
| `R2_ENDPOINT` | Release job environment; account-specific S3 endpoint |
| `R2_ACCESS_KEY_ID` | GitHub Actions secret from an R2 Object Read & Write token scoped to `chudware-releases` |
| `R2_SECRET_ACCESS_KEY` | Matching GitHub Actions secret |
| `CHUDWARE_GATEWAY_URL` | Release registration job environment, for example the production gateway origin |
| `CHUDWARE_GATEWAY_RELEASE_TOKEN` | GitHub Actions secret accepted by `POST /api/releases/latest/publish` |

R2 is not enabled for the account yet. Publication intentionally fails with an actionable message until R2 is enabled, the `chudware-releases` bucket exists, and the three R2 values are configured.

## Rotation and verification

- Renew platform certificates before expiry; timestamped older Windows releases remain verifiable after certificate expiry.
- Rotate an R2 or gateway credential without rebuilding artifacts.
- Treat Ed25519 private-key rotation as a product trust migration: ship the new public key through an update signed by the old key before using the new private key.
- Verify Windows with `signtool verify /pa /v <installer>`.
- Verify macOS with `codesign --verify --deep --strict --verbose=2 Chudware.app` and `spctl --assess --type execute --verbose=4 Chudware.app`.
- Verify a release sidecar by hashing the artifact, then verifying the base64-decoded `.sig` over the lowercase ASCII digest with the pinned Ed25519 public key.
