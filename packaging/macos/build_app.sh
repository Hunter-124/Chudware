#!/usr/bin/env bash
# Assemble the AppleClang host build into a redistributable Chudware.app and zip.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="$REPO/dist"
KEEP_APP=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        --out)
            [ "$#" -ge 2 ] || { echo "error: --out requires a directory" >&2; exit 2; }
            OUT="$2"
            shift 2
            ;;
        --keep-app) KEEP_APP=1; shift ;;
        *) echo "error: unknown argument: $1" >&2; exit 2 ;;
    esac
done

[ "$(uname -s)" = "Darwin" ] || {
    echo "error: packaging/macos/build_app.sh must run on macOS" >&2
    exit 1
}

ARCH="$(uname -m)"
case "$ARCH" in
    arm64) PLATFORM_SUFFIX="macos-arm64"; BREW_PREFIX="/opt/homebrew" ;;
    x86_64) PLATFORM_SUFFIX="macos-x86_64"; BREW_PREFIX="/usr/local" ;;
    *) echo "error: unsupported macOS architecture: $ARCH" >&2; exit 1 ;;
esac
BUILD="$REPO/host/build-$PLATFORM_SUFFIX"
BIN="$BUILD/bin/chudware"
[ -x "$BIN" ] || {
    echo "error: $BIN not found; run ./build.sh all on this Mac first" >&2
    exit 1
}

# Import the canonical helper rather than implementing another version parser. The helper
# reads the exact generated header compiled into this binary.
VERSION_HELPER="$(sed -n '/^chudware_read_version() {/,/^}/p' "$REPO/build.sh")"
[ -n "$VERSION_HELPER" ] || { echo "error: chudware_read_version missing from build.sh" >&2; exit 1; }
# shellcheck disable=SC2294
_eval_version_helper() { eval "$VERSION_HELPER"; }
_eval_version_helper
chudware_read_version "$BUILD" || {
    echo "error: $BUILD/generated/api/chudware_version.h is missing or invalid; reconfigure the host build" >&2
    exit 1
}
VERSION="$CHUDWARE_VERSION"

for tool in otool install_name_tool ditto plutil; do
    command -v "$tool" >/dev/null 2>&1 || { echo "error: required tool not found: $tool" >&2; exit 1; }
done

APP="$OUT/Chudware.app"
CONTENTS="$APP/Contents"
MACOS="$CONTENTS/MacOS"
RESOURCES="$CONTENTS/Resources"
FRAMEWORKS="$CONTENTS/Frameworks"
rm -rf "$APP"
mkdir -p "$MACOS" "$RESOURCES" "$FRAMEWORKS"

sed "s/@CHUDWARE_VERSION@/$VERSION/g" "$REPO/packaging/macos/Info.plist" > "$CONTENTS/Info.plist"
plutil -lint "$CONTENTS/Info.plist" >/dev/null
cp "$REPO/packaging/macos/Chudware.icns" "$RESOURCES/Chudware.icns"
cp "$REPO/LICENSE-EULA.txt" "$RESOURCES/LICENSE-EULA.txt"
cp "$REPO/packaging/macos/RELEASE_NOTES.md" "$RESOURCES/RELEASE_NOTES.md"
cp "$BIN" "$MACOS/Chudware-bin"
chmod +x "$MACOS/Chudware-bin"

if [ -d "$BUILD/bin/assets" ]; then
    cp -R "$BUILD/bin/assets" "$MACOS/assets"
elif [ -d "$REPO/assets" ]; then
    cp -R "$REPO/assets" "$MACOS/assets"
fi

ENGINE=""
for candidate in \
    "$BUILD/bin/libchudware.dylib" \
    "$REPO/core/build-occt-$PLATFORM_SUFFIX/bin/libchudware.dylib" \
    "$REPO/core/build-$PLATFORM_SUFFIX/bin/libchudware.dylib" \
    "$REPO/build/$PLATFORM_SUFFIX/bin/libchudware.dylib"; do
    if [ -f "$candidate" ]; then ENGINE="$candidate"; break; fi
done
[ -n "$ENGINE" ] || {
    echo "error: no libchudware.dylib found for $ARCH; run ./build.sh all" >&2
    exit 1
}
cp "$ENGINE" "$FRAMEWORKS/libchudware.dylib"
chmod u+w "$FRAMEWORKS/libchudware.dylib"
ENGINE_DIR="$(dirname "$ENGINE")"
for sibling in libnglib.dylib libngcore.dylib lib3mf.dylib; do
    if [ -f "$ENGINE_DIR/$sibling" ]; then
        cp "$ENGINE_DIR/$sibling" "$FRAMEWORKS/$sibling"
        chmod u+w "$FRAMEWORKS/$sibling"
    fi
done

HOSTD=""
for candidate in \
    "$REPO/core/build-occt-$PLATFORM_SUFFIX/bin/chudware-hostd" \
    "$REPO/core/build-$PLATFORM_SUFFIX/bin/chudware-hostd" \
    "$REPO/build/$PLATFORM_SUFFIX/bin/chudware-hostd"; do
    if [ -x "$candidate" ]; then HOSTD="$candidate"; break; fi
