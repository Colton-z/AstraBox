"""The gate that keeps every raised error code registered.

A checker with a blind collector passes everything and says so confidently, so
these tests feed it source it has never seen: each construction that reaches the
error envelope, and each way the baseline can go wrong. The tree's own state is
checked too, because a green gate here and a red one in `make test` would mean
the two disagree about what the tree contains.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_error_registry", REPO_ROOT / "scripts" / "check_error_registry.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


# --- the collector ----------------------------------------------------------


_ALL_FOUR_CONSTRUCTIONS = '''
from astrabox.common.utils.errors import APIError, make_api_error
from astrabox.common.utils.api_response import error_response

_MODULE_LEVEL = "VIA_MODULE_CONSTANT"


def raise_them(result):
    raise APIError(code="VIA_KEYWORD", message="m", status_code=400)


def positional():
    raise APIError("VIA_POSITIONAL", "m", 400)


def via_factory():
    raise make_api_error(code=_MODULE_LEVEL, message="m")


def returned():
    return error_response("VIA_ERROR_RESPONSE", "m")


def through_a_helper(result):
    _ensure_command_success(result, "VIA_HELPER_POSITIONAL", "m")


def through_a_kwarg(result):
    _clone(result, error_code="VIA_ERROR_CODE_KEYWORD")


def forwarded(code):
    raise APIError(code=code, message="m", status_code=400)
'''


def test_the_collector_sees_every_construction_that_reaches_the_envelope(
    tmp_path: Path,
) -> None:
    (tmp_path / "sample.py").write_text(_ALL_FOUR_CONSTRUCTIONS, encoding="utf-8")

    raised, forwarded = checker.collect(tmp_path)

    assert set(raised) == {
        "VIA_KEYWORD",
        "VIA_POSITIONAL",
        "VIA_MODULE_CONSTANT",
        "VIA_ERROR_RESPONSE",
        "VIA_HELPER_POSITIONAL",
        "VIA_ERROR_CODE_KEYWORD",
    }
    # The site whose code is a parameter is recorded as a site, not invented as
    # a code: a runtime value is not a literal the registry can hold.
    assert len(forwarded) == 1


def test_the_collector_resolves_a_constant_imported_one_hop(tmp_path: Path) -> None:
    """`turn_preparation.py` names its codes this way, so the hop is not optional."""
    (tmp_path / "codes.py").write_text('SOMEWHERE_ELSE = "IMPORTED_CODE"\n', encoding="utf-8")
    (tmp_path / "user.py").write_text(
        "from .codes import SOMEWHERE_ELSE\n"
        "from astrabox.common.utils.errors import APIError\n"
        "def go():\n"
        "    raise APIError(code=SOMEWHERE_ELSE, message='m', status_code=500)\n",
        encoding="utf-8",
    )

    raised, forwarded = checker.collect(tmp_path)

    assert "IMPORTED_CODE" in raised
    assert forwarded == []


def test_the_collector_does_not_invent_codes_from_upper_case_strings(
    tmp_path: Path,
) -> None:
    """The reason the collector is exact rather than a pattern.

    Treating any upper-case literal as a candidate finds roughly twice the true
    number across this package — collection names, states and transports all
    have the shape of a code.
    """
    (tmp_path / "sample.py").write_text(
        'COLLECTION_NAME = "SESSIONS"\n'
        "def go(db):\n"
        "    db.get_collection(COLLECTION_NAME)\n"
        "    return dict(state='TERMINATED', transport='STREAMABLE_HTTP')\n",
        encoding="utf-8",
    )

    raised, _forwarded = checker.collect(tmp_path)

    assert raised == {}


# --- the four rules ---------------------------------------------------------


def _reasons(
    raised: dict[str, set[str]], registered: set[str], baseline: set[str]
) -> str:
    return " | ".join(reason for reason, _codes in checker.evaluate(raised, registered, baseline))


def test_a_new_unregistered_code_fails() -> None:
    assert "no registry row" in _reasons({"BRAND_NEW": {"a.py:1"}}, set(), set())


def test_a_code_in_the_baseline_is_tolerated() -> None:
    assert _reasons({"OLD": {"a.py:1"}}, set(), {"OLD"}) == ""


def test_registering_a_code_requires_removing_its_baseline_line() -> None:
    """Otherwise the baseline never shrinks and stops describing anything."""
    assert "still listed as arrears" in _reasons({"OLD": {"a.py:1"}}, {"OLD"}, {"OLD"})


def test_a_baseline_line_for_a_code_nobody_raises_fails() -> None:
    assert "raised nowhere" in _reasons({}, set(), {"DELETED"})


def test_a_row_with_no_raise_site_fails_unless_it_is_declared_non_literal() -> None:
    """A non-literal wire code is correct; a row for a code nothing uses is not."""
    assert "no raise site" in _reasons({}, {"NOBODY_RAISES_ME"}, set())
    non_literal = next(iter(checker.NON_LITERAL_CODES))
    assert _reasons({}, {non_literal}, set()) == ""


# --- the tree itself --------------------------------------------------------


def test_the_tree_passes_its_own_gate() -> None:
    raised, _forwarded = checker.collect()
    from astrabox.common.utils.errors import _ERROR_SPECS

    failures = checker.evaluate(raised, set(_ERROR_SPECS), checker.read_baseline())

    assert failures == [], failures


def test_the_baseline_holds_only_codes_that_are_still_unregistered() -> None:
    """Reads the file, not the checker's view of it, so a parse bug cannot hide."""
    from astrabox.common.utils.errors import _ERROR_SPECS

    lines = checker.BASELINE_PATH.read_text(encoding="utf-8").splitlines()
    codes = [line for line in lines if line and not line.startswith("#")]

    assert codes == sorted(codes), "baseline is not sorted"
    assert len(codes) == len(set(codes)), "baseline repeats a code"
    assert [code for code in codes if code in _ERROR_SPECS] == []


