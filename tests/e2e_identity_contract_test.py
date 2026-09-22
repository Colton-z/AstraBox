from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
OIDC_BROWSER_SOURCES = (
    REPO / "tests/e2e-ui/fixtures/oidcLogin.ts",
    REPO / "tests/e2e-ui/specs/oidc-login.parallel.spec.ts",
    REPO
    / "tests/e2e-ui/specs/investment-research-production-journey.exclusive.spec.ts",
)


def test_browser_oidc_uses_the_platform_admin_role_wire_value() -> None:
    sources = "\n".join(path.read_text(encoding="utf-8") for path in OIDC_BROWSER_SOURCES)

    assert "platform_admin" not in sources
    assert "'admin'" in sources