done
if [ -n "$HOSTD" ]; then
    cp "$HOSTD" "$MACOS/chudware-hostd"
    chmod +x "$MACOS/chudware-hostd"
else
    echo "WARNING: chudware-hostd was not found; the app will use its in-process fallback" >&2
fi

# Bootstrap looks beside the executable for app/requirements.txt and service packages.
APP_PAYLOAD="$MACOS/app"
mkdir -p "$APP_PAYLOAD/services"
if [ -d "$REPO/engine" ]; then
    rsync -a --exclude='__pycache__' --exclude='*.pyc' --exclude='build' --exclude='test_*' \
        "$REPO/engine/" "$APP_PAYLOAD/engine/"
fi
if [ -f "$REPO/services/__init__.py" ]; then
    cp "$REPO/services/__init__.py" "$APP_PAYLOAD/services/__init__.py"
else
    : > "$APP_PAYLOAD/services/__init__.py"
fi
for package in agent mcp installer plm dag browser deliverables ipc serving verify parts reveng sim vision; do
    if [ -d "$REPO/services/$package" ]; then
        rsync -a --exclude='__pycache__' --exclude='*.pyc' --exclude='build' --exclude='test_*' \
            "$REPO/services/$package/" "$APP_PAYLOAD/services/$package/"
    fi
