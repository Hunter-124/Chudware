#!/bin/sh
# Chudware for Linux — installer.
#
# Served at https://app.chudware.com/install.sh and meant to be run as
#
#   curl -fsSL https://app.chudware.com/install.sh | sh -s -- --token <token>
#
# Get the token from the Downloads section of the portal; it is valid for
# fifteen minutes and is what authorises the download.
#
# Deliberately POSIX sh, not bash: this runs on whatever /bin/sh the machine
# has, which on Debian and Ubuntu is dash. No arrays, no [[ ]], no ${x^^}, no
# process substitution.
#
# Piping a script into a shell is only as trustworthy as the script, so this one
# is written to be read:
#   * every expansion is quoted, and nothing is ever passed through `eval`;
#   * downloads land in a private mktemp directory removed by a trap on exit;
#   * sha256 and the domain-separated Ed25519 artifact signature are verified
#     against public keys pinned in this script BEFORE anything is installed;
#   * nothing is installed from a partial download — the verify step happens
#     after the transfer completes, on the complete file;
#   * privilege escalation is announced before it happens and never assumed.
#
# Exit status: 0 installed (or planned, under --dry-run), non-zero otherwise.

set -eu

BASE_URL="${CHUDWARE_BASE_URL:-https://app.chudware.com}"
CHANNEL="stable"
FORMAT="auto"
PREFIX="${HOME:-/tmp}/.local/bin"
TOKEN="${CHUDWARE_TOKEN:-}"
DRY_RUN="no"
TMP=""

PROGRAM="chudware"
APPLICATIONS="${HOME:-/tmp}/.local/share/applications"

usage() {
    cat <<'USAGE'
Chudware for Linux — installer.

Usage:
  install.sh --token <token> [options]

Options:
  --token <token>     Download token from the portal's Downloads section.
                      May also be given as CHUDWARE_TOKEN. Required to
                      download; not required for --dry-run.
  --channel <name>    stable (default) or beta.
  --format <name>     auto (default), deb, rpm or appimage.
                      auto picks .deb on apt/dpkg systems, .rpm on
                      dnf/yum/zypper/rpm systems, and the AppImage otherwise
                      or whenever the packages cannot be installed.
  --prefix <dir>      Where the AppImage is installed. Default ~/.local/bin.
                      Ignored for .deb and .rpm, which install to /opt/chudware.
  --base-url <url>    Gateway to install from. Default https://app.chudware.com,
                      or CHUDWARE_BASE_URL. For staging and testing.
  --dry-run           Resolve and print the download and verification plan,
                      without downloading bytes or installing anything.
                      This does NOT authenticate the artifact.
  -h, --help          This text.

Verification requires locally installed OpenSSL 3 or newer with Ed25519 support.
No verifier is downloaded and no environment or manifest public key is trusted.

Examples:
  curl -fsSL https://app.chudware.com/install.sh | sh -s -- --token TOKEN
  sh install.sh --token TOKEN --channel beta --format rpm
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
        --token)
            [ "$#" -ge 2 ] || die "--token needs a value"
            TOKEN="$2"; shift 2 ;;
        --token=*)
            TOKEN="${1#--token=}"; shift ;;
        --channel)
            [ "$#" -ge 2 ] || die "--channel needs a value"
            CHANNEL="$2"; shift 2 ;;
        --channel=*)
            CHANNEL="${1#--channel=}"; shift ;;
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
        --base-url)
            [ "$#" -ge 2 ] || die "--base-url needs a value"
            BASE_URL="$2"; shift 2 ;;
        --base-url=*)
            BASE_URL="${1#--base-url=}"; shift ;;
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

case "$CHANNEL" in
    stable|beta) ;;
    *) die "--channel must be stable or beta (got '$CHANNEL')" ;;
esac

case "$FORMAT" in
    auto|deb|rpm|appimage) ;;
    *) die "--format must be auto, deb, rpm or appimage (got '$FORMAT')" ;;
esac

# Strip a trailing slash so every URL below is built the same way.
while [ "${BASE_URL%/}" != "$BASE_URL" ]; do
    BASE_URL="${BASE_URL%/}"
done
case "$BASE_URL" in
    http://*|https://*) ;;
    *) die "--base-url must be an http(s) URL (got '$BASE_URL')" ;;
esac

# ---------------------------------------------------------------------------
# Machine
# ---------------------------------------------------------------------------

