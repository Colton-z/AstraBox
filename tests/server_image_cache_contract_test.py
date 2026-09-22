from pathlib import Path


_SERVER_DOCKERFILE = (
    Path(__file__).resolve().parents[1] / "containers" / "server" / "Dockerfile"
)


def test_embedded_gateway_layer_does_not_depend_on_the_application_wheel() -> None:
    dockerfile = _SERVER_DOCKERFILE.read_text(encoding="utf-8")

    gateway_install = dockerfile.index("RUN python -m venv /opt/litellm")
    application_install = dockerfile.index(
        "RUN --mount=type=bind,from=build,source=/dist,target=/dist"
    )

    assert gateway_install < application_install, (
        "changing AstraBox code must not reinstall the independently pinned "
        "LiteLLM environment"
    )
