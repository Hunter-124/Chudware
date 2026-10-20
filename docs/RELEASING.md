# Releasing Chudware

Two pipelines, one product. Know which one you are in.

| | `nightly-release.yml` | `release.yml` |
|---|---|---|
| Trigger | 09:17 UTC nightly + manual dispatch | `v*` tag push + manual dispatch |
| Channel | **beta only** (the gateway rejects anything else) | beta or stable |
| Signing key | `release-ci-v1`, in CI, from a GitHub secret | `release-v1`, offline, age-encrypted, owner workstation |
| Human work | protected-environment approval, then admin promotion | protected approval and offline owner ceremony |

The configured nightly flow publishes beta releases; `release.yml` prepares
bounded handoffs and stops for the owner's offline signing ceremony.
`POST /api/releases/ci/publish` is beta-only; stable promotion requires the
admin endpoint and its evidence/publisher gates.

Both flows require the [execution trust boundary](reference/CI_RUNNERS.md#execution-trust-boundary-supplychain-rs-2):
branch/PR code runs only on `chudware-untrusted-ephemeral`; release, signing,
control and maintenance use the disjoint `chudware-trusted-protected` group
and their separate protected environments. Labels or YAML guards alone are
not authorization. Exact defining-workflow/ref restrictions, independently
authenticated clean images, environment approvals and secret migration need
external evidence before release execution. Old runner inventories or passing
local policy fixtures do not establish that authority.

This document describes source-enforced release contracts and operator commands,
not a current production-readiness attestation. Source/unit/local-fixture proof
and even an owned real-browser/SDK run do not establish genuine signing,
authenticated CI/images, approved private runtime wheels, installed host/ingress/
rotation/recovery policy, or genuine provider account/signature/cache authority.
Retain historical vulnerable audit evidence unchanged; new source controls do not
retroactively prove that the exposed deployment was isolated or remediated.

## The automatic flow

Every night, and on demand from the Actions tab:

Nightly runs share one concurrency group with cancellation disabled. A new scheduled
or manual run queues rather than cancelling the platform legs of an in-flight release.


1. **`plan`** (gateway host, `cc-linux-livingroom`; metadata and API calls
   only, it compiles nothing) reads `GET https://app.chudware.com/api/releases/base-version`
   for the stable base (highest stable patch + 1, floor `0.1.0`) and builds the
   nightly version `<base>-beta.<run number>` — e.g. `0.1.0-beta.42`. On a
   *scheduled* run it asks the gateway over loopback whether `ci_builds` already
   holds a `published` row for `HEAD`; if so the night is skipped. A manual
   dispatch always builds. Every Chudcode-backed build leg is recorded as
   `queued`: `linux/x86_64`, `windows/x86_64` and `macos/x86_64`.
2. **`linux-beta`** (`cc-linux-build`, the private NVMe build guest; never the
   serving host) configures core and host with
   `-DCHUDWARE_VERSION_OVERRIDE=<version> -DCHUDWARE_DEVELOPER_BUILD=OFF`, builds,
   runs `ctest` for core and host, takes a headless `CHUDWARE_SHOT=m1` screenshot
   under Xvfb, stages the immutable AppDir via
   `packaging/linux/build_appimage.sh --prepare-signing`, signs the payload
   manifest with `packaging/ci_sign.py`, and lets `packaging/finalize_release.py`
   embed the signature, build the AppImage and generate the SBOM, SLSA provenance
   and platform-test evidence. The finished AppImage is then extracted and
   verified — zero `not found` in `ldd`, `chudware-hostd --status` loads and
   reports this exact version, and the signed payload manifest matches the
   extracted bytes — before anything is offered to a customer. It then packages
   that same verified tree as `chudware_<version>_amd64.deb` and
   `chudware-<version>-<release>.x86_64.rpm` via
   `packaging/linux/build_linux_packages.sh`, signs each package's own digest
   under both the beta and the stable version, and publishes them with
   `format` `deb` / `rpm`, with separate evidence bound to each package's actual
   bytes. Both formats are required: native packaging, test/evidence, signing
   or publication failures fail the job rather than omit a format. They need
   `rpm-build` and `xz` on the qualified protected Linux image.
3. **`windows-beta`** (`cc-win-package`) does the same for Windows: junctions,
   protected assets, `windows-occt` preset plus the host GUI at the pinned
   version, real core/host CTest results, compiled sidecars, the **Inno Setup**
   installer `Chudware-Setup-<version>-x86_64.exe`, and the per-machine MSI.
   Both Inno Setup and modern WiX are hard image prerequisites. Each artifact
   receives its own byte-bound evidence/signatures; native failure cannot fall
   back to publishing only the other format or a zip.
4. **`macos-beta`** (`cc-mac-intel`, x86_64) configures the `macos-x86_64`
   core preset and `host/build-macos-x86_64` at the pinned version, runs
   `ctest` for core and host, then packages with
   `packaging/macos/build_app.sh`: `--prepare-signing` retains the immutable
   `Chudware.app` and emits the payload preimage, `packaging/ci_sign.py` signs
   it, and `--finalize-signing` seals that exact bundle and archives it with
   `ditto` as `Chudware-<version>-macos-x86_64.zip`. The finished zip is then
   expanded and verified before anything is offered to a customer:
   `Chudware.app` is present with its `payload-manifest.v1.json` + `.sig`
   verifying against `release-ci-v1`, `otool -L` shows no shipped Mach-O file
   loading an absolute path outside `/System` or `/usr/lib`, and
   `chudware-hostd --status` loads and reports this exact version. That is
   the same shape `host/src/update/apply_macos.cpp` requires of an update: it
   expands the zip with `ditto -x -k`, refuses an archive whose root is not
   `Chudware.app`, and re-verifies the in-bundle payload manifest before
   swapping the app.

   The Intel Mac has Command Line Tools only, so no step here uses
   `xcodebuild`, and the nightly beta is signed but unnotarized: it makes no
   Gatekeeper claim. `macbook-air-m2` can still run the CAD core-only
   `macos-arm64` preset, but no Mac ARM Chudcode builder or release artifact
   exists; it cannot package a Chudcode-backed release.
5. **Publish.** Every build job gets its bytes into `<releases_dir>/incoming/`
   on the gateway host through the authenticated HTTP API and then calls
   `POST /api/releases/ci/publish`. No builder reads `gateway.env`, holds a
   production SSH credential or writes the gateway's release directory; only
   `plan`, which runs on the gateway host and builds nothing, reads the token
   from `gateway.env` and talks to **`http://127.0.0.1:8300`**.
   * **`linux-beta`, `windows-beta` and `macos-beta`** hold the same token as
     the `CHUDWARE_GATEWAY_RELEASE_TOKEN` secret and reach the gateway over TLS
     on `https://app.chudware.com`: the artifact and each payload-manifest,
     signature and evidence sidecar is streamed in under its canonical name with
     `PUT /api/releases/ci/upload/<name>` (curl `--upload-file`, so a 300 MB
     installer is never buffered in memory), and the job compares the sha256
     the endpoint reports against the local digest before moving on. A missing
     local file or a failed upload fails that publish. Then the ordinary
     `ci/publish` and `ci/status` calls follow over the same origin, with the
     body's digests computed from the same local `RUNNER_TEMP` files that were
     uploaded. Those runners need nothing but outbound 443. The Linux and macOS
     legs share `packaging/ci_gateway.sh`; Windows uses
     `packaging/windows/publish_release.ps1`.

     Anything larger than 64 MiB goes up in 64 MiB chunks
     (`?offset=<n>`, and `&complete=1` on the last one), because the proxy in
     front of the public origin answers **413** for bodies over 100 MB and the
     Windows installer is ~230 MB. The offset must equal the bytes already
     staged, so a retried or interleaved chunk is rejected instead of corrupting
     an artifact, and only the finalizing call reports a digest.

   On every host the token reaches curl through a `0600` `--config` file, never
   on a command line. The gateway recomputes the digest and size, verifies both
   signatures, and moves everything into `<releases_dir>/store/beta/<version>/`.
6. **Status.** Every transition (`queued`, `building`, `testing`, `packaging`,
   `publishing`, `published`, `failed`) is posted to `POST
   /api/releases/ci/status` and shows up in `/admin` under Dashboard and
   Releases -> CI builds. A failed job reports `failed` from an `if: always()`
   step, so a red night is visible in the admin UI, not only in Actions.

### Two signatures per build

Each build signs the same artifact bytes twice: once as `<base>-beta.<run>` and
once as `<base>`. The beta signature publishes the beta row; the stable signature
rides along on that row (`releases.stable_version` / `stable_signature`) so
promotion needs no rebuild and no new signature. Both use key id
`release-ci-v1`, public key
`d7434dc67f4d6e11cd4c8ced9b5ebbccf2e1477b5949d52d7685b6ca43a010f6`.

### Evidence

Every platform runs both actual CTest suites through
`scripts/run_release_ctest.py`, using `--no-tests=error` and retained JUnit/logs.
`test-results.json` records actual process exits and executed/failed/skipped
counts; an empty, all-disabled, failed or malformed suite is not passing
evidence. Prepared handoffs hash-bind the results and both suites' JUnit/log
files alongside their build locks.

`packaging/generate_release_evidence.py --test-results ...` independently
recomputes counts and verifies those raw-file digests before creating
artifact/commit-bound SBOM, provenance and platform-test documents. They are
signed only after successful native verification, and their digests are sent
in the publish body. Command names or a supplied `passed` flag cannot replace
the actual results.

Any native finalization, test, evidence-generation or evidence-signing failure
stops release signing/publication. Retained installer/AppImage bytes after a
failed command are never salvaged as a publishable beta. There is no
evidence-less channel or missing-evidence publication fallback.

Browser release acceptance is a separate end-to-end surface: the actual WASM
canvas, tenant-safe CAD save/reopen with persisted solve state, SDK streaming and
session identity, work census and safe cutover must be exercised in an owned
disposable environment. A source test, HTTP hello, local provider-wire fixture or
prerequisite skip is not that acceptance. Fixture thinking/signature byte
preservation and cache-prefix declarations do not verify a genuine provider
thinking signature or actual provider cache hit. Record observed failures and
unavailable capabilities explicitly, never as passing/zero state.

### Operator native qualification

`python packaging/qualify_native_release.py inspect --platform windows` (or
`--platform macos`) reports required tools, publisher policy, retained CTest
inputs, and the complete qualification command without running native tools.
On the genuine target OS, run the emitted `qualify ARTIFACT ... --out NEW_DIRECTORY`
command separately for each EXE, MSI, or archived app. It reuses the owner-policy
verifier, checks private artifact snapshots and retained input identities, and
emits the existing unsigned evidence and signing requests. It never signs,
publishes, or grants promotion authority. Linux/Wine inspection cannot substitute
for Windows Authenticode or macOS notarization; absent native tools and authorized
publisher fixtures remain explicit prerequisites. Native verifier subprocesses
fail on nonzero exit, timeout, excess output, or mutation rather than retaining
partial evidence.

## Promoting beta to stable in /admin

1. Open `https://app.chudware.com/admin` and go to **Releases**.
2. Find the beta row for the version you want. Rows carry two badges: a stable
   pair badge (the build signed a stable version too) and an evidence badge.
   **Promote to stable** appears only when the gateway reports the row
   promotable: Linux needs the signed pair and evidence; Windows and macOS also
   need a verified native publisher attestation.
3. Click **Promote to stable**. The gateway captures the artifact and every
   sidecar into a private snapshot, authenticates the payload manifest, SBOM,
   provenance and platform-test signatures, checks that they describe this
   artifact, commit and passing core/host suites, and for Windows/macOS checks the
   owner-signed publisher policy and actual publisher in the provenance. It then
   re-verifies the stable signature, copies that snapshot to
   `store/stable/<stable version>/`, and inserts the stable row. The bytes
   customers download are the exact bytes that were tested — no rebuild.
4. Promote each platform you intend to ship. `GET /api/releases/latest` starts
   serving the new stable per platform immediately; the in-app updater picks it
   up on its next poll.
5. A second promote of the same version returns 409. To withdraw, delete the
   stable row from the same table (installed clients stop being offered it).

Promotion never moves the supported-version floor. Nightly publishes send
`min_supported = 0.1.0` on purpose: the gateway refuses to move the floor
backward, so a nightly that raised it would strand installed clients. Raising the
floor is a deliberate act through `packaging/publish.py register --min-supported`.

The artifact signature binds `key_id`, version, platform, architecture and
artifact SHA-256; it does **not** bind `min_supported`. The current floor comes
from the authenticated gateway registration/HTTPS metadata path, and the
desktop uses it for mandatory-update UI. Do not describe it as a signed
minimum-version policy or infer its authority from an artifact signature.
Production approval must explicitly establish the floor issuer, rollback
policy and authenticated distribution authority. This remediation neither
chooses a new floor nor authorizes a floor increase.

## Verifying a published release end to end

`GET /api/releases/latest` names the target identity and, for an entitled
caller, a download URL. When no CDN credential is configured the URL is this
gateway's own `/api/releases/artifact/<key>` route, and **that route is
entitlement-gated**: the updater sends its entitlement with the artifact GET
too (same origin only), so an unentitled client sees metadata and no URL.

To prove a published build actually installs, run the production check-and-stage
path against the live endpoint from a host build:

```sh
ninja -C host/build-linux test_updater
CHUDWARE_UPDATE_CHANNEL=beta \
CHUDWARE_UPDATE_ENTITLEMENT="<compact entitlement token>" \
  host/build-linux/bin/test_updater --live \
  https://app.chudware.com/api/releases/latest 0.1.0-beta.0 /tmp/live-updater
```

It fetches the manifest, downloads the artifact, and verifies size, SHA-256,
the Ed25519 release signature and the *inner* payload manifest of the extracted
tree before the atomic stage rename; it prints every status transition and
asserts the running app was untouched. A token comes from the same flow the app
uses — `POST /desktop/authorize` with an admin session cookie and a
base64url Ed25519 device key returns a handoff URL carrying `token=`.

Note that while `min_supported` is `0.1.0` every `0.1.0-beta.N` client is
*below* the floor, so the status reads **Mandatory** the whole way through:
`setStatus` pins that state and only the message tracks progress.

The install half — replacing the running AppImage with the staged one — ends in
`execv`, which would launch the GUI. Run it with the install directory on a
noexec mount so the relaunch is blocked *after* the replacement lands:

```sh
unshare -Ur -m sh -c 'mkdir -p /tmp/nx && mount -t tmpfs -o noexec,size=600M tmpfs /tmp/nx &&
  host/build-linux/bin/test_updater --apply-once /tmp/live-updater /tmp/nx/Chudware.AppImage'
```

`result=Failed` with `relaunch Failed` is the expected outcome there: it means
the staged artifact was re-verified, installed byte for byte over the old file
and left executable, and only the (deliberately blocked) relaunch failed. On a
normal mount the same call returns `Relaunching` and the new build takes over.

## Required GitHub secrets

| Secret | Used by | Content |
|---|---|---|
| `CHUDWARE_RELEASE_CI_KEY` | the three protected nightly release jobs | base64 32-byte Ed25519 **seed** for `release-ci-v1` (single line, no PEM); release-environment scoped, never branch/PR accessible. |
| `CHUDWARE_GATEWAY_RELEASE_TOKEN` | protected `linux-beta`, `windows-beta`, `macos-beta` | the release publication credential delivered only by the protected release environment. These jobs use HTTPS, not production SSH or `gateway.env`; protected metadata-only `plan` reads its host-local credential and uses loopback. |

Nightly upload no longer uses `CHUDWARE_LIVINGROOM_SSH_HOST`,
`CHUDWARE_LIVINGROOM_SSH_KEY` or `CHUDWARE_LIVINGROOM_HOST_KEY`; builders use
outbound HTTPS. Removing a workflow reference is not proof of revocation:
the external secret/consumer inventory must establish migration and retirement.

The two nightly credentials above are not the entire release-secret inventory.
Offline preparation also uses protected staging/native-publisher inputs;
control and maintenance have separately scoped credentials. Follow the runner
trust policy and offline CI evidence collector rather than putting those values
in repository-wide secrets. Public keys, origins and environment-file paths are
not secret values.
On every host the token is handed to curl through a `0600` `--config` file, never
on a command line: a shared runner's process list is readable, and GitHub's log
masking does not help there.

Rotating `release-ci-v1` means: generate a new seed, add the new public key to
all three keyrings (`core/security/release_keys.h`,
`Webservice/gateway/releases.py`, `packaging/publish.py`), ship a host build that
trusts it, then replace the secret. Keep the old key in the keyrings until no
installed client still verifies against it.

## Runner prerequisites

**`cc-linux-build`, the private NVMe build guest, never the gateway host** (it
publishes over TLS with the `CHUDWARE_GATEWAY_RELEASE_TOKEN` secret and needs no
production file):

* `cmake`, `ninja`, `ctest`, `python3`, `curl`, `tar`, `file`, `ldd`,
  `sha256sum`, `strip`, `rsync`
* `xvfb-run` for the headless screenshot
* the pinned Linux x86_64 `chud` artifact in the CAD runtime release mirror,
  or an explicit verified `CHUDWARE_CHUD_BIN` (see
  `docs/reference/CI_RUNNERS.md`, "Pinned Chudcode artifact")
* Nuitka and the rest of `packaging/requirements-release.txt` in the release
  Python (the AppImage compiles the Python sidecars)
* outbound HTTPS to `https://app.chudware.com`

**`cc-win-package`:**

* Visual Studio with the x64 C++ toolset (`vcvars64.bat` or the DevShell module)
* `cmake`, `ninja`, `ctest`, `git`, `python`, `curl` on `PATH`
  (`%USERPROFILE%\chudware-ci-tools\{bin,python}` is added automatically, as in
  `native-build.yml`)
* **Inno Setup 6** — `ISCC.exe` in `%USERPROFILE%\chudware-ci-tools\innosetup`
  (portable unpack; the runner service account cannot run Inno's installer),
  `%ProgramFiles(x86)%\Inno Setup 6`, on `PATH`, or named by the `ISCC`
  environment variable. `runner-provision.yml` (Actions -> run) installs Inno
  into exactly these locations.
* Nuitka and `packaging/requirements-release.txt` for the compiled sidecars.
  Nuitka's onefile mode on Windows needs Dependency Walker and asks for it
  interactively, which a CI shell answers "no"; `build_sidecars.ps1` passes
  `--assume-yes-for-downloads`, so the first release build fetches it into
  `%LOCALAPPDATA%\Nuitka` and later builds reuse the cache. That runner
  therefore needs outbound HTTPS beyond app.chudware.com on its first build.
* a pinned Windows x86_64 `chud.exe` in the CAD runtime release mirror, or an
  explicit verified `CHUDWARE_CHUD_BIN`. The nightly job fetches and verifies
  its digest-keyed cache with the CAD job token; no runner service environment
  change is required. Build missing Windows bytes with
  `chudcode-runtime-windows-build.yml`, then pin their real SHA-256.
* `CHUDWARE_SIGN_IDENTITY` if you want Authenticode on the installer; without it
  the installer is unsigned and SmartScreen will warn (see `docs/SIGNING.md`)
* outbound HTTPS to `app.chudware.com`. This runner cannot reach the gateway's
  LAN address and does not need to: it uploads and publishes over TLS, and the
  job's first channel step fails loudly with the unreachable origin if 443 is
  blocked.

**`cc-mac-intel` (imac-intel, x86_64):**

* Xcode **Command Line Tools** only (`xcode-select -p`, `clang`, `xcrun`). No
  step uses `xcodebuild`, and the capability check asserts the compiler rather
  than the IDE.
* `cmake`, `ninja`, `ctest`, `python3`, `git`, `curl`, `tar`, `zip`, `rsync`,
  plus the Command Line Tools' `ditto`, `otool`, `install_name_tool`, `plutil`
  and `codesign`
* Homebrew, with `opencascade` installed: off Windows `ProvisionOCCT.cmake`
  downloads nothing and the `macos-x86_64` preset `find_package`s OCCT
  through `/usr/local`.
* a release Python 3.11+ carrying `packaging/requirements-release.txt` (Nuitka
  for the compiled `chudware-worker`/`chudware-mcp` sidecars, `cryptography` for
  every signature). macOS ships 3.9.6, so the job picks its interpreter by
  capability: `~/chudware-ci-tools/release-python/bin/python3` first, then
  `python3.14`..`python3` from `PATH`, and the first one that imports both
  `cryptography` and `nuitka` wins.
* the pinned macOS x86_64 `chud` artifact in the CAD runtime release mirror,
  or an explicit verified `CHUDWARE_CHUD_BIN`. The nightly capability step
  downloads and verifies it before packaging, without a private-runtime PAT
  or manual Mac installation. Packaging itself stays offline; see
  `docs/reference/CI_RUNNERS.md`, "Pinned Chudcode artifact".
* outbound HTTPS to `app.chudware.com`, for the same reason as the Windows
  builder: the Intel Mac cannot route to `192.168.0.183`.

`runner-provision.yml` can prepare the Intel builder: dispatch it with
`runner_label` set to `cc-mac-intel` and it installs the brew formulae
(`cmake ninja python@3.13 opencascade`) and the release-Python venv at
`~/chudware-ci-tools/release-python`, then prints a capability report that exits
non-zero on any `MISSING`. Homebrew itself is the one thing it will not install:
that installer needs an interactive sudo, so it fails with an explicit
instruction instead. The ARM Mac's CAD core-only environment may also be
provisioned, but it is not a Chudcode-backed release builder. macOS steps
are bash 3.2 (Apple's `/bin/bash`): no
associative arrays, no `mapfile`, no `${var^^}`, and string accumulators instead
of arrays, because expanding an empty array under `set -u` is an
unbound-variable error before bash 4.4.

Windows steps are Windows PowerShell 5.1 with ASCII-only bodies. `bash` on those
hosts is an empty WSL shim that exits 0 without running anything, and PowerShell
5.1 misreads non-ASCII in the runner's BOM-less script; `scripts/verify-ci-workflows.py`
enforces both rules.

`packaging/requirements-release.txt` holds two kinds of pin: the build tools
(Nuitka, cryptography, PyYAML, patchelf) and the runtime packages the compiled
sidecars must bundle. Nuitka can bundle only what the release Python imports, so
each bundled package is also force-included (`--include-package`) by the three
packaging scripts under the same platform condition as its marker, and a release
Python without it fails the build instead of shipping without the feature. Today
that is `pycolmap` (plus its dependency `numpy`) for Gaussian/sparse reverse
engineering on Linux x86_64 and Windows x86_64 release legs. The dependency
also supports macOS arm64 core-only builds, but macOS ARM is not a
Chudcode-backed release leg. `pycolmap` 3.13.0 publishes no Intel macOS
wheel, so the x86_64 `.app` ships without it and `reveng_status` reports
sparse SfM unavailable there. Each active `nightly-release` leg `pip install`s
the file into its release Python before building, so a new pin needs no
re-provision; `runner-provision` syncs it into an existing venv too.

## Prepared-handoff finalization

`packaging/finalize_release.py` rejects final installers/archives in a prepared
handoff; a retained canonical EXE or MSI is never adopted after a builder failure.
The generated Windows script also checks both canonical output paths immediately
before building. Every native Python invocation runs through an explicit
`$LASTEXITCODE` check: Windows PowerShell 5.1's `$ErrorActionPreference = 'Stop'`
alone does not turn a native nonzero exit into a terminating error. A failed
payload verifier, EXE/MSI builder, platform-evidence writer or completion command
terminates the script before the next stage.
For prepared Windows payloads, the installer and its artifact-signing request
both use the version resolved from the retained manifest, even when the portable
builder command omits `--version`.

Beta/preview finalization still permits installers without Authenticode and makes
no platform-signing claim. Missing or failed platform **test** evidence is not a
beta fallback: every channel requires canonical `test-results.json` plus the
core/host JUnit XML and native command logs. The consumer requires both executed
suites, zero process exits/failures/errors, and at least one non-skipped test per
suite. It re-parses JUnit counts, validates the recorded native argv and hashes,
and rejects missing files, replaced bytes, unsafe paths and forged count JSON.
All five retained files are hash-bound in prepared `locked_inputs`; supplied
`--handoff-metadata` must equal the retained canonical metadata byte for byte.
Completion copies the consumed files into `evidence/retained-test-results/` (or
`msi/evidence/retained-test-results/`) and binds their digests to the artifact and
commit in the existing signed SLSA provenance and CycloneDX inputs.
`platform-test.v1` and all existing artifact/evidence signing domains are unchanged.
The evidence generator now requires `--test-results PATH`; label-only
`--command`/`--passed` and `test-commands.txt` are not accepted evidence.

### Owner-authorized native publisher policy

Stable Windows/macOS finalization additionally requires a publisher policy
authenticated by the fixed offline **`release-v1` owner public key**, never a CI
key, a supplied payload public key or an environment signing selector. No real
Windows certificate fingerprints or Apple Team IDs are supplied by the repository:
those independently authenticated owner inputs remain release prerequisites.

The exact canonical JSON schema is `chudware-platform-publishers-v1`, with
`key_id: "release-v1"`, `windows.certificate_sha256` (an allowlist of lowercase
64-hex SHA-256 fingerprints of the actual leaf certificate DER) and
`macos.team_ids` (an allowlist of ten-character uppercase alphanumeric Apple
Team IDs). `macos.bundle_id` must be `com.chudware.Chudware`. Authenticate the
reviewed policy's exact canonical bytes with a detached Ed25519 signature over
`b"chudware-platform-publishers-v1\0" + canonical_policy_bytes`.
The existing offline owner ceremony supports that purpose:

```bash
python packaging/platform_publishers.py platform-publishers.v1.json --out publisher-request.bin
python packaging/sign_release.py platform-publishers publisher-request.bin \
  --encrypted-key release.key.age --identity AGE_ID --output platform-publishers.v1.sig
```

These are owner-workstation instructions, not commands to run in CI or during
source remediation. The signer rejects any non-owner key for this purpose;
`ci_sign.py` excludes it entirely. Keep the reviewed policy and its detached
signature together as `platform-publishers.v1.json` and `.sig` in the prepared
handoff root, with **both** hashes in `locked_inputs`. Optional explicit
`--platform-publisher-policy`/`--platform-publisher-policy-signature` finalizer
arguments must be paired and byte-identical to those retained inputs; they
cannot replace the prepared policy. Portable finalization retains the pair in
`portable-inputs/`, and completion requires those copies to match the locked
handoff. A policy or signature swap after preparation fails closed.

`platform-evidence` authenticates policy before native verification and verifies
private read-only artifact/policy snapshots, checking for source/snapshot changes
before recording success. Windows requires valid SigTool trust plus the OS's
actual embedded Authenticode signer certificate matching the owner allowlist.
`CHUDWARE_SIGN_IDENTITY` remains a builder signing selector only, and `SIGNTOOL`
may locate the SDK verifier; neither establishes publisher authority. macOS
expands the exact ZIP into disposable storage, requires `codesign`, stapler and
Gatekeeper verification, and checks both the actual codesign TeamIdentifier/
Identifier and the sealed Info.plist bundle ID against policy. It never substitutes
verification of a retained app for verification of archived bytes.

Platform evidence records the artifact digest, policy digest and actual publisher.
Stable completion re-authenticates policy and re-verifies native trust/publisher
binding before emitting signing requests, rather than trusting a JSON assertion.
Native verification refuses to run on a different platform. Without a valid
owner-authorized policy, stable Windows/macOS releases are blocked.

For Windows/macOS, the existing signed SLSA `runDetails.byproducts` now carries
one standard ResourceDescriptor named `platform-signing.v1.json`. Its digest is
SHA-256 of the canonical report bytes including the terminating LF. Positive
`annotations` contain the actual report, the authenticated publisher-policy
object and its canonical base64 detached signature. The evidence generator
re-verifies the real artifact against that policy before embedding the report;
providing a JSON assertion is not sufficient. Supply
`--platform-signing-evidence`, `--platform-publisher-policy` and
`--platform-publisher-policy-signature` together to request positive native proof.
The generator retains the canonical report beside the evidence files.

If native inputs are absent, beta evidence honestly carries a `verified: false`
report with reason `owner_authorized_native_verification_not_supplied`, without
publisher-policy claims. An owner-authenticated, actually verified beta can carry
positive native proof; the portable scripts attempt that only when retained policy
and signing/notarization configuration are available. Every attempted native
verification failure is terminal. Stable Windows/macOS publication and promotion
require positive, artifact-matching native proof, independently authenticated owner
policy and actual publisher membership; an artifact's pre-minted stable signature
does not replace those requirements. Linux has no native-publisher byproduct.
Exact SLSA `externalParameters`, `platform-test.v1` fields and existing signing
domains remain unchanged.

### Disposable generated-script smoke plan

Do not run this against a customer handoff, an active user session or production.
On a disposable Windows host with **Windows PowerShell 5.1** and the release
Python dependencies available, run:

```powershell
python -m pytest packaging/test_finalize_release.py packaging/test_release_evidence.py packaging/test_release_signing.py services/installer/test_build_release_closure.py -q
if ($LASTEXITCODE -ne 0) { throw 'finalization regressions failed' }
```

The runtime cases generate the actual Windows finalizer in temporary directories
and put a disposable `python.cmd` command fixture first on PATH. For beta and
stable, exercise native nonzero exits at payload verification, EXE/MSI creation,
platform verification/evidence writing where applicable, and each completion
command; the recorded calls must stop at that failure. Add retained canonical
EXE/MSI bytes after script generation and require refusal before native commands.
The success cases require both publish bundles; a separate disposable verifier
process confirms that even a zero SigTool exit cannot bless unsigned bytes without
valid OS Authenticode and an authorized publisher. Unit cases use disposable test
authorities only, never the product owner's private key.

These command fixtures prove sequencing only, not Authenticode or notarization.
The Python unit cases also simulate native acceptance/rejection and preserve the
identity-bound signing-request and separate MSI evidence contracts. A Linux run
skips Windows-native cases and is **not** Windows runtime proof. Release-host
acceptance additionally needs a real signed disposable EXE/MSI, the authorized
Windows certificate/identity and SDK, and, for macOS, a notarized/stapled disposable
archive and Apple verification tools. Both platforms need the owner-authenticated,
hash-bound publisher policy. Run the generated finalizer against that disposable
handoff; check its exit status and the actual publisher/policy/artifact bindings
in both platform evidence and the completed publish bundles. Also require refusal
of a trust-valid artifact from a different publisher, changed policy/retained test
bytes, and retained canonical installer names. No fixture, generated script or
passing unit suite establishes those authenticated inputs or customer release
readiness. Neither this plan nor source changes constitute exercised native proof.

## Chudcode artifact pin

`vendor/chudcode/CHUDCODE_ARTIFACT.lock` is the release authority for the
compiled RPC service (`chud`) that runs every online agent turn. A lock with
`null` artifact URL/hash fields declares unpublished required inputs and
deliberately makes package/deploy paths fail closed. The supported targets are
exactly Linux x86_64, macOS x86_64 and Windows x86_64. A future repin pins all
three atomically from one published commit:

```bash
python3 scripts/pin_chudcode.py pin \
  --version X.Y.Z --rpc-protocol 2 --repository-commit COMMIT \
  --artifact linux-x86_64=/secure/chud-linux-x86_64 --url linux-x86_64=https://... \
  --artifact macos-x86_64=/secure/chud-macos-x86_64 --url macos-x86_64=https://... \
  --artifact windows-x86_64=/secure/chud-windows-x86_64.exe --url windows-x86_64=https://...
```

The lock's `version` is the runtime release. `engine_version` is optional
source provenance only; it never changes executable identity. Every artifact
must report exactly `Chudcode vX.Y.Z` from `--version` (the executable's own
identity string); engine provenance is never accepted as or appended to it.

### Current pin status (2026-10-11)

The lock pins `chudcode-v0.4.3` (the thin fork on upstream oh-my-pi 18.4.4,
`engine_version` 18.4.4). Linux x86_64 and macOS x86_64 are the upstream
release assets. Windows x86_64 (`6627829b…`) is the native build from
`chudcode-runtime-windows-build.yml`, published to the CAD mirror, because the
Linux cross-compiled Windows CLI crashes inside Bun 1.4.0 at startup; Chudcode
releases no longer ship it. Between a version bump and the native Windows
build, the Windows row is `null` and Windows installer legs fail closed. Locks
written before the x64-only matrix also list
`linux-aarch64`/`macos-arm64`; `load_lock` still accepts those so rollback can
verify retained releases, but `pin` and `check-lock` require the exact
supported set. ARM targets (Linux ARM, macOS ARM) are not release targets. A
macOS ARM build happens only by hand, when the hardware is available; CI never
blocks a release on it.

The runtime repository is private. Nightly uses the CAD repository's
`chudcode-runtime-v<lock version>` release mirror and its own ephemeral
`github.token` (`contents: read`), not a broad OAuth token or a new PAT secret.
`pin_chudcode.py ensure` verifies an explicit input or a digest-keyed cache,
and authenticates a cold download through the GitHub API without forwarding
Authorization to its signed release CDN. The locked digest remains
authoritative. The global `check-lock` and `pin` require all three targets.

The `stage` command used by packaging is offline and accepts only a local
`CHUDWARE_CHUD_BIN`; it verifies the executable format/architecture, SHA-256,
and `--version` product identity before copying it as canonical `chud` or
`chud.exe`. Network retrieval is available only through explicit `fetch` or
`ensure` invocations before packaging.

### Artifact-lock operating rules

The lock is the complete identity of the embedded online runtime, not a source-vendoring
hint. It records schema `chudcode-artifact-lock-v1`, the Chudcode product version, repository
commit, RPC protocol 2, and exactly three immutable release-platform rows. Each row pairs
the canonical filename with an unauthenticated HTTPS URL and SHA-256.

`pin` validates every executable format, executes the host-target binary for product-version
identity, writes canonical JSON to a temporary file, and replaces the lock only after those
checks pass. All three `--artifact` and `--url` pairs are mandatory, so a partial platform
update is intentionally impossible. Useful focused checks are:

```bash
python3 scripts/pin_chudcode.py check-lock
python3 scripts/pin_chudcode.py verify --target linux-x86_64 --input /secure/chud-linux-x86_64
python3 scripts/pin_chudcode.py stage --target linux-x86_64 \
  --input /secure/chud-linux-x86_64 --output /tmp/chud
```

`stage` re-verifies before and after copying and atomically publishes only canonical
`chud`/`chud.exe`. It never downloads. Only explicit `fetch` or `ensure` steps
perform network I/O. Anonymous redirects remain refused; authenticated
GitHub release downloads allow only GitHub's HTTPS release CDN and never
forward credentials there. Mirror bytes must match the committed lock.

The packaged services worker resolves the signed-payload-adjacent canonical binary. Developer
runs may override it with `CHUDWARE_CHUDCODE_BIN`; release jobs use `CHUDWARE_CHUD_BIN` only
as the local staging input. Do not add a package-manager install, source checkout, or second
provider runtime to a release image.

The release also carries CAD's checked-in `assets/ui-shared-manifest.json` from the same
Chudcode release line. Its bytes must match Chudcode's file and its schema must be
`chud-ui-shared-v1`; packaging must copy the checked-in bytes rather than regenerate them.
This manifest is a shared palette/tone/status/usage/font/icon vocabulary, not executable
widget code or runtime state.

## Offline runtime preparation

`scripts/prepare_runtime_wheels.py` separates observed candidates from reviewed
install inputs. Run it with the independently authenticated target base Python
using `-I -S`. `context` prints its exact interpreter/marker/wheel-tag context.
`candidate --wheel-directory WHEELS --output REVIEW.json` writes an **unapproved**
review document, not an installable lock. Include each release/gate declarations
file with repeated `--additional-requirements PATH`; review the full transitive
closure, expected hashes, source identities, target context and native artifact pin.
Local cache presence and matching self-computed hashes do not establish authority.

After independent review, `prepare --manifest REVIEW.json --review-sha256 SHA256
--wheel-directory WHEELS --output NEW_BUNDLE` consumes the selected reviewed bytes.
Pass the same declarations and target context options used for the review.
The atomic bundle retains the existing strict `RUNTIME_WHEELS.lock` schema,
`REVIEWED_RUNTIME.json`, the wheelhouse, declarations and native lock.
`validate-installed --bundle BUNDLE --venv VENV --review-sha256 SHA256` checks
installed package and base-interpreter bytes. Preparation never downloads,
resolves packages, invokes a backend, installs from source or creates a venv.

When the private SDK wheel itself is missing, the explicit operator command
`scripts/build_runtime_sdk.py build-sdk --manifest BUILD_REVIEW.json
--review-sha256 SHA256 --source-archive PINNED_SOURCE.tar
--wheel-directory APPROVED_BACKEND_WHEELS --output NEW_CANDIDATE` builds from
exact reviewed source/archive, backend, base-Python and native-pin identities.
It runs the approved backend in private scratch storage with an allowlisted
environment and no dependency resolution or network-fetch step. This is **not**
an OS network/filesystem sandbox: the reviewed backend and base remain trusted
executable prerequisites. Its result remains **unapproved** for runtime
installation; this is never an automatic installer/provisioner fallback.

Two local builds of SDK commit `2eaba2f4606984172f31f108459d9529d4ee5e38`
using independently hashed setuptools 83.0.0 produced identical wheel SHA-256
`8ae5f2a80a73d525d66ee8a8b0cb4efe30476e2af54c57d96fc79fb02046b433`.
That is scratch reproducibility evidence, not release/image approval. The full
per-target reviewed wheelhouse and authenticated build image are still required.

## Offline CI and host evidence

`scripts/collect_ci_release_evidence.py --bundle EXPORT_BUNDLE.json
--repo OWNER/REPO --repo-id ID --approved-tag TAG` assesses sanitized,
hash-bound organization/enterprise exports against the current defining-workflow,
reusable-ref, runner-group, protection and secret-name/scope policies.
It never reads secret values or operates CI. Conformant offline exports can pass
policy/completeness checks while the command still exits nonzero: authenticated
current API evidence, independent controller/image authority and signed release
approval cannot be manufactured from supplied files.

`Webservice/deploy/collect_release_host_evidence.py --packet SANITIZED_PACKET.json
--trusted-key KEY_ID=PUBLIC_KEY_HEX` authenticates explicitly pinned Ed25519
operator/provider/control-plane/incident-review packets for all eight P3 gates.
The exact schema and signing domain are documented in the collector. A signature
authenticates the signer's reported claims, not independent host/provider truth.
`--observe-local` explicitly enables bounded, read-only allowlisted probes with
no escalation; without it no command probes run. `--fixture-root DIR` permits
disposable unit-file qualification but can never satisfy release requirements.
Missing evidence, stale/contradictory claims or unavailable capabilities fail
closed. Neither collector signs, rotates credentials, changes host policy,
operates backups or authorizes deployment.


## Browser cell deployment authority

Browser execution uses one socket-activated tenant cell per workspace, not the
optional shared local/native hostd. `Webservice/gateway/cells.py` enumerates actual
`chudware-cell@*.service` units and uses the hostd-owned private operator socket
for `cell_census`/`cell_drain`. Non-control sessions, RPCs, dirty documents, native
cache-verification/profiler work, busy/background workers and unknown census each
pin cutover. A cached SDK alone is idle only with positive settle/queue/STT and
process-ownership evidence. Manager errors, transitions, invocation changes and
missing/malformed census never become zero work. `/healthz` and zero browser
bridges are not quiescence authority.

The existing operator entrypoints are:

```bash
bash Webservice/deploy/update.sh staging
bash Webservice/deploy/update.sh prod --continuous
bash Webservice/deploy/update.sh prod --tag v1.2.0
CHUDWARE_DEPLOY_ENV=prod bash Webservice/deploy/install_webd.sh --restart
bash Webservice/deploy/rollback.sh --list
bash Webservice/deploy/rollback.sh --to <qualified-release-id>
```

The `/admin` System panel is read-only for deployment: it displays the selected
operator-written status snapshot and `update.log`, with instructions for these
qualified entrypoints. Browser admins cannot launch builds or restart the gateway;
the former `POST /api/admin/deploy/rebuild` and `/api/admin/deploy/redeploy`
routes, buttons and unsafe shell shortcuts have been removed, not replaced by
an alternate launcher. Status/log reads require an active admin session and never
grant deployment or rollback authority. Automatic master activation remains
suspended pending independent release qualification.

These are conditional deployment commands, not instructions to exercise a live
system from an investigation. They require protected matching configuration,
real admin authority, authenticated runtime/base/wheelhouse inputs, quota/cgroup/
OS prerequisites and independently authorized prior payload or exact-commit
first-cutover acknowledgment. `deployment_release.py promotion-preflight` accepts
only root-controlled schema-7 authority at
`/var/lib/chudware-deployment-trust/<environment>/<joint_sha256>`, containing the
reviewed full-tree digest plus LF, or the exact first-cutover record at
`<trust-root>/<environment>/first-cutover/<40-hex-target-commit>`. Self-computed
service-owned hashes, source fixtures and a first-cutover assertion without the
required independently drilled off-host recovery do not create that authority.

`deployment_drain.sh` carries the private matching lease on fd 8 (flock on fd 9)
to `gateway_preflight.py deployment-drain prepare|check|health|complete|begin|status|release
--env-file PATH --environment prod|staging --lease-fd 8 --port 8300|8310`.
`Webservice/gateway/deployment_drain.py` persists gateway-private maintenance,
closes new admission, retains accepted executor/stream/session work, and fences
cell invocation/drain generations. `prepare` waits for complete quiescence,
requests only generation-fenced **self-exit when quiescent**, then proves all
cells actually inactive before any mutation. `CHUDWARE_WEBD_RESTART_MAX_WAIT`
(default 600 seconds) limits only the operator wait, never agent/customer work.
`complete` requires inactive cells, exact liveness and authenticated admin health
before releasing that same lease. No shared/native-daemon stop/restart, forced
drain, bridge-only fallback or unsafe census substitution participates.

Premutation refusal releases only the owner's lease; nested consumers never
release their caller's. Postmutation failure retains maintenance and cannot
silently undo gateway source/dependencies/schema. Lost release acknowledgment
triggers same-lease reclosure; unconfirmed reclosure/release is unsafe, not
success. Gateway restart or elapsed time cannot clear maintenance. `rollback.sh`
has no `--force`; `--automatic` exits 5 because payload authority alone cannot
prove compatible gateway/schema recovery. Raw `release` is a premutation
abandonment operation, not a health/recovery gate. Protected recovery must retain
customer data and privately recover the matching lease; the source has no turnkey
lost-descriptor custody command. Exact commands, fields and failure handling:
[`Webservice/deploy/RUNBOOK.md`, §h](../Webservice/deploy/RUNBOOK.md#h-pull--rebuild--redeploy).

Provider authority is likewise separate: the gateway-owned `provider_broker.py`
keeps validated BYOK bytes on the host and gives the cell only a principal-bound
`cpg1` grant through its private `provider.sock` / port-18081 forwarder. It checks
live account/role/epoch/right/key authority while streaming and resolves rotated
keys on the host. The port-18080 CONNECT broker is research egress, not credential
inheritance. Only validated Anthropic/OpenAI/xAI/OpenRouter/Gemini BYOK runs;
pool/subscription/sponsor/host credentials are not browser-cell capabilities.
Local wire fixtures do not attest real provider readiness or billing.

## Versioning

`core/cmake/ChudwareVersion.cmake` derives the product version from
`git describe --tags`. The release pipeline overrides it:

```sh
cmake -S . -B build/x -DCHUDWARE_VERSION_OVERRIDE=0.1.0-beta.42
# -- Chudware version 0.1.0-beta.42 (f62b2da7, 2026-08-19, dirty=0)
```

The value must match `^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$` or the
configure fails. `CHUDWARE_VERSION` carries the full string (prerelease
included), which is what every artifact name, payload manifest and release
manifest uses; `project(VERSION ...)` gets the numeric triple only, because CMake
rejects a prerelease suffix there. An override is never marked dirty — the
pipeline pinned the commit it built.

## Manual dispatch and debugging

* **Run one now:** Actions -> `nightly-release` -> Run workflow. Dispatch skips
  the "already published" check, so it always builds.
* **Watch progress without Actions:** `/admin` -> Releases -> CI builds, or
  `GET /api/admin/ci/builds?limit=20`.
* **A macOS release row stuck on `queued`:** check `imac-intel` /
  `cc-mac-intel` availability and re-dispatch when the runner is online.
  The ARM Mac may run core-only CI but has no Chudcode release artifact,
  so waiting on `cc-mac-arm` cannot publish a Chudcode-backed release.
* **A rejected publish:** nothing is registered or moved into `store/`; the
  gateway validates and publishes only a private snapshot of the staged files.
  Staged bytes are never served and are collected as `incoming/` orphans. Re-run
  the job to rebuild and re-upload everything before publishing.
* **No Promote button on a row:** its evidence bundle is incomplete, or a
  Windows/macOS beta records `verified:false` native publisher verification
  (`owner_authorized_native_verification_not_supplied`). `POST
  /api/admin/releases/promote` answers 422. The beta row still installs and
  serves; publish a build with owner-authorized native verification to promote.
* **Signing a request by hand** (same code path CI uses):

  ```sh
  CHUDWARE_RELEASE_CI_KEY="$(cat ~/.chudware/release-ci-v1.seed.b64)" \
    python3 packaging/ci_sign.py \
      --expect-public-key d7434dc67f4d6e11cd4c8ced9b5ebbccf2e1477b5949d52d7685b6ca43a010f6 \
      artifact --key-id release-ci-v1 --version 0.1.0-beta.42 \
      --platform linux --arch x86_64 --artifact dist/Chudware-0.1.0-beta.42-linux-x86_64.AppImage
  ```

* **A failed upload:** `PUT /api/releases/ci/upload/<name>` streams to
  `incoming/<name>.part` and renames only on success, so a dropped connection
  leaves no half-file for `ci/publish` to trip over. Every call carries
  `--retry 5 --retry-delay 15 --retry-max-time 600`, and the job compares the
  sha256 the endpoint returns against the local digest, so transport corruption
  is named at the file that caused it instead of as a 422 an hour later. A 413
  means the artifact passed the endpoint's 2 GiB ceiling.
* **Transport rules.** Only `plan` runs on the gateway host: it must use
  `http://127.0.0.1:8300` and read the token from `gateway.env`, never the
  public URL, and it must never compile. `linux-beta`, `windows-beta` and
  `macos-beta` are builders and must use `https://app.chudware.com` with the
  `CHUDWARE_GATEWAY_RELEASE_TOKEN` secret; no builder gets `gateway.env`, a NAS
  release path or an SSH path, and adding one back would put production
  secrets on a build guest. On both transports the token goes to curl in a
  `0600` `--config` file, never in argv.