MACHINE="$(uname -m 2>/dev/null || printf 'unknown')"
case "$MACHINE" in
    x86_64|amd64) ARCH="x86_64" ;;
    aarch64|arm64)
        die "Chudware does not publish an ARM64 Linux build yet (this machine reports '$MACHINE').
       Nothing here will work on it, so this installer stops rather than
       installing an x86_64 binary that cannot run.
       Use the browser workspace at $BASE_URL in the meantime." ;;
    *)
        die "unsupported CPU architecture '$MACHINE'; Chudware for Linux is x86_64 only" ;;
esac

if [ "$(uname -s 2>/dev/null || printf unknown)" != "Linux" ]; then
    die "this installer is for Linux; download the macOS or Windows build from $BASE_URL"
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

# Body of a GET on stdout. Fails (non-zero) on any HTTP error status.
http_body() {
    if [ "$DOWNLOADER" = "curl" ]; then
        curl -fsSL --retry 2 --max-time 60 -- "$1"
    else
        wget -q -O - --tries=3 --timeout=60 -- "$1"
    fi
}

# Transfer a URL to a path. Writes to "<dest>.part" and renames only on
# success, so an interrupted transfer can never be mistaken for a complete one.
http_download() {
    _url="$1"
    _dest="$2"
    rm -f "$_dest" "$_dest.part"
    if [ "$DOWNLOADER" = "curl" ]; then
        curl -fSL --retry 2 --progress-bar -o "$_dest.part" -- "$_url"
    else
        wget -q --show-progress --tries=3 -O "$_dest.part" -- "$_url"
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
    # Use the requested identity and computed digest, NOT mutable manifest data.
    printf '%s\000%s\000%s\000%s\000%s\000%s' \
        'chudware-artifact-v1' "$KEY_ID" "$VERSION" 'linux' "$ARCH" "$ACTUAL_SHA" \
        > "$TMP/artifact.preimage"
    if ! openssl pkeyutl -verify -pubin -keyform DER \
        -inkey "$TMP/release-key.der" -rawin -in "$TMP/artifact.preimage" \
        -sigfile "$TMP/artifact.sig" > "$TMP/verify.log" 2>&1; then
        die "ARTIFACT SIGNATURE VERIFICATION FAILED for $KEY_ID/$VERSION/linux/$ARCH.
       Nothing has been installed. The signature must match the pinned release
       key, requested identity and downloaded bytes. Verification requires local
       OpenSSL 3 or newer with Ed25519 support."
    fi
    info "Ed25519 artifact signature OK: $KEY_ID/$VERSION/linux/$ARCH"
}

# Minimal JSON readers for this gateway's own compact responses. FastAPI emits
# `{"a":1,"b":"x"}` with no whitespace and no nesting inside a build object, and
# none of the values (platform, arch, format, version, digest, URL) can contain
# a brace or a quote. That is a narrow enough contract to read with sed, and it
# keeps this script free of a jq dependency it cannot assume.
json_string() {
    printf '%s' "$2" | sed -n 's/.*"'"$1"'":"\([^"]*\)".*/\1/p'
}

json_number() {
    printf '%s' "$2" | sed -n 's/.*"'"$1"'":\([0-9][0-9]*\).*/\1/p'
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

# Echoes the API format name this machine can install natively: "deb", "rpm",
# or "default" (the AppImage) when no package manager matches.
detect_native_format() {
    if have apt-get && have dpkg; then
        printf 'deb'
    elif have dnf || have yum || have zypper || have rpm; then
        printf 'rpm'
    else
        printf 'default'
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

WANT_FORMAT=""
case "$FORMAT" in
    appimage) WANT_FORMAT="default" ;;
    deb) WANT_FORMAT="deb" ;;
    rpm) WANT_FORMAT="rpm" ;;
    auto)
        WANT_FORMAT="$(detect_native_format)"
        if [ "$WANT_FORMAT" != "default" ] && [ "$CAN_ESCALATE" = "no" ]; then
            # A package install needs root. Without it, silently failing at the
            # last step would waste a 325 MB download.
            info "No root and no sudo, so the .$WANT_FORMAT package cannot be installed."
            info "Falling back to the AppImage in $PREFIX."
            WANT_FORMAT="default"
        fi
        ;;
esac

if [ "$WANT_FORMAT" != "default" ] && [ "$CAN_ESCALATE" = "no" ]; then
    die "--format $FORMAT installs a system package, which needs root; re-run with sudo, or use --format appimage"
fi

# ---------------------------------------------------------------------------
# Resolve the build
# ---------------------------------------------------------------------------

step "Asking $BASE_URL which $CHANNEL builds exist"
AVAILABLE=""
if ! AVAILABLE="$(http_body "$BASE_URL/api/releases/available?channel=$CHANNEL")"; then
    die "could not reach $BASE_URL/api/releases/available (channel $CHANNEL)"
