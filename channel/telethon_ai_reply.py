"""Per-protocol-account AI reply configuration and Telethon message handling."""

import asyncio
import logging
import random
import time
from collections import defaultdict, deque
from typing import Any

from group.ai_group_reply import ask_ollama
from utils import load_json, save_json

PROTOCOL_AI_REPLY_FILE = "data/protocol_ai_reply.json"

# Protocol accounts are opt-in.  This prevents a newly logged-in account from
# replying in every group until an owner enables a specific group in the panel.
DEFAULT_SETTINGS = {
    "enabled": False,
    # 频道内容评论与普通群聊 AI 回复分开控制，默认关闭，避免新登录
    # 的协议号在群内自动发言。
    "channel_comment_enabled": False,
    "probability_percent": 50,
    "min_interval_sec": 10,
    "max_replies_per_hour": 1000,
}
CONTEXT_LIMIT = 1
MAX_MESSAGE_LENGTH = 500
MIN_DELAY = 0.5
MAX_DELAY = 1.5

_message_history = defaultdict(lambda: deque(maxlen=CONTEXT_LIMIT))
_last_reply_time = defaultdict(float)
_reply_history = defaultdict(deque)
_processing_chats = set()


def _config_path(bot_name: str) -> str:
    name = str(bot_name or "").strip()
    return f"data/{name}/protocol_ai_reply.json" if name else PROTOCOL_AI_REPLY_FILE


def _clamp_int(value: Any, minimum: int, maximum: int, default: int) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _as_bool(value: Any, default: bool = False) -> bool:
    """Parse persisted JSON booleans without treating ``"false"`` as true."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "开启"}
    return bool(value)


def _load_config(bot_name: str) -> dict:
    data = load_json(_config_path(bot_name))
    if not isinstance(data, dict):
        data = {}
    sessions = data.get("sessions")
    if not isinstance(sessions, dict):
        data["sessions"] = {}
    return data


def _save_config(bot_name: str, data: dict) -> None:
    save_json(_config_path(bot_name), data)


def get_group_settings(bot_name: str, session_name: str, group_id: int) -> dict:
    """Return one protocol-account group's AI settings without creating config."""
    data = _load_config(bot_name)
    session = data.get("sessions", {}).get(str(session_name), {})
    groups = session.get("groups", {}) if isinstance(session, dict) else {}
    raw = groups.get(str(group_id), {}) if isinstance(groups, dict) else {}
    if not isinstance(raw, dict):
        raw = {}
    return {
        "enabled": _as_bool(raw.get("enabled"), DEFAULT_SETTINGS["enabled"]),
        "channel_comment_enabled": _as_bool(
            raw.get("channel_comment_enabled"),
            DEFAULT_SETTINGS["channel_comment_enabled"],
        ),
        "probability_percent": _clamp_int(
            raw.get("probability_percent", DEFAULT_SETTINGS["probability_percent"]),
            0,
            100,
            DEFAULT_SETTINGS["probability_percent"],
        ),
        "min_interval_sec": _clamp_int(
            raw.get("min_interval_sec", DEFAULT_SETTINGS["min_interval_sec"]),
            0,
            3600,
            DEFAULT_SETTINGS["min_interval_sec"],
        ),
        "max_replies_per_hour": _clamp_int(
            raw.get("max_replies_per_hour", DEFAULT_SETTINGS["max_replies_per_hour"]),
            1,
            10000,
            DEFAULT_SETTINGS["max_replies_per_hour"],
        ),
    }


def set_group_ai_enabled(
    bot_name: str, session_name: str, group_id: int, enabled: bool
) -> dict:
    """Persist the AI switch for a single session/group pair."""
    data = _load_config(bot_name)
    sessions = data.setdefault("sessions", {})
    session = sessions.setdefault(str(session_name), {})
    if not isinstance(session, dict):
        session = {}
        sessions[str(session_name)] = session
    groups = session.setdefault("groups", {})
    if not isinstance(groups, dict):
        groups = {}
        session["groups"] = groups

    current = get_group_settings(bot_name, session_name, group_id)
    current["enabled"] = bool(enabled)
    groups[str(group_id)] = current
    _save_config(bot_name, data)
    return current


