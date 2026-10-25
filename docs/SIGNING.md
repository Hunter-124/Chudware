# Release signing

Chudware releases carry two independent kinds of signature:

1. **Release signatures (required).** Ed25519 signatures over every payload
   manifest and every update manifest. The application and the in-app updater
   verify them against public keys compiled into
   `core/security/release_keys.h`. They are what makes an update trustworthy.
2. **Platform code signing (optional).** Authenticode on Windows and a
   Developer ID signature plus notarization on macOS. These only affect
   SmartScreen and Gatekeeper prompts; they add no trust the updater relies on.

[RELEASING.md](RELEASING.md) describes the release workflow that uses these
values; [reference/CI.md](reference/CI.md) lists every secret.

## Ed25519 release keys

`core/security/release_keys.h` trusts two keys:

| Key id | Private half | Used by |
|---|---|---|
| `release-ci-v1` | `CHUDWARE_RELEASE_SIGNING_KEY` repository secret | `release.yml`, through `packaging/ci_sign.py` and `packaging/github_release_manifest.py` |
| `release-v1` | Age-encrypted file kept offline by the maintainer | Signing a prepared request by hand with `packaging/sign_release.py` |

The secret is the canonical base64 encoding of the 32-byte Ed25519 seed, not a
PEM file. Every signing tool takes the expected public key and refuses to sign
if the seed does not match it.

### Creating a key

Generate the seed on a trusted machine and send it straight to the repository
secret:

```bash
umask 077
python3 - > release-key.b64 <<'PY'
import base64
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
key = Ed25519PrivateKey.generate()
seed = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                         serialization.NoEncryption())
public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
print(base64.b64encode(seed).decode())
print("public key:", public.hex(), file=__import__("sys").stderr)
PY
gh secret set CHUDWARE_RELEASE_SIGNING_KEY --repo Hunter-124/Chudware < release-key.b64
shred -u release-key.b64
```

Keep an encrypted offline backup of the seed if losing it would be a problem;
GitHub secrets cannot be read back.

### Rotating a key

Clients only trust keys compiled into them, so rotation takes two releases:

1. Add the new public key under a new id (for example `release-ci-v2`) to
   `core/security/release_keys.h` and to `RELEASE_PUBLIC_KEYS` in
   `packaging/github_release_manifest.py`. Release that build, signed with the
   old key.
2. Once users have updated, replace the secret, set `CHUDWARE_RELEASE_KEY_ID`
   and `CHUDWARE_RELEASE_PUBLIC_KEY` in `.github/workflows/release.yml` to the
   new key, and release again.

Remove the old key from the trust ring only after no supported version still
needs it. If a private key leaks, remove its public key in the next release
and publish a security advisory directing users to a verified replacement.
Already-installed clients cannot have their compiled trust keys remotely revoked.
Updates remain optional; there is no minimum-version lockout.

## Windows code signing (optional)

Without a certificate the release ships unsigned installers, the build prints
`WARNING: unsigned build — SmartScreen/Gatekeeper will warn users`, and the
release notes say so.

To sign, obtain an Authenticode code-signing certificate (OV or EV) for the
publishing entity and store it as two repository secrets:

| Secret | Value |
|---|---|
| `CHUDWARE_WINDOWS_CERTIFICATE` | Base64-encoded PFX containing the certificate, its chain and private key |
| `CHUDWARE_WINDOWS_CERTIFICATE_PASSWORD` | The PFX password |

The `windows` job imports the PFX into the runner's `CurrentUser\My` store and
exports the certificate's subject as `CHUDWARE_SIGN_IDENTITY`.
`services/installer/build_release.py` and `services/installer/build_msi.py` then
run `signtool sign /n <subject> /fd SHA256 /tr http://timestamp.digicert.com /td SHA256`
and `signtool verify /pa` on the installer and the MSI.

Certificates issued since 2023 keep their private key in a hardware token or a
cloud HSM and cannot be exported as a PFX. To use one of those, replace the
import step in `release.yml` with the provider's signtool integration and keep
exporting the certificate subject as `CHUDWARE_SIGN_IDENTITY`.

Check a signed installer with `signtool verify /pa /v Chudware-Setup-<v>-x86_64.exe`.

## macOS signing and notarization (optional)

Without a certificate the app carries no Developer ID signature. Gatekeeper
blocks the first launch until the user allows it under **System Settings >
Privacy & Security > Open Anyway**, and the release notes say so.

To sign, enroll in the Apple Developer Program, create a **Developer ID
Application** certificate, export it with its private key as a `.p12`, and set:

| Secret | Value |
|---|---|
| `CHUDWARE_MACOS_CERTIFICATE` | Base64-encoded `.p12` |
| `CHUDWARE_MACOS_CERTIFICATE_PASSWORD` | The `.p12` password |
| `CHUDWARE_NOTARY_APPLE_ID` | Apple ID used for notarization |
| `CHUDWARE_NOTARY_TEAM_ID` | Developer team ID |
| `CHUDWARE_NOTARY_PASSWORD` | App-specific password for that Apple ID |

The `macos` job imports the identity into a temporary keychain, exports it as
`CHUDWARE_SIGN_IDENTITY`, and stores the notarization credentials as the
keychain profile named in `CHUDWARE_NOTARIZE_PROFILE`.
`packaging/macos/build_app.sh` signs every nested Mach-O file and the bundle
with the hardened runtime, submits the zip with `notarytool --wait`, staples
the ticket and rebuilds the zip. It does not re-sign the bundled `chud`
runtime, so notarization also needs a Chudcode release whose macOS binary
already carries a valid Developer ID signature. The notarization secrets are
optional; without them the app is signed but not notarized.

Check a signed app with:

```bash
codesign --verify --deep --strict --verbose=2 Chudware.app
spctl --assess --type execute --verbose=4 Chudware.app
```

## Linux

AppImage, `.deb` and `.rpm` packages are not GPG-signed. Their integrity comes
from `SHA256SUMS`, from the signed update manifest for the AppImage, and from
the signed payload manifest that `chudware-hostd` verifies at startup.
