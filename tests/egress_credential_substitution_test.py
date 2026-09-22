"""Egress credential substitution semantics — the normative contract tests.

Every sandbox backend that sets ``supports_egress_credential_injection`` runs
its proxy through :mod:`astrabox.seams.egress_credentials`; these tests ARE
the substitution contract (placeholder minting, host gating incl. wildcard +
port entries, header/body location gating, multi-credential requests, and the
fail-closed refusal rule).
"""

from __future__ import annotations

import pytest

from astrabox.seams.egress_credentials import (
    PLACEHOLDER_PREFIX,
    EgressCredential,
    EgressCredentialMap,
    EgressSubstitutionRefused,
    host_matches,
    mint_placeholder,
)


def _cred(
    cid: str = "cred-1",
    *,
    secret_name: str = "GITHUB_TOKEN",
    secret_value: str = "ghp_real",
    networking: dict | None = None,
    header: bool = True,
    body: bool = True,
    allowed_requests: dict | None = None,
) -> EgressCredential:
    return EgressCredential(
        credential_id=cid,
        secret_name=secret_name,
        secret_value=secret_value,
        placeholder=mint_placeholder(cid),
        networking=networking or {"type": "limited", "allowed_hosts": ["api.github.com"]},
        injection_location={"header": header, "body": body},
        allowed_requests=allowed_requests or {},
    )


# ── placeholder minting ──────────────────────────────────────────────────────


def test_placeholder_is_opaque_prefixed_and_session_unique() -> None:
    a, b = mint_placeholder("c1"), mint_placeholder("c1")
    assert a.startswith(PLACEHOLDER_PREFIX + "c1::")
    assert a != b, "placeholders must be nonce-unique per mint (session-scoped)"


def test_placeholder_context_is_stable_within_one_sandbox_generation() -> None:
    first = mint_placeholder("c1", context="session-1:generation-1")
    repeated = mint_placeholder("c1", context="session-1:generation-1")
    next_generation = mint_placeholder("c1", context="session-1:generation-2")

    assert first == repeated
    assert first != next_generation
    assert first.startswith(PLACEHOLDER_PREFIX + "c1::")


def test_sandbox_env_exposes_only_placeholders() -> None:
    cred = _cred()
    env = EgressCredentialMap([cred]).sandbox_env()
    assert env == {"GITHUB_TOKEN": cred.placeholder}
    assert "ghp_real" not in str(env)


# ── host matching ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("host", "port", "entry", "expected"),
    [
        ("api.github.com", 443, "api.github.com", True),
        ("API.GITHUB.COM", 443, "api.github.com", True),
        ("api.github.com", 443, "github.com", False),
        ("sub.api.github.com", 443, "*.github.com", True),
        ("api.github.com", 443, "*.github.com", True),
        ("github.com", 443, "*.github.com", False),  # apex needs its own entry
        ("api.github.com", 8443, "api.github.com:8443", True),
        ("api.github.com", 443, "api.github.com:8443", False),
        ("api.github.com", None, "api.github.com:443", True),  # None -> https default
        ("evil.com", 443, "api.github.com", False),
    ],
)
def test_host_matches(host: str, port: int | None, entry: str, expected: bool) -> None:
    assert host_matches(host, port, entry) is expected


# ── substitution + gating ────────────────────────────────────────────────────


def test_header_and_body_substitution_on_allowed_host() -> None:
    cred = _cred()
    result = EgressCredentialMap([cred]).apply(
        scheme="https",
        host="api.github.com",
        port=443,
        headers={"Authorization": f"Bearer {cred.placeholder}"},
        body=f'{{"token": "{cred.placeholder}"}}',
    )
    assert result.headers["Authorization"] == "Bearer ghp_real"
    assert result.body == '{"token": "ghp_real"}'
    assert result.substituted_credential_ids == ["cred-1"]


def test_request_without_placeholders_passes_through_untouched() -> None:
    cred = _cred()
    result = EgressCredentialMap([cred]).apply(
        scheme="https",
        host="evil.com", port=443, headers={"X": "y"}, body="plain"
    )
    assert result.headers == {"X": "y"} and result.body == "plain"
    assert result.substituted_credential_ids == []


def test_disallowed_host_refuses_fail_closed() -> None:
    cred = _cred()
    with pytest.raises(EgressSubstitutionRefused) as exc:
        EgressCredentialMap([cred]).apply(
            scheme="https",
            host="attacker.example",
            port=443,
            headers={"Authorization": cred.placeholder},
        )
    assert exc.value.location == "host" and exc.value.credential_id == "cred-1"


def test_unrestricted_networking_substitutes_anywhere() -> None:
    cred = _cred(networking={"type": "unrestricted"})
    result = EgressCredentialMap([cred]).apply(
        scheme="https",
        host="anywhere.example", port=443, headers={"A": cred.placeholder}
    )
    assert result.headers["A"] == "ghp_real"


def test_disabled_header_location_refuses() -> None:
    cred = _cred(header=False, body=True)
    with pytest.raises(EgressSubstitutionRefused) as exc:
        EgressCredentialMap([cred]).apply(
            scheme="https",
            host="api.github.com", port=443, headers={"A": cred.placeholder}
        )
    assert exc.value.location == "header"


