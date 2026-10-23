#!/bin/sh
# Chudware for Linux — installer.
#
# Downloads a release from GitHub Releases and installs it:
#
#   curl -fsSL https://github.com/Hunter-124/Chudware/releases/latest/download/install.sh | sh
#   sh install.sh --version 0.2.0 --format rpm
#
# Deliberately POSIX sh, not bash: this runs on whatever /bin/sh the machine
# has, which on Debian and Ubuntu is dash. No arrays, no [[ ]], no ${x^^}, no
# process substitution.
#
# Piping a script into a shell is only as trustworthy as the script, so this one
# is written to be read:
#   * every expansion is quoted, and nothing is ever passed through `eval`;
#   * downloads land in a private mktemp directory removed by a trap on exit;
#   * the AppImage is checked against the release's signed update manifest
#     (update-linux-x86_64.json): size, SHA-256 and the domain-separated Ed25519
#     artifact signature against public keys pinned in this script;
#   * .deb and .rpm packages are checked against the release's SHA256SUMS; the
#     payload inside them carries the same signed manifest the application
#     verifies at startup;
#   * nothing is installed from a partial download — the verify step happens
#     after the transfer completes, on the complete file;
#   * privilege escalation is announced before it happens and never assumed.
#
# Exit status: 0 installed (or planned, under --dry-run), non-zero otherwise.

set -eu

REPO="${CHUDWARE_RELEASE_REPO:-Hunter-124/Chudware}"
VERSION=""
FORMAT="auto"
PREFIX="${HOME:-/tmp}/.local/bin"
DRY_RUN="no"
TMP=""

PROGRAM="chudware"
APPLICATIONS="${HOME:-/tmp}/.local/share/applications"

usage() {
    cat <<'USAGE'
Chudware for Linux — installer.

Usage:
  install.sh [options]

Options:
  --version <v>       Install this release (tag v<v>). Default: the latest release.
  --format <name>     auto (default), deb, rpm or appimage.
                      auto picks .deb on apt/dpkg systems, .rpm on
                      dnf/yum/zypper/rpm systems, and the AppImage otherwise
                      or whenever the packages cannot be installed.
  --prefix <dir>      Where the AppImage is installed. Default ~/.local/bin.
                      Ignored for .deb and .rpm, which install to /opt/chudware.
  --repo <owner/name> GitHub repository to install from. Default
                      Hunter-124/Chudware, or CHUDWARE_RELEASE_REPO.
  --dry-run           Resolve and print the download and verification plan,
                      without downloading the artifact or installing anything.
                      This does NOT authenticate the artifact.
  -h, --help          This text.

The AppImage is verified with locally installed OpenSSL 3 or newer (Ed25519).
No verifier is downloaded and no environment or manifest public key is trusted.

Examples:
  curl -fsSL https://github.com/Hunter-124/Chudware/releases/latest/download/install.sh | sh
  sh install.sh --version 0.2.0 --format rpm
  sh install.sh --dry-run
USAGE
}

info() { printf '%s\n' "$*"; }
step() { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }

cleanup() {
    if [ -n "$TMP" ] && [ -d "$TMP" ]; then
        rm -rf "$TMP"
    fi
}

# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------

while [ "$#" -gt 0 ]; do
    case "$1" in
        --version)
            [ "$#" -ge 2 ] || die "--version needs a value"
            VERSION="$2"; shift 2 ;;
        --version=*)
            VERSION="${1#--version=}"; shift ;;
        --format)
            [ "$#" -ge 2 ] || die "--format needs a value"
            FORMAT="$2"; shift 2 ;;
        --format=*)
            FORMAT="${1#--format=}"; shift ;;
        --prefix)
            [ "$#" -ge 2 ] || die "--prefix needs a value"
            PREFIX="$2"; shift 2 ;;
        --prefix=*)
            PREFIX="${1#--prefix=}"; shift ;;
        --repo)
            [ "$#" -ge 2 ] || die "--repo needs a value"
            REPO="$2"; shift 2 ;;
        --repo=*)
            REPO="${1#--repo=}"; shift ;;
        --dry-run)
            DRY_RUN="yes"; shift ;;
        -h|--help)
            usage; exit 0 ;;
        *)
            printf 'error: unknown option: %s\n\n' "$1" >&2
            usage >&2
            exit 2 ;;
    esac
