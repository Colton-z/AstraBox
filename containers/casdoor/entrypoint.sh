#!/bin/sh
set -eu
umask 077

read_hex_secret() {
  path="$1"
  variable="$2"
  [ -n "$path" ] || {
    printf '%s is required\n' "$variable" >&2
    exit 1
  }
  [ -r "$path" ] || {
    printf '%s is unreadable: %s\n' "$variable" "$path" >&2
    exit 1
  }
  value="$(tr -d '\r\n' < "$path")"
  case "$value" in
    *[!0-9a-f]*|'')
      printf '%s must contain a lowercase hexadecimal secret\n' "$variable" >&2
      exit 1
      ;;
  esac
  [ "${#value}" -eq 64 ] || {
    printf '%s must contain exactly 64 hexadecimal characters\n' "$variable" >&2
    exit 1
  }
  printf '%s' "$value"
}

escape_sed_replacement() {
  printf '%s' "$1" | sed 's/[\\&|]/\\&/g'
}

if [ -z "${dataSourceName:-}" ]; then
  secret_path="${CASDOOR_DATABASE_PASSWORD_FILE:-}"
  [ -n "$secret_path" ] || {
    printf '%s\n' "CASDOOR_DATABASE_PASSWORD_FILE is required" >&2
    exit 1
  }
  [ -r "$secret_path" ] || {
    printf '%s\n' "CASDOOR_DATABASE_PASSWORD_FILE is unreadable: $secret_path" >&2
    exit 1
  }
  database_password="$(tr -d '\r\n' < "$secret_path")"
  [ -n "$database_password" ] || {
    printf '%s\n' "CASDOOR_DATABASE_PASSWORD_FILE is empty: $secret_path" >&2
    exit 1
  }
  export dataSourceName="user=casdoor password=$database_password host=postgres port=5432 sslmode=disable dbname=casdoor"
fi

client_secret="$(read_hex_secret "${CASDOOR_OIDC_CLIENT_SECRET_FILE:-}" CASDOOR_OIDC_CLIENT_SECRET_FILE)"
api_client_secret="$(read_hex_secret "${CASDOOR_OIDC_API_CLIENT_SECRET_FILE:-}" CASDOOR_OIDC_API_CLIENT_SECRET_FILE)"
admin_password="$(read_hex_secret "${CASDOOR_ADMIN_PASSWORD_FILE:-}" CASDOOR_ADMIN_PASSWORD_FILE)"
client_id="${CASDOOR_OIDC_CLIENT_ID:-}"
case "$client_id" in
  ''|*[!A-Za-z0-9._-]*)
    printf '%s\n' "CASDOOR_OIDC_CLIENT_ID must contain only letters, numbers, dot, underscore, or hyphen" >&2
    exit 1
    ;;
esac
api_client_id="${CASDOOR_OIDC_API_CLIENT_ID:-}"
case "$api_client_id" in
  ''|*[!A-Za-z0-9._-]*)
    printf '%s\n' "CASDOOR_OIDC_API_CLIENT_ID must contain only letters, numbers, dot, underscore, or hyphen" >&2
    exit 1
    ;;
esac
console_origin="${ASTRABOX_CONSOLE_ORIGIN:-}"
case "$console_origin" in
  http://*|https://*) ;;
  *)
    printf '%s\n' "ASTRABOX_CONSOLE_ORIGIN must be an absolute HTTP(S) origin" >&2
    exit 1
    ;;
esac
case "$console_origin" in
  *[\"\\\ ]*|*/)
    printf '%s\n' "ASTRABOX_CONSOLE_ORIGIN must not contain quotes, spaces, backslashes, or a trailing slash" >&2
    exit 1
    ;;
esac

template=/conf/init_data.template.json
init_data=/run/astrabox-casdoor/init_data.json
[ -r "$template" ] || {
  printf 'Casdoor init template is unreadable: %s\n' "$template" >&2
  exit 1
}
sed \
  -e "s|__ASTRABOX_OIDC_CLIENT_ID__|$(escape_sed_replacement "$client_id")|g" \
  -e "s|__ASTRABOX_OIDC_CLIENT_SECRET__|$(escape_sed_replacement "$client_secret")|g" \
  -e "s|__ASTRABOX_OIDC_API_CLIENT_ID__|$(escape_sed_replacement "$api_client_id")|g" \
  -e "s|__ASTRABOX_OIDC_API_CLIENT_SECRET__|$(escape_sed_replacement "$api_client_secret")|g" \
  -e "s|__ASTRABOX_CASDOOR_ADMIN_PASSWORD__|$(escape_sed_replacement "$admin_password")|g" \
  -e "s|__ASTRABOX_CONSOLE_ORIGIN__|$(escape_sed_replacement "$console_origin")|g" \
  "$template" >"$init_data"
chmod 0600 "$init_data"

exec /server
