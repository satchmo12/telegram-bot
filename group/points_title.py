# -*- coding: utf-8 -*-
"""Native Telegram member tags driven by per-group point ranges."""
from __future__ import annotations

import asyncio
import re
from typing import Any, Optional

from telegram.error import TelegramError

from info.economy import register_points_change_listener
from info.storage import load_group_info
from utils import get_group_whitelist, get_runtime_bot_name


RULES_KEY = "points_custom_title_levels"
ENABLED_KEY = "points_custom_titles_enabled"
MAX_TITLE_LENGTH = 16
MAX_LEVELS = 20
_REGULAR_STATUSES = {"member", "restricted"}
_EMOJI_RE = re.compile(r"[\U0001F000-\U0010FFFF\u2600-\u27BF]")


def _as_int(value, default: Optional[int] = None) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_title_levels(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Return validated, ordered point-title rules without mutating config."""
    levels = config.get(RULES_KEY, []) if isinstance(config, dict) else []
    if not isinstance(levels, list):
        return []
    result = []
    seen_levels = set()
    for index, item in enumerate(levels, start=1):
        if not isinstance(item, dict):
            continue
        level = _as_int(item.get("level"), index)
        minimum = _as_int(item.get("min_points"))
        maximum = _as_int(item.get("max_points"))
        title = str(item.get("title") or "").strip()
        if (
            level is None
            or level < 1
            or level in seen_levels
            or minimum is None
            or maximum is None
            or minimum > maximum
            or not title
        ):
            continue
        seen_levels.add(level)
        result.append({
            "level": level,
            "min_points": minimum,
            "max_points": maximum,
            "title": title[:MAX_TITLE_LENGTH],
        })
    return sorted(result, key=lambda item: (item["level"], item["min_points"]))[:MAX_LEVELS]


def title_level_for_points(config: dict[str, Any], points: int) -> Optional[dict[str, Any]]:
    score = _as_int(points, 0) or 0
    for level in normalize_title_levels(config):
        if level["min_points"] <= score <= level["max_points"]:
            return level
    return None


def managed_title_names(config: dict[str, Any]) -> set[str]:
    return {item["title"] for item in normalize_title_levels(config)}


def title_ranges_overlap(levels: list[dict[str, Any]], minimum: int, maximum: int, *, exclude_level=None) -> bool:
    for item in levels:
        if exclude_level is not None and int(item["level"]) == int(exclude_level):
            continue
        if max(minimum, item["min_points"]) <= min(maximum, item["max_points"]):
            return True
    return False


def title_text_is_valid(title: str) -> bool:
    return bool(title) and len(title) <= MAX_TITLE_LENGTH and not _EMOJI_RE.search(title)


def title_feature_enabled(config: dict[str, Any]) -> bool:
    return bool((config or {}).get(ENABLED_KEY, False)) and bool(normalize_title_levels(config or {}))


def _member_tag(member: dict) -> str:
    return str((member or {}).get("tag") or "").strip()


async def sync_member_points_title(bot, chat_id, user_id, points: int, config: dict[str, Any]) -> str:
    """Update one native tag, never overwriting a non-points/manual tag.

    Returns a small outcome label intended for logs or optional batch summaries.
    """
    if not title_feature_enabled(config):
        return "disabled"
    try:
        member = await bot._post(
            "getChatMember", data={"chat_id": int(chat_id), "user_id": int(user_id)}
        )
    except TelegramError as exc:
        print(f"读取积分头衔成员失败 chat={chat_id} user={user_id}: {exc}")
        return "member_error"
    if not isinstance(member, dict) or str(member.get("status") or "") not in _REGULAR_STATUSES:
        return "not_regular_member"

    existing_tag = _member_tag(member)
    managed_titles = managed_title_names(config)
    desired_rule = title_level_for_points(config, points)
    desired_tag = str(desired_rule["title"]) if desired_rule else ""

    # A human/operator or another system owns tags unknown to this feature.
    # Do not overwrite or clear them.
    if existing_tag and existing_tag not in managed_titles:
        return "manual_tag_preserved"
    if existing_tag == desired_tag:
        return "unchanged"

    try:
        await bot._post(
            "setChatMemberTag",
            data={"chat_id": int(chat_id), "user_id": int(user_id), "tag": desired_tag},
        )
    except TelegramError as exc:
        print(f"同步积分头衔失败 chat={chat_id} user={user_id}: {exc}")
        return "set_error"
    return "cleared" if not desired_tag else "updated"


async def resync_known_members(bot, chat_id, config: dict[str, Any], *, limit: int = 500) -> dict[str, int]:
    """Apply a newly saved rule set to economy users already known to the bot."""
    result = {"updated": 0, "cleared": 0, "preserved": 0, "skipped": 0, "failed": 0}
    users = load_group_info(str(chat_id)).get("users", {})
    if not isinstance(users, dict):
        return result
    for raw_user_id, user in list(users.items())[:limit]:
        user_id = _as_int(raw_user_id)
        if user_id is None or not isinstance(user, dict):
            continue
        outcome = await sync_member_points_title(
            bot, chat_id, user_id, _as_int(user.get("points"), 0) or 0, config
        )
        if outcome == "updated":
            result["updated"] += 1
        elif outcome == "cleared":
            result["cleared"] += 1
        elif outcome == "manual_tag_preserved":
            result["preserved"] += 1
        elif outcome in {"member_error", "set_error"}:
            result["failed"] += 1
        else:
            result["skipped"] += 1
        # Be conservative when applying a fresh configuration to a large group.
        await asyncio.sleep(0.05)
    return result


def register_points_title_handlers(app) -> None:
    """Subscribe this bot to synchronous point changes from all economy flows."""
    app_name = str(app.bot_data.get("name", "")).strip()

    async def on_points_change(chat_id: str, user_id: str, balance: int):
        # Multiple bots can run in one process. The ContextVar is bound by the
        # update/job currently changing points, so only that bot should act.
        runtime_name = get_runtime_bot_name()
        if runtime_name and app_name and runtime_name != app_name:
            return
        config = get_group_whitelist().get(str(chat_id), {})
        if not isinstance(config, dict):
            return
        await sync_member_points_title(app.bot, chat_id, user_id, balance, config)

    register_points_change_listener(on_points_change)