done

case "$FORMAT" in
    auto|deb|rpm|appimage) ;;
    *) die "--format must be auto, deb, rpm or appimage (got '$FORMAT')" ;;
esac

case "$REPO" in
    */*/*|/*|*/|.*|*/.*|*[!A-Za-z0-9_./-]*) die "--repo must be OWNER/NAME (got '$REPO')" ;;
    */*) ;;
    *) die "--repo must be OWNER/NAME (got '$REPO')" ;;
esac

# Same shape packaging/artifact_names.py accepts: major.minor.patch[-prerelease].
if [ -n "$VERSION" ]; then
    printf '%s\n' "$VERSION" |
        grep -Eq '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]+)?$' ||
        die "--version must be major.minor.patch with an optional -prerelease (got '$VERSION')"
fi

RELEASES="https://github.com/$REPO/releases"
if [ -n "$VERSION" ]; then
    ASSETS="$RELEASES/download/v$VERSION"
else
    ASSETS="$RELEASES/latest/download"
fi

# ---------------------------------------------------------------------------
# Machine
# ---------------------------------------------------------------------------

MACHINE="$(uname -m 2>/dev/null || printf 'unknown')"
case "$MACHINE" in
    x86_64|amd64) ARCH="x86_64" ;;
    aarch64|arm64)
        die "Chudware does not publish an ARM64 Linux build yet (this machine reports '$MACHINE').
       This installer stops rather than installing an x86_64 binary that cannot
       run. Build from source instead: https://github.com/$REPO" ;;
    *)
        die "unsupported CPU architecture '$MACHINE'; Chudware for Linux is x86_64 only" ;;
esac

if [ "$(uname -s 2>/dev/null || printf unknown)" != "Linux" ]; then
    die "this installer is for Linux; download the macOS or Windows build from $RELEASES"
fi

# ---------------------------------------------------------------------------
# HTTP and digests
# ---------------------------------------------------------------------------

if have curl; then
    DOWNLOADER="curl"
elif have wget; then
    DOWNLOADER="wget"
else
    die "neither curl nor wget is installed; one of them is required"
fi

# Body of a GET on stdout. Fails (non-zero) on any HTTP error status. GitHub
# serves release assets through a redirect to its CDN, which both tools follow.
http_body() {
    if [ "$DOWNLOADER" = "curl" ]; then
        curl -fsSL --proto '=https' --retry 2 --max-time 60 -- "$1"
    else
        wget -q -O - --https-only --tries=3 --timeout=60 -- "$1"
    fi
}

# Transfer a URL to a path. Writes to "<dest>.part" and renames only on
# success, so an interrupted transfer can never be mistaken for a complete one.
http_download() {
    _url="$1"
    _dest="$2"
    rm -f "$_dest" "$_dest.part"
    if [ "$DOWNLOADER" = "curl" ]; then
        curl -fSL --proto '=https' --retry 2 --progress-bar -o "$_dest.part" -- "$_url"
    else
        wget -q --show-progress --https-only --tries=3 -O "$_dest.part" -- "$_url"
    fi
    mv -- "$_dest.part" "$_dest"
}

sha256_of() {
    if have sha256sum; then
        sha256sum -- "$1" | cut -d' ' -f1
    elif have shasum; then
        shasum -a 256 -- "$1" | cut -d' ' -f1
    elif have openssl; then
        openssl dgst -sha256 -- "$1" | sed 's/.*= *//'
    else
        printf ''
    fi
}

# Product trust ring from core/security/release_keys.h, encoded as Ed25519
# SubjectPublicKeyInfo DER (302a300506032b6570032100 + the 32-byte public key).
# Rotation must update these pins with the product ring before signing switches.
# Deliberately no key file, environment override, or manifest-provided key.
release_public_key() {
    case "$1" in
        release-v1)
            printf '%s' 'MCowBQYDK2VwAyEAKay64UG8yvCyLhqU000LxzYeUm0L/hLIl5S8kyKWbdc=' ;;
        release-ci-v1)
            printf '%s' 'MCowBQYDK2VwAyEA10NNxn9NbhHNTIztm167zPLhR3tZSdUtdoW2ykOgEPY=' ;;
        *) return 1 ;;
    esac
}

