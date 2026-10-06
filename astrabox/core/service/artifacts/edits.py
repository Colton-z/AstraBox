"""Apply an ordered set of replacements without publishing partial edits."""

from __future__ import annotations

from typing import Any

from astrabox.common.utils.errors import APIError


def apply_edits(content: str, edits: Any) -> str:
    if not isinstance(edits, list) or not edits:
        raise APIError(
            code="ARTIFACT_EDIT_NOT_APPLICABLE",
            message="edits must be a non-empty array",
            status_code=400,
        )
    for edit in edits:
        if (
            not isinstance(edit, dict)
            or set(edit) - {"old_string", "new_string", "replace_all"}
            or not isinstance(edit.get("old_string"), str)
            or not edit["old_string"]
            or not isinstance(edit.get("new_string"), str)
            or ("replace_all" in edit and type(edit["replace_all"]) is not bool)
        ):
            raise APIError(
                code="ARTIFACT_EDIT_NOT_APPLICABLE", message="invalid replacement", status_code=400
            )
        first = content.find(edit["old_string"])
        ambiguous = first >= 0 and content.find(edit["old_string"], first + 1) >= 0
        if first < 0 or (ambiguous and not edit.get("replace_all", False)):
            raise APIError(
                code="ARTIFACT_EDIT_NOT_APPLICABLE",
                message="replacement is absent or ambiguous",
                status_code=400,
            )
        content = content.replace(
            edit["old_string"], edit["new_string"], -1 if edit.get("replace_all") else 1
        )
    return content
