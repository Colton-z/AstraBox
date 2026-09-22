from pathlib import Path

from scripts.check_i18n import validate_source_usage, validate_wording


def test_validate_wording_checks_translation_values_not_internal_keys() -> None:
    catalogues = {
        "en": {
            "internal.seam.key": "Choose the plugin interface to use.",
            "bad": "Review the wiring contract before continuing.",
        },
        "zh": {
            "internal.seam.key": "选择要使用的插件接口。",
            "bad": "查看凭证如何到达工具的边界。",
        },
    }

    assert validate_wording("manage.json", catalogues) == [
        "manage.json: en bad uses discouraged wording 'wiring'",
        "manage.json: en bad uses discouraged wording 'contract'",
        "manage.json: zh bad uses discouraged wording '凭证如何到达工具'",
        "manage.json: zh bad uses discouraged wording '边界'",
    ]


def test_validate_source_usage_reports_missing_and_unused_keys(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    source_root.mkdir()
    (source_root / "Page.tsx").write_text(
        """
        t('common:save');
        const stateKey = 'manage:status.ready';
        t(stateKey);
        t(`manage:dynamic.${value}`);
        t('manage:missing');
        // 'common:comment_only' is documentation, not a production use.
        """,
        encoding="utf-8",
    )
    (source_root / "Page.test.tsx").write_text(
        "t('common:test_only')",
        encoding="utf-8",
    )

    catalogues = {
        "common": {
            "comment_only": "Comment only",
            "save": "Save",
            "test_only": "Test only",
        },
        "manage": {
            "dynamic.first": "First",
            "dynamic.second": "Second",
            "status.ready": "Ready",
        },
    }

    assert validate_source_usage(catalogues, source_root) == [
        "source uses missing translation key manage:missing",
        "unused translation key common:comment_only",
        "unused translation key common:test_only",
    ]
