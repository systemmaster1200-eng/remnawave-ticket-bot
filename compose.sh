#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)

read_use_builtin_nginx() {
  if [ -n "${USE_BUILTIN_NGINX+x}" ]; then
    printf '%s\n' "$USE_BUILTIN_NGINX"
    return
  fi

  env_file="$SCRIPT_DIR/.env"
  if [ ! -f "$env_file" ]; then
    printf 'true\n'
    return
  fi

  awk -F= '
    /^[[:space:]]*#/ { next }
    $1 ~ /^[[:space:]]*USE_BUILTIN_NGINX[[:space:]]*$/ {
      value = substr($0, index($0, "=") + 1)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
      gsub(/\r$/, "", value)
      if (value ~ /^".*"$/ || value ~ /^'\''.*'\''$/) {
        value = substr(value, 2, length(value) - 2)
      }
      print tolower(value)
      found = 1
      exit
    }
    END {
      if (!found) print "true"
    }
  ' "$env_file"
}

if [ -z "${COMPOSE_PROFILES+x}" ]; then
  use_builtin_nginx="$(read_use_builtin_nginx)"
  case "$use_builtin_nginx" in
    1|true|yes|on)
      export COMPOSE_PROFILES="builtin-nginx"
      ;;
    0|false|no|off)
      unset COMPOSE_PROFILES
      ;;
    *)
      echo "Некорректное значение USE_BUILTIN_NGINX: $use_builtin_nginx" >&2
      exit 1
      ;;
  esac
fi

cd "$SCRIPT_DIR"
exec docker compose "$@"
