# Standalone static site (landing + guides). No build step, no backend.
# Self-contained on purpose: `docker build -t fintrack-site ./site` runs it
# on any host without the rest of the stack (docs/32).
# nginx unprivileged (audit L22): runs as user nginx (uid 101), no root
# anywhere — so the listen port must be >1024: the site serves on 8080 inside
# the container. Compose must map 127.0.0.1:8090:8080 and health-probe
# http://127.0.0.1:8080/ (docker-compose.yml is owned elsewhere — reported as
# a deviation; the site will not start until that one-liner lands).
# Same image family as nginx:alpine (busybox wget included → still usable as
# the compose healthcheck probe).
FROM nginxinc/nginx-unprivileged:alpine

COPY index.html changelog.html trust.html styles.css /usr/share/nginx/html/
COPY guides /usr/share/nginx/html/guides/
COPY assets /usr/share/nginx/html/assets/

RUN printf '%s\n' \
    'server {' \
    '  listen 8080;' \
    '  server_name _;' \
    '  server_tokens off;' \
    '  root /usr/share/nginx/html;' \
    '  add_header Content-Security-Policy "default-src '"'"'self'"'"'; style-src '"'"'self'"'"' '"'"'unsafe-inline'"'"'; img-src '"'"'self'"'"' data:; frame-ancestors '"'"'none'"'"'" always;' \
    '  add_header X-Frame-Options "DENY" always;' \
    '  add_header X-Content-Type-Options "nosniff" always;' \
    '  add_header Referrer-Policy "strict-origin-when-cross-origin" always;' \
    '  location = /changelog {' \
    '    try_files /changelog.html =404;' \
    '  }' \
    '  location = /trust {' \
    '    try_files /trust.html =404;' \
    '  }' \
    '  location / {' \
    '    try_files $uri $uri/ =404;' \
    '  }' \
    '}' > /etc/nginx/conf.d/default.conf

EXPOSE 8080