fi

# One build object per line: nothing in a value contains a brace, so splitting
# on braces isolates them, and the platform key identifies a build line. Two tr
# calls rather than one with a two-character SET2, whose padding behaviour POSIX
# leaves undefined.
BUILDS="$(printf '%s' "$AVAILABLE" | tr '{' '\n' | tr '}' '\n' | grep '"platform"' || true)"
[ -n "$BUILDS" ] || die "$BASE_URL published no $CHANNEL builds at all"

build_line() {
    printf '%s\n' "$BUILDS" | while IFS= read -r line; do
        [ "$(json_string platform "$line")" = "linux" ] || continue
        [ "$(json_string arch "$line")" = "$ARCH" ] || continue
        [ "$(json_string format "$line")" = "$1" ] || continue
        printf '%s' "$line"
        break
    done
}

BUILD="$(build_line "$WANT_FORMAT")"
if [ -z "$BUILD" ] && [ "$WANT_FORMAT" != "default" ]; then
    warn "no .$WANT_FORMAT package is published for linux/$ARCH on the $CHANNEL channel"
    info "Falling back to the AppImage."
    WANT_FORMAT="default"
    BUILD="$(build_line default)"
fi
[ -n "$BUILD" ] || die "no linux/$ARCH build is published on the $CHANNEL channel"

VERSION="$(json_string version "$BUILD")"
CATALOGUE_SHA="$(json_string sha256 "$BUILD")"
SIZE="$(json_number size "$BUILD")"
[ -n "$VERSION" ] || die "the release catalogue returned a build with no version"

case "$WANT_FORMAT" in
    deb) PLAN="install the .deb package with apt/dpkg into /opt/chudware" ;;
    rpm) PLAN="install the .rpm package with dnf/yum/zypper/rpm into /opt/chudware" ;;
    *) PLAN="install the AppImage as $PREFIX/$PROGRAM" ;;
esac

info ""
info "  channel   $CHANNEL"
info "  version   $VERSION"
info "  target    linux/$ARCH"
info "  format    $WANT_FORMAT"
info "  size      $(human_bytes "$SIZE")"
info "  sha256    $CATALOGUE_SHA"
info "  plan      $PLAN"
info ""

# ---------------------------------------------------------------------------
# Release metadata (authenticated only after artifact signature verification)
# ---------------------------------------------------------------------------

MANIFEST_URL="$BASE_URL/api/releases/latest?channel=$CHANNEL&platform=linux&arch=$ARCH&format=$WANT_FORMAT"
if [ -n "$TOKEN" ]; then
    # The token goes in the query string, not a header: a redirect to the
    # artifact route drops headers, and both places are accepted.
    MANIFEST_URL="$MANIFEST_URL&download_token=$TOKEN"
fi

step "Fetching the release manifest"
MANIFEST=""
if ! MANIFEST="$(http_body "$MANIFEST_URL")"; then
    die "could not fetch the release manifest for $CHANNEL linux/$ARCH ($WANT_FORMAT)"
fi

EXPECTED_SHA="$(json_string sha256 "$MANIFEST")"
EXPECTED_SIZE="$(json_number size "$MANIFEST")"
ARTIFACT_URL="$(json_string url "$MANIFEST")"
KEY_ID="$(json_string key_id "$MANIFEST")"
SIGNATURE="$(json_string signature "$MANIFEST")"

[ "$(json_string version "$MANIFEST")" = "$VERSION" ] &&
    [ "$(json_string platform "$MANIFEST")" = "linux" ] &&
    [ "$(json_string arch "$MANIFEST")" = "$ARCH" ] &&
    [ "$(json_string format "$MANIFEST")" = "$WANT_FORMAT" ] ||
    die "the release manifest does not match the requested release identity; refusing to install"
[ -n "$KEY_ID" ] || die "missing artifact signing key_id; refusing to install"
PUBLIC_KEY="$(release_public_key "$KEY_ID")" ||
    die "unknown artifact signing key_id '$KEY_ID'; refusing to install"
[ -n "$SIGNATURE" ] || die "missing artifact signature; refusing to install"
case "$SIGNATURE" in
    *[!A-Za-z0-9+/=]*) die "malformed artifact signature; refusing to install" ;;
esac
[ "${#SIGNATURE}" = "88" ] || die "malformed artifact signature; refusing to install"
case "$SIGNATURE" in
    *==) ;;
    *) die "malformed artifact signature; refusing to install" ;;
esac

