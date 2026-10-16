"""How people find Agents: categories, tags and favorites.

A platform administrator defines the categories; whoever may manage an Agent
chooses its categories from them and writes its tags. Categories are the
navigation (the home page's tabs), so they are records with an order that a
rename touches once. Tags are the author's own words for search and filtering,
so they stay free text, normalized so that one word is one tag. Favorites are
each user's own, one record per user and Agent.
"""

from __future__ import annotations

import re
from typing import Any

from astrabox.common.utils.errors import APIError
from astrabox.core.service.orchestrator.schema_validation import invalid_request
from astrabox.persistence.repository.agent_category_repository import (
    AgentCategoryRepository,
    CategoryNameTaken,
)
from astrabox.persistence.repository.agent_favorite_repository import AgentFavoriteRepository

#: The most tags one Agent carries.
MAX_AGENT_TAGS = 5
MAX_TAG_LENGTH = 32
MAX_CATEGORY_NAME_LENGTH = 40

_categories: AgentCategoryRepository | None = None
_favorites: AgentFavoriteRepository | None = None


def category_repository() -> AgentCategoryRepository:
    global _categories
    if _categories is None:
        _categories = AgentCategoryRepository()
    return _categories


def favorite_repository() -> AgentFavoriteRepository:
    global _favorites
    if _favorites is None:
        _favorites = AgentFavoriteRepository()
    return _favorites


def normalize_tag(raw: Any) -> str:
    """One tag as stored: lower case, no leading ``#``, words joined by ``-``."""
    text = str(raw or "").strip().lstrip("#").strip().casefold()
    return re.sub(r"\s+", "-", text)


def normalize_tags(raw: Any) -> list[str]:
    """An Agent's tags as stored, or the reason they cannot be."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise invalid_request("display_meta.tags must be a list of strings")
    tags: list[str] = []
    for item in raw:
        tag = normalize_tag(item)
        if not tag or tag in tags:
            continue
        if len(tag) > MAX_TAG_LENGTH:
            raise invalid_request(
                f"tag {tag!r} is longer than {MAX_TAG_LENGTH} characters"
            )
        tags.append(tag)
    if len(tags) > MAX_AGENT_TAGS:
        raise invalid_request(f"an Agent carries at most {MAX_AGENT_TAGS} tags, got {len(tags)}")
    return tags


def _category_name(raw: Any) -> tuple[str, str]:
    """The name as shown and the key two names that read the same share."""
    name = re.sub(r"\s+", " ", str(raw or "")).strip()
    if not name:
        raise invalid_request("a category needs a name")
    if len(name) > MAX_CATEGORY_NAME_LENGTH:
        raise invalid_request(
            f"a category name is at most {MAX_CATEGORY_NAME_LENGTH} characters"
        )
    return name, name.casefold()


def _public(category: dict[str, Any]) -> dict[str, Any]:
    return {
        "category_id": category["category_id"],
        "name": category["name"],
        "position": int(category.get("position") or 0),
    }


def _not_found(category_id: str) -> APIError:
    return APIError(
        code="AGENT_CATEGORY_NOT_FOUND",
        message=f"Agent category {category_id!r} does not exist",
        status_code=404,
    )


async def list_categories() -> list[dict[str, Any]]:
    return [_public(row) for row in await category_repository().list_all()]


async def create_category(name: Any) -> dict[str, Any]:
    shown, key = _category_name(name)
    try:
        return _public(await category_repository().create(name=shown, name_key=key))
    except CategoryNameTaken:
        raise _name_taken(shown) from None


def _name_taken(name: str) -> APIError:
    return APIError(
        code="AGENT_CATEGORY_NAME_TAKEN",
        message=f"an Agent category named {name!r} already exists",
        status_code=409,
    )


async def rename_category(category_id: str, name: Any) -> dict[str, Any]:
    shown, key = _category_name(name)
    try:
        renamed = await category_repository().rename(category_id, name=shown, name_key=key)
    except CategoryNameTaken:
        raise _name_taken(shown) from None
    if renamed is None:
        raise _not_found(category_id)
    return _public(renamed)


async def reorder_categories(category_ids: Any) -> list[dict[str, Any]]:
    """Put every category in the given order; the list must name each once."""
    if not isinstance(category_ids, list) or not await category_repository().reorder(
        [str(category_id) for category_id in category_ids]
    ):
        raise invalid_request("category_ids must list every Agent category exactly once")
    return await list_categories()


async def delete_category(category_id: str) -> int:
    """Delete a category, taking it off every Agent; how many Agents it was on."""
    unbound = await category_repository().delete(category_id)
    if unbound is None:
        raise _not_found(category_id)
    return unbound


async def category_agent_counts() -> dict[str, int]:
    """How many Agents each category holds, for the administrator."""
    return await category_repository().agent_counts()


async def checked_category_ids(raw: Any) -> list[str]:
    """An Agent's categories as stored: existing ids, each once, in the given order."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise invalid_request("categories must be a list of category ids")
    wanted: list[str] = []
    for item in raw:
        category_id = str(item or "").strip()
        if category_id and category_id not in wanted:
            wanted.append(category_id)
    if not wanted:
        return []
    known = {row["category_id"] for row in await category_repository().list_all()}
    unknown = [category_id for category_id in wanted if category_id not in known]
    if unknown:
        raise invalid_request(f"unknown Agent categories: {', '.join(unknown)}")
    return wanted


async def normalize_catalog_fields(editable: dict[str, Any]) -> dict[str, Any]:
    """Store an authoring request's categories and tags in their one form."""
    if "categories" in editable:
        editable["categories"] = await checked_category_ids(editable["categories"])
    display_meta = editable.get("display_meta")
    if isinstance(display_meta, dict) and "tags" in display_meta:
        editable["display_meta"] = {
            **display_meta,
            "tags": normalize_tags(display_meta.get("tags")),
        }
    return editable
