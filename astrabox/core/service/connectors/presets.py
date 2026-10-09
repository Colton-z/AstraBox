"""Mainstream work apps an Assistant can use through the connector hub.

Each preset names the MCP server the hub forwards a user's calls to and how
the hub obtains that user's token. The server is the vendor's own remote MCP
server where the vendor runs a generally available one. Google's are a
developer preview whose terms forbid offering them to users outside one's own
domain, so Google's apps are served by the deployment's own
google_workspace_mcp instances, which call Google's generally available REST
APIs with the user's token; ``url_env`` names the variable holding each
instance's address.

``registration`` says how the hub identifies itself to the vendor: ``dynamic``
servers register a client on first use (RFC 7591); ``operator`` servers need
each deployment's own OAuth app, which an administrator enters. Endpoints and
registration modes were read from each vendor's MCP and OAuth documentation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ConnectorPreset:
    connector_id: str
    name: str
    registration: str
    #: The vendor's own MCP server.
    url: str = ""
    #: The variable naming the deployment's own server, for a vendor without one.
    url_env: str = ""
    #: The vendor's setup guide, where one was confirmed.
    docs_url: str = ""
    #: The scopes requested instead of everything the server's metadata lists.
    #: Set where that list over-reaches (Google's includes deleting the whole
    #: mailbox): the capability ChatGPT's own app for the vendor has, which is
    #: what Dots acts with, narrowed to what the server can use.
    scopes: tuple[str, ...] = ()
    #: The vendor's authorization and token endpoints, where they are fixed and
    #: the server cannot be asked (the deployment's own servers are not public).
    authorization_endpoint: str = ""
    token_endpoint: str = ""
    #: Extra authorization-request parameters the vendor needs, for example
    #: Google's ``access_type=offline`` without which it issues no refresh token.
    authorize_params: dict[str, str] = field(default_factory=dict)
    #: Whether the RFC 8707 ``resource`` must be left out of the authorization.
    #: Google's authorization server does not take one, and the deployment's
    #: own server address means nothing to it.
    omit_resource: bool = False

    def upstream_url(self) -> str:
        """The server the hub forwards to; empty when the deployment runs none."""

        if self.url_env:
            return deployed_server_urls().get(self.url_env, "")
        return self.url


_GOOGLE_OFFLINE = {"access_type": "offline", "prompt": "consent"}
_GOOGLE_AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
#: google_workspace_mcp checks each token against Google's userinfo endpoint,
#: which answers only with the email scope.
_GOOGLE_IDENTITY = ("openid", "https://www.googleapis.com/auth/userinfo.email")
_GOOGLE_DOCS = "https://github.com/taylorwilsdon/google_workspace_mcp"

GMAIL_MCP_URL_ENV = "ASTRABOX_GMAIL_MCP_URL"
GOOGLE_CALENDAR_MCP_URL_ENV = "ASTRABOX_GOOGLE_CALENDAR_MCP_URL"
GOOGLE_DRIVE_MCP_URL_ENV = "ASTRABOX_GOOGLE_DRIVE_MCP_URL"

def deployed_server_urls() -> dict[str, str]:
    """The deployment's own servers, by the variable naming each."""

    return {
        GMAIL_MCP_URL_ENV: str(os.environ.get(GMAIL_MCP_URL_ENV) or "").strip(),
        GOOGLE_CALENDAR_MCP_URL_ENV: str(os.environ.get(GOOGLE_CALENDAR_MCP_URL_ENV) or "").strip(),
        GOOGLE_DRIVE_MCP_URL_ENV: str(os.environ.get(GOOGLE_DRIVE_MCP_URL_ENV) or "").strip(),
    }


CONNECTOR_PRESETS: tuple[ConnectorPreset, ...] = (
    ConnectorPreset(
        connector_id="gmail",
        name="Gmail",
        registration="operator",
        url_env=GMAIL_MCP_URL_ENV,
        docs_url=_GOOGLE_DOCS,
        # Read, draft and send, not permanent deletion (ChatGPT's Gmail app).
        scopes=(*_GOOGLE_IDENTITY, "https://www.googleapis.com/auth/gmail.modify"),
        authorization_endpoint=_GOOGLE_AUTHORIZE,
        token_endpoint=_GOOGLE_TOKEN,
        authorize_params=_GOOGLE_OFFLINE,
        omit_resource=True,
    ),
    ConnectorPreset(
        connector_id="google-calendar",
        name="Google Calendar",
        registration="operator",
        url_env=GOOGLE_CALENDAR_MCP_URL_ENV,
        docs_url=_GOOGLE_DOCS,
        # Read calendars, read and write events (ChatGPT's Calendar app);
        # google_workspace_mcp lists calendars with calendar.readonly.
        scopes=(
            *_GOOGLE_IDENTITY,
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/calendar.events",
        ),
        authorization_endpoint=_GOOGLE_AUTHORIZE,
        token_endpoint=_GOOGLE_TOKEN,
        authorize_params=_GOOGLE_OFFLINE,
        omit_resource=True,
    ),
    ConnectorPreset(
        connector_id="google-drive",
        name="Google Drive",
        registration="operator",
        url_env=GOOGLE_DRIVE_MCP_URL_ENV,
        docs_url=_GOOGLE_DOCS,
        # Create, update, share and move files (ChatGPT's Drive app).
        scopes=(*_GOOGLE_IDENTITY, "https://www.googleapis.com/auth/drive"),
        authorization_endpoint=_GOOGLE_AUTHORIZE,
        token_endpoint=_GOOGLE_TOKEN,
        authorize_params=_GOOGLE_OFFLINE,
        omit_resource=True,
    ),
    ConnectorPreset(
        connector_id="notion",
        name="Notion",
        registration="dynamic",
        url="https://mcp.notion.com/mcp",
    ),
    ConnectorPreset(
        connector_id="linear",
        name="Linear",
        registration="dynamic",
        url="https://mcp.linear.app/mcp",
    ),
    ConnectorPreset(
        connector_id="atlassian",
        name="Jira and Confluence",
        registration="dynamic",
        url="https://mcp.atlassian.com/v2/mcp",
    ),
    ConnectorPreset(
        connector_id="github",
        name="GitHub",
        registration="operator",
        url="https://api.githubcopilot.com/mcp/",
        docs_url="https://github.com/github/github-mcp-server/blob/main/docs/host-integration.md",
    ),
    ConnectorPreset(
        connector_id="slack",
        name="Slack",
        registration="operator",
        url="https://mcp.slack.com/mcp",
        docs_url="https://docs.slack.dev/ai/slack-mcp-server/",
    ),
)


def connector_preset(connector_id: str) -> ConnectorPreset | None:
    return next(
        (preset for preset in CONNECTOR_PRESETS if preset.connector_id == connector_id), None
    )
