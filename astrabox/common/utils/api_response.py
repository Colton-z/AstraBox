from __future__ import annotations

from typing import Any

from astrabox.common.utils.errors import APIError, make_api_error


def success_response(data: Any = None, code: str = "OK") -> dict[str, Any]:
    return {"code": code, "message": "success", "data": data}


def error_response(
    code: str | APIError,
    message: str | None = None,
    data: Any = None,
) -> dict[str, Any]:
    if isinstance(code, APIError):
        return code.to_response_payload()
    return make_api_error(
        code=str(code or "UNKNOWN_ERROR"),
        message=str(message or ""),
        data=data,
    ).to_response_payload()
