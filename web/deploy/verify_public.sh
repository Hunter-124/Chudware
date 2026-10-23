#!/usr/bin/env bash
#
# Usage: OWNER_PASSWORD=... Webservice/deploy/verify_public.sh
#
# Replaces the runbook recipe that authenticated with HTTP Basic and
# CHUDWARE_WEB_SSO_SHARED_SECRET. That path no longer exists in auth.py, so it
# now returns 401 for every route and reads as a total authorization failure.
# Post-cutover acceptance, run from outside the LAN over the public internet.
# curl is used deliberately: Cloudflare bot protection 403s Python-urllib's
# default agent, which is indistinguishable from a real authorization failure.
set -u

UA='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36'
PASS_COUNT=0
FAIL_COUNT=0

check() { # label expected url
  local label="$1" expect="$2" url="$3"
  local code
  code=$(curl -sL -o /dev/null -w '%{http_code}' -A "$UA" --max-time 25 "$url")
  if [ "$code" = "$expect" ]; then
    printf '  ok   %-4s %s\n' "$code" "$label"
    PASS_COUNT=$((PASS_COUNT + 1))
  else
    printf '  FAIL %-4s (wanted %s) %s\n' "$code" "$expect" "$label"
    FAIL_COUNT=$((FAIL_COUNT + 1))
  fi
}

echo 'PUBLIC MARKETING (anonymous, redirects followed)'
for p in / /pricing /gallery /download /docs/ /docs/ai-copilot/ /changelog/ \
         /legal/privacy /legal/terms /404.html; do
  check "chudware.com$p" 200 "https://chudware.com$p"
done
check 'www.chudware.com/' 200 'https://www.chudware.com/'

echo
echo 'NO ACCESS WALL ON MARKETING (must not reach cloudflareaccess.com)'
final=$(curl -sL -o /dev/null -w '%{url_effective}' -A "$UA" --max-time 25 https://chudware.com/)
if [[ "$final" == *cloudflareaccess.com* ]]; then
  echo "  FAIL apex still redirects to Access: $final"; FAIL_COUNT=$((FAIL_COUNT + 1))
else
  echo "  ok   apex terminates at $final"; PASS_COUNT=$((PASS_COUNT + 1))
fi

echo
echo 'APP PUBLIC SIGN-IN SURFACE (self-serve signup must work anonymously)'
check 'app/signin' 200 'https://app.chudware.com/signin'
check 'app/signup' 200 'https://app.chudware.com/signup'
check 'app/healthz' 200 'https://app.chudware.com/healthz'

echo
echo 'APP PRIVILEGED SURFACE (anonymous must never get 200)'
for p in / /workspace/list /bucket /api/me /api/sessions /api/provider-keys \
         /api/admin/accounts /api/admin/ops /static/admin.js; do
  code=$(curl -s -o /dev/null -w '%{http_code}' -A "$UA" --max-time 25 "https://app.chudware.com$p")
  if [ "$code" = "200" ]; then
    printf '  FAIL %-4s OPEN TO THE WORLD: %s\n' "$code" "$p"; FAIL_COUNT=$((FAIL_COUNT + 1))
  else
    printf '  ok   %-4s closed: %s\n' "$code" "$p"; PASS_COUNT=$((PASS_COUNT + 1))
  fi
done

echo
echo 'ADMIN IS BEHIND THE ACCOUNT SYSTEM, NOT CLOUDFLARE ACCESS'
# Accept: text/html is load-bearing: auth.py::_deny_http answers a browser
# navigation with the 303 to /signin and an XHR with a JSON 401, so curl's
# default 'Accept: */*' would measure the wrong branch and read as a failure.
read -r code loc < <(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' \
  -A "$UA" -H 'Accept: text/html' --max-time 25 https://app.chudware.com/admin)
if [[ "$loc" == *cloudflareaccess.com* ]]; then
  echo "  FAIL /admin still walled off by Cloudflare Access: $loc"; FAIL_COUNT=$((FAIL_COUNT + 1))
elif [ "$code" = "303" ] && [[ "$loc" == */signin?next=%2Fadmin ]]; then
  echo '  ok   /admin sends an anonymous browser to the gateway sign-in'; PASS_COUNT=$((PASS_COUNT + 1))
else
  echo "  FAIL /admin gave $code -> '$loc', wanted 303 to /signin?next=%2Fadmin"; FAIL_COUNT=$((FAIL_COUNT + 1))
fi

echo
if [ -z "${OWNER_PASSWORD:-}" ]; then
  echo 'REAL SIGN-IN — skipped (set OWNER_PASSWORD to include it)'
else
echo 'REAL SIGN-IN WITH THE OWNER PASSWORD (over the public internet)'
JAR=$(mktemp)
login=$(curl -s -o /dev/null -w '%{http_code}' -A "$UA" --max-time 25 -c "$JAR" \
  -H 'Content-Type: application/json' \
  --data-binary @- https://app.chudware.com/api/auth/login < <(
    python3 -c 'import json, os; print(json.dumps({"email": "haedmonds69@gmail.com", "password": os.environ["OWNER_PASSWORD"]}))'))
if [ "$login" = "200" ] && grep -q chudware_sid "$JAR"; then
  echo '  ok   login returned 200 and set a chudware_sid cookie'; PASS_COUNT=$((PASS_COUNT + 1))
else
  echo "  FAIL login=$login cookie=$(grep -c chudware_sid "$JAR")"; FAIL_COUNT=$((FAIL_COUNT + 1))
fi

me=$(curl -s -A "$UA" --max-time 25 -b "$JAR" https://app.chudware.com/api/me)
if [[ "$me" == *'"role":"admin"'* ]] || [[ "$me" == *'"role": "admin"'* ]]; then
  echo "  ok   /api/me with that session: $(echo "$me" | cut -c1-120)"; PASS_COUNT=$((PASS_COUNT + 1))
else
  echo "  FAIL /api/me did not show an admin session: $(echo "$me" | cut -c1-160)"
  FAIL_COUNT=$((FAIL_COUNT + 1))
fi

# The whole point of the cutover: the session cookie alone opens /admin. This
# jar holds no CF_Authorization, so a 200 here proves the account store is the
# authority and no edge credential is involved.
admin=$(curl -s -o /dev/null -w '%{http_code}' -A "$UA" -H 'Accept: text/html' \
  --max-time 25 -b "$JAR" https://app.chudware.com/admin)
if [ "$admin" = "200" ]; then
  echo '  ok   /admin opens on the session cookie alone'; PASS_COUNT=$((PASS_COUNT + 1))
else
  echo "  FAIL /admin gave $admin to a signed-in admin session"; FAIL_COUNT=$((FAIL_COUNT + 1))
fi
rm -f "$JAR"
fi

echo
echo "PASSED $PASS_COUNT / FAILED $FAIL_COUNT"
[ "$FAIL_COUNT" -eq 0 ] || exit 1
