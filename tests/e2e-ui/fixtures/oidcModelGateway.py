"""Task-owned supplier controls for the independent OIDC inference journey."""
from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


config = json.loads(Path(sys.argv[1]).read_text())
action = sys.argv[2]
payload = json.load(sys.stdin)


def call(base, route, body=None, *, method=None, basic=None, bearer=None):
    headers = {"Content-Type": "application/json"}
    if basic:
        headers["Authorization"] = "Basic " + base64.b64encode(":".join(basic).encode()).decode()
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    request = urllib.request.Request(base + route, headers=headers, method=method,
                                     data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def gateway(route, body=None):
    return call(config["gateway_internal"], route, body, bearer=Path(config["master_file"]).read_text().strip())


def idp(route, body=None):
    result = call(config["idp_internal"], route, body, basic=(config["platform_client"], config["platform_secret"]))
    if result.get("status") != "ok":
        raise RuntimeError("The task-owned identity administration operation was refused")
    return result.get("data")


def replace_policy(value):
    target = Path(config["policy_file"])
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(value))
    temporary.replace(target)


def user_info():
    return idp("/api/get-user?" + urllib.parse.urlencode({"id": "astrabox/employee"}))


def signed(claims, key):
    def encode(value):
        return base64.urlsafe_b64encode(value).decode().rstrip("=")
    header = encode(json.dumps({"alg": "RS256", "typ": "JWT", "kid": config["certificate_id"]}).encode())
    body = encode(json.dumps(claims).encode())
    signing = (header + "." + body).encode()
    signature = subprocess.check_output(["openssl", "dgst", "-sha256", "-sign", key], input=signing)
    return signing.decode() + "." + encode(signature)


if action in {"user_disabled", "user_admin"}:
    user = user_info()
    user["isForbidden" if action == "user_disabled" else "isAdmin"] = bool(payload.get("disabled", payload.get("admin")))
    idp("/api/update-user?id=astrabox/employee", user)
    result = {"updated": True}
elif action == "revoke":
    records = idp("/api/get-tokens?owner=admin&organization=astrabox")
    found = [entry for entry in records if entry.get("accessToken") == payload["token"]]
    for entry in found:
        # Casdoor3.128 exposes token-record deletion, not an invented RFC7009 route.
        deleted = idp("/api/delete-token", {"owner": entry["owner"], "name": entry["name"], "organization": entry["organization"]})
        assert deleted == "Affected", "Casdoor must confirm a Token row was actually deleted"
    remaining = idp("/api/get-tokens?owner=admin&organization=astrabox")
    assert not any(entry.get("accessToken") == payload["token"] for entry in remaining), "Revoked Access Token row remains"
    result = {"revoked": bool(found), "already_inactive": not found}
elif action == "application":
    policy = json.loads(Path(config["policy_baseline"]).read_text())
    app = policy["providers"][0]["applications"][0]
    if payload.get("removed"):
        policy["providers"][0]["applications"] = []
    elif "enabled" in payload:
        app["enabled"] = payload["enabled"]
    replace_policy(policy)
    result = {"updated": True}
elif action in {"provider_down", "provider_up"}:
    subprocess.run(["docker", "stop" if action == "provider_down" else "start", config["casdoor_container"]], check=True, capture_output=True, timeout=20)
    if action == "provider_up":
        deadline = time.monotonic() + 12
        while True:
            try:
                call(config["idp_internal"], "/.well-known/openid-configuration")
                break
            except (OSError, ValueError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)
    result = {"updated": True}
elif action == "signed_variants":
    claims = {"iss": config["issuer"], "aud": [config["desktop_client"]], "azp": config["desktop_client"],
              "owner": "astrabox", "sub": payload["subject"], "iat": int(time.time()), "exp": int(time.time()) + 600}
    changes = {"unrecorded_same_signed_jwt": ({}, 401), "wrong_issuer": ({"iss": "https://untrusted.invalid"}, 401),
               "wrong_audience": ({"aud": ["other-resource"]}, 401), "untrusted_client": ({"azp": "other-client"}, 403),
               "wrong_organization": ({"owner": "other-org"}, 403), "expired": ({"exp": int(time.time()) - 1}, 401)}
    result = {name: {"token": signed({**claims, **change}, config["certificate_key_file"]), "status": status}
              for name, (change, status) in changes.items()}
    valid = result["unrecorded_same_signed_jwt"]["token"]
    head, body, signature = valid.split(".")
    # Modify the signed content, leaving the signature unchanged.
    bad = {**claims, "sub": "wrong-signature-subject"}
    body = base64.urlsafe_b64encode(json.dumps(bad).encode()).decode().rstrip("=")
    result["bad_signature"] = {"token": head + "." + body + "." + signature, "status": 401}