done
for module in "$REPO"/services/*.py; do
    [ -f "$module" ] || continue
    case "$(basename "$module")" in __init__.py|test_*) continue ;; esac
    cp "$module" "$APP_PAYLOAD/services/"
done
cp "$REPO/requirements.txt" "$APP_PAYLOAD/requirements.txt"

# Resolve @rpath/@loader_path entries when deciding whether a dependency belongs in the
# bundle. Homebrew's OCCT bottles normally use absolute Cellar names, but formula revisions
# can switch to @rpath without notice.
resolve_dependency() {
    local owner="$1" dep="$2" candidate occt_prefix
    case "$dep" in
        /*) candidate="$dep" ;;
        @loader_path/*) candidate="$(dirname "$owner")/${dep#@loader_path/}" ;;
        @executable_path/*) candidate="$MACOS/${dep#@executable_path/}" ;;
        @rpath/*)
            candidate="$(dirname "$owner")/${dep#@rpath/}"
            if [ ! -f "$candidate" ]; then candidate="$FRAMEWORKS/${dep#@rpath/}"; fi
            if [ ! -f "$candidate" ]; then candidate="$ENGINE_DIR/${dep#@rpath/}"; fi
            if [ ! -f "$candidate" ] && command -v brew >/dev/null 2>&1; then
                occt_prefix="$(brew --prefix opencascade 2>/dev/null || true)"
                if [ -n "$occt_prefix" ]; then candidate="$occt_prefix/lib/${dep#@rpath/}"; fi
            fi
            ;;
        *) return 1 ;;
    esac
    [ -f "$candidate" ] || return 1
    (cd "$(dirname "$candidate")" && printf '%s/%s\n' "$PWD" "$(basename "$candidate")")
}

should_bundle() {
    case "$1" in
        "$BREW_PREFIX"/*|"$REPO"/core/build-*/*|"$REPO"/build/*) return 0 ;;
        *) return 1 ;;
    esac
}

TMP_QUEUE="$(mktemp -d "${TMPDIR:-/tmp}/chudware-dylibs.XXXXXX")"
trap 'rm -rf "$TMP_QUEUE"' EXIT
QUEUE="$TMP_QUEUE/queue"
SEEN="$TMP_QUEUE/seen"
: > "$QUEUE"
: > "$SEEN"
printf '%s\n' "$MACOS/Chudware-bin" >> "$QUEUE"
for bundled_dylib in "$FRAMEWORKS"/*.dylib; do
    [ -f "$bundled_dylib" ] && printf '%s\n' "$bundled_dylib" >> "$QUEUE"
done
[ -n "$HOSTD" ] && printf '%s\n' "$MACOS/chudware-hostd" >> "$QUEUE"

index=1
while :; do
    current="$(sed -n "${index}p" "$QUEUE")"
    [ -n "$current" ] || break
    index=$((index + 1))
    grep -Fqx "$current" "$SEEN" && continue
    printf '%s\n' "$current" >> "$SEEN"
    chmod u+w "$current"

    while IFS= read -r dependency; do
        [ -n "$dependency" ] || continue
        resolved="$(resolve_dependency "$current" "$dependency" || true)"
        [ -n "$resolved" ] || continue
        should_bundle "$resolved" || continue
        base="$(basename "$resolved")"
        destination="$FRAMEWORKS/$base"
        if [ ! -f "$destination" ]; then
            cp -L "$resolved" "$destination"
            chmod u+w "$destination"
            printf '%s\n' "$destination" >> "$QUEUE"
        fi
        case "$current" in
            "$MACOS"/*) replacement="@loader_path/../Frameworks/$base" ;;
            *) replacement="@loader_path/$base" ;;
        esac
        [ "$dependency" = "$replacement" ] || install_name_tool -change "$dependency" "$replacement" "$current"
    done < <(otool -L "$current" | sed -n '2,$p' | awk '{print $1}')

    # CMake's ELF token is literal in Mach-O load commands. Translate every such LC_RPATH;
    # copying host/cmake/cef.cmake's $ORIGIN form would leave an unusable bundle.
    while IFS= read -r old_rpath; do
        [ -n "$old_rpath" ] || continue
        case "$old_rpath" in
            \$ORIGIN*) new_rpath="@loader_path${old_rpath#\$ORIGIN}" ;;
            *) continue ;;
        esac
        install_name_tool -rpath "$old_rpath" "$new_rpath" "$current"
    done < <(otool -l "$current" | awk '/cmd LC_RPATH/{want=1; next} want && $1=="path"{print $2; want=0}')

done

for dylib in "$FRAMEWORKS"/*.dylib; do
    [ -f "$dylib" ] || continue
    install_name_tool -id "@rpath/$(basename "$dylib")" "$dylib"
done

cat > "$MACOS/Chudware" <<'LAUNCHER'
#!/bin/sh
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
EULA="$HERE/../Resources/LICENSE-EULA.txt"
STATE="$HOME/Library/Application Support/Chudware/eula-@CHUDWARE_VERSION@.accepted"
if [ ! -f "$STATE" ]; then
    while :; do
        choice=$(osascript <<'APPLESCRIPT'
button returned of (display dialog "Please review the Chudware End User License Agreement before using Chudware. By selecting Accept, you agree to its terms." with title "Chudware License Agreement" buttons {"Decline", "View License", "Accept"} default button "Accept" cancel button "Decline" with icon caution)
APPLESCRIPT
        ) || exit 1
        case "$choice" in
            "Accept") mkdir -p "$(dirname "$STATE")"; printf '%s\n' accepted > "$STATE"; break ;;
            "View License") open "$EULA" ;;
            *) exit 1 ;;
        esac
    done
fi
export CHUDWARE_LIBCHUDWARE="$HERE/../Frameworks/libchudware.dylib"
if [ -x "$HERE/chudware-hostd" ]; then export CHUDWARE_HOSTD_BIN="$HERE/chudware-hostd"; fi
exec "$HERE/Chudware-bin" "$@"
LAUNCHER
sed -i '' "s/@CHUDWARE_VERSION@/$VERSION/g" "$MACOS/Chudware"
chmod +x "$MACOS/Chudware"

# Fail before signing if any non-system dependency still resolves outside the bundle.
for executable in "$MACOS/Chudware-bin" "$MACOS/chudware-hostd" "$FRAMEWORKS"/*.dylib; do
    [ -f "$executable" ] || continue
    while IFS= read -r dependency; do
        dependency_target=""
        case "$dependency" in
            /System/*|/usr/lib/*) continue ;;
            @loader_path/*) dependency_target="$(dirname "$executable")/${dependency#@loader_path/}" ;;
            @rpath/*) dependency_target="$FRAMEWORKS/${dependency#@rpath/}" ;;
            @executable_path/*) dependency_target="$MACOS/${dependency#@executable_path/}" ;;
        esac
        if [ -z "$dependency_target" ] || [ ! -f "$dependency_target" ]; then
            echo "error: unresolved external Mach-O dependency in $executable: $dependency" >&2
            exit 1
        fi
    done < <(otool -L "$executable" | sed -n '2,$p' | awk '{print $1}')
done

SIGN_IDENTITY="${CHUDWARE_SIGN_IDENTITY:-}"
NOTARIZE_PROFILE="${CHUDWARE_NOTARIZE_PROFILE:-}"
if [ -n "$SIGN_IDENTITY" ]; then
    echo "==> signing Chudware.app with identity: $SIGN_IDENTITY"
    codesign --force --deep --options runtime --timestamp --sign "$SIGN_IDENTITY" "$APP"
    codesign --verify --deep --strict --verbose=2 "$APP"
else
    echo "WARNING: unsigned build — SmartScreen/Gatekeeper will warn users" >&2
fi

mkdir -p "$OUT"
ZIP="$OUT/Chudware-$VERSION-macos-$ARCH.zip"
rm -f "$ZIP"
ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP"

if [ -n "$NOTARIZE_PROFILE" ]; then
    [ -n "$SIGN_IDENTITY" ] || {
        echo "error: CHUDWARE_NOTARIZE_PROFILE requires CHUDWARE_SIGN_IDENTITY" >&2
        exit 1
    }
    echo "==> notarizing with keychain profile: $NOTARIZE_PROFILE"
    xcrun notarytool submit "$ZIP" --keychain-profile "$NOTARIZE_PROFILE" --wait
    xcrun stapler staple "$APP"
    rm -f "$ZIP"
    ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP"
fi

if [ "$KEEP_APP" != "1" ]; then rm -rf "$APP"; fi
size="$(stat -f %z "$ZIP")"
echo "==> built: $ZIP ($size bytes)"
