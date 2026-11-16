#!/usr/bin/env bash
# M87 evidence capture: the SAME probe run before and after the fix.
#   usage: bash probe.sh <label> | tee docs/assets/m87/<label>.txt
# Everything is a read except the two register calls, and the probe account is
# deleted again at the end (DELETE /users/me) so prod keeps no residue.
set -u
BASE="${BASE:-https://fintrac.princessandmark.com}"
LABEL="${1:-probe}"
MAIL="m87-${LABEL}-$(date +%s | tail -c 6)@example.com"
PW="$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))')"  # per-run secret (t_32cd7b8e): never a committed literal

echo "=== M87 evidence: ${LABEL} @ ${BASE} $(date -u +%Y-%m-%dT%H:%M:%SZ)"

echo
echo "--- 1. POST /auth/register (fresh address)"
REG=$(curl -sS -o /tmp/m87_reg1.json -w 'HTTP %{http_code}' -X POST "$BASE/api/v1/auth/register" \
  -H 'Content-Type: application/json' -d "{\"email\":\"$MAIL\",\"password\":\"$PW\"}")
echo "$REG"
# Never write a credential into the transcript: the token is used below and dropped.
python3 -c "import re;print(re.sub(r'eyJ[A-Za-z0-9_.-]+', '<redacted-jwt>', open('/tmp/m87_reg1.json').read()))"

echo "--- 1b. POST /auth/login with the password just posted (M87 recovery path)"
curl -sS -o /tmp/m87_login.json -w 'HTTP %{http_code}\n' -X POST "$BASE/api/v1/auth/login" \
  -H 'Content-Type: application/json' -d "{\"email\":\"$MAIL\",\"password\":\"$PW\"}"
TOKEN=$(python3 -c "import json;print(json.load(open('/tmp/m87_login.json')).get('access_token',''))" 2>/dev/null || true)
echo "  token acquired: $([ -n "$TOKEN" ] && echo yes || echo NO)"

echo "--- 2. POST /auth/register (SAME address again = the oracle)"
curl -sS -o /tmp/m87_reg2.json -w 'HTTP %{http_code}\n' -X POST "$BASE/api/v1/auth/register" \
  -H 'Content-Type: application/json' -d "{\"email\":\"$MAIL\",\"password\":\"other-password\"}"
cat /tmp/m87_reg2.json; echo

echo "--- 3. POST /auth/passkeys/login/options (address with NO account)"
curl -sS -o /tmp/m87_opt1.json -w 'HTTP %{http_code}\n' -X POST "$BASE/api/v1/auth/passkeys/login/options" \
  -H 'Content-Type: application/json' -d '{"email":"definitely-not-registered-m87@example.com"}'
cat /tmp/m87_opt1.json; echo

echo "--- 4. POST /auth/passkeys/login/options (address WITH an account)"
curl -sS -o /tmp/m87_opt2.json -w 'HTTP %{http_code}\n' -X POST "$BASE/api/v1/auth/passkeys/login/options" \
  -H 'Content-Type: application/json' -d "{\"email\":\"$MAIL\"}"
cat /tmp/m87_opt2.json; echo

echo "--- 5. GET /docs (anonymous)"
curl -sS -o /tmp/m87_docs.html -w 'HTTP %{http_code} content-type=%{content_type}\n' "$BASE/docs"
head -c 120 /tmp/m87_docs.html; echo

echo "--- 6. GET /openapi.json (anonymous)"
curl -sS -o /tmp/m87_openapi.json -w 'HTTP %{http_code} content-type=%{content_type}\n' "$BASE/openapi.json"
head -c 120 /tmp/m87_openapi.json; echo

echo "--- 7. GET /openapi.json with a session access token"
if [ -n "${TOKEN}" ]; then
  curl -sS -o /tmp/m87_openapi_auth.json -w 'HTTP %{http_code}\n' "$BASE/openapi.json" \
    -H "Authorization: Bearer $TOKEN"
  head -c 60 /tmp/m87_openapi_auth.json; echo
else
  echo "(register returned no token — session unavailable for this probe)"
fi

echo "--- 8. response headers on /"
curl -sSI "$BASE/" | grep -iE '^(strict-transport-security|permissions-policy|cross-origin-opener-policy|cross-origin-resource-policy|content-security-policy):' || echo "(none of the scanned headers present)"

echo "--- 9. CORS preflight from the REAL origin"
curl -sS -o /dev/null -w 'HTTP %{http_code} allow-origin=%header{access-control-allow-origin}\n' -X OPTIONS "$BASE/api/v1/auth/login" \
  -H 'Origin: https://fintrac.princessandmark.com' \
  -H 'Access-Control-Request-Method: POST' \
  -H 'Access-Control-Request-Headers: content-type'

echo "--- 10. CORS preflight from http://localhost:5173"
curl -sS -o /dev/null -w 'HTTP %{http_code} allow-origin=%header{access-control-allow-origin}\n' -X OPTIONS "$BASE/api/v1/auth/login" \
  -H 'Origin: http://localhost:5173' \
  -H 'Access-Control-Request-Method: POST' \
  -H 'Access-Control-Request-Headers: content-type'

echo "--- 11. cleanup: delete the probe account"
if [ -n "${TOKEN}" ]; then
  curl -sS -o /dev/null -w 'DELETE /users/me HTTP %{http_code}\n' -X DELETE "$BASE/api/v1/users/me" \
    -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -d "{\"current_password\":\"$PW\"}"
else
  echo "(no token — probe account left behind: $MAIL)"
fi
echo "=== end ${LABEL}"
