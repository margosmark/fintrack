#!/usr/bin/env bash
# Live smoke battery for fin_tracker — run AFTER `docker compose up -d --build`.
#   BASE=https://fintrac.princessandmark.com bash smoke.sh
# Steps 8-9 ssh into the deploy host running the stack. Give that target at
# run time with SMOKE_SSH_HOST; the default is a non-resolving placeholder so
# no LAN/topology reference ever lives in this repo — with the var unset those
# steps self-skip ("skipped (no <host> ssh in BatchMode)"), fail-closed.
# Prints statuses and error codes only: no tokens, no cookie values, no secrets.
# Expected values are shown as `expect:` hints next to each actual result — print,
# don't assert, so the script survives intentional contract changes.
# ORDER: the login-burst check is LAST because it rate-limits this client's IP
# on /auth/login for ~1 minute afterwards.
set -u
BASE="${BASE:-https://fintrac.princessandmark.com}"
SMOKE_SSH_HOST="${SMOKE_SSH_HOST:-example.internal}"    # steps 8-9 ssh target (operator-supplied at runtime)
PASS="smoke-$(openssl rand -hex 9)"                      # throwaway, generated per run
EMAIL="smoke-$(date +%s)-${RANDOM}@example.com"          # throwaway account, deleted below
J=$(mktemp)
cleanup() {
  # t_498845bf: delete the throwaway on ANY exit, not only the happy path —
  # a crash between register and item 10 used to leak the account into the
  # production DB (users.id 371 was exactly that). Before the login there is
  # no session to delete with (M87: register opens none), so the covered
  # window is "logged in and not yet deleted".
  rm -f "$J" /tmp/ftsmoke*
  if [ -n "${T2:-}" ]; then
    curl --max-time 20 -s -o /dev/null -X DELETE "$BASE/api/v1/users/me" \
      -H "Authorization: Bearer $T2" \
      -H 'Content-Type: application/json' \
      -d "{\"current_password\":\"$PASS\"}" || true
  fi
}
trap cleanup EXIT
say(){ printf '%s\n' "$*"; }

say "== 1 demo login: cookie attrs + refresh token absent from body"
H=$(curl --max-time 20 -si -X POST "$BASE/api/v1/auth/demo")
say "  http: $(printf '%s' "$H" | head -1 | tr -d '\r')   expect: 200"
printf '%s' "$H" | grep -i '^set-cookie:' | sed -E 's/(refresh_token=)[^;]+/\1<REDACTED>/' | tr -d '\r' | sed 's/^/  /'
printf '%s' "$H" | grep -iq 'set-cookie:.*secure'      && say "  Secure: OK"        || say "  Secure: MISSING"
printf '%s' "$H" | grep -iq 'set-cookie:.*httponly'    && say "  HttpOnly: OK"      || say "  HttpOnly: MISSING"
printf '%s' "$H" | tail -1 | grep -q '"refresh_token"' && say "  body refresh_token: PRESENT (expect absent)" || say "  body refresh_token: absent OK"
T=$(printf '%s' "$H" | tail -1 | python3 -c 'import sys,json;print(json.load(sys.stdin).get("access_token",""))' 2>/dev/null || true)
[ -n "$T" ] || { say "  FATAL: no demo token — aborting"; exit 1; }

say "== 2 demo PATCH llm_base_url: expect 403 demo_account_restricted (guard present)"
printf '  http: '; curl --max-time 20 -s -o /tmp/ftsmoke2 -w '%{http_code}\n' -X PATCH "$BASE/api/v1/users/me" \
  -H "Authorization: Bearer $T" -H 'Content-Type: application/json' \
  -d '{"llm_base_url":"http://169.254.169.254/latest/meta-data"}'
say "  body: $(head -c 200 /tmp/ftsmoke2)"

say "== 3 register throwaway account (uniform body, opens NO session — M87)"
R=$(curl --max-time 20 -si -X POST "$BASE/api/v1/auth/register" -H 'Content-Type: application/json' -c "$J" \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PASS\"}")
say "  http: $(printf '%s' "$R" | head -1 | tr -d '\r')   expect: 200"
printf '%s' "$R" | tail -1 | grep -q '"refresh_token"' && say "  body refresh_token: PRESENT (expect absent)" || say "  body refresh_token: absent OK"
printf '%s' "$R" | grep -iq '^set-cookie:' && say "  Set-Cookie: PRESENT (expect absent)" || say "  Set-Cookie: absent OK"
# M87 (docs/42): register never issues a session — the throwaway's token
# comes from sign-in with the password just posted, and that is where the
# refresh cookie must appear (cookie-only, never in the body).
R=$(curl --max-time 20 -si -X POST "$BASE/api/v1/auth/login" -H 'Content-Type: application/json' -c "$J" \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PASS\"}")
say "  login http: $(printf '%s' "$R" | head -1 | tr -d '\r')   expect: 200"
printf '%s' "$R" | tail -1 | grep -q '"refresh_token"' && say "  body refresh_token: PRESENT (expect absent)" || say "  body refresh_token: absent OK"
T2=$(printf '%s' "$R" | tail -1 | python3 -c 'import sys,json;print(json.load(sys.stdin).get("access_token",""))' 2>/dev/null || true)
[ -n "$T2" ] || { say "  FATAL: login gave no token — aborting"; exit 1; }

