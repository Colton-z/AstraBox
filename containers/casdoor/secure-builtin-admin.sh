#!/bin/sh
# Replace Casdoor's default password for its own administrator.
#
#   secure-builtin-admin.sh CASDOOR_URL PASSWORD_FILE CHECKED_FILE
#
# Casdoor creates its global administrator, built-in/admin, with the password
# 123 when it initializes an empty database (object/init.go, InitDb), before it
# reads the deployment's seed, and the seed adds only missing records, so the
# seed cannot replace that password. Casdoor's own API does: sign in as that
# account at /api/login and call /api/set-password.
#
# Waits for Casdoor at CASDOOR_URL, then makes sure built-in/admin does not
# accept 123: it keeps the password in PASSWORD_FILE, replaces 123 with it, or
# keeps a password an administrator chose. It writes CHECKED_FILE when one of
# those holds, and exits non-zero, without writing it, on anything else.
set -eu

casdoor="$1"
password_file="$2"
checked="$3"
password="$(tr -d '\r\n' <"$password_file")"
cookies="$(mktemp "$(dirname "$checked")/built-in-admin.XXXXXX")"
trap 'rm -f "$cookies"' EXIT

log() {
  printf 'astrabox: %s\n' "$*" >&2
}

# Prints ok, rejected (the password is wrong), or Casdoor's answer for
# anything else, such as a locked account.
login() {
  answer="$(printf '{"application":"app-built-in","organization":"built-in","username":"admin","password":"%s","type":"login"}' "$1" \
    | curl -sS -c "$cookies" -H 'Content-Type: application/json' -H 'Accept-Language: en' \
      --data-binary @- "$casdoor/api/login" 2>&1)" || true
  case "$answer" in
    *'"status":"ok"'*) printf 'ok' ;;
    *'password or code is incorrect'*) printf 'rejected' ;;
    *) printf '%s' "$answer" ;;
  esac
}

attempts=0
until curl -fsS -o /dev/null "$casdoor/.well-known/openid-configuration" 2>/dev/null; do
  attempts=$((attempts + 1))
  if [ "$attempts" -ge 120 ]; then
    log "Casdoor did not answer within 120 seconds; built-in/admin was not checked"
    exit 1
  fi
  sleep 1
done

# The generated password first: once it is set, no start signs in with a
# wrong password, and Casdoor counts wrong ones towards locking the account.
result="$(login "$password")"
case "$result" in
  ok) : >"$checked"; exit 0 ;;
  rejected) ;;
  *) log "cannot check built-in/admin: $result"; exit 1 ;;
esac

result="$(login 123)"
case "$result" in
  ok) ;;
  rejected)
    log "built-in/admin has a password other than Casdoor's default and the generated one; it is kept"
    : >"$checked"
    exit 0 ;;
  *) log "cannot check built-in/admin: $result"; exit 1 ;;
esac

answer="$(printf '%s' "$password" \
  | curl -sS -b "$cookies" -H 'Accept-Language: en' \
    --data-urlencode userOwner=built-in --data-urlencode userName=admin \
    --data-urlencode oldPassword=123 --data-urlencode newPassword@- \
    "$casdoor/api/set-password" 2>&1)" || true
case "$answer" in
  *'"status":"ok"'*) ;;
  *) log "cannot replace the default password of built-in/admin: $answer"; exit 1 ;;
esac
if [ "$(login "$password")" != ok ]; then
  log "built-in/admin does not accept the generated password after it was set"
  exit 1
fi
log "replaced the default password of built-in/admin with the generated one"
: >"$checked"
