"""An Environment image is a pin; empty follows the selected engine adapter.

Two meanings, and the difference is reference versus copy. An environment that
names an image runs that image and upgrading the deployment does not move it —
which is how an environment for another engine binds to its own image. An
environment that names none resolves its adapter's default at create time, so
the selected engine's image contract stays the live answer.

Seeding the field with the deployment default collapses the two: it writes a pin
nobody chose, frozen at whatever the image happened to be when the store was
first created, and from then on the deployment setting cannot move that
environment. Nothing reports it, because a pin is a legitimate state.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from astrabox import __version__
from astrabox.common.utils.errors import APIError
from astrabox.config.release_images import (
    IMAGE_PREFIX_ENV,
    IMAGE_TAG_ENV,
    PUBLISHED_IMAGE_PREFIX,
)
from astrabox.core.model import AgentView
from astrabox.core.service.orchestrator.engine import registry
from astrabox.core.service.orchestrator.runtime.config_resolver import (
    resolve_runtime_template_name,
)
from astrabox.providers.sandbox_image import (
    DEFAULT_AGENT_IMAGE_ENV,
    resolve_agent_image,
)
from astrabox.providers import register_builtin_providers


def _view(
    runtime_template_name: str | None,
    *,
    engine_kind: str = "claude_code",
    name: str = "claude-code",
) -> AgentView:
    register_builtin_providers()
    view = AgentView(name=name)
    view.engine_kind = engine_kind
    view.runtime_template_name = runtime_template_name
    return view


def test_an_unpinned_claude_environment_follows_the_deployment_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DEFAULT_AGENT_IMAGE_ENV, "registry/agent:deployed")

    assert resolve_runtime_template_name(_view("")) == "registry/agent:deployed"
    assert resolve_runtime_template_name(_view(None)) == "registry/agent:deployed"


def test_an_unpinned_claude_environment_moves_when_the_deployment_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point: the deployment setting stays live, not copied once."""
    unpinned = _view("")

    monkeypatch.setenv(DEFAULT_AGENT_IMAGE_ENV, "registry/agent:first")
    first = resolve_runtime_template_name(unpinned)
    monkeypatch.setenv(DEFAULT_AGENT_IMAGE_ENV, "registry/agent:second")
    second = resolve_runtime_template_name(unpinned)

    assert (first, second) == ("registry/agent:first", "registry/agent:second"), (
        "an environment that pins nothing must resolve the image at create time; "
        "resolving it once and keeping the answer is the defect"
    )


def test_a_pinned_environment_is_carried_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pin is a feature: another engine's environment binds to its own image."""
    monkeypatch.setenv(DEFAULT_AGENT_IMAGE_ENV, "registry/agent:deployed")

    assert resolve_runtime_template_name(
        _view(
            "registry/hermes:2.1",
            engine_kind="assistant",
            name="assistant-hermes",
        )
    ) == "registry/hermes:2.1", (
        "a named image wins over the deployment default and keeps winning; "
        "upgrading the deployment must not move an environment that chose one"
    )


@pytest.mark.parametrize(
    ("engine_kind", "component"),
    [
        ("assistant", "sandbox-hermes"),
        ("deepseek_harness", "sandbox-deepseek-harness"),
    ],
)
def test_an_unpinned_environment_follows_its_engine_adapter_image(
    monkeypatch: pytest.MonkeyPatch,
    engine_kind: str,
    component: str,
) -> None:
    """Each engine runs its own release image, not the Claude override."""
    monkeypatch.setenv(DEFAULT_AGENT_IMAGE_ENV, "registry/claude:deployed")
    monkeypatch.delenv(IMAGE_PREFIX_ENV, raising=False)
    monkeypatch.delenv(IMAGE_TAG_ENV, raising=False)

    assert resolve_runtime_template_name(
        _view("", engine_kind=engine_kind)
    ) == f"{PUBLISHED_IMAGE_PREFIX}{component}:{__version__}"


def test_an_unpinned_adapter_without_a_default_image_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        registry,
        "get_engine_adapter",
        lambda _engine_kind: SimpleNamespace(
            capabilities=SimpleNamespace(default_runtime_image=None)
        ),
    )

    with pytest.raises(APIError) as raised:
        resolve_runtime_template_name(_view("", engine_kind="third_party"))

    assert raised.value.code == "ENGINE_RUNTIME_IMAGE_REQUIRED"
    assert "runtime_template_name" in raised.value.message


def test_an_unpinned_environment_never_resolves_to_its_own_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resolved value is a container image, so a record name cannot stand in."""
    monkeypatch.setenv(DEFAULT_AGENT_IMAGE_ENV, "registry/agent:deployed")

    resolved = resolve_runtime_template_name(_view("", name="claude-code"))

    assert resolved != "claude-code", (
        "falling back to the record's name hands the create path a container "
        "image named after an environment, which no registry can serve"
    )


async def test_the_view_overlay_carries_an_empty_field_as_empty() -> None:
    """Only the resolver decides what "unset" means, so the overlay must not."""
    from astrabox.core.service.orchestrator.agent_config_service import (
        AgentConfigService,
    )

    view = AgentView(name="agent")
    AgentConfigService._overlay_environment_runtime(
        object.__new__(AgentConfigService),
        view,
        {"engine_kind": "claude_code", "sandbox_backend": "open_sandbox"},
    )

    assert not view.runtime_template_name, (
        "substituting the environment name here would make every unpinned "
        "environment look pinned to the resolver"
    )


def test_the_seeded_environment_pins_no_image() -> None:
    """The seed must not copy the deployment default onto the record.

    Asserted against the seed source rather than a run of it: the write is one
    dict literal, and what matters is that the image keys are absent from it.
    """
    import inspect

    from astrabox.api import app as app_module

    source = inspect.getsource(app_module._seed_default_agent_if_empty)
    seed_body = source.split("upsert_by_name", 1)[1].split("_seed_default_model", 1)[0]

    assert '"runtime_template_name"' not in seed_body, (
        "a seeded environment must pin no image: the value would be whatever the "
        "deployment was configured with the day the store was created, and it "
        "would outrank the deployment setting from then on"
    )
    assert '"runtime_image"' not in seed_body, (
        "same for the sibling field, which is written and read by nobody"
    )


def test_the_claude_deployment_image_is_resolved_each_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The env var is live configuration, not a value captured at import."""
    monkeypatch.setenv(DEFAULT_AGENT_IMAGE_ENV, "registry/agent:one")
    assert resolve_agent_image() == "registry/agent:one"
    monkeypatch.setenv(DEFAULT_AGENT_IMAGE_ENV, "registry/agent:two")
    assert resolve_agent_image() == "registry/agent:two"


def test_every_consumer_of_the_resolver_uses_it_as_an_image() -> None:
    """Why the record-name fallback had to go, pinned so it cannot return.

    The resolver's result is passed as ``image=`` at every call site. A fallback
    to the record's own name was only ever coherent when a backend registered
    templates by name; against a container registry it yields an image nobody
    can pull.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "astrabox"
    sources = [p.read_text(encoding="utf-8") for p in root.rglob("*.py")]
    joined = "\n".join(sources)

    assert "default_runtime_image" in joined
    assert 'or ""' in joined  # the pin check itself
    assert "return str(template.name or \"\").strip()" not in joined, (
        "the record-name fallback must not come back: it silently substitutes an "
        "environment name where a container image is required"
    )