# --- the vocabulary the rows draw from --------------------------------------


def test_a_row_cannot_invent_an_owner() -> None:
    from astrabox.common.utils.errors import ERROR_OWNERS, register_error

    with pytest.raises(ValueError) as caught:
        register_error("MADE_UP_OWNER_CODE", owner="whoever")

    assert "whoever" in str(caught.value)
    assert "unknown" not in ERROR_OWNERS, "the fallback's admission is not a row's answer"


def test_an_unregistered_code_owns_up_instead_of_blaming_the_platform() -> None:
    """`platform` was a claim about a code that can arrive from outside."""
    from astrabox.common.utils.errors import APIError, error_spec

    spec = error_spec("A_CODE_NOBODY_REGISTERED")
    envelope = APIError(
        code="A_CODE_NOBODY_REGISTERED", message="m", status_code=502
    ).to_error_envelope()

    assert spec.owner == "unknown"
    assert envelope["owner"] == "unknown"
    assert envelope["category"] == "unregistered"


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (
            "SANDBOX_CORRELATED_CREATE_UNSUPPORTED",
            (501, "runtime.startup", False, "platform"),
        ),
        ("SANDBOX_ASSIGNMENT_INVALID", (500, "runtime.startup", False, "platform")),
        ("SANDBOX_ASSIGNMENT_CONFLICT", (409, "runtime.startup", False, "runtime")),
        ("SANDBOX_ASSIGNMENT_AMBIGUOUS", (409, "runtime.startup", False, "runtime")),
        (
            "SANDBOX_CLIENT_POOL_UNSUPPORTED",
            (501, "runtime.preparation", False, "platform"),
        ),
        (
            "SANDBOX_CLIENT_POOL_UNAVAILABLE",
            (503, "runtime.preparation", True, "runtime"),
        ),
        (
            "SANDBOX_CLEANUP_UNCONFIRMED",
            (502, "runtime.sandbox", True, "runtime"),
        ),
    ],
)
def test_startup_assignment_errors_route_to_the_responsible_owner(
    code: str,
    expected: tuple[int, str, bool, str],
) -> None:
    from astrabox.common.utils.errors import error_spec

    spec = error_spec(code)
    assert (spec.status_code, spec.category, spec.retryable, spec.owner) == expected
