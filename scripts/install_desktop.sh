#!/usr/bin/env bash
# Provision the runtime, then invoke the canonical packaging/linux/ installer.
# Installs chudware.desktop + hicolor icons into ~/.local/share (no root).
# Run after a Linux build to match the launcher's WM_CLASS=chudware.
#
# Usage (from repo root):  scripts/install_desktop.sh
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
"$ROOT/scripts/provision_venv.sh"
exec "$ROOT/packaging/linux/install_desktop.sh"
