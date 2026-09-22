from __future__ import annotations

from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]
_PRODUCTION_ROOT = _REPO_ROOT / "astrabox"
_RETIRED_STORE_NAMES = (
    "session_journal",
    "ai_sdk_frames",
    "ui_messages",
)


def test_retired_projection_stores_cannot_reenter_production_code() -> None:
    offenders: list[str] = []
    for path in _PRODUCTION_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for retired_name in _RETIRED_STORE_NAMES:
            if retired_name in text:
                offenders.append(f"{path.relative_to(_REPO_ROOT)}: {retired_name}")

    assert offenders == [], (
        "commands, engine frames, and terminal facts have one durable store; "
        "a retired projection store reappeared:\n" + "\n".join(offenders)
    )


def test_only_the_unified_session_event_repository_exists() -> None:
    repository_dir = _PRODUCTION_ROOT / "persistence" / "repository"
    assert (repository_dir / "session_event_repository.py").is_file()
    assert not (repository_dir / "session_journal_repository.py").exists()
    assert not (repository_dir / "ai_sdk_frame_repository.py").exists()
    assert not (repository_dir / "ui_message_repository.py").exists()


def test_the_retired_second_translation_pipeline_is_absent() -> None:
    orchestrator_dir = _PRODUCTION_ROOT / "core/service/orchestrator"
    for retired_module in (
        "canonical_turn_projector.py",
        "data_stream_translator.py",
        "transcript_projector.py",
    ):
        assert not (orchestrator_dir / retired_module).exists()


def test_the_retired_frame_adapter_scaffold_is_absent() -> None:
    kernel_dir = _PRODUCTION_ROOT / "core/service/orchestrator/session_kernel"
    for retired_module in (
        "adapters/__init__.py",
        "adapters/models.py",
        "streaming/__init__.py",
        "streaming/models.py",
    ):
        assert not (kernel_dir / retired_module).exists()

    offenders = [
        str(path.relative_to(_REPO_ROOT))
        for path in _PRODUCTION_ROOT.rglob("*.py")
        if "adapter_version" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], (
        "frame provenance belongs in source_kind; the retired adapter-version "
        "fence reappeared:\n" + "\n".join(offenders)
    )


def test_the_kernel_exposes_no_mutable_ui_message_record() -> None:
    models = (
        _PRODUCTION_ROOT
        / "core/service/orchestrator/session_kernel/projections/models.py"
    ).read_text(encoding="utf-8")

    assert "UiMessageRecord" not in models
