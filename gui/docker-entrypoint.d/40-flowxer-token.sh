#!/bin/sh
# Inject FLOWXER_API_TOKEN into the nginx mixer proxy when staging is locked down.
set -eu
inc=/etc/nginx/conf.d/flowxer-auth.inc
token="${FLOWXER_API_TOKEN:-}"
if [ -n "$token" ]; then
  printf 'proxy_set_header Authorization "Bearer %s";\n' "$token" > "$inc"
else
  : > "$inc"
fi
