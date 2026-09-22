#!/bin/sh
set -eu

# POSTGRES_* belongs only to the bootstrap superuser. Runtime services receive
# separate non-superuser roles and databases, so no runtime service can read or
# alter another service's tables. The maintained Compose stack supplies each
# value through a service-scoped secret file; explicit environment values remain
# available only for operators that replace the Compose topology themselves.
read_secret() {
  secret_path="$1"
  explicit_value="$2"
  setting_name="$3"
  if [ -n "$secret_path" ]; then
    [ -r "$secret_path" ] || {
      printf '%s\n' "$setting_name points at an unreadable file: $secret_path" >&2
      exit 1
    }
    secret_value="$(tr -d '\r\n' < "$secret_path")"
  else
    secret_value="$explicit_value"
  fi
  [ -n "$secret_value" ] || {
    printf '%s\n' "$setting_name is required" >&2
    exit 1
  }
  printf '%s' "$secret_value"
}

astrabox_password="$(read_secret "${ASTRABOX_POSTGRES_PASSWORD_FILE:-}" "${ASTRABOX_POSTGRES_PASSWORD:-}" ASTRABOX_POSTGRES_PASSWORD_FILE)"
litellm_password="$(read_secret "${LITELLM_POSTGRES_PASSWORD_FILE:-}" "${LITELLM_POSTGRES_PASSWORD:-}" LITELLM_POSTGRES_PASSWORD_FILE)"
casdoor_password="$(read_secret "${CASDOOR_POSTGRES_PASSWORD_FILE:-}" "${CASDOOR_POSTGRES_PASSWORD:-}" CASDOOR_POSTGRES_PASSWORD_FILE)"

psql --set=ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set=astrabox_password="$astrabox_password" \
  --set=litellm_password="$litellm_password" \
  --set=casdoor_password="$casdoor_password" <<'SQL'
CREATE USER astrabox WITH PASSWORD :'astrabox_password';
CREATE DATABASE astrabox OWNER astrabox;
CREATE USER litellm WITH PASSWORD :'litellm_password';
CREATE DATABASE litellm OWNER litellm;
CREATE USER casdoor WITH PASSWORD :'casdoor_password';
CREATE DATABASE casdoor OWNER casdoor;
REVOKE ALL ON DATABASE astrabox FROM PUBLIC;
REVOKE ALL ON DATABASE litellm FROM PUBLIC;
REVOKE ALL ON DATABASE casdoor FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE astrabox TO astrabox;
GRANT CONNECT, TEMPORARY ON DATABASE litellm TO litellm;
GRANT CONNECT, TEMPORARY ON DATABASE casdoor TO casdoor;
SQL
