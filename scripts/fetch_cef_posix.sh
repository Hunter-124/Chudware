#!/usr/bin/env bash
# Reconstruct the exact SDK from authenticated archive bytes, never an existing
# extracted directory. Optional roots support bounded offline cache smoke tests.
set -euo pipefail
cache_root="${HOME}/chudware-cef-sdk"
destination_root="${HOME}/chudware-cef-sdk"
offline=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --cache-root) cache_root="$2"; shift 2 ;;
    --destination-root) destination_root="$2"; shift 2 ;;
    --offline) offline=1; shift ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
case "$(uname -s):$(uname -m)" in
  Linux:x86_64) flavor=linux64; sha256=f90dec4c5c42a7bbd4f2bd80a7a77e0ac6aacfc6627bb43572d803e77f26dfbc ;;
  Darwin:arm64) flavor=macosarm64; sha256=232412125e50f61597151eba49cee4082ccde55dc900bd8fd3252662f8c230f4 ;;
  Darwin:x86_64) flavor=macosx64; sha256=01fd96697c601c460387af8925e1bd09ad545509773e9f498f8240f036bf76f2 ;;
  *) echo 'No authenticated CEF SDK identity for this OS/architecture' >&2; exit 1 ;;
esac
version='149.0.6+g0d0eeb6+chromium-149.0.7827.201'
sdk_name="cef_binary_${version}_${flavor}_minimal"
file="${sdk_name}.tar.bz2"
# SHA256 derived from inert HTTPS bytes after checking the primary upstream
# https://cef-builds.spotifycdn.com/index.json SHA1 before extraction/execution:
# Linux d46ec0d5723771bd1c9678c429e1cdb1f1ef0a72;
# mac arm64 c8defef52974eb8fe01f556f16efd7f51c7b5972;
# mac x64 13f1397fec50641e854f3f302b9889bf6ebee1fd.
url="https://cef-builds.spotifycdn.com/${file//+/%2B}"
mkdir -p "${cache_root}"
archive="${cache_root}/${file}"
if [ ! -f "${archive}" ]; then
  [ "${offline}" = 0 ] || { echo 'Authenticated CEF archive missing (offline)' >&2; exit 1; }
  curl --proto '=https' --proto-redir '=https' -fL --retry 3 -o "${archive}" "${url}"
fi
if command -v sha256sum >/dev/null 2>&1; then
  actual="$(sha256sum "${archive}")"
else
  actual="$(shasum -a 256 "${archive}")"
fi
actual="${actual%% *}"
[ "${actual}" = "${sha256}" ] || { echo 'CEF cached archive SHA-256 mismatch; refusing extraction' >&2; exit 1; }
mkdir -p "${destination_root}"
sdk="${destination_root}/${sdk_name}"
# Only the pinned managed SDK, not unrelated versions/platforms, is replaced.
rm -rf "${sdk}"
tar -xjf "${archive}" -C "${destination_root}"
[ -f "${sdk}/CMakeLists.txt" ] || { echo 'Authenticated CEF SDK layout mismatch' >&2; exit 1; }
printf '%s\n' "${sdk}"
