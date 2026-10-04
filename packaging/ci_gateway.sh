# shellcheck shell=bash
# Gateway client for the bash legs of nightly-release.yml (linux-beta and
# macos-beta). Sourced, never executed. The Windows leg has the PowerShell twin
# in packaging/windows/publish_release.ps1; both speak the same API.
#
# Every call goes to the public TLS origin ${CHUDWARE_GATEWAY_PUBLIC_BASE}
# with the release token taken ONLY from the run-scoped 0600 curl --config file
# ${RUNNER_TEMP}/gw.curlrc: never argv (another process on a shared runner can
# read a command line, and GitHub's log masking does not help there), never a
# log. No builder reads gateway.env or writes the gateway's release directory;
# bytes reach incoming/ only through PUT /api/releases/ci/upload/<name>.
#
# Stays within Apple's bash 3.2: no arrays, no mapfile, no ${var^^}. Every
# helper ends in an explicit return and guards each command with `|| return 1`,
# because linux-beta calls them inside `if` tests in a step that runs without
# -e, where bash suppresses errexit for the whole call tree; without explicit
# returns a failed upload could report success.
#
# Requires: RUNNER_TEMP, CHUDWARE_GATEWAY_PUBLIC_BASE,
# CHUDWARE_GATEWAY_CURL_RETRY (word-split on purpose), CHUDWARE_RELEASE_PYTHON,
# and for ci_status RELEASE_COMMIT/RELEASE_PLATFORM/RELEASE_ARCH/RELEASE_VERSION.

# gw_open: write the 0600 curl config from GATEWAY_TOKEN, prove the public
# origin is reachable, and install this file as ${RUNNER_TEMP}/gw.sh, which
# every later step (and the failure reporter) sources. Run once per job.
gw_open() {
  [ -n "${GATEWAY_TOKEN:-}" ] || {
    echo '::error::CHUDWARE_GATEWAY_RELEASE_TOKEN is not set; this runner publishes over TLS and has no other credential'
    return 1
  }
  [ -n "${CHUDWARE_GATEWAY_PUBLIC_BASE:-}" ] || {
    echo '::error::CHUDWARE_GATEWAY_PUBLIC_BASE is not set'; return 1; }

  # Run-scoped: RUNNER_TEMP is wiped between jobs. The subshell keeps the
  # umask change local to this write.
  ( umask 077 && printf 'header = "Authorization: Bearer %s"\n' "${GATEWAY_TOKEN}" \
      > "${RUNNER_TEMP}/gw.curlrc" ) || return 1
  chmod 600 "${RUNNER_TEMP}/gw.curlrc" || return 1

  # Reachability is proven before hours of building depend on it. This
  # endpoint is anonymous and read-only, so a 200 here means DNS, TLS and
  # routing all work; the token itself is proven by the first ci/status.
  local probe
  probe="$(curl --fail-with-body --silent --show-error --max-time 30 \
    "${CHUDWARE_GATEWAY_PUBLIC_BASE}/api/releases/base-version")" || {
    echo "::error::cannot reach ${CHUDWARE_GATEWAY_PUBLIC_BASE} from this runner"
    return 1
  }
  echo "gateway reachable: ${probe}"

  cp "${GITHUB_WORKSPACE}/packaging/ci_gateway.sh" "${RUNNER_TEMP}/gw.sh" || return 1
  return 0
}

gw_post() {  # gw_post <path> <json file>
  curl --config "${RUNNER_TEMP}/gw.curlrc" --fail-with-body --silent --show-error \
    --max-time 300 ${CHUDWARE_GATEWAY_CURL_RETRY} \
    -H 'Content-Type: application/json' --data-binary "@$2" \
    "${CHUDWARE_GATEWAY_PUBLIC_BASE}$1"
}