def set_group_ai_comment_enabled(
    bot_name: str, session_name: str, group_id: int, enabled: bool
) -> dict:
    """Persist the automatic channel-comment switch for one session/group."""
    data = _load_config(bot_name)
    sessions = data.setdefault("sessions", {})
    session = sessions.setdefault(str(session_name), {})
    if not isinstance(session, dict):
        session = {}
        sessions[str(session_name)] = session
    groups = session.setdefault("groups", {})
    if not isinstance(groups, dict):
        groups = {}
        session["groups"] = groups

    current = get_group_settings(bot_name, session_name, group_id)
    current["channel_comment_enabled"] = bool(enabled)
    groups[str(group_id)] = current
    _save_config(bot_name, data)
    return current


def get_enabled_sessions(bot_name: str) -> set[str]:
    """Return sessions that have normal AI replies or channel comments enabled."""
    sessions = _load_config(bot_name).get("sessions", {})
    if not isinstance(sessions, dict):
        return set()
    enabled_sessions = set()
    for session_name, session in sessions.items():
        groups = session.get("groups", {}) if isinstance(session, dict) else {}
        if not isinstance(groups, dict):
            continue
        if any(
            isinstance(settings, dict)
            and (
                _as_bool(settings.get("enabled"), DEFAULT_SETTINGS["enabled"])
                or _as_bool(
                    settings.get("channel_comment_enabled"),
                    DEFAULT_SETTINGS["channel_comment_enabled"],
                )
            )
            for settings in groups.values()
        ):
            enabled_sessions.add(str(session_name))
    return enabled_sessions


def remove_session_config(bot_name: str, session_name: str) -> None:
    data = _load_config(bot_name)
    sessions = data.get("sessions", {})
    if isinstance(sessions, dict) and sessions.pop(str(session_name), None) is not None:
        _save_config(bot_name, data)


def _chat_key(bot_name: str, session_name: str, chat_id: int) -> tuple[str, str, int]:
    return str(bot_name or ""), str(session_name or ""), int(chat_id)


def _can_reply(key: tuple[str, str, int], settings: dict) -> bool:
    now = time.time()
    if now - _last_reply_time[key] < settings["min_interval_sec"]:
        return False
    history = _reply_history[key]
    while history and now - history[0] > 3600:
        history.popleft()
    return len(history) < settings["max_replies_per_hour"]


def _context_text(key: tuple[str, str, int]) -> str:
    return "\n".join(
        f"用户{item['sender_id']}: {item['text']}" for item in _message_history[key]
    )


def _is_channel_post_message(message: Any, sender: Any = None) -> bool:
    """Whether a group message originates from a channel post.

    Telegram represents channel posts forwarded into a group with a
    ``MessageFwdHeader``. Attribute names differ between Telethon releases,
    so accept both the old ``fwd_from`` alias and the newer ``forward`` one.
    A linked discussion group can also expose the original channel as sender.
    """
    forward = getattr(message, "fwd_from", None) or getattr(message, "forward", None)
    if forward is not None:
        if getattr(forward, "channel_post", None) is not None:
            return True
        source_peer = getattr(forward, "from_id", None)
        if getattr(source_peer, "channel_id", None) is not None:
            return True
        if source_peer and source_peer.__class__.__name__ == "PeerChannel":
            return True

    # This covers a channel post delivered to its linked discussion group.
    return bool(
        getattr(message, "post", False)
        and (
            bool(getattr(sender, "broadcast", False))
            or sender.__class__.__name__ == "Channel"
        )
    )


