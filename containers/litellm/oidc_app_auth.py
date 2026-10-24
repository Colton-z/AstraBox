"""Independent OIDC inference clients, mapped to LiteLLM's native principals.

The policy names trusted issuers and applications. It grants inference only;
platform identity and IdP administrator groups confer no gateway privileges.
LiteLLM owns model access, spend, budgets and rate limits after authentication.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
import jwt
import yaml
from fastapi import HTTPException, Request
from litellm import BudgetExceededError
from litellm.proxy._types import LitellmUserRoles, ProxyException, UserAPIKeyAuth
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _PolicyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Application(_PolicyModel):
    id: str = Field(min_length=1)
    client_id: str = Field(min_length=1)
    audiences: tuple[str, ...] = Field(min_length=1)
    organizations: tuple[str, ...] = Field(min_length=1)
    organization_claim: str = "owner"
    client_id_claim: str = "azp"
    introspection_client_id: str = Field(min_length=1)
    introspection_secret_file: str = Field(min_length=1)
    app_user_id: str = Field(min_length=1)
    models: tuple[str, ...] = Field(min_length=1)
    team_id: str | None = None
    subject_models: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    subject_teams: dict[str, str] = Field(default_factory=dict)
    required_scopes: tuple[str, ...] = ()
    enabled: bool = True

    @model_validator(mode="after")
    def explicit_models(self) -> Application:
        if any(not model.strip() or model in {"*", "all-proxy-models"} for model in self.models):
            raise ValueError("application models must name explicit gateway routes")
        if any(not value.strip() for value in (*self.audiences, *self.organizations)):
            raise ValueError("audiences and organizations must be non-empty")
        return self


class Provider(_PolicyModel):
    kind: Literal["oidc", "casdoor"] = "oidc"
    issuer: str = Field(min_length=1)
    internal_issuer: str | None = None
    algorithms: tuple[Literal["RS256", "ES256"], ...] = ("RS256", "ES256")
    applications: tuple[Application, ...] = ()

    @model_validator(mode="after")
    def unique_clients(self) -> Provider:
        _http_url(self.issuer, public=True)
        if self.internal_issuer:
            _http_url(self.internal_issuer)
        clients = [app.client_id for app in self.applications]
        if len(clients) != len(set(clients)) or not self.algorithms:
            raise ValueError("an issuer needs unique client IDs and signature algorithms")
        return self

    def endpoint(self, public_url: str) -> str:
        _http_url(public_url, public=True)
        issuer = self.issuer.rstrip("/")
        if self.internal_issuer and public_url.startswith(issuer + "/"):
            return self.internal_issuer.rstrip("/") + public_url[len(issuer):]
        return public_url


class Policy(_PolicyModel):
    providers: tuple[Provider, ...] = ()
    jwks_cache_seconds: int = Field(default=300, ge=1, le=3600)

    @model_validator(mode="after")
    def distinct_principals(self) -> Policy:
        issuers = [provider.issuer for provider in self.providers]
        apps = [app for provider in self.providers for app in provider.applications]
        if len(issuers) != len(set(issuers)):
            raise ValueError("configure an issuer once")
        for values in ([app.id for app in apps], [app.app_user_id for app in apps]):
            if len(values) != len(set(values)):
                raise ValueError("applications need distinct IDs and LiteLLM application users")
        return self


def _http_url(value: str, *, public: bool = False) -> None:
    address = urlsplit(value)
    if (
        address.scheme not in {"https", "http"}
        or not address.hostname
        or address.username is not None
        or address.password is not None
        or (public and address.scheme != "https" and address.hostname not in {"localhost", "127.0.0.1", "::1"})
    ):
        raise ValueError("OIDC endpoints require HTTPS, or HTTP on loopback")


def employee_id(issuer: str, subject: str) -> str:
    """The same issuer/subject pair has one customer identity across clients."""
    value = json.dumps([issuer, subject], ensure_ascii=False, separators=(",", ":"))
    return "oidc:" + hashlib.sha256(value.encode()).hexdigest()


def _error(message: str, code: int, kind: str = "oidc_inference_auth") -> ProxyException:
    return ProxyException(message=message, type=kind, param=None, code=code)


@lru_cache(maxsize=4)
def _parse_policy(content: str) -> Policy:
    return Policy.model_validate(yaml.safe_load(content))


@lru_cache(maxsize=32)
def _jwks_client(url: str, lifespan: int) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(url, lifespan=lifespan, timeout=10)


class OidcApplicationAuth:
    """Read the current policy and validate each inference request independently."""

    def __init__(self, path: str, *, platform_issuer: str = "", platform_clients: tuple[str, ...] = ()):
        self.path = Path(path)
        self.platform_issuer = platform_issuer
        self.platform_clients = frozenset(value for value in platform_clients if value)
        self.policy()

    def policy(self) -> Policy:
        # Re-reading the file makes a disable/removal apply on the next request.
        # Only parsing is cached; malformed edits never reuse the previous policy.
        policy = _parse_policy(self.path.read_text())
        for provider in policy.providers:
            if provider.issuer == self.platform_issuer and len(self.platform_clients) < 2:
                raise ValueError("a shared issuer requires platform client-bound token introspection")
            for app in provider.applications:
                if provider.issuer == self.platform_issuer and app.client_id in self.platform_clients:
                    raise ValueError("inference applications must be separate from platform clients")
                if not Path(app.introspection_secret_file).read_text().strip():
                    raise ValueError("an introspection secret file is empty")
        return policy

    async def authenticate(self, request: Request, token: str) -> UserAPIKeyAuth | None:
        try:
            hints = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError as exc:
            raise _error("Invalid OIDC access token", 401) from exc
        issuer = hints.get("iss")
        # This is dispatch only. The existing platform adapter still validates
        # a console/API credential against its own provider and client policy.
        legacy_client = hints.get("azp") or hints.get("client_id")
        if legacy_client is not None and not isinstance(legacy_client, str):
            raise _error("Invalid OIDC client identity", 401)
        audiences = hints.get("aud")
        audience_values = [audiences] if isinstance(audiences, str) else audiences
        platform_audience = (
            legacy_client is None and isinstance(audience_values, list)
            and any(value in self.platform_clients for value in audience_values if isinstance(value, str))
        )
        if issuer == self.platform_issuer and (legacy_client in self.platform_clients or platform_audience):
            return None
        try:
            policy = self.policy()
        except (OSError, ValueError, yaml.YAMLError) as exc:
            raise _error("OIDC inference policy is unavailable", 503) from exc
        provider = next((item for item in policy.providers if item.issuer == issuer), None)
        if provider is None:
            raise _error("Untrusted OIDC issuer", 401)
        app = next(
            (item for item in provider.applications if hints.get(item.client_id_claim) == item.client_id),
            None,
        )
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            document = await self._discovery(client, provider)
            claims = await self._claims(provider, policy, document, token, app)
            if app is None or not app.enabled:
                raise _error("OIDC application is not authorized for inference", 403)
            if claims.get(app.client_id_claim) != app.client_id:
                raise _error("OIDC application is not authorized for inference", 403)
            if claims.get(app.organization_claim) not in app.organizations:
                raise _error("OIDC organization is not authorized for inference", 403)
            if claims.get("type") == "application":
                raise _error("An employee access token is required", 403)
            await self._active_employee(client, provider, app, document, token, claims)
        if (request.method, request.url.path) not in {
            ("GET", "/models"), ("GET", "/v1/models"),
            ("POST", "/chat/completions"), ("POST", "/v1/chat/completions"),
            ("POST", "/messages"), ("POST", "/v1/messages"),
        }:
            raise _error("This OIDC application can only use inference routes", 403)
        try:
            return await self._native_grant(provider, app, claims, request)
        except ProxyException:
            raise
        except BudgetExceededError as exc:
            raise _error(exc.message, exc.status_code, "budget_exceeded") from exc
        except HTTPException as exc:
            raise _error(str(exc.detail), exc.status_code, "inference_policy") from exc
        except Exception as exc:
            # An independent credential must never fall through auto-mode's
            # native-key dispatcher when its grant database is unavailable.
            raise _error("OIDC inference grant validation is unavailable", 503) from exc

    @staticmethod
    async def _inference_request(request: Request, grant: UserAPIKeyAuth) -> None:
        if request.query_params:
            raise _error("Inference policy cannot be overridden by query parameters", 403)
        if request.method != "POST":
            return
        from litellm.proxy.common_utils.http_parsing_utils import _safe_set_request_parsed_body

        # This entry point exposes the standard inference protocols, not the
        # proxy's per-request upstream credentials, routing or mock controls.
        if request.url.path.endswith("/messages"):
            from litellm.types.llms.anthropic import AnthropicMessagesRequestOptionalParams

            fields = set(AnthropicMessagesRequestOptionalParams.__annotations__) | {"model", "messages"}
        else:
            from openai.types.chat.completion_create_params import CompletionCreateParamsBase

            fields = set(CompletionCreateParamsBase.__annotations__) | {"stream"}
        body = await request.json()
        if not isinstance(body, dict):
            raise _error("Inference requests require a JSON object", 400)
        if set(body) - fields:
            raise _error("Only standard inference parameters are allowed", 403)
        if body.get("model") not in grant.models:
            raise _error("The employee is not authorized for this model", 403)
        if "user" in body and body["user"] != grant.end_user_id:
            raise _error("The employee identity cannot be overridden", 403)
        body["user"] = grant.end_user_id
        _safe_set_request_parsed_body(request=request, parsed_body=body)
        request._json = body
        request._body = json.dumps(body).encode()

    @staticmethod
    async def _discovery(client: httpx.AsyncClient, provider: Provider) -> dict[str, Any]:
        url = (provider.internal_issuer or provider.issuer).rstrip("/") + "/.well-known/openid-configuration"
        try:
            response = await client.get(url)
            response.raise_for_status()
            document = response.json()
            if not isinstance(document, dict) or document.get("issuer") != provider.issuer:
                raise ValueError("discovery issuer mismatch")
            for name in ("jwks_uri", "introspection_endpoint", "userinfo_endpoint"):
                provider.endpoint(document[name])
            return document
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise _error("OIDC provider discovery is unavailable", 503) from exc

    @staticmethod
    async def _claims(
        provider: Provider, policy: Policy, document: dict[str, Any], token: str,
        app: Application | None,
    ) -> dict[str, Any]:
        def decode() -> dict[str, Any]:
            key = _jwks_client(provider.endpoint(document["jwks_uri"]), policy.jwks_cache_seconds)
            return jwt.decode(
                token, key.get_signing_key_from_jwt(token).key,
                algorithms=list(provider.algorithms), issuer=provider.issuer,
                audience=list(app.audiences) if app else None,
                options={"require": ["iss", "sub", "aud", "exp", "iat"], "verify_aud": app is not None},
            )
        try:
            claims = await asyncio.to_thread(decode)
            if not isinstance(claims.get("sub"), str) or not claims["sub"]:
                raise jwt.InvalidTokenError("subject is missing")
            return claims
        except jwt.PyJWKClientConnectionError as exc:
            raise _error("OIDC signing keys are unavailable", 503) from exc
        except jwt.PyJWTError as exc:
            raise _error("Invalid or expired OIDC access token", 401) from exc

    @staticmethod
    async def _active_employee(
        client: httpx.AsyncClient, provider: Provider, app: Application,
        document: dict[str, Any], token: str, claims: dict[str, Any],
    ) -> None:
        try:
            secret = Path(app.introspection_secret_file).read_text().strip()
            response = await client.post(
                provider.endpoint(document["introspection_endpoint"]),
                auth=(app.introspection_client_id, secret),
                data={"token": token, "token_type_hint": "access_token"},
            )
            response.raise_for_status()
            active = response.json()
            if not isinstance(active, dict) or active.get("active") is not True:
                raise _error("OIDC access token is inactive", 401)
            if active.get("client_id") != app.client_id or active.get("sub") != claims["sub"]:
                raise _error("OIDC access token identity does not match", 401)
            if active.get("iss") not in (None, provider.issuer):
                raise _error("OIDC access token issuer does not match", 401)
            scopes = active.get("scope", "")
            if not isinstance(scopes, str):
                raise ValueError("invalid introspection scope")
            if not set(app.required_scopes) <= set(scopes.split()):
                raise _error("OIDC access token lacks inference scopes", 403)
            response = await client.get(
                provider.endpoint(document["userinfo_endpoint"]),
                headers={"Authorization": "Bearer " + token},
            )
            if response.status_code in {400, 401, 403}:
                raise _error("OIDC employee is not active", 401)
            response.raise_for_status()
            employee = response.json()
            if not isinstance(employee, dict):
                raise ValueError("invalid UserInfo response")
            if employee.get("status") == "error" or employee.get("isForbidden") is True or employee.get("isDeleted") is True:
                raise _error("OIDC employee is not active", 401)
            if employee.get("sub") != claims["sub"]:
                raise _error("OIDC employee identity does not match", 401)
            if provider.kind == "casdoor":
                # Casdoor 3.128 introspection and UserInfo do not check the
                # current IsForbidden/IsDeleted flags. Its application API
                # can look up the stable UUID without trusting a stale name.
                response = await client.get(
                    provider.endpoint(provider.issuer.rstrip("/") + "/api/get-user"),
                    auth=(app.introspection_client_id, secret),
                    params={"owner": claims[app.organization_claim], "userId": claims["sub"]},
                )
                response.raise_for_status()
                account = response.json()
                if not isinstance(account, dict) or account.get("status") != "ok":
                    raise ValueError("Casdoor account lookup is unavailable")
                current = account.get("data")
                if current is None:
                    raise _error("OIDC employee is not active", 401)
                if not isinstance(current, dict):
                    raise ValueError("invalid Casdoor account response")
                if current.get("id") != claims["sub"] or current.get("owner") != claims[app.organization_claim]:
                    raise _error("OIDC employee identity does not match", 401)
                if current.get("isForbidden") is not False or current.get("isDeleted") is not False:
                    raise _error("OIDC employee is not active", 401)
        except (httpx.HTTPError, OSError, KeyError, TypeError, ValueError) as exc:
            raise _error("OIDC provider validation is unavailable", 503) from exc

    @staticmethod
    async def _native_grant(
        provider: Provider, app: Application, claims: dict[str, Any], request: Request,
    ) -> UserAPIKeyAuth:
        from litellm.proxy.auth.auth_checks import (
            get_end_user_object, get_team_object, get_user_object, _unpriced_models_in_request,
        )
        from litellm.proxy.proxy_server import llm_router, prisma_client, proxy_logging_obj, user_api_key_cache

        if prisma_client is None:
            raise _error("OIDC inference requires the gateway database", 503)
        subject = claims["sub"]
        customer_id = employee_id(provider.issuer, subject)
        common = dict(prisma_client=prisma_client, user_api_key_cache=user_api_key_cache,
                      parent_otel_span=None, proxy_logging_obj=proxy_logging_obj)
        app_user = await get_user_object(user_id=app.app_user_id, user_id_upsert=False, **common)
        customer = await get_end_user_object(end_user_id=customer_id, **common)
        if app_user is None or customer is None:
            raise _error("The application and employee need explicit gateway grants", 403)
        if app_user.user_role not in {LitellmUserRoles.INTERNAL_USER, LitellmUserRoles.INTERNAL_USER_VIEW_ONLY}:
            raise _error("The gateway application must use a non-administrator account", 403)
        if customer.blocked:
            raise _error("The employee gateway grant is blocked", 403)
        models = list(app.models)
        if subject in app.subject_models:
            models = [name for name in models if name in app.subject_models[subject]]
        native_models = app_user.models or []
        if native_models and "all-proxy-models" not in native_models:
            models = [name for name in models if name in native_models]
        if not models:
            raise _error("The employee has no authorized inference models", 403)
        team_id = app.subject_teams.get(subject, app.team_id)
        team = await get_team_object(team_id=team_id, **common) if team_id else None
        if team and team.models and "all-proxy-models" not in team.models:
            models = [name for name in models if name in team.models]
        if not models:
            raise _error("The employee has no authorized team models", 403)
        if _unpriced_models_in_request(model=models, llm_router=llm_router):
            raise _error("OIDC inference routes require configured model pricing", 503)
        budget = customer.litellm_budget_table
        fingerprint = hashlib.sha256(json.dumps(
            [provider.issuer, app.client_id, subject], ensure_ascii=False, separators=(",", ":"),
        ).encode()).hexdigest()
        grant = UserAPIKeyAuth(
            token=fingerprint, api_key=fingerprint, key_alias="oidc:" + app.id,
            user_id=app.app_user_id, end_user_id=customer_id, team_id=team_id,
            user_role=LitellmUserRoles.INTERNAL_USER, models=models,
            allowed_routes=["/models", "/v1/models", "/chat/completions", "/v1/chat/completions", "/messages", "/v1/messages"],
            user_spend=app_user.spend, user_max_budget=app_user.max_budget,
            user_rpm_limit=app_user.rpm_limit, user_tpm_limit=app_user.tpm_limit,
            end_user_max_budget=budget.max_budget if budget else None,
            end_user_rpm_limit=budget.rpm_limit if budget else None,
            end_user_tpm_limit=budget.tpm_limit if budget else None,
            team_spend=team.spend if team else None,
            team_max_budget=team.max_budget if team else None,
            team_rpm_limit=team.rpm_limit if team else None,
            team_tpm_limit=team.tpm_limit if team else None,
            team_models=models if team else [],
            metadata={"oidc_issuer": provider.issuer, "oidc_application": app.id,
                      "oidc_client_id": app.client_id, "oidc_employee_id": customer_id},
        )

        # Run the vendor's governance only for this independent credential.
        # Global custom-auth switches would also change AstraBox's own tokens.
        from litellm.proxy.auth.auth_checks import common_checks
        from litellm.proxy.auth.user_api_key_auth import (
            _enforce_key_and_fallback_model_access,
            _reserve_budget_after_common_checks,
            _run_post_custom_auth_checks,
            _should_skip_budget_checks,
            get_global_proxy_spend,
        )
        from litellm.proxy.proxy_server import general_settings, llm_model_list, litellm_proxy_admin_name

        await OidcApplicationAuth._inference_request(request, grant)
        data = await request.json() if request.method == "POST" else {}
        route = request.url.path
        grant = await _run_post_custom_auth_checks(
            valid_token=grant, request=request, request_data=data,
            route=route, parent_otel_span=None,
        )
        await _enforce_key_and_fallback_model_access(
            valid_token=grant, request_data=data, route=route, request=request,
            llm_model_list=llm_model_list, llm_router=llm_router,
        )
        settings = {**general_settings, "apply_user_budget_to_team_keys": True}
        skip_budget = _should_skip_budget_checks(
            request_data=data, route=route, request=request, llm_router=llm_router,
        )
        global_spend = await get_global_proxy_spend(
            litellm_proxy_admin_name=litellm_proxy_admin_name,
            user_api_key_cache=user_api_key_cache, prisma_client=prisma_client,
            token=fingerprint, proxy_logging_obj=proxy_logging_obj,
        )
        await common_checks(
            request_body=data, team_object=team, user_object=app_user,
            end_user_object=customer, global_proxy_spend=global_spend,
            general_settings=settings, route=route, llm_router=llm_router,
            proxy_logging_obj=proxy_logging_obj, valid_token=grant,
            request=request, skip_budget_checks=skip_budget,
        )
        await _reserve_budget_after_common_checks(
            user_api_key_auth_obj=grant, request_data=data, route=route,
            llm_router=llm_router, team_object=team, user_object=app_user,
            prisma_client=prisma_client, user_api_key_cache=user_api_key_cache,
            proxy_logging_obj=proxy_logging_obj, skip_budget_checks=skip_budget,
            general_settings=settings, end_user_id=customer_id,
            end_user_object=customer,
        )
        return grant
