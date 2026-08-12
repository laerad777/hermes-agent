"""Static, registry-independent tool surface for typed Jerome reviewers."""

from __future__ import annotations

import copy
import json

from types import MappingProxyType
from typing import Any, Callable

from tools.file_tools import (
    READ_FILE_SCHEMA,
    SEARCH_FILES_SCHEMA,
    _handle_read_file,
    _handle_search_files,
)
from tools.kanban_tools import (
    KANBAN_ATTACHMENTS_SCHEMA,
    KANBAN_BLOCK_SCHEMA,
    KANBAN_COMPLETE_SCHEMA,
    KANBAN_HEARTBEAT_SCHEMA,
    KANBAN_SHOW_SCHEMA,
    _handle_attachments,
    _handle_block,
    _handle_complete,
    _handle_heartbeat,
    _handle_show,
)
from tools.review_exec_tool import REVIEW_EXEC_SCHEMA, _handle_review_exec
from tools.skills_tool import (
    SKILLS_LIST_SCHEMA,
    SKILL_VIEW_SCHEMA,
    _skill_view_with_bump,
    skills_list,
)
from tools.web_tools import WEB_EXTRACT_SCHEMA, WEB_SEARCH_SCHEMA, web_extract_tool, web_search_tool

_REVIEW_ROLES = frozenset({"planner", "architect", "critic", "verifier"})


def typed_reviewer_active() -> bool:
    """Return whether this process consumed a PID-bound reviewer grant."""
    from tools.reviewer_authority import require_activation

    try:
        activation = require_activation()
    except RuntimeError:
        return False
    return activation.get("role") in _REVIEW_ROLES


def _handle_skills_list(args: dict, **kw: Any) -> str:
    return skills_list(category=args.get("category") or "", task_id=kw.get("task_id") or "")


def _handle_web_search(args: dict, **_kw: Any) -> str:
    return web_search_tool(args.get("query", ""), limit=args.get("limit", 5))


async def _handle_web_extract(args: dict, **_kw: Any) -> str:
    urls = args.get("urls", [])
    return await web_extract_tool(
        urls[:5] if isinstance(urls, list) else [],
        format=args.get("format") or "markdown",
        char_limit=args.get("char_limit"),
    )


# Serialize at import time so later registry registrations or mutable schema
# aliases cannot alter the reviewer contract. JSON also gives callers fresh
# dictionaries, preventing one session from mutating the next session's schema.
_SCHEMA_JSON = json.dumps(
    [
        READ_FILE_SCHEMA,
        SEARCH_FILES_SCHEMA,
        WEB_SEARCH_SCHEMA,
        WEB_EXTRACT_SCHEMA,
        SKILLS_LIST_SCHEMA,
        SKILL_VIEW_SCHEMA,
        KANBAN_SHOW_SCHEMA,
        KANBAN_COMPLETE_SCHEMA,
        KANBAN_BLOCK_SCHEMA,
        KANBAN_HEARTBEAT_SCHEMA,
        KANBAN_ATTACHMENTS_SCHEMA,
        REVIEW_EXEC_SCHEMA,
    ],
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
)

_HANDLERS = MappingProxyType(
    {
        "read_file": (_handle_read_file, False),
        "search_files": (_handle_search_files, False),
        "web_search": (_handle_web_search, False),
        "web_extract": (_handle_web_extract, True),
        "skills_list": (_handle_skills_list, False),
        "skill_view": (_skill_view_with_bump, False),
        "kanban_show": (_handle_show, False),
        "kanban_complete": (_handle_complete, False),
        "kanban_block": (_handle_block, False),
        "kanban_heartbeat": (_handle_heartbeat, False),
        "kanban_attachments": (_handle_attachments, False),
        "review_exec": (_handle_review_exec, False),
    }
)


def canonical_reviewer_definitions() -> list[dict[str, Any]]:
    """Return the exact canonical schemas without consulting the registry."""
    schemas = json.loads(_SCHEMA_JSON)
    return [{"type": "function", "function": schema} for schema in schemas]


def canonical_reviewer_handler(name: str) -> tuple[Callable[..., Any], bool] | None:
    """Return a canonical handler and async flag for an allowed reviewer tool."""
    return _HANDLERS.get(name)


def canonical_reviewer_schema(name: str) -> dict[str, Any] | None:
    """Return a fresh canonical function schema for argument coercion."""
    for definition in canonical_reviewer_definitions():
        schema = definition["function"]
        if schema["name"] == name:
            return copy.deepcopy(schema)
    return None
