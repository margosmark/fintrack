#!/usr/bin/env bash
# M87 post-fix evidence, part 2: MCP-token docs access, garbage bearer,
# and the API port without nginx in front. Appends to post_fix_evidence.txt.
set -u
BASE="https://fintrac.princessandmark.com"
OUT="$(dirname "$0")/post_fix_evidence.txt"
authz() { printf '%s %s %s' 'Authorization:' "$2" "$1"; }

{
echo "=== M87 evidence: post_fix (part 2) @ $(date -u +%Y-%m-%dT%H:%M:%SZ)"
MAIL="m87-mcptok-$(date +%s | tail -c 6)@example.com"
PW="$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))')"  # per-run secret (t_32cd7b8e): never a committed literal
curl -sS -o /tmp/m_r.json -w 'register: HTTP %{http_code}\n' -X POST "$BASE/api/v1/auth/register" \
  -H 'Content-Type: application/json' -d "{\"email\":\"$MAIL\",\"password\":\"$PW\"}"
curl -sS -o /tmp/m_l.json -w 'login:     HTTP %{http_code}\n' -X POST "$BASE/api/v1/auth/login" \
  -H 'Content-Type: application/json' -d "{\"email\":\"$MAIL\",\"password\":\"$PW\"}"
SESSION_TOKEN=$(python3 -c "import json;print(json.load(open('/tmp/m_l.json'))['access_token'])")
curl -sS -o /tmp/m_tok.json -w 'mcp-token: HTTP %{http_code}\n' -X POST "$BASE/api/v1/auth/mcp-tokens" \
  -H 'Content-Type: application/json' -H "$(authz "$SESSION_TOKEN" 'Bearer')" -d '{"name":"m87-docs-probe","policy":"read_only"}'
MCP_TOKEN=$(python3 -c "import json;print(json.load(open('/tmp/m_tok.json'))['token'])")
echo -n "GET /openapi.json with LIVE MCP token: "
curl -sS -o /tmp/m_o.json -w 'HTTP %{http_code} ' "$BASE/openapi.json" -H "$(authz "$MCP_TOKEN" 'Bearer')"
head -c 40 /tmp/m_o.json; echo
echo -n "GET /docs with LIVE MCP token:        "
curl -sS -o /tmp/m_d.html -w 'HTTP %{http_code} type=%{content_type}\n' "$BASE/docs" -H "$(authz "$MCP_TOKEN" 'Bearer')"
echo -n "GET /openapi.json with GARBAGE bearer: "
curl -sS -o /tmp/m_g.json -w 'HTTP %{http_code} ' "$BASE/openapi.json" -H "$(authz "not-a-real-token" 'Bearer')"
head -c 90 /tmp/m_g.json; echo

echo "--- API port (uvicorn :8000, no nginx in front)"
ssh example.internal 'for p in /docs /redoc /openapi.json; do printf "  %s -> " "$p"; curl -s -o /tmp/o -w "HTTP %{http_code} type=%{content_type} " "http://127.0.0.1:8000$p"; head -c 55 /tmp/o; echo; done'

echo -n "cleanup DELETE /users/me: "
curl -sS -o /dev/null -w 'HTTP %{http_code}\n' -X DELETE "$BASE/api/v1/users/me" \
  -H "$(authz "$SESSION_TOKEN" 'Bearer')" -H 'Content-Type: application/json' -d "{\"current_password\":\"$PW\"}"
echo "=== end part 2"
} | tee -a "$OUT"