[ -n "$EXPECTED_SHA" ] || die "the release manifest carries no sha256; refusing to install unverifiable bytes"
case "$EXPECTED_SHA" in
    *[!0-9a-f]*) die "the release manifest sha256 must be lowercase hexadecimal" ;;
esac
[ "${#EXPECTED_SHA}" = "64" ] || die "the release manifest sha256 must have 64 characters"
if [ -n "$CATALOGUE_SHA" ] && [ "$CATALOGUE_SHA" != "$EXPECTED_SHA" ]; then
    die "the release catalogue and the release manifest disagree about the sha256 of $CHANNEL $VERSION ($WANT_FORMAT); refusing to continue"
fi

if [ "$DRY_RUN" = "yes" ]; then
    step "Dry run: stopping before any download or install"
    if [ -z "$ARTIFACT_URL" ]; then
        if [ -z "$TOKEN" ]; then
            info "No download token was given, so the manifest carried no download URL."
            info "A real run needs --token <token> from the portal's Downloads section."
        else
            info "The token was not accepted, so the manifest carried no download URL."
        fi
    else
        info "Would download: $ARTIFACT_URL"
    fi
    info "Would verify sha256 $EXPECTED_SHA over $(human_bytes "$EXPECTED_SIZE")."
    info "Would verify the Ed25519 artifact signature against pinned key $KEY_ID using local OpenSSL."
    info "Artifact NOT authenticated: dry run does not download or verify bytes."
    info "Would then $PLAN, only after both checks pass."
    exit 0
fi

if [ -z "$ARTIFACT_URL" ]; then
    if [ -z "$TOKEN" ]; then
        die "no download token: pass --token <token> or set CHUDWARE_TOKEN.
       Sign in at $BASE_URL, open Downloads, and copy the install command —
       it already contains a fresh token."
    fi
    die "the download token was refused. Tokens expire after 15 minutes;
       open Downloads at $BASE_URL and copy a fresh install command."
fi

have openssl || die "OpenSSL 3 or newer with Ed25519 support is required to verify artifact signatures.
       No verifier will be downloaded and nothing has been installed."

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

DOWNLOAD_URL="$ARTIFACT_URL"
case "$ARTIFACT_URL" in
    */api/releases/artifact/*)
        # This gateway's own artifact route, which needs the token too. A CDN
        # pre-signed URL is deliberately left untouched: it already carries its
        # own authorisation, and an extra query parameter would invalidate the
        # signature over it.
        if [ -n "$TOKEN" ]; then
            case "$ARTIFACT_URL" in
                *\?*) DOWNLOAD_URL="$ARTIFACT_URL&download_token=$TOKEN" ;;
                *) DOWNLOAD_URL="$ARTIFACT_URL?download_token=$TOKEN" ;;
            esac
        fi
        ;;
esac

step "Downloading Chudware $VERSION ($(human_bytes "$EXPECTED_SIZE"))"
http_download "$DOWNLOAD_URL" "$ARTIFACT" || die "download failed"

step "Verifying sha256"
ACTUAL_SHA="$(sha256_of "$ARTIFACT")"
if [ -z "$ACTUAL_SHA" ]; then
    die "no sha256 tool found (sha256sum, shasum or openssl).
       The download cannot be verified, so it will not be installed.
       Install coreutils and re-run."
fi
if [ "$ACTUAL_SHA" != "$EXPECTED_SHA" ]; then
    printf 'error: SHA256 MISMATCH — the download does not match the release manifest.\n' >&2
    printf '       expected %s\n' "$EXPECTED_SHA" >&2
    printf '       actual   %s\n' "$ACTUAL_SHA" >&2
    printf '       Nothing has been installed. Re-run; if it happens again, do NOT\n' >&2
    printf '       install this file and report it at %s.\n' "$BASE_URL" >&2
    exit 1
fi
if [ -n "$EXPECTED_SIZE" ]; then
    ACTUAL_SIZE="$(wc -c < "$ARTIFACT" | tr -d ' ')"
    if [ "$ACTUAL_SIZE" != "$EXPECTED_SIZE" ]; then
        die "downloaded $ACTUAL_SIZE bytes but the manifest declares $EXPECTED_SIZE; not installing"
    fi
fi
info "sha256 OK: $ACTUAL_SHA"
step "Verifying the Ed25519 artifact signature"
verify_artifact_signature
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
if [ "$WANT_FORMAT" = "default" ]; then
    info "Run it with: $PREFIX/$PROGRAM"
else
    info "Run it with: chudware"
fi
info "Release notes and your account: $BASE_URL"