verify_artifact_signature() {
    # Decode and round-trip to reject OpenSSL's permissive base64 parsing,
    # including noncanonical pad bits. Ed25519 signatures are exactly 64 bytes.
    printf '%s' "$SIGNATURE" | openssl base64 -d -A > "$TMP/artifact.sig" ||
        die "malformed artifact signature; nothing has been installed"
    [ "$(wc -c < "$TMP/artifact.sig" | tr -d ' ')" = "64" ] &&
        [ "$(openssl base64 -A -in "$TMP/artifact.sig")" = "$SIGNATURE" ] ||
        die "malformed artifact signature; nothing has been installed"
    printf '%s' "$PUBLIC_KEY" | openssl base64 -d -A > "$TMP/release-key.der" ||
        die "cannot decode the pinned release public key"
    # Frozen signing tuple: NUL separators, no terminal NUL, no format field.
    # Use the resolved identity and computed digest, NOT mutable manifest data.
    printf '%s\000%s\000%s\000%s\000%s\000%s' \
        'chudware-artifact-v1' "$KEY_ID" "$VERSION" 'linux' "$ARCH" "$ACTUAL_SHA" \
        > "$TMP/artifact.preimage"
    if ! openssl pkeyutl -verify -pubin -keyform DER \
        -inkey "$TMP/release-key.der" -rawin -in "$TMP/artifact.preimage" \
        -sigfile "$TMP/artifact.sig" > "$TMP/verify.log" 2>&1; then
        die "ARTIFACT SIGNATURE VERIFICATION FAILED for $KEY_ID/$VERSION/linux/$ARCH.
       Nothing has been installed. The signature must match the pinned release
       key, release identity and downloaded bytes. Verification requires local
       OpenSSL 3 or newer with Ed25519 support."
    fi
    info "Ed25519 artifact signature OK: $KEY_ID/$VERSION/linux/$ARCH"
}

# Minimal readers for the update manifest packaging/github_release_manifest.py
# writes: one flat JSON object, one "key": value pair per line, and no value
# (version, digest, key id, base64 signature, URL) that contains a quote. That
# is narrow enough to read with sed and keeps this script free of jq.
json_string() {
    printf '%s\n' "$2" | sed -n 's/^[[:space:]]*"'"$1"'"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p'
}

json_number() {
    printf '%s\n' "$2" | sed -n 's/^[[:space:]]*"'"$1"'"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p'
}

human_bytes() {
    _bytes="${1:-0}"
    if [ "$_bytes" -ge 1073741824 ] 2>/dev/null; then
        printf '%s.%s GB' "$((_bytes / 1073741824))" "$(((_bytes % 1073741824) * 10 / 1073741824))"
    elif [ "$_bytes" -ge 1048576 ] 2>/dev/null; then
        printf '%s MB' "$((_bytes / 1048576))"
    else
        printf '%s bytes' "$_bytes"
    fi
}

# ---------------------------------------------------------------------------
# Package manager detection
# ---------------------------------------------------------------------------

# Echoes the format this machine can install natively: "deb", "rpm", or
# "appimage" when no package manager matches.
detect_native_format() {
    if have apt-get && have dpkg; then
        printf 'deb'
    elif have dnf || have yum || have zypper || have rpm; then
        printf 'rpm'
    else
        printf 'appimage'
    fi
}

AS_ROOT=""
CAN_ESCALATE="no"
if [ "$(id -u)" = "0" ]; then
    CAN_ESCALATE="yes"
elif have sudo; then
    AS_ROOT="sudo"
    CAN_ESCALATE="yes"
fi

WANT_FORMAT="$FORMAT"
if [ "$FORMAT" = "auto" ]; then
    WANT_FORMAT="$(detect_native_format)"
    if [ "$WANT_FORMAT" != "appimage" ] && [ "$CAN_ESCALATE" = "no" ]; then
        # A package install needs root. Without it, silently failing at the
        # last step would waste a large download.
        info "No root and no sudo, so the .$WANT_FORMAT package cannot be installed."
        info "Falling back to the AppImage in $PREFIX."
        WANT_FORMAT="appimage"
    fi
