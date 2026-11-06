#!/usr/bin/env bash
# Initialize only an ephemeral GitHub Actions Linux job container.
# Test collection replaces HOME; checkout trust must survive that isolation.
set -euo pipefail
: "${GITHUB_ENV:?Run inside a GitHub Actions job container}"
: "${GITHUB_WORKSPACE:?Set the trusted checkout path}"

state="$(mktemp -d /tmp/chudware-ci.XXXXXXXX)"
install -d -m 700 "$state/home" "$state/data" "$state/tmp"
export HOME="$state/home" XDG_DATA_HOME="$state/data" TMPDIR="$state/tmp"
{
    echo "HOME=$HOME"
    echo "XDG_DATA_HOME=$XDG_DATA_HOME"
    echo "TMPDIR=$TMPDIR"
} >> "$GITHUB_ENV"

# Trust this checkout only, in this disposable container, across HOME changes.
git config --system --add safe.directory "$GITHUB_WORKSPACE"
git -C "$GITHUB_WORKSPACE" describe --tags --always --dirty
