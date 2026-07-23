#!/usr/bin/env bash
# Convenience wrapper — canonical installer lives under packaging/linux/.
# Installs Chudware.desktop + hicolor icons into ~/.local/share (no root).
# One-shot after a Linux build so the taskbar / launcher / alt-tab show the
# Chudware icon (matches WM_CLASS=Chudware set in host/src/app.cpp).
#
# Usage (from repo root):  scripts/install_desktop.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec "$ROOT/packaging/linux/install_desktop.sh"