def test_disabled_body_location_refuses() -> None:
    cred = _cred(header=True, body=False)
    with pytest.raises(EgressSubstitutionRefused) as exc:
        EgressCredentialMap([cred]).apply(
            scheme="https",
            host="api.github.com", port=443, headers={}, body=cred.placeholder
        )
    assert exc.value.location == "body"


def test_allowed_method_and_path_are_enforced_before_substitution() -> None:
    cred = _cred(
        allowed_requests={"methods": ["GET"], "paths": ["/repos/acme/private/*"]}
    )
    allowed = EgressCredentialMap([cred]).apply(
        scheme="https",
        host="api.github.com",
        port=443,
        method="GET",
        path="/repos/acme/private/issues",
        headers={"Authorization": cred.placeholder},
    )
    assert allowed.headers["Authorization"] == "ghp_real"

    with pytest.raises(EgressSubstitutionRefused) as wrong_method:
        EgressCredentialMap([cred]).apply(
            scheme="https",
            host="api.github.com",
            port=443,
            method="DELETE",
            path="/repos/acme/private/issues",
            headers={"Authorization": cred.placeholder},
        )
    assert wrong_method.value.location == "method"

    with pytest.raises(EgressSubstitutionRefused) as wrong_path:
        EgressCredentialMap([cred]).apply(
            scheme="https",
            host="api.github.com",
            port=443,
            method="GET",
            path="/user",
            headers={"Authorization": cred.placeholder},
        )
    assert wrong_path.value.location == "path"


def test_multi_credential_request_substitutes_each_by_its_own_rules() -> None:
    gh = _cred("gh", secret_name="GH", secret_value="gh-real")
    slack = _cred(
        "slack",
        secret_name="SLACK",
        secret_value="xoxb-real",
        networking={"type": "limited", "allowed_hosts": ["slack.com", "*.slack.com"]},
    )
    # Host allowed for gh only: a request carrying BOTH placeholders must be
    # refused (fail-closed on the slack one), never partially substituted.
    with pytest.raises(EgressSubstitutionRefused):
        EgressCredentialMap([gh, slack]).apply(
            scheme="https",
            host="api.github.com",
            port=443,
            headers={"A": gh.placeholder, "B": slack.placeholder},
        )
    # Each on its own allowed host works.
    ok = EgressCredentialMap([gh, slack]).apply(
        scheme="https",
        host="api.slack.com", port=443, headers={"B": slack.placeholder}
    )
    assert ok.headers["B"] == "xoxb-real"


def test_map_rejects_foreign_placeholders() -> None:
    cred = EgressCredential(
        credential_id="c",
        secret_name="X",
        secret_value="v",
        placeholder="not-a-minted-placeholder",
        networking={"type": "unrestricted"},
        injection_location={"header": True, "body": True},
    )
    with pytest.raises(ValueError, match="mint_placeholder"):
        EgressCredentialMap([cred])


# ── TLS gating / secret hygiene / streamed-body window ──────────────────────


def test_plaintext_http_refuses_even_on_allowed_host() -> None:
    """A cleartext request to an ALLOWED host must not receive the secret."""
    cred = _cred()
    with pytest.raises(EgressSubstitutionRefused) as exc:
        EgressCredentialMap([cred]).apply(
            scheme="http",
            host="api.github.com",
            port=80,
            headers={"Authorization": cred.placeholder},
        )
    assert exc.value.location == "scheme"


def test_allow_insecure_http_optin_permits_cleartext_target() -> None:
    cred = EgressCredential(
        credential_id="internal",
        secret_name="TOKEN",
        secret_value="real",
        placeholder=mint_placeholder("internal"),
        networking={"type": "limited", "allowed_hosts": ["intranet.local"]},
        injection_location={"header": True, "body": True},
        allow_insecure_http=True,
    )
    result = EgressCredentialMap([cred]).apply(
        scheme="http", host="intranet.local", port=80, headers={"A": cred.placeholder}
    )
    assert result.headers["A"] == "real"


def test_secret_value_is_masked_in_repr() -> None:
    cred = _cred(secret_value="super-secret-value")
    assert "super-secret-value" not in repr(cred)
    assert "super-secret-value" not in repr(EgressCredentialMap([cred])._by_placeholder)


def test_substitution_result_repr_never_prints_the_substituted_secret() -> None:
    """The substitution result masks the secret carried in headers and body.

    ``EgressCredential`` uses ``repr=False``, but the returned request is also
    loggable by the proxy and therefore must not expose the substituted value.
    """
    cred = _cred(secret_value="super-secret-value")
    result = EgressCredentialMap([cred]).apply(
        scheme="https",
        host="api.github.com",
        port=443,
        headers={"Authorization": f"Bearer {cred.placeholder}"},
        body=f'{{"token": "{cred.placeholder}"}}',
    )
    assert result.headers["Authorization"] == "Bearer super-secret-value"
    assert "super-secret-value" not in repr(result)
    assert "super-secret-value" not in str(result)
    # The safe fields still show (operators need the audit trail).
    assert "cred-1" in repr(result)


def test_max_placeholder_length_names_the_stream_scan_window() -> None:
    a, b = _cred("short"), _cred("a-much-longer-credential-identifier")
    m = EgressCredentialMap([a, b])
    assert m.max_placeholder_length == max(len(a.placeholder), len(b.placeholder))
    assert EgressCredentialMap([]).max_placeholder_length == 0
