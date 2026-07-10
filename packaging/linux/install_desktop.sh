#!/usr/bin/env bash
# Install the Billet desktop entry + icon into the user's local XDG dirs so the
# app shows a proper name/icon in the KDE/GNOME taskbar and launcher.
#
# The running window's WM_CLASS is "Billet" (set in host/src/app.cpp), and this
# entry's StartupWMClass=Billet binds the live window to it — so the taskbar icon
# resolves even for a window launched outside the launcher.
#
# The binary is still named AI-CAD this rename wave (user-facing identity only),
# so Exec keeps pointing at host/build-linux/bin/AI-CAD.
#
# Usage:  packaging/linux/install_desktop.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BIN="$REPO/host/build-linux/bin/AI-CAD"
ICON_SRC="$REPO/assets/logo_icon.png"
DOC_ICON_SRC="$REPO/assets/doc_icon.png"
# freedesktop icon name for the native document MIME type (application/vnd.billet.stepplus,
# registered by services/installer/setup_file_assoc.py): the MIME with '/' -> '-'.
MIME_ICON_NAME="application-vnd.billet.stepplus"

[ -x "$BIN" ] || { echo "error: binary not found/executable: $BIN"; echo "build it: cmake -S host -B host/build-linux && ninja -C host/build-linux AI-CAD"; exit 1; }
[ -f "$ICON_SRC" ] || { echo "error: icon not found: $ICON_SRC"; exit 1; }

APPS="$HOME/.local/share/applications"
ICONS="$HOME/.local/share/icons/hicolor"
mkdir -p "$APPS"

# Install the icon under the theme name "Billet" at several sizes so the shell can
# pick a crisp one. Requires ImageMagick for downscaling; falls back to the 256px
# source at 256x256 if `convert`/`magick` are unavailable.
install_icon() {
  local size="$1"
  local dir="$ICONS/${size}x${size}/apps"
  mkdir -p "$dir"
  if command -v magick >/dev/null 2>&1; then
    magick "$ICON_SRC" -resize "${size}x${size}" "$dir/Billet.png"
  elif command -v convert >/dev/null 2>&1; then
    convert "$ICON_SRC" -resize "${size}x${size}" "$dir/Billet.png"
  elif [ "$size" = "256" ]; then
    cp "$ICON_SRC" "$dir/Billet.png"
  fi
}
for s in 16 32 48 64 128 256; do install_icon "$s"; done

# Install the branded DOCUMENT icon for the Billet MIME type so Dolphin/Nautilus
# show .step+ files with the doc page icon (not a blank page). Same resize
# mechanism as install_icon, but into the theme's mimetypes/ dirs.
install_mime_icon() {
  local size="$1"
  local dir="$ICONS/${size}x${size}/mimetypes"
  mkdir -p "$dir"
  if command -v magick >/dev/null 2>&1; then
    magick "$DOC_ICON_SRC" -resize "${size}x${size}" "$dir/$MIME_ICON_NAME.png"
  elif command -v convert >/dev/null 2>&1; then
    convert "$DOC_ICON_SRC" -resize "${size}x${size}" "$dir/$MIME_ICON_NAME.png"
  elif [ "$size" = "256" ]; then
    cp "$DOC_ICON_SRC" "$dir/$MIME_ICON_NAME.png"
  fi
}
if [ -f "$DOC_ICON_SRC" ]; then
  for s in 16 32 48 64 128 256; do install_mime_icon "$s"; done
else
  echo "note: $DOC_ICON_SRC not found — skipping the document MIME icon"
fi

# Write the desktop entry with absolute Exec/Icon so it works without a PATH install.
cat > "$APPS/Billet.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Billet
Comment=AI-native parametric CAD
Exec=$BIN %F
Icon=Billet
Categories=Graphics;Engineering;
Terminal=false
StartupWMClass=Billet
EOF

# Migration cleanup: retire the pre-rename AI-CAD entry + hicolor icons, and the
# stale ui/imgui entry (Exec pointed at the removed aicad2-dev binary).
for STALE in "$APPS/AI-CAD.desktop" "$APPS/aicad2-dev.desktop"; do
  [ -f "$STALE" ] && { rm -f "$STALE"; echo "removed stale $STALE"; }
done
for s in 16 32 48 64 128 256; do
  OLD_ICON="$ICONS/${s}x${s}/apps/AI-CAD.png"
  [ -f "$OLD_ICON" ] && { rm -f "$OLD_ICON"; echo "removed stale $OLD_ICON"; }
  # Old MIME icon name (application/vnd.aicad.stepplus, pre-rename).
  OLD_MIME_ICON="$ICONS/${s}x${s}/mimetypes/application-vnd.aicad.stepplus.png"
  [ -f "$OLD_MIME_ICON" ] && { rm -f "$OLD_MIME_ICON"; echo "removed stale $OLD_MIME_ICON"; }
done

command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" 2>/dev/null || true
command -v gtk-update-icon-cache   >/dev/null 2>&1 && gtk-update-icon-cache -f -t "$ICONS" 2>/dev/null || true

echo "installed Billet.desktop + icon."
echo "If you have a pinned taskbar launcher, un-pin and re-pin it (it may cache the old AI-CAD/aicad2-dev entry)."
