#!/usr/bin/env bash
# Install the AI-CAD desktop entry + icon into the user's local XDG dirs so the
# app shows a proper name/icon in the KDE/GNOME taskbar and launcher.
#
# The running window's WM_CLASS is "AI-CAD" (set in host/src/app.cpp), and this
# entry's StartupWMClass=AI-CAD binds the live window to it — so the taskbar icon
# resolves even for a window launched outside the launcher.
#
# Usage:  packaging/linux/install_desktop.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BIN="$REPO/host/build-linux/bin/AI-CAD"
ICON_SRC="$REPO/assets/logo_icon.png"

[ -x "$BIN" ] || { echo "error: binary not found/executable: $BIN"; echo "build it: cmake -S host -B host/build-linux && ninja -C host/build-linux AI-CAD"; exit 1; }
[ -f "$ICON_SRC" ] || { echo "error: icon not found: $ICON_SRC"; exit 1; }

APPS="$HOME/.local/share/applications"
ICONS="$HOME/.local/share/icons/hicolor"
mkdir -p "$APPS"

# Install the icon under the theme name "AI-CAD" at several sizes so the shell can
# pick a crisp one. Requires ImageMagick for downscaling; falls back to the 256px
# source at 256x256 if `convert`/`magick` are unavailable.
install_icon() {
  local size="$1"
  local dir="$ICONS/${size}x${size}/apps"
  mkdir -p "$dir"
  if command -v magick >/dev/null 2>&1; then
    magick "$ICON_SRC" -resize "${size}x${size}" "$dir/AI-CAD.png"
  elif command -v convert >/dev/null 2>&1; then
    convert "$ICON_SRC" -resize "${size}x${size}" "$dir/AI-CAD.png"
  elif [ "$size" = "256" ]; then
    cp "$ICON_SRC" "$dir/AI-CAD.png"
  fi
}
for s in 16 32 48 64 128 256; do install_icon "$s"; done

# Write the desktop entry with absolute Exec/Icon so it works without a PATH install.
cat > "$APPS/AI-CAD.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=AI-CAD
Comment=AI-native parametric CAD
Exec=$BIN %F
Icon=AI-CAD
Categories=Graphics;Engineering;
Terminal=false
StartupWMClass=AI-CAD
EOF

# Retire the stale ui/imgui entry (Exec pointed at the removed aicad2-dev binary).
STALE="$APPS/aicad2-dev.desktop"
[ -f "$STALE" ] && { rm -f "$STALE"; echo "removed stale $STALE"; }

command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" 2>/dev/null || true
command -v gtk-update-icon-cache   >/dev/null 2>&1 && gtk-update-icon-cache -f -t "$ICONS" 2>/dev/null || true

echo "installed AI-CAD.desktop + icon."
echo "If you have a pinned taskbar launcher, un-pin and re-pin it (it may cache the old aicad2-dev entry)."
