#!/bin/sh
set -eu

# POSTGRES_* belongs only to the bootstrap superuser. Runtime services receive
# separate non-superuser roles and databases, so no runtime service can read or
# alter another service's tables. The maintained Compose stack supplies each
# value through a service-scoped secret file; explicit environment values remain
# available only for operators that replace the Compose topology themselves.
#
# The Casdoor role exists only where team login can run. The all-in-one image
# (containers/all-in-one/Dockerfile) runs this same script without a Casdoor
# password, and then no Casdoor role or database is created. Compose always
# names the file, so an unreadable or empty one still stops initialisation.
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
casdoor_password=""
if [ -n "${CASDOOR_POSTGRES_PASSWORD_FILE:-}" ] || [ -n "${CASDOOR_POSTGRES_PASSWORD:-}" ]; then
  casdoor_password="$(read_secret "${CASDOOR_POSTGRES_PASSWORD_FILE:-}" "${CASDOOR_POSTGRES_PASSWORD:-}" CASDOOR_POSTGRES_PASSWORD_FILE)"
fi

psql --set=ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set=astrabox_password="$astrabox_password" \
  --set=litellm_password="$litellm_password" <<'SQL'
CREATE USER astrabox WITH PASSWORD :'astrabox_password';
CREATE DATABASE astrabox OWNER astrabox;
CREATE USER litellm WITH PASSWORD :'litellm_password';
CREATE DATABASE litellm OWNER litellm;
REVOKE ALL ON DATABASE astrabox FROM PUBLIC;
REVOKE ALL ON DATABASE litellm FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE astrabox TO astrabox;
GRANT CONNECT, TEMPORARY ON DATABASE litellm TO litellm;
SQL

# No `exit` here: the official postgres image sources a non-executable init
# script, and an exit would end its entrypoint before initialisation completes.
if [ -n "$casdoor_password" ]; then
  psql --set=ON_ERROR_STOP=1 \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" \
    --set=casdoor_password="$casdoor_password" <<'SQL'
CREATE USER casdoor WITH PASSWORD :'casdoor_password';
CREATE DATABASE casdoor OWNER casdoor;
REVOKE ALL ON DATABASE casdoor FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE casdoor TO casdoor;
SQL
fi