fi

if [ "$WANT_FORMAT" != "appimage" ] && [ "$CAN_ESCALATE" = "no" ]; then
    die "--format $FORMAT installs a system package, which needs root; re-run with sudo, or use --format appimage"
fi

# ---------------------------------------------------------------------------
# Resolve the release
# ---------------------------------------------------------------------------

step "Fetching the release manifest from $ASSETS"
MANIFEST=""
if ! MANIFEST="$(http_body "$ASSETS/update-linux-$ARCH.json")"; then
    die "could not fetch $ASSETS/update-linux-$ARCH.json; is there a Linux build in that release?"
fi

MANIFEST_VERSION="$(json_string version "$MANIFEST")"
APPIMAGE_SHA="$(json_string sha256 "$MANIFEST")"
APPIMAGE_SIZE="$(json_number size "$MANIFEST")"
KEY_ID="$(json_string key_id "$MANIFEST")"
SIGNATURE="$(json_string signature "$MANIFEST")"

[ -n "$MANIFEST_VERSION" ] || die "the release manifest carries no version; refusing to install"
if [ -n "$VERSION" ] && [ "$MANIFEST_VERSION" != "$VERSION" ]; then
    die "the release manifest is for $MANIFEST_VERSION, not the requested $VERSION; refusing to install"
fi
VERSION="$MANIFEST_VERSION"
[ "$(json_string platform "$MANIFEST")" = "linux" ] &&
    [ "$(json_string arch "$MANIFEST")" = "$ARCH" ] ||
    die "the release manifest does not describe linux/$ARCH; refusing to install"
case "$APPIMAGE_SHA" in
    *[!0-9a-f]*|'') die "the release manifest sha256 must be lowercase hexadecimal" ;;
esac
[ "${#APPIMAGE_SHA}" = "64" ] || die "the release manifest sha256 must have 64 characters"

# The update manifest names exactly one artifact: the AppImage.
APPIMAGE_NAME="Chudware-$VERSION-linux-$ARCH.AppImage"
SUMS=""
case "$WANT_FORMAT" in
    deb|rpm)
        step "Fetching SHA256SUMS"
        SUMS="$(http_body "$ASSETS/SHA256SUMS")" ||
            die "could not fetch $ASSETS/SHA256SUMS; the .$WANT_FORMAT package cannot be verified"
        case "$WANT_FORMAT" in
            deb) PATTERN='chudware_.*_amd64\.deb' ;;
            *) PATTERN='chudware-.*\.x86_64\.rpm' ;;
        esac
        ENTRY="$(printf '%s\n' "$SUMS" | grep -E "^[0-9a-f]{64}  $PATTERN\$" || true)"
        if [ -z "$ENTRY" ]; then
            warn "no .$WANT_FORMAT package is published for linux/$ARCH in this release"
            info "Falling back to the AppImage."
            WANT_FORMAT="appimage"
        else
            [ "$(printf '%s\n' "$ENTRY" | wc -l | tr -d ' ')" = "1" ] ||
                die "SHA256SUMS lists more than one .$WANT_FORMAT package; refusing to guess"
            EXPECTED_SHA="${ENTRY%% *}"
            ARTIFACT_NAME="${ENTRY#*  }"
            EXPECTED_SIZE=""
            # The checksum list must agree with the signed manifest about the AppImage.
            LISTED_APPIMAGE="$(printf '%s\n' "$SUMS" | grep -F "  $APPIMAGE_NAME" | cut -d' ' -f1 || true)"
            if [ -n "$LISTED_APPIMAGE" ] && [ "$LISTED_APPIMAGE" != "$APPIMAGE_SHA" ]; then
                die "SHA256SUMS and the signed release manifest disagree about $APPIMAGE_NAME; refusing to continue"
            fi
        fi
        ;;