async def handle_protocol_group_message(bot_name: str, session_name: str, client, event) -> None:
    """Reply in a group, or comment on a channel post, when that feature is enabled."""
    if not getattr(event, "is_group", False):
        return

    chat_id = getattr(event, "chat_id", None)
    message = getattr(event, "message", None)
    if chat_id is None or message is None:
        return

    settings = get_group_settings(bot_name, session_name, int(chat_id))
    if not settings["enabled"] and not settings["channel_comment_enabled"]:
        return
    if getattr(event, "out", False):
        # Do not react to messages sent by this same protocol account; otherwise
        # it could trigger a loop with its own replies/comments.
        print(f"[协议号AI][SKIP] 自己发送的消息 session={session_name} group={chat_id}")
        return

    # Telethon resolves the sender lazily. Do it only after confirming this
    # group has at least one AI feature enabled.
    sender = None
    try:
        sender = await event.get_sender()
        if bool(getattr(sender, "bot", False)):
            print(f"[协议号AI][SKIP] 机器人消息 session={session_name} group={chat_id}")
            return
    except Exception as exc:
        # A transient entity lookup failure should not disable normal group AI.
        print(f"[协议号AI][WARN] 无法确认发送者类型 session={session_name} group={chat_id}: {exc}")

    text = (getattr(event, "raw_text", None) or getattr(message, "message", None) or "").strip()
    if not text or text.startswith("/") or len(text) > MAX_MESSAGE_LENGTH:
        return

    is_channel_post = _is_channel_post_message(message, sender)
    # A channel post gets exactly one reply: when the comment switch is on,
    # use a threaded comment instead of also emitting the normal group reply.
    should_comment = settings["channel_comment_enabled"] and is_channel_post
    should_reply = settings["enabled"] and not should_comment
    if not should_reply and not should_comment:
        return

    key = _chat_key(bot_name, session_name, int(chat_id))
    sender_id = getattr(event, "sender_id", 0) or 0
    _message_history[key].append({"sender_id": sender_id, "text": text})

    if key in _processing_chats or not _can_reply(key, settings):
        return
    # The ordinary group AI retains its configured probability. An enabled
    # channel-comment switch comments on every eligible post (subject to the
    # existing cooldown/rate limits), matching the meaning of "自动评论".
    if should_reply and random.random() > settings["probability_percent"] / 100:
        return

    _processing_chats.add(key)
    try:
        reply_text = await ask_ollama(
            _context_text(key),
            text,
            mode="channel_comment" if should_comment else "reply",
        )
        if not reply_text:
            return

        await asyncio.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
        # The switches may have changed while Ollama was generating content.
        send_settings = get_group_settings(bot_name, session_name, int(chat_id))
        still_enabled = (
            send_settings["channel_comment_enabled"] if should_comment else send_settings["enabled"]
        )
        if not still_enabled or not _can_reply(key, send_settings):
            return

        if should_comment:
            # Always reply to the source post so the output is a real comment,
            # rather than an unrelated message in the group timeline.
            await client.send_message(
                int(chat_id),
                reply_text,
                reply_to=getattr(message, "id", None),
                link_preview=False,
            )
        elif random.random() < 0.8:
            await client.send_message(int(chat_id), reply_text, link_preview=False)
        else:
            await client.send_message(
                int(chat_id),
                reply_text,
                reply_to=getattr(message, "id", None),
                link_preview=False,
            )

        now = time.time()
        _last_reply_time[key] = now
        _reply_history[key].append(now)
        _message_history[key].append({"sender_id": 0, "text": reply_text})
        action = "评论" if should_comment else "回复"
        print(f"[协议号AI] {action}成功 session={session_name} group={chat_id}: {reply_text!r}")
    except Exception:
        logging.exception(
            "协议号 AI %s失败 session=%s group=%s",
            "评论" if should_comment else "回复",
            session_name,
            chat_id,
        )
    finally:
        _processing_chats.discard(key)
