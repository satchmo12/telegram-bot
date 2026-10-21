"""Per-group schedulers for proactively posting online-baby and teacher rosters."""

from __future__ import annotations

import time

from telegram.error import BadRequest, Forbidden
from telegram.ext import ContextTypes

from utils import get_group_whitelist, get_runtime_bot_name


ONLINE_BABY_MIN_INTERVAL = 1
ONLINE_BABY_MAX_INTERVAL = 1440
ONLINE_BABY_DEFAULT_INTERVAL = 30

TEACHER_MIN_INTERVAL = 1
TEACHER_MAX_INTERVAL = 1440
TEACHER_DEFAULT_INTERVAL = 30

# This is intentionally in-memory: the stable minute offset avoids a burst of
# every enabled group after a restart, while a normal schedule resumes quickly.
_last_sent_at: dict[str, float] = {}


def _interval_minutes(
    value,
    *,
    default: int = ONLINE_BABY_DEFAULT_INTERVAL,
    minimum: int = ONLINE_BABY_MIN_INTERVAL,
    maximum: int = ONLINE_BABY_MAX_INTERVAL,
) -> int:
    try:
        interval = int(value)
    except (TypeError, ValueError):
        interval = default
    return max(minimum, min(maximum, interval))


def _due_chat_ids(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    enabled_key: str,
    interval_key: str,
    schedule_name: str,
    default_interval: int,
    minimum_interval: int,
    maximum_interval: int,
) -> list[tuple[int, str]]:
    """Return configured groups whose independently staggered schedule is due."""
    groups = get_group_whitelist(context)
    if not isinstance(groups, dict):
        return []

    now = time.time()
    current_minute = int(now // 60)
    due_chat_ids: list[tuple[int, str]] = []
    for chat_id, cfg in groups.items():
        if not isinstance(cfg, dict):
            continue
        if not bool(cfg.get(enabled_key, False)):
            continue
        if not bool(cfg.get("enabled", True)) or not bool(cfg.get("bot_enabled", True)):
            continue
        if not bool(cfg.get("bot_in_group", True)) or bool(cfg.get("bot_muted", False)):
            continue
        try:
            numeric_chat_id = int(chat_id)
        except (TypeError, ValueError):
            continue

        interval = _interval_minutes(
            cfg.get(interval_key),
            default=default_interval,
            minimum=minimum_interval,
            maximum=maximum_interval,
        )
        key = f"{schedule_name}:{_runtime_chat_key(context, str(chat_id))}"
        # Spread groups across the configured interval instead of issuing every
        # scheduled post in the same minute.
        if current_minute % interval != abs(hash(key)) % interval:
            continue
        if now - _last_sent_at.get(key, 0.0) < interval * 60:
            continue
        due_chat_ids.append((numeric_chat_id, key))
    return due_chat_ids


def _runtime_chat_key(context: ContextTypes.DEFAULT_TYPE, chat_id: str) -> str:
    bot_name = get_runtime_bot_name() or str(getattr(context.bot, "username", "") or "bot")
    return f"{bot_name}:{chat_id}"


async def schedule_online_babies(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the existing online-baby display to groups whose interval is due."""
    due_chat_ids = _due_chat_ids(
        context,
        enabled_key="online_baby_auto_enabled",
        interval_key="online_baby_interval_min",
        schedule_name="online_baby",
        default_interval=ONLINE_BABY_DEFAULT_INTERVAL,
        minimum_interval=ONLINE_BABY_MIN_INTERVAL,
        maximum_interval=ONLINE_BABY_MAX_INTERVAL,
    )
    if not due_chat_ids:
        return

    # Import lazily to keep group configuration registration independent from
    # the channel feature's large publishing module during application startup.
    from channel.publish_setting import send_online_babies_to_group

    now = time.time()
    for chat_id, key in due_chat_ids:
        try:
            sent = await send_online_babies_to_group(context, chat_id)
            if sent:
                _last_sent_at[key] = now
        except (Forbidden, BadRequest) as exc:
            print(f"⚠️ 在线宝宝定时调用失败 chat={chat_id}: {exc}")
        except Exception as exc:
            print(f"⚠️ 在线宝宝定时调用异常 chat={chat_id}: {exc}")


async def schedule_teachers(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the existing teacher roster to groups whose interval is due."""
    due_chat_ids = _due_chat_ids(
        context,
        enabled_key="teacher_auto_enabled",
        interval_key="teacher_interval_min",
        schedule_name="teacher",
        default_interval=TEACHER_DEFAULT_INTERVAL,
        minimum_interval=TEACHER_MIN_INTERVAL,
        maximum_interval=TEACHER_MAX_INTERVAL,
    )
    if not due_chat_ids:
        return

    from channel.publish_setting import send_teachers_to_group

    now = time.time()
    for chat_id, key in due_chat_ids:
        try:
            sent = await send_teachers_to_group(context, chat_id)
            if sent:
                _last_sent_at[key] = now
        except (Forbidden, BadRequest) as exc:
            print(f"⚠️ 老师定时调用失败 chat={chat_id}: {exc}")
        except Exception as exc:
            print(f"⚠️ 老师定时调用异常 chat={chat_id}: {exc}")