esac
if [ "$WANT_FORMAT" = "appimage" ]; then
    ARTIFACT_NAME="$APPIMAGE_NAME"
    EXPECTED_SHA="$APPIMAGE_SHA"
    EXPECTED_SIZE="$APPIMAGE_SIZE"
    [ -n "$KEY_ID" ] || die "missing artifact signing key_id; refusing to install"
    PUBLIC_KEY="$(release_public_key "$KEY_ID")" ||
        die "unknown artifact signing key_id '$KEY_ID'; refusing to install"
    case "$SIGNATURE" in
        *[!A-Za-z0-9+/=]*|'') die "malformed artifact signature; refusing to install" ;;
    esac
    [ "${#SIGNATURE}" = "88" ] || die "malformed artifact signature; refusing to install"
    case "$SIGNATURE" in
        *==) ;;
        *) die "malformed artifact signature; refusing to install" ;;
    esac
fi
ARTIFACT_URL="$ASSETS/$ARTIFACT_NAME"

case "$WANT_FORMAT" in
    deb) PLAN="install the .deb package with apt/dpkg into /opt/chudware"
         CHECK="SHA-256 from SHA256SUMS" ;;
    rpm) PLAN="install the .rpm package with dnf/yum/zypper/rpm into /opt/chudware"
         CHECK="SHA-256 from SHA256SUMS" ;;
    *) PLAN="install the AppImage as $PREFIX/$PROGRAM"
       CHECK="size, SHA-256 and Ed25519 signature (key $KEY_ID) from the signed manifest" ;;
esac

info ""
info "  release   $VERSION ($RELEASES)"
info "  target    linux/$ARCH"
info "  artifact  $ARTIFACT_NAME"
[ -z "$EXPECTED_SIZE" ] || info "  size      $(human_bytes "$EXPECTED_SIZE")"
info "  sha256    $EXPECTED_SHA"
info "  verify    $CHECK"
info "  plan      $PLAN"
info ""

if [ "$DRY_RUN" = "yes" ]; then
    step "Dry run: stopping before any download or install"
    info "Would download: $ARTIFACT_URL"
    info "Artifact NOT authenticated: dry run does not download or verify bytes."
    info "Would then $PLAN, only after verification passes."
    exit 0
fi

if [ "$WANT_FORMAT" = "appimage" ]; then
    have openssl || die "OpenSSL 3 or newer with Ed25519 support is required to verify the AppImage signature.
       No verifier will be downloaded and nothing has been installed."
fi

# ---------------------------------------------------------------------------
# Download and verify
# ---------------------------------------------------------------------------

trap cleanup EXIT HUP INT TERM
# TMPDIR is honoured, but only when it actually exists: a stale TMPDIR in the
# environment (very common in service accounts and CI shells) otherwise turns
# into a bare "mktemp: failed to create directory via template" with no clue
# which variable is at fault.
SCRATCH="${TMPDIR:-/tmp}"
if [ ! -d "$SCRATCH" ] || [ ! -w "$SCRATCH" ]; then
    [ -z "${TMPDIR:-}" ] || warn "TMPDIR is not a writable directory ($SCRATCH); using /tmp"
    SCRATCH="/tmp"
fi
TMP="$(mktemp -d "$SCRATCH/chudware-install.XXXXXX")" ||
    die "could not create a temporary directory in $SCRATCH"
# mktemp -d gives 0700. apt-get drops to the unprivileged `_apt` user to touch a
# local .deb and cannot read it out of a 0700 directory, so widen the directory
# (the artifact is a public release, there is nothing secret in here) while the
# trap still guarantees it is removed on any exit.
chmod 0755 "$TMP"

case "$WANT_FORMAT" in
    deb) ARTIFACT="$TMP/chudware.deb" ;;
    rpm) ARTIFACT="$TMP/chudware.rpm" ;;
    *) ARTIFACT="$TMP/chudware.AppImage" ;;
esac

step "Downloading $ARTIFACT_NAME"
http_download "$ARTIFACT_URL" "$ARTIFACT" || die "download failed"

step "Verifying sha256"
ACTUAL_SHA="$(sha256_of "$ARTIFACT")"
if [ -z "$ACTUAL_SHA" ]; then
    die "no sha256 tool found (sha256sum, shasum or openssl).
       The download cannot be verified, so it will not be installed.
       Install coreutils and re-run."