say "== 4 PATCH private/metadata IP: expect 422 invalid_llm_base_url (SSRF write-time guard)"
printf '  http: '; curl --max-time 20 -s -o /tmp/ftsmoke4 -w '%{http_code}\n' -X PATCH "$BASE/api/v1/users/me" \
  -H "Authorization: Bearer $T2" -H 'Content-Type: application/json' \
  -d '{"llm_base_url":"http://169.254.169.254/latest/meta-data"}'
say "  body: $(head -c 200 /tmp/ftsmoke4)"

say "== 5 PATCH loopback URL: expect 200 (loopback allowed by egress policy)"
printf '  http: '; curl --max-time 20 -s -o /dev/null -w '%{http_code}\n' -X PATCH "$BASE/api/v1/users/me" \
  -H "Authorization: Bearer $T2" -H 'Content-Type: application/json' \
  -d '{"llm_base_url":"http://127.0.0.1:11434/v1"}'

say "== 6 export/full: key count (expect: model tables minus refresh_tokens + mcp_tokens; grows with schema)"
curl --max-time 20 -s "$BASE/api/v1/export/full" -H "Authorization: Bearer $T2" -o /tmp/ftsmoke6
python3 - <<'PY'
import json
d = json.load(open('/tmp/ftsmoke6'))
if isinstance(d, dict):
    print(f"  keys: {len(d)}"); print("  names:", ",".join(sorted(d)))
else:
    print("  unexpected shape:", type(d).__name__, str(d)[:160])
PY

say "== 7 refresh round-trip via cookie over HTTPS: expect 200, no token in body"
printf '  http: '; curl --max-time 20 -s -o /tmp/ftsmoke7 -w '%{http_code}\n' -X POST "$BASE/api/v1/auth/refresh" \
  -b "$J" -c "$J" -H 'Content-Type: application/json' -d '{}' -H "Authorization: Bearer $T2"
grep -q '"refresh_token"' /tmp/ftsmoke7 && say "  body refresh_token: PRESENT (expect absent)" || say "  body refresh_token: absent OK"

say "== 8 MCP internal endpoint e2e over the compose network (public nginx must deny it)"
if ssh -o ConnectTimeout=5 -o BatchMode=yes "$SMOKE_SSH_HOST" true 2>/dev/null; then
  ssh "$SMOKE_SSH_HOST" "docker exec fin_tracker-api-1 python -c 'import os,httpx; s=os.environ.get(\"MCP_INTERNAL_SECRET\",\"\"); r=httpx.post(\"http://api:8000/api/v1/auth/validate-mcp-token\",json={\"token\":\"junk\"},headers={\"X-Internal-Secret\":s},timeout=10); print(\"  secret_set:\", bool(s), \"status:\", r.status_code, \"expect: 401 (secret accepted, token invalid)\")'" 2>/dev/null || say "  skipped (api container exec failed)"
  printf '  public: '; for p in validate-mcp-token internal-issue-token; do
    printf '%s=%s ' "$p" "$(curl --max-time 20 -s -o /dev/null -w '%{http_code}' -X POST "$BASE/api/v1/auth/$p" -H 'Content-Type: application/json' -d '{}')"
  done; say " (expect 403 403)"
  say "== 9 site container direct: expect docker port 127.0.0.1:8090 and HTTP 200"
  ssh "$SMOKE_SSH_HOST" "docker port fin_tracker-site-1; curl --max-time 20 -s -o /dev/null -w '  site: %{http_code}\n' http://127.0.0.1:8090/"
else
  say "  skipped (no $SMOKE_SSH_HOST ssh in BatchMode)"
fi

say "== 10 delete throwaway account: expect 204 (field is current_password)"
printf '  http: '; curl --max-time 20 -s -o /tmp/ftsmoke10 -w '%{http_code}\n' -X DELETE "$BASE/api/v1/users/me" \
  -H "Authorization: Bearer $T2" -H 'Content-Type: application/json' \
  -d "{\"current_password\":\"$PASS\"}"

say "== 11 login burst: print codes until first 429 (rate limiter live through the proxy)"
for i in $(seq 1 12); do
  c=$(curl --max-time 20 -s -o /dev/null -w '%{http_code}' -X POST "$BASE/api/v1/auth/login" \
    -H 'Content-Type: application/json' \
    -d "{\"email\":\"burst-$i-$RANDOM@example.com\",\"password\":\"wrongpass\"}")
  printf '%s ' "$c"
  [ "$c" = "429" ] && { echo "-> 429 at attempt $i (expect <=11)"; break; }
done
echo
say "SMOKE_DONE — this client is now rate-limited on /auth/login for ~1 min"
