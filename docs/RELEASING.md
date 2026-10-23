# Releasing Chudware

Releases are built by [`.github/workflows/release.yml`](../.github/workflows/release.yml)
on GitHub-hosted runners and published as
[GitHub Releases](https://github.com/Hunter-124/Chudware/releases). The in-app
updater reads signed manifests attached to the latest release. Signing keys and
optional platform certificates are described in [SIGNING.md](SIGNING.md); the
other workflows are described in [reference/CI.md](reference/CI.md).

## Cutting a release

1. Make sure `master` is green: `fast-gate`, `linux-gate` and `native-build`.
2. Pick the version. Tags are `v<major>.<minor>.<patch>` with an optional
   prerelease suffix (`v0.3.0-beta.1`). A suffix publishes a GitHub
   prerelease, which the updater never offers to users.
3. Tag the commit and push the tag:

   ```bash
   git tag -a v0.3.0 -m "Chudware 0.3.0"
   git push origin v0.3.0
   ```

4. Follow the `release` run under Actions. Nothing is published unless all
   three platform builds succeed; the publish job then creates the release in
   one step, with download and code-signing notes followed by the generated
   changelog.

To rebuild a tag that already exists (for example after adding a signing
secret), run the `release` workflow manually and enter the tag. The publish
job refuses to overwrite an existing release, so delete it first with
`gh release delete v0.3.0`; the tag itself stays.

## What gets published

| Job | Runner | Assets |
|---|---|---|
| `linux` | `ubuntu-24.04`, `fedora:44` container | `Chudware-<v>-linux-x86_64.AppImage`, `chudware_<v>_amd64.deb`, `chudware-<v>-1.x86_64.rpm` |
| `windows` | `windows-2025` | `Chudware-Setup-<v>-x86_64.exe` (Inno Setup), `Chudware-<v>-x86_64.msi` (WiX) |
| `macos` | `macos-15` (Apple silicon) | `Chudware-<v>-macos-arm64.zip` |
| `publish` | `ubuntu-24.04` | `update-linux-x86_64.json`, `update-windows-x86_64.json`, `update-macos-arm64.json`, `install.sh` (copied from `packaging/linux/install.sh`), `SHA256SUMS` |

Package names for prereleases follow each format's rules; `packaging/artifact_names.py`
is the single source of those names. Each platform job:

1. builds the core and host with `CHUDWARE_DEVELOPER_BUILD=OFF`, link-time
   optimization and `CHUDWARE_VERSION_OVERRIDE=<v>`, against the pinned CEF SDK;
2. runs the core and host test suites through `scripts/run_release_ctest.py`;
3. stages the payload (host, engine, `chudware-hostd`, the Nuitka-compiled
   `chudware-worker` and `chudware-mcp` sidecars and the pinned `chud` runtime),
   writes `payload-manifest.v1.json` and signs it with the release key;
4. seals the payload into the platform package and verifies the result: every
   library resolves inside the bundle, `chudware-hostd --status` reports the
   release version, and the signed payload manifest matches the extracted bytes.

`chudware-hostd` verifies the same payload manifest at startup, so a modified
installation is detected even outside the updater.

## Update manifests

The updater fetches

```text
https://github.com/Hunter-124/Chudware/releases/latest/download/update-<platform>-<arch>.json
```

where `<platform>` is `linux`, `windows` or `macos` and `<arch>` is `x86_64` or
`arm64`. GitHub resolves `latest` to the newest published release that is not a
draft or prerelease and redirects to the asset; the updater follows the
redirect over HTTPS. `CHUDWARE_UPDATE_MANIFEST_URL` overrides the URL, which is
how a prerelease is tested before it is promoted.

Each manifest is written by `packaging/github_release_manifest.py`:

```json
{
  "version": "0.3.0",
  "min_supported": "0.0.0",
  "platform": "linux",
  "arch": "x86_64",
  "key_id": "release-ci-v1",
  "sha256": "<64 lowercase hex characters>",
  "size": 412345678,
  "signature": "<base64 Ed25519 signature>",
  "notes_url": "https://github.com/Hunter-124/Chudware/releases/tag/v0.3.0",
  "url": "https://github.com/Hunter-124/Chudware/releases/download/v0.3.0/Chudware-0.3.0-linux-x86_64.AppImage"
}
```

The signature covers the NUL-separated preimage

```text
chudware-artifact-v1 \0 key_id \0 version \0 platform \0 arch \0 sha256
```

The updater accepts a manifest only if `key_id` names a key compiled into
`core/security/release_keys.h`, the signature verifies, and `platform` and
`arch` match the running build. It then downloads `url`, checks `size` and
`sha256`, verifies the payload manifest inside the downloaded package, and only
then stages the update. An installed version older than `min_supported` is told
the update is mandatory; set the repository variable
`CHUDWARE_MIN_SUPPORTED_VERSION` before tagging to raise that floor.

### Checking a published release

```bash
sha256sum --check --ignore-missing SHA256SUMS
```

To exercise the real update path against a published release from a host
build tree:

```bash
cmake --build host/build-linux --target test_updater
host/build-linux/bin/test_updater --live \
  https://github.com/Hunter-124/Chudware/releases/download/v0.3.0/update-linux-x86_64.json \
  0.2.0 /tmp/live-updater
```

It downloads the artifact, runs every verification gate and stages the update
without touching the running application.

## Release key handling

Payload and update manifests are signed with the Ed25519 key `release-ci-v1`.
Its private seed lives only in the `CHUDWARE_RELEASE_SIGNING_KEY` repository
secret; its public half is compiled into `core/security/release_keys.h` and
listed in `RELEASE_PUBLIC_KEYS` in `packaging/github_release_manifest.py`. The signing tools check the seed against
that public key before they sign, so a wrong or rotated secret fails the
release instead of producing manifests no client can verify. Never commit the
seed or pass it on a command line.

Rotating the key is a client migration: add the new public key to
`core/security/release_keys.h` and `packaging/github_release_manifest.py`, ship a release
signed with the old key, and switch the secret and `CHUDWARE_RELEASE_KEY_ID` in
`release.yml` only after that release has been out long enough for users to
update. [SIGNING.md](SIGNING.md) covers generating a key.

## Versioning

`core/cmake/ChudwareVersion.cmake` derives the version from
`git describe --tags`. Release builds override it:

```bash
cmake --preset linux-core -DCHUDWARE_VERSION_OVERRIDE=0.3.0
# -- Chudware version 0.3.0 (<commit>, <date>, dirty=0)
```

The value must match `^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$` or configure
fails. The full string, prerelease included, is used for artifact names and
manifests; CMake's `project(VERSION ...)` receives only the numeric triple.

## Chudcode runtime

The agent runtime `chud` comes from the
[Chudcode](https://github.com/Hunter-124/chudcode) project.
`vendor/chudcode/CHUDCODE_ARTIFACT.lock` pins one release per target
(`linux-x86_64`, `macos-arm64`, `macos-x86_64`, `windows-x86_64`) by URL and
SHA-256, and release jobs fetch it with:

```bash
python3 scripts/pin_chudcode.py ensure --target linux-x86_64 --cache-dir /tmp/chudcode
```

The download is verified against the lock before packaging; a target whose
lock row is unpinned fails the release. To move to a new Chudcode release,
download its assets and repin every target in one step:

```bash
base=https://github.com/Hunter-124/chudcode/releases/download/chudcode-vX.Y.Z
python3 scripts/pin_chudcode.py pin \
  --version X.Y.Z --rpc-protocol 2 --repository-commit <commit> \
  --artifact linux-x86_64=chudcode-linux-x64 --url linux-x86_64=$base/chudcode-linux-x64 \
  --artifact macos-arm64=chudcode-darwin-arm64 --url macos-arm64=$base/chudcode-darwin-arm64 \
  --artifact macos-x86_64=chudcode-darwin-x64 --url macos-x86_64=$base/chudcode-darwin-x64 \
  --artifact windows-x86_64=chudcode-windows-x64.exe --url windows-x86_64=$base/chudcode-windows-x64.exe
python3 scripts/pin_chudcode.py check-lock
```
