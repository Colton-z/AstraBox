"""Release metadata for the Hermes runtime image."""

from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]


def test_pinned_hermes_license_and_notice_agree() -> None:
    """The NOTICE names a licence; the pin names the release it describes.

    The pinned source's LICENSE and pyproject.toml both declare MIT. Pin the
    reviewed source identity here so an image update requires a fresh licence
    check; a matching release number alone does not identify the source.
    """

    dockerfile = (_ROOT / "containers" / "sandbox-hermes" / "Dockerfile").read_text(
        encoding="utf-8"
    )
    notice = (_ROOT / "NOTICE").read_text(encoding="utf-8")

    assert "HERMES_AGENT_VERSION=0.21.0" in dockerfile
    assert "HERMES_SOURCE_COMMIT=29112bef099274229cadff79cdff7bf7b99c4b77" in dockerfile
    assert "MIT-licensed Hermes agent" in dockerfile
    assert "Hermes Agent (Nous Research, MIT License)" in notice
