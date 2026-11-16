# ---- Build stage ----
# Build-stage Node is pinned to an EXPLICIT PATCH, not the floating `node:22`
# tag: a floating tag resolves to whatever 22.x Docker Hub serves that day, so
# two builds a week apart can compile the frontend on different Node runtimes
# (CVE-2026-111497 hygiene — the build stage is not shipped, see the serve
# stage below: the runtime image carries no node binary at all, so this pin
# is build-supply-chain hygiene, NOT a runtime exposure).
FROM node:22.23.4-alpine AS build

# Deploy identity (QA finding 2026-10-10): which commit this image was built
# from, stamped into dist/index.html's <meta name="build-commit"> by
# scripts/stamp-build.mjs (see the comment there). docker-compose.yml passes
# it from the deploy host's checkout, e.g.
#   GIT_SHA=$(git rev-parse HEAD) docker compose up -d --build web
# The guard below runs BEFORE npm ci so a forgotten arg fails in ~1s with the
# exact command to run, instead of producing an image whose commit is — again
# — unknowable from outside. REQUIRE_BUILD_COMMIT is what makes a missing sha
# fatal rather than silently stamped "unknown"; a LOCAL `npm run build` sets
# neither and still succeeds, stamping "unknown" (a local dist is never served).
ARG GIT_SHA
ENV GIT_SHA=${GIT_SHA} REQUIRE_BUILD_COMMIT=1
RUN echo "$GIT_SHA" | grep -qE '^[0-9a-f]{7,40}(-dirty)?$' || { \
      echo "stamp-build: GIT_SHA must be a 7-40 hex commit id, optionally with a -dirty suffix (got '$GIT_SHA')." >&2; \
      echo "  pass it at build time, e.g.  GIT_SHA=\$(git rev-parse HEAD) docker compose up -d --build web" >&2; \
      exit 1; \
    }

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm ci

COPY . .
RUN npm run build
# Fail the image if index.html's inline scripts no longer hash to the sha256
# sources pinned in the CSP below (QA F2 — a stale hash means the browser
# blocks the snippet and the light-theme first paint breaks again).
RUN node scripts/csp-hash-check.mjs

# ---- Serve stage ----
# nginx unprivileged: runs as user nginx (uid 101), no root anywhere — so the
# listen port must be >1024 (8080; docker-compose.yml maps 8080:8080 to match).
# Same image family as nginx:alpine (busybox wget included → usable as the
# compose web healthcheck).
FROM nginxinc/nginx-unprivileged:alpine

COPY --from=build /app/dist /usr/share/nginx/html

