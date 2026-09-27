"""A title is written in the writing systems the user wrote in.

With thinking off, a title model given an English message that is mostly
formulas answered in Chinese. The request now names the writing systems of the
user's own message, and a title in any other non-Latin writing system is a
recorded failure rather than a sidebar title the user cannot read.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from astrabox.core.service.orchestrator.session_title_service import (
    ConversationTitleModel,
    SessionTitleGenerationError,
    TitleModelRequestConfig,
    _writing_systems,
)

_ENGLISH_FORMULAS = (
    "Before you answer, check step by step: for every integer n > 1, is n^4 + 4 "
    "composite? Then tell me which n in 2..12 make n^2 + n + 41 prime."
)
_CHINESE = "帮我比较一下这两款手机的续航和拍照，哪个更适合出差？"
_JAPANESE = "来月の東京出張のために、社内の会議室を予約する手順をまとめてください。"
_SPANISH = "¿Me ayudas a preparar una lista de la compra para una cena vegetariana?"
_ASSISTANT = "Here is the answer."


def _model(title: str, requests: list[str]) -> ConversationTitleModel:
    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body["messages"][1]["content"])
        content = json.dumps({"should_generate": True, "title": title}, ensure_ascii=False)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return ConversationTitleModel(
        settings=SimpleNamespace(title_model_request_timeout_seconds=60),
        http_client_factory=lambda **kwargs: httpx.AsyncClient(
            transport=httpx.MockTransport(respond), **kwargs
        ),
    )


_CONFIG = TitleModelRequestConfig(
    base_url="https://gateway.test", api_key="test-only", model_name="label-model"
)


async def _decide(user_text: str, title: str, requests: list[str]) -> str:
    decision = await _model(title, requests).decide_title_generation(
        user_text=user_text, assistant_text=_ASSISTANT, turn_index=1, request_config=_CONFIG
    )
    return decision.title


async def _generate(user_text: str, title: str, requests: list[str]) -> str:
    return await _model(title, requests).generate_title(
        user_text=user_text, assistant_text=_ASSISTANT, request_config=_CONFIG
    )


@pytest.mark.parametrize("operation", [_decide, _generate])
async def test_an_english_formula_message_never_gets_a_chinese_title(operation) -> None:
    requests: list[str] = []

    with pytest.raises(SessionTitleGenerationError, match=r"Chinese characters \(Han\)"):
        await operation(_ENGLISH_FORMULAS, "n^4+4 与 n^2+n+41 素数", requests)

    assert "using Latin letters only" in requests[0]


@pytest.mark.parametrize(
    ("user_text", "title"),
    [
        (_ENGLISH_FORMULAS, "Compositeness of n^4 + 4"),
        (_CHINESE, "iPhone 与 Pixel 续航对比"),
        (_JAPANESE, "社内会議室の予約手順"),
        (_JAPANESE, "社内会議室予約手順"),
        (_SPANISH, "Lista de la compra vegetariana"),
    ],
)
@pytest.mark.parametrize("operation", [_decide, _generate])
async def test_a_title_in_the_users_writing_systems_is_kept(operation, user_text, title) -> None:
    requests: list[str] = []

    assert await operation(user_text, title, requests) == title


async def test_a_japanese_title_for_a_chinese_message_is_refused() -> None:
    requests: list[str] = []

    with pytest.raises(SessionTitleGenerationError, match="Japanese kana"):
        await _decide(_CHINESE, "手機の比較", requests)

    assert "Chinese characters (Han)" in requests[0]


async def test_a_message_without_letters_constrains_nothing() -> None:
    requests: list[str] = []

    assert await _decide("2 + 2 = ?", "算术题", requests) == "算术题"
    assert "writing systems" not in requests[0] and "Latin" not in requests[0]


def test_writing_systems_come_from_letters_only() -> None:
    assert _writing_systems("n^4 + 4 = (n^2 + 2n + 2) 🙂 42") == {"Latin"}
    assert _writing_systems("𝑛 ＡＢ") == {"Latin"}
    assert _writing_systems("人々") == {"Chinese characters (Han)"}
    assert _writing_systems("ｶﾀｶﾅ ーの") == {"Japanese kana"}
    assert _writing_systems("한국어") == {"Korean Hangul"}
    assert _writing_systems("Привет") == {"Cyrillic"}
    assert _writing_systems("123 + 456") == frozenset()
