#!/usr/bin/env bash
# Install the chudware desktop entry + icon into the user's local XDG dirs so the
# app shows a proper name/icon in the KDE/GNOME taskbar and launcher.
#
# The running window's WM_CLASS is "chudware" (set in host/src/app.cpp), and this
# entry's StartupWMClass=chudware binds the live window to it — so the taskbar icon
# resolves even for a window launched outside the launcher.
#
# The binary is named chudware (deep rename); a Chudware compat symlink ships beside
# it for legacy scripts, but Exec points at the canonical host/build-linux/bin/chudware.
#
# Usage:  packaging/linux/install_desktop.sh
#    or:  scripts/install_desktop.sh   (thin wrapper)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BIN="$REPO/host/build-linux/bin/chudware"
ICON_SRC="$REPO/assets/logo_icon.png"
DOC_ICON_SRC="$REPO/assets/doc_icon.png"
# freedesktop icon name for the native document MIME type (application/vnd.chudware.stepplus,
# registered by services/installer/setup_file_assoc.py): the MIME with '/' -> '-'.
MIME_ICON_NAME="application-vnd.chudware.stepplus"

[ -x "$BIN" ] || { echo "error: binary not found/executable: $BIN"; echo "build it: cmake -S host -B host/build-linux && ninja -C host/build-linux Chudware"; exit 1; }
[ -f "$ICON_SRC" ] || { echo "error: icon not found: $ICON_SRC"; exit 1; }

APPS="$HOME/.local/share/applications"
ICONS="$HOME/.local/share/icons/hicolor"
mkdir -p "$APPS"

# Install the icon under the theme name "Chudware" at several sizes so the shell can
# pick a crisp one. Prefer ImageMagick; fall back to Python/Pillow; last resort
# copy the 256px source only into the 256 slot.
install_icon() {
  local size="$1"
  local dir="$ICONS/${size}x${size}/apps"
  mkdir -p "$dir"
  local dest="$dir/Chudware.png"
  if command -v magick >/dev/null 2>&1; then
    magick "$ICON_SRC" -resize "${size}x${size}" "$dest"
  elif command -v convert >/dev/null 2>&1; then
    convert "$ICON_SRC" -resize "${size}x${size}" "$dest"
  elif command -v python3 >/dev/null 2>&1; then
    python3 - "$ICON_SRC" "$dest" "$size" <<'PY'
import sys
from PIL import Image
src, dest, size = sys.argv[1], sys.argv[2], int(sys.argv[3])
Image.open(src).convert("RGBA").resize((size, size), Image.Resampling.LANCZOS).save(dest)
PY
  elif [ "$size" = "256" ]; then
    cp "$ICON_SRC" "$dest"
  fi
}
for s in 16 32 48 64 128 256; do install_icon "$s"; done

# Install the branded DOCUMENT icon for the Chudware MIME type so Dolphin/Nautilus
# show .step+ files with the doc page icon (not a blank page). Same resize
# mechanism as install_icon, but into the theme's mimetypes/ dirs.
install_mime_icon() {
  local size="$1"
  local dir="$ICONS/${size}x${size}/mimetypes"
  mkdir -p "$dir"
  local dest="$dir/$MIME_ICON_NAME.png"
  if command -v magick >/dev/null 2>&1; then
    magick "$DOC_ICON_SRC" -resize "${size}x${size}" "$dest"
  elif command -v convert >/dev/null 2>&1; then
    convert "$DOC_ICON_SRC" -resize "${size}x${size}" "$dest"
  elif command -v python3 >/dev/null 2>&1; then
    python3 - "$DOC_ICON_SRC" "$dest" "$size" <<'PY'
import sys
from PIL import Image
src, dest, size = sys.argv[1], sys.argv[2], int(sys.argv[3])
Image.open(src).convert("RGBA").resize((size, size), Image.Resampling.LANCZOS).save(dest)
PY
  elif [ "$size" = "256" ]; then
    cp "$DOC_ICON_SRC" "$dest"
  fi
}
if [ -f "$DOC_ICON_SRC" ]; then
  for s in 16 32 48 64 128 256; do install_mime_icon "$s"; done
else
  echo "note: $DOC_ICON_SRC not found — skipping the document MIME icon"
fi

# Write the desktop entry with absolute Exec/Icon so it works without a PATH install.
cat > "$APPS/chudware.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Chudware
Comment=Chudware — AI-native parametric CAD
Exec=$BIN %F
Icon=Chudware
Categories=Graphics;Engineering;
Terminal=false
StartupWMClass=chudware
EOF

# Migration cleanup: retire the pre-rename launcher entries (Billet.desktop, AI-CAD.desktop)
# and the stale ui/imgui entry (Exec pointed at the removed aicad2-dev binary) so old
# installs don't show a duplicate launcher entry alongside chudware.desktop above.
for STALE in "$APPS/Billet.desktop" "$APPS/AI-CAD.desktop" "$APPS/aicad2-dev.desktop"; do
  [ -f "$STALE" ] && { rm -f "$STALE"; echo "removed stale $STALE"; }
done
for s in 16 32 48 64 128 256; do
  for OLD in Billet AI-CAD; do
    OLD_ICON="$ICONS/${s}x${s}/apps/${OLD}.png"
    [ -f "$OLD_ICON" ] && { rm -f "$OLD_ICON"; echo "removed stale $OLD_ICON"; }
  done
  # Old MIME icon name (application/vnd.aicad.stepplus, pre-rename).
  OLD_MIME_ICON="$ICONS/${s}x${s}/mimetypes/application-vnd.aicad.stepplus.png"
  [ -f "$OLD_MIME_ICON" ] && { rm -f "$OLD_MIME_ICON"; echo "removed stale $OLD_MIME_ICON"; }
done

command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" 2>/dev/null || true
command -v gtk-update-icon-cache   >/dev/null 2>&1 && gtk-update-icon-cache -f -t "$ICONS" 2>/dev/null || true

echo "installed chudware.desktop + icon."
echo "If you have a pinned taskbar launcher, un-pin and re-pin it (it may cache the old Billet/AI-CAD/aicad2-dev entry)."