# Inline-generated config:
#   - SPA fallback so client-side routes (e.g. /login) resolve to index.html
#   - /api proxied to the compose service named `api` (docker-compose.yml provides
#     this hostname on the default network) WITHOUT path rewrite, so requests hit
#     http://api:8000/<original-path> unchanged.
#   - Request body capped at 10m and hardening headers on every response (`always`
#     so they are emitted even for 4xx/5xx). nginx add_header inheritance drops
#     server-level headers in any location declaring its own add_header, so the
#     hardening headers (incl. HSTS) are repeated inside the two locations below
#     that declare their own add_header.
#   - `server_tokens off` hides the nginx version banner (audit L21).
#   - realip (audit M3): TLS terminates on the front proxy, which
#     sets X-Forwarded-For to the real client. Only that peer may rewrite
#     $remote_addr from XFF; any other peer keeps its socket IP, so a
#     client-supplied XFF never reaches the API unstripped — and
#     `proxy_set_header X-Forwarded-For` REPLACES with $remote_addr instead of
#     appending, so the API rate limiter's rightmost-XFF key is one real client
#     instead of the proxy IP that every public caller previously shared.
#   - M5 HSTS (extended M87): Strict-Transport-Security at server level; the TLS
#     terminator passes upstream response headers through to
#     browsers, so this reaches https responses. max-age + includeSubDomains +
#     preload per the M87 own-the-edge decision (docs/42) — note the cost: it
#     now binds EVERY subdomain of the site family to HTTPS, so a new
#     HTTP-only sibling subdomain on this host breaks until it serves TLS.
#     The http→https redirect itself is issued by the
#     terminator (outside this repo).
#   - M87 (docs/42) additive hardening, all `always`: Permissions-Policy
#     (camera/microphone/geolocation/payment off — nothing in the app uses
#     them, so a compromised bundle cannot ask), Cross-Origin-Opener-Policy
#     same-origin (no cross-origin window reach into this one), and
#     Cross-Origin-Resource-Policy same-origin (this origin's responses are
#     not readable as cross-origin subresources; CSP already forbids loading
#     any, this closes the header-based path too).
#   - M9: the internal MCP endpoints are denied at this proxy; the MCP
#     container calls the API directly over the compose network
#     (FIN_TRACKER_URL http://api:8000) and never needs this hop.
#   - M80: `/mcp/` is proxied to the MCP container. The trailing slash on
#     proxy_pass drops the `/mcp` prefix, so the public streamable-http URL
#     is `https://<host>/mcp/mcp` while the container keeps serving `/mcp`.
#     The upstream is a VARIABLE plus `resolver 127.0.0.11` (Docker's DNS)
#     on purpose: compose hands the mcp container a new IP every time it is
#     recreated, and a static upstream would be resolved ONCE at nginx start
#     — stale until someone restarts the web container. With the resolver it
#     is looked up per request, and nginx also starts fine while the mcp
#     container is down (502 on /mcp/ only instead of a dead proxy).
#     Buffering is off because the transport answers with `text/event-stream`,
#     and the read timeout mirrors `/api`.
#   - The CSP is written out at THREE `add_header` levels (server,
#     `location = /index.html`, `location /assets/`) and the three must stay
#     BYTE-IDENTICAL: nginx's `add_header` does not inherit, so a location that
#     declares its own header silently DROPS the server-level one. The CSP also
#     carries `base-uri 'self'; form-action 'self'` — the two directives
#     `default-src` does NOT fall back to, so without them a `<base href>` or a
#     form posting off-origin is allowed by the header as written. The app has
#     no `<base>` and no HTML form, so both are pure lockdown.
#     `scripts/csp-hash-check.mjs` runs in the build stage and fails the image
#     when the three copies drift apart or a hash goes stale.
#   - M72 F2: `script-src` is `'self'` plus two sha256 hashes — the only inline
#     scripts shipped are index.html's pre-hydration theme bootstrap and the
#     status-bar theme-color snippet, both content-addressed. Editing either
#     snippet changes its hash: the build stage runs
#     `node scripts/csp-hash-check.mjs`, which fails the image build and prints
#     the replacement hash to paste here (a nonce would need per-response
#     signing in nginx; hashes keep this static).
RUN printf '%s\n' \
    'log_format safe '"'"'$remote_addr - $remote_user [$time_local] "$request_method $uri $server_protocol" $status $body_bytes_sent "$http_referer" "$http_user_agent" "$http_x_forwarded_for"'"'"';' \
    'server {' \
    '  listen 8080;' \
    '  server_name _;' \
    '  root /usr/share/nginx/html;' \
    '  client_max_body_size 10m;' \
    '  server_tokens off;' \
    '  # M98b F3: access_log lives HERE, inside `server`, not at http level.' \
    '  # The base image /etc/nginx/nginx.conf already declares' \
    '  #   access_log /var/log/nginx/access.log main;' \
    '  # with main built on $request (the raw request line, query string' \
    '  # included), and SAME-LEVEL access_log directives accumulate — so an' \
    '  # http-level access_log next to log_format safe above logged every' \
    '  # request TWICE to the same stdout, once through main carrying the raw' \
    '  # query (/sharing/accept?token=<invite> and every SSE ?token= JWT into' \
    '  # docker logs). A server-level access_log REPLACES the inherited' \
    '  # http-level ones (nginx inherits access_log from the previous level' \
    '  # only when the current level declares none), so exactly one line is' \
    '  # written and it goes through the `safe` format: $uri never carries a' \
    '  # query string, so tokens are absent from the line, not truncated in' \
    '  # place. log_format safe stays at http level because log_format is' \
    '  # http-only. Verified live: docker logs | grep -c token= == 0 while' \
    '  # the same requests still log their 200s.' \
    '  access_log /var/log/nginx/access.log safe;' \
    '  # never emit an absolute redirect carrying the internal port (:8080)' \
    '  absolute_redirect off;' \
    '  port_in_redirect off;' \
    '  # M3 realip: TLS terminates on the front proxy, which sets' \
    '  # X-Forwarded-For to the real client IP. Rewrite $remote_addr from XFF' \
    '  # only when the peer IS that proxy; any other peer keeps its socket' \
    '  # IP and its client-supplied XFF is ignored.' \
    '  set_real_ip_from 10.10.11.100;' \
    '  real_ip_header X-Forwarded-For;' \
    '  real_ip_recursive on;' \
    '  # M5/M87 HSTS: includeSubDomains + preload (docs/42 own-the-edge).' \
    '  add_header Strict-Transport-Security "max-age=31536000; includeSubDomains; preload" always;' \
    '  add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;' \
    '  add_header Cross-Origin-Opener-Policy "same-origin" always;' \
    '  add_header Cross-Origin-Resource-Policy "same-origin" always;' \
    "  add_header Content-Security-Policy \"default-src 'self'; script-src 'self' 'sha256-+ZALdw0iadt/tBIxsuaCO41RtzhTbzMDens3eBB3Ntc=' 'sha256-qf3l4/KPFG0mZjBijINyOtWln+1RnQ+Cz7atNIcdFpk='; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'\" always;" \
    '  add_header X-Frame-Options "DENY" always;' \
    '  add_header X-Content-Type-Options "nosniff" always;' \
    '  add_header Referrer-Policy "strict-origin-when-cross-origin" always;' \
    '  location = /index.html {' \
    '    add_header Cache-Control "no-cache, must-revalidate";' \
    "    add_header Content-Security-Policy \"default-src 'self'; script-src 'self' 'sha256-+ZALdw0iadt/tBIxsuaCO41RtzhTbzMDens3eBB3Ntc=' 'sha256-qf3l4/KPFG0mZjBijINyOtWln+1RnQ+Cz7atNIcdFpk='; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'\" always;" \
    '    add_header X-Frame-Options "DENY" always;' \
    '    add_header X-Content-Type-Options "nosniff" always;' \
    '    add_header Referrer-Policy "strict-origin-when-cross-origin" always;' \
    '    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains; preload" always;' \
    '    add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;' \
    '    add_header Cross-Origin-Opener-Policy "same-origin" always;' \
    '    add_header Cross-Origin-Resource-Policy "same-origin" always;' \
    '    try_files $uri =404;' \
    '  }' \
    '  location /assets/ {' \
    '    add_header Cache-Control "public, max-age=31536000, immutable";' \
    "    add_header Content-Security-Policy \"default-src 'self'; script-src 'self' 'sha256-+ZALdw0iadt/tBIxsuaCO41RtzhTbzMDens3eBB3Ntc=' 'sha256-qf3l4/KPFG0mZjBijINyOtWln+1RnQ+Cz7atNIcdFpk='; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'\" always;" \
    '    add_header X-Frame-Options "DENY" always;' \
    '    add_header X-Content-Type-Options "nosniff" always;' \
    '    add_header Referrer-Policy "strict-origin-when-cross-origin" always;' \
    '    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains; preload" always;' \
    '    add_header Permissions-Policy "camera=(), microphone=(), geolocation=(), payment=()" always;' \
    '    add_header Cross-Origin-Opener-Policy "same-origin" always;' \
    '    add_header Cross-Origin-Resource-Policy "same-origin" always;' \
    '  }' \
    '  # /help is an SPA route but dist/help/ exists (guide screenshots,' \
    '  # docs/32), so the $uri/ branch of location / would 301 to /help/ then' \
    '  # 403 — the deep link never reached the app (QA, 2026-09-28). Serve' \
    '  # index.html for the exact path; /help/<file> screenshots still match' \
    '  # location / by $uri.' \
    '  location = /help {' \
    '    try_files /index.html =404;' \
    '  }' \
    '  # t_fe44260e (docs/32): the static marketing site at /site/.' \
    '  # PM decision: ONLY these two locations change; root / stays the SPA' \
    '  # fallback, and /changelog + /guides at root keep the status quo (SPA' \
    '  # shell) — the canonical public URL is /site/ and every site-internal' \
    '  # link is relative, so the prefix proxy serves them unchanged.' \
    '  # Variable upstream + resolver, the same pattern as /mcp/ below: a' \
    '  # recreated site container gets a new IP that a static upstream would' \
    '  # cache until nginx reloads, and nginx must start even while the site' \
    '  # container is down (502 on /site/ only, SPA and API untouched).' \
    '  # rewrite strips the /site/ prefix (proxy_pass with a variable does' \
    '  # no URI substitution of its own), so the site image'\''s own' \
    '  # `location = /changelog` still resolves. No add_header here: the' \
    '  # server-level hardening (CSP/HSTS/XFO/COOP/CORP) is inherited.' \
    '  # The site image ALSO ships its own hardening set (CSP/XFO/nosniff/' \
    '  # Referrer-Policy — site/Dockerfile) because it must stay hardened' \
    '  # when run standalone (docs/32 self-contained image), so proxied' \
    '  # responses used to carry every one of those headers TWICE and two' \
    '  # CSPs. proxy_hide_header drops the upstream copies: this location' \
    '  # still inherits the server-level set — the stricter one (superset' \
    '  # CSP + HSTS/PP/COOP/CORP the site image does not send) — and the' \
    '  # browser sees exactly one of each. Hiding is done HERE, not in the' \
    '  # site image, precisely because that image is not always behind' \
    '  # this proxy (docs/32: docker run on any host).' \
    '  location = /site {' \
    '    return 301 /site/;' \
    '  }' \
    '  location /site/ {' \
    '    resolver 127.0.0.11 valid=10s ipv6=off;' \
    '    set $site_upstream http://site:8080;' \
    '    rewrite ^/site/(.*)$ /$1 break;' \
    '    proxy_pass $site_upstream;' \
    '    proxy_set_header Host $host;' \
    '    proxy_set_header X-Real-IP $remote_addr;' \
    '    proxy_set_header X-Forwarded-For $remote_addr;' \
    '    proxy_set_header X-Forwarded-Proto $scheme;' \
    '    proxy_hide_header Content-Security-Policy;' \
    '    proxy_hide_header X-Frame-Options;' \
    '    proxy_hide_header X-Content-Type-Options;' \
    '    proxy_hide_header Referrer-Policy;' \
    '  }' \
    '  # M87 (docs/42): the API map. Without this block all three paths fell' \
    '  # through to `location /` and got index.html — a 200 that looked like a' \
    '  # page while saying nothing. They now reach the API, where' \
    '  # `require_docs_access` answers 401 {detail, code} to an anonymous caller' \
    '  # and 200 to a session access token or a live MCP token. NO add_header' \
    '  # in here on purpose: the server-level hardening (CSP included) is' \
    '  # inherited, which is also why the Swagger UI CDN script is blocked at' \
    '  # the edge — /openapi.json is the usable map here, and the interactive' \
    '  # UI wants local dev (API_DOCS_PUBLIC=true) or the API port itself.' \
    '  location ~ ^/(docs|redoc|openapi\.json)$ {' \
    '    proxy_pass http://api:8000;' \
    '    proxy_set_header Host $host;' \
    '    proxy_set_header X-Real-IP $remote_addr;' \
    '    proxy_set_header X-Forwarded-For $remote_addr;' \
    '    proxy_set_header X-Forwarded-Proto $scheme;' \
    '    proxy_buffering off;' \
    '    proxy_cache off;' \
    '    proxy_read_timeout 60s;' \
    '  }' \
    '  location / {' \
    '    try_files $uri $uri/ /index.html;' \
    '  }' \
    '  # M9: internal MCP auth endpoints — deny at the proxy; ^~ prefix match' \
    '  # beats location /api. The MCP container calls http://api:8000 directly' \
    '  # on the compose network, so nothing legitimate needs these via nginx.' \
    '  location ^~ /api/v1/auth/internal-issue-token {' \
    '    deny all;' \
    '  }' \
    '  location ^~ /api/v1/auth/validate-mcp-token {' \
    '    deny all;' \
    '  }' \
    '  location ^~ /api/v1/auth/internal-mcp-audit {' \
    '    deny all;' \
    '  }' \
    '  # M80: public streamable-http endpoint -> the MCP container. The' \
    '  # trailing slash on proxy_pass strips the /mcp prefix, so a request' \
    '  # for /mcp/mcp reaches the server as /mcp. No add_header in here on' \
    '  # purpose: declaring one would drop the server-level hardening' \
    '  # headers, so HSTS/CSP/XFO above are inherited instead.' \
    '  location /mcp/ {' \
    '    resolver 127.0.0.11 valid=10s ipv6=off;' \
    '    set $mcp_upstream http://mcp:8001;' \
    '    rewrite ^/mcp/(.*)$ /$1 break;' \
    '    proxy_pass $mcp_upstream;' \
    '    proxy_http_version 1.1;' \
    '    proxy_set_header Connection "";' \
    '    proxy_set_header Host $host;' \
    '    proxy_set_header X-Real-IP $remote_addr;' \
    '    proxy_set_header X-Forwarded-For $remote_addr;' \
    '    proxy_set_header X-Forwarded-Proto $scheme;' \
    '    proxy_buffering off;' \
    '    proxy_cache off;' \
    '    proxy_read_timeout 86400s;' \
    '  }' \
    '  location /api {' \
    '    proxy_pass http://api:8000;' \
    '    proxy_set_header Host $host;' \
    '    proxy_set_header X-Real-IP $remote_addr;' \
    '    proxy_set_header X-Forwarded-For $remote_addr;' \
    '    proxy_set_header X-Forwarded-Proto $scheme;' \
    '    proxy_buffering off;' \
    '    proxy_cache off;' \
    '    proxy_read_timeout 86400s;' \
    '  }' \
    '}' > /etc/nginx/conf.d/default.conf

EXPOSE 8080