elif action == "native":
    home = Path(payload["home"])
    assert home == Path(config["dsh_home"])
    records = [p for p in (home / "sessions").rglob("*.jsonl") if p.is_file()]
    assert records, "DSH must retain its native JSONL independently of SDK result events"
    texts = [p.read_text() for p in records]
    root_texts = [text for text in texts if json.loads(text.splitlines()[0]).get("id") == payload["session_id"]]
    assert len(root_texts) == 1, "Read the exact native root session, not another conversation"
    root_rows = [json.loads(line) for line in root_texts[0].splitlines()]
    model_turns = [row["data"]["turn"] for row in root_rows if row.get("type") == "assistant/message"
                   and row["data"]["message"].get("source", {}).get("kind") == "model"]
    wire = [json.loads(line) for line in Path(config["transport_log"]).read_text().splitlines()]
    model_requests = [row for row in wire if row["mode"] == "ingress" and row["method"] == "POST" and row["path"] == "/v1/messages" and row["authentication_header_sha256"].get(payload["credential_header"]) in payload["credential_sha256"]]
    model_requests = [{**row, "credential_sha256": row["authentication_header_sha256"][payload["credential_header"]]} for row in model_requests]
    result = {"model_requests": model_requests, "contents": root_texts[0], "model_message_turns": model_turns, "files": [{"path": str(p.relative_to(home)), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in records]}
elif action in {"rate", "budget"}:
    customer = gateway("/customer/info?end_user_id=" + urllib.parse.quote(config["employee_id"]))
    if action == "budget":
        spend = float(customer["spend"])
        assert spend > 0, "A budget denial must be backed by actual recorded inference spend"
        gateway("/budget/update", {"budget_id": customer["budget_id"], "max_budget": 10 if payload.get("restore") else spend / 2})
    elif payload["dimension"] == "employee":
        gateway("/budget/update", {"budget_id": customer["budget_id"], "tpm_limit": 1 if payload["limited"] else 100000})
    else:
        dimension = payload["dimension"]
        gateway("/user/update" if dimension == "application" else "/team/update", {
            "user_id" if dimension == "application" else "team_id": config["app_user_id"] if dimension == "application" else config["team_id"],
            "tpm_limit": 1 if payload["limited"] else 100000,
        })
    # Budget CRUD does not invalidate customer auth cache in pinned LiteLLM.
    # Wait its declared fixture TTL; do not discover stale grants by inference.
    time.sleep(config["management_cache_ttl_seconds"] + 0.1)
    result = {"updated": True}
elif action == "existing_key":
    answer = call(config["gateway_internal"], "/v1/chat/completions", {
        "model": config["model"], "messages": [{"role": "user", "content": "Reply with only the word maple."}], "max_tokens": 1024,
    }, bearer=Path(config["virtual_key_file"]).read_text().strip())
    assert "maple" in answer["choices"][0]["message"]["content"]
    result = {"virtual_key_inference": True}
elif action == "audit":
    deadline = time.monotonic() + 12
    while True:
        customer = gateway("/customer/info?end_user_id=" + urllib.parse.quote(config["employee_id"]))
        if float(customer["spend"]) > 0:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("Native customer spend did not settle")
        time.sleep(0.25)
    sql = '''SELECT json_agg(row_to_json(s)) FROM (SELECT "request_id", "api_key", "user", "end_user", "team_id", "spend", "metadata" FROM "LiteLLM_SpendLogs" WHERE "end_user" = :'employee') s;'''
    query = sql.replace(":'employee'", "'" + config["employee_id"] + "'")
    rows = json.loads(subprocess.check_output(["docker", "exec", config["postgres_container"], "psql", "-U", "postgres", "-d", "litellm", "-Atc", query], text=True))
    assert rows and all(row["user"] == config["app_user_id"] and row["team_id"] == config["team_id"] for row in rows)
    encoded = json.dumps(rows)
    # JWT-shaped bearer values must not enter the stored usage/audit facts.
    logs = subprocess.check_output(["docker", "logs", config["server_container"]], stderr=subprocess.STDOUT, text=True)
    native_logs = "\n".join(p.read_text() for p in Path(config["dsh_home"]).rglob("*.jsonl"))
    assert all(token not in encoded + logs + native_logs for token in payload["access_tokens"])
    result = {"employee": config["employee_id"], "application": config["app_user_id"], "team": config["team_id"],
              "spend": float(customer["spend"]), "actual_requests": len(rows), "raw_access_token_retained": False, "native_spend_rows": rows}
else:
    raise ValueError("Unknown task-owned fixture control")
print(json.dumps(result))
