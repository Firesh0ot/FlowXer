#!/bin/sh
# WIDGET_FRAME_ANCESTORS (a CSP source list, default 'self'): who may frame the widget pages
# (/widget/<id> carries "Content-Security-Policy: frame-ancestors <list>"), and which origins
# GET /widgets answers with Access-Control-Allow-Origin (those named in the list, or any with *).
set -eu
# No globbing: * is a CSP source here.
set -f
conf=/etc/nginx/conf.d
self_only="'self'"
ancestors="${WIDGET_FRAME_ANCESTORS:-$self_only}"
case "$ancestors" in
  *[\"\;\{\}\\]*)
    echo "WIDGET_FRAME_ANCESTORS must be a CSP source list (no \" ; { } or backslash)" >&2
    exit 78
    ;;
esac
printf 'add_header Content-Security-Policy "frame-ancestors %s" always;\n' "$ancestors" > "$conf/flowxer-widgets-csp.inc"
printf 'add_header Access-Control-Allow-Origin $flowxer_widget_origin always;\nadd_header Vary Origin always;\n' \
  > "$conf/flowxer-widgets-cors.inc"
{
  echo 'map $http_origin $flowxer_widget_origin {'
  echo '    default "";'
  for source in $ancestors; do
    case "$source" in
      "*") echo '    "~." $http_origin;' ;;
      \'*) ;; # keywords: 'self' needs no CORS, 'none' allows nothing
      *) printf '    "%s" $http_origin;\n' "$(printf '%s' "$source" | sed 's:/*$::' | tr 'A-Z' 'a-z')" ;;
    esac
  done
  echo '}'
} > "$conf/flowxer-widgets-map.conf"
