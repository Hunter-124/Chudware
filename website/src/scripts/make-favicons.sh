#!/usr/bin/env bash
# Rasterize the one brand mark (public/assets/logo.svg) into the favicon set
# that chudware.com serves.
#
# Google Search only shows a site's own icon when it can CRAWL one from the
# hostname's home page; anything else gets the generic globe. That rules out a
# data: URI, and it wants raster sizes that are a multiple of 48 px. So the site
# serves the set at stable root paths:
#   favicon.ico           16/32/48 px, the path every crawler and browser tries
#   icon-192.png          192 px (4×48) raster for Search
#   icon-512.png          Organization logo in the home page's structured data
#   apple-touch-icon.png  180 px, opaque — iOS paints transparent corners black
#
# Requires ImageMagick 7 with the librsvg delegate. Re-run after changing the
# mark and commit the outputs; nothing generates them at build time.
set -euo pipefail

SITE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SRC="$SITE/public/assets/logo.svg"
BG="#12181c"  # the mark's own tile colour

render() {  # render <px> <out> [flatten]
  local args=(-background none -density 1200 "$SRC" -resize "$1x$1")
  [[ "${3:-}" == flatten ]] && args+=(-background "$BG" -flatten -alpha off)
  magick "${args[@]}" -depth 8 -strip "$2"
}

out="$(mktemp -d)"
trap 'rm -rf "$out"' EXIT
for px in 16 32 48; do render "$px" "$out/$px.png"; done
magick "$out/16.png" "$out/32.png" "$out/48.png" "$out/favicon.ico"
render 192 "$out/icon-192.png"
render 512 "$out/icon-512.png"
render 180 "$out/apple-touch-icon.png" flatten

install -m 0644 "$out/favicon.ico" "$out/icon-192.png" "$out/icon-512.png" \
  "$out/apple-touch-icon.png" "$SITE/public/"