gw_upload() {  # gw_upload <local file> <remote name>; echoes the gateway's json
  # --upload-file streams from disk; --data-binary would read a ~500 MB
  # artifact into memory first, and the handler wants a raw body either way.
  # The gateway writes <name>.part and renames, so ci/publish never
  # sees a torn upload.
  #
  # Over the edge's 100 MB body cap the artifact goes up in chunks (a
  # single PUT of a ~300 MB artifact is answered 413 by the proxy in front
  # of the public origin). Each chunk names the offset it continues and
  # the last one finalizes, so only that reply carries a digest.
  local size chunk offset read_bytes query response
  # A failed wc must not read as an empty size: the loop below would then
  # upload nothing and return success.
  size="$(wc -c < "$1")" || return 1
  size="$(printf '%s' "${size}" | tr -d ' ')"
  case "${size}" in ''|*[!0-9]*) echo "::error::cannot size $1" >&2; return 1 ;; esac
  if [ "${size}" -le 67108864 ]; then
    curl --config "${RUNNER_TEMP}/gw.curlrc" --fail-with-body --silent --show-error \
      --max-time 3600 ${CHUDWARE_GATEWAY_CURL_RETRY} --upload-file "$1" \
      "${CHUDWARE_GATEWAY_PUBLIC_BASE}/api/releases/ci/upload/$2"
    return $?
  fi
  chunk="${RUNNER_TEMP}/upload.chunk"
  offset=0
  response=""
  while [ "${offset}" -lt "${size}" ]; do
    # No status=none: macOS dd before the FreeBSD resync (Big Sur and earlier)
    # rejects it as an unknown operand. Its transfer summary goes to a file
    # and is shown only when dd fails.
    dd if="$1" of="${chunk}" bs=1048576 skip="$((offset / 1048576))" count=64 \
       2> "${chunk}.err" || {
        cat "${chunk}.err" >&2 2> /dev/null; rm -f "${chunk}" "${chunk}.err"; return 1; }
    read_bytes="$(wc -c < "${chunk}" | tr -d ' ')"
    [ "${read_bytes}" -gt 0 ] || { rm -f "${chunk}" "${chunk}.err"; return 1; }
    query="offset=${offset}"
    if [ "$((offset + read_bytes))" -ge "${size}" ]; then
      query="offset=${offset}&complete=1"
    fi
    response="$(curl --config "${RUNNER_TEMP}/gw.curlrc" --fail-with-body --silent \
      --show-error --max-time 3600 ${CHUDWARE_GATEWAY_CURL_RETRY} \
      --upload-file "${chunk}" \
      "${CHUDWARE_GATEWAY_PUBLIC_BASE}/api/releases/ci/upload/$2?${query}")" || {
        rm -f "${chunk}" "${chunk}.err"; printf '%s' "${response}"; return 1; }
    offset="$((offset + read_bytes))"
  done
  rm -f "${chunk}" "${chunk}.err"
  printf '%s' "${response}"
  return 0
}

gw_upload_verified() {  # gw_upload_verified <local file> <remote name>
  local file="$1" remote="$2" expected reported response
  [ -f "${file}" ] || { echo "::error::missing upload ${file}"; return 1; }
  expected="$("${CHUDWARE_RELEASE_PYTHON}" - "${file}" <<'PY'
import hashlib, sys
value = hashlib.sha256()
with open(sys.argv[1], "rb") as stream:
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        value.update(chunk)
print(value.hexdigest())
PY
)" || return 1
  response="$(gw_upload "${file}" "${remote}")" || {
    echo "::error::upload failed for ${remote}: ${response}"; return 1; }
  # The gateway re-digests these bytes again at publish time; comparing here
  # names transport corruption at the file that caused it instead of as a 422
  # an hour later.
  reported="$(printf '%s' "${response}" | "${CHUDWARE_RELEASE_PYTHON}" -c \
    'import json,sys; print(json.load(sys.stdin).get("sha256",""))')" || reported=""
  if [ "${reported}" != "${expected}" ]; then
    echo "::error::the gateway stored a different digest for ${remote}: expected ${expected}, got '${reported}'"
    return 1
  fi
  echo "uploaded ${remote} sha256=${reported}"
  return 0
}

# gw_upload_release <artifact> <canonical name> <payload dir> <evidence dir|"">
# Upload one artifact and every sidecar the gateway pairs with it by name into
# incoming/. Sidecars carry the artifact's own name plus a suffix; the payload
# signature REPLACES the .json (payload-manifest.v1.sig), while evidence
# signatures append .sig to the full evidence file name. An empty evidence dir
# means this build ships without an evidence bundle. A gateway 422 at
# ci/publish deletes the staged artifact (sidecars survive), so callers upload
# immediately before each publish and never reuse an upload across a retry.
gw_upload_release() {
  local artifact="$1" name="$2" payload="$3" evidence="$4" document suffix uploaded=0
  gw_upload_verified "${artifact}" "${name}" || return 1
  uploaded=$((uploaded + 1))
  for suffix in json sig; do
    gw_upload_verified "${payload}/payload-manifest.v1.${suffix}" \
      "${name}.payload-manifest.v1.${suffix}" || return 1
    uploaded=$((uploaded + 1))
  done
  if [ -n "${evidence}" ]; then
    for document in sbom.cdx.json provenance.slsa.json platform-test.v1.json; do
      for suffix in '' '.sig'; do
        gw_upload_verified "${evidence}/${document}${suffix}" "${name}.${document}${suffix}" \
          || return 1
        uploaded=$((uploaded + 1))
      done
    done
  fi
  echo "uploaded ${uploaded} files for ${name} into the gateway staging directory"
  return 0
}

ci_status() {  # ci_status <status> <detail>
  local body="${RUNNER_TEMP}/ci-status.json"
  "${CHUDWARE_RELEASE_PYTHON}" - "$1" "$2" > "${body}" <<'PY'
import json, os, sys
json.dump({
    "run_id": os.environ["GITHUB_RUN_ID"],
    "commit_sha": os.environ["RELEASE_COMMIT"],
    "channel": "beta",
    "platform": os.environ["RELEASE_PLATFORM"],
    "arch": os.environ["RELEASE_ARCH"],
    "version": os.environ["RELEASE_VERSION"],
    "status": sys.argv[1],
    "detail": sys.argv[2],
}, sys.stdout)
PY
  gw_post /api/releases/ci/status "${body}" > /dev/null \
    || echo "::warning::ci/status $1 was not recorded"
  return 0
}