fi
if [ "$ACTUAL_SHA" != "$EXPECTED_SHA" ]; then
    printf 'error: SHA256 MISMATCH — the download does not match the release metadata.\n' >&2
    printf '       expected %s\n' "$EXPECTED_SHA" >&2
    printf '       actual   %s\n' "$ACTUAL_SHA" >&2
    printf '       Nothing has been installed. Re-run; if it happens again, do NOT\n' >&2
    printf '       install this file and report it at https://github.com/%s/issues.\n' "$REPO" >&2
    exit 1
fi
if [ -n "$EXPECTED_SIZE" ]; then
    ACTUAL_SIZE="$(wc -c < "$ARTIFACT" | tr -d ' ')"
    if [ "$ACTUAL_SIZE" != "$EXPECTED_SIZE" ]; then
        die "downloaded $ACTUAL_SIZE bytes but the manifest declares $EXPECTED_SIZE; not installing"
    fi
fi
info "sha256 OK: $ACTUAL_SHA"
if [ "$WANT_FORMAT" = "appimage" ]; then
    step "Verifying the Ed25519 artifact signature"
    verify_artifact_signature
fi
# Same reason as the directory: apt's sandbox user must be able to read it.
chmod 0644 "$ARTIFACT"

# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

announce_escalation() {
    if [ -n "$AS_ROOT" ]; then
        step "Escalating with sudo to run: $*"
        info "    (you may be prompted for your password)"
    else
        step "Running as root: $*"
    fi
}

run_privileged() {
    announce_escalation "$@"
    if [ -n "$AS_ROOT" ]; then
        "$AS_ROOT" "$@"
    else
        "$@"
    fi
}

install_deb() {
    # apt-get resolves the package's dependencies; dpkg alone does not, so it is
    # the fallback rather than the first choice.
    if have apt-get; then
        run_privileged apt-get install -y -- "$ARTIFACT" && return 0
        warn "apt-get could not install the package directly; falling back to dpkg"
    fi
    run_privileged dpkg -i -- "$ARTIFACT" || {
        if have apt-get; then
            warn "dpkg reported unmet dependencies; asking apt-get to resolve them"
            run_privileged apt-get -f install -y
        else
            return 1
        fi
    }
}

install_rpm() {
    if have dnf; then
        run_privileged dnf install -y "$ARTIFACT" && return 0
    elif have yum; then
        run_privileged yum install -y "$ARTIFACT" && return 0
    elif have zypper; then
        run_privileged zypper --non-interactive install --allow-unsigned-rpm "$ARTIFACT" && return 0
    fi
    run_privileged rpm -Uvh --replacepkgs "$ARTIFACT"
}

install_appimage() {
    mkdir -p "$PREFIX"
    _target="$PREFIX/$PROGRAM"
    # Install through a temporary name in the SAME directory and rename, so a
    # running copy is replaced atomically and never left half-written.
    cp -- "$ARTIFACT" "$_target.new"
    chmod 0755 "$_target.new"
    mv -- "$_target.new" "$_target"
    info "installed $_target"

    mkdir -p "$APPLICATIONS"
    # Icon is the freedesktop theme name the .deb and .rpm install into
    # hicolor; with the AppImage there is no icon on disk, so the launcher
    # falls back to a generic one rather than showing nothing.
    cat > "$APPLICATIONS/$PROGRAM.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Chudware
Comment=Parametric CAD
Exec=$_target %f
Icon=$PROGRAM
Terminal=false
Categories=Graphics;Engineering;
StartupWMClass=chudware
DESKTOP
    info "installed $APPLICATIONS/$PROGRAM.desktop"
    if have update-desktop-database; then
        update-desktop-database "$APPLICATIONS" 2>/dev/null || true
    fi

    case ":${PATH}:" in
        *":$PREFIX:"*) ;;
        *) warn "$PREFIX is not on your PATH; run it as $_target, or add the directory to PATH" ;;
    esac
}

case "$WANT_FORMAT" in
    deb) step "Installing the .deb package"; install_deb ;;
    rpm) step "Installing the .rpm package"; install_rpm ;;
    *) step "Installing the AppImage"; install_appimage ;;
esac

info ""
step "Chudware $VERSION is installed"
if [ "$WANT_FORMAT" = "appimage" ]; then
    info "Run it with: $PREFIX/$PROGRAM"
else
    info "Run it with: chudware"
fi
info "Release notes: $RELEASES/tag/v$VERSION"
