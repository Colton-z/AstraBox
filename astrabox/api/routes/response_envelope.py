"""The typed form of the success envelope every ``/api/v1`` handler returns.

:func:`astrabox.common.utils.api_response.success_response` is the single place
that builds it, and the payload a caller wants always sits at ``data``. Naming
that shape in ``response_model`` is what puts it into the OpenAPI document, and
from there into a generated client.

``response_model`` rewrites the response rather than merely describing it, so a
declaration carries three obligations:

* Pass ``response_model_exclude_unset=True`` on every route that declares one.
  Without it, a declared field the handler did not emit is written to the wire
  as ``null``, adding a key the caller never received before.
* Set ``extra="allow"`` on the payload model. A field the model does not list
  then still reaches the client instead of being deleted in transit. This is
  the opposite of the request models in this package, which close with
  ``extra="forbid"`` so an unknown input fails loud.
* Declare a field only where the write path fixes its type. A declared type is
  an assertion pydantic enforces: it coerces ``"3"`` to ``3`` on the way out,
  and answers 500 for a value it cannot coerce or a required field the handler
  omitted.

A handler that returns a ``Response`` object bypasses all of this — FastAPI
sends the object as-is, so the model documents the route without validating it.
Return the envelope dict itself where the declaration is meant to hold.
"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel

PayloadT = TypeVar("PayloadT")


class ApiEnvelope(BaseModel, Generic[PayloadT]):
    """``{"code", "message", "data"}`` with the operation's payload at ``data``."""

    code: str
    message: str
    data: PayloadT


__all__ = ["ApiEnvelope", "PayloadT"]
