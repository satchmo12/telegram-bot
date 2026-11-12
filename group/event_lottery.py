"""Configurable scheduled group-event lotteries.

The feature is intentionally separate from the existing points/talk lotteries:
these events are published posts with explicit prizes, entry requirements and a
scheduled draw time.
"""
import html
import random
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from forward.message_forward import build_message_payload
from group.grouplist import load_users
from utils import (
    bot_datetime_from_timestamp,
    bot_now,
    get_bot_path,
    get_bot_timezone,
    get_group_whitelist,
    load_json,
    save_json,
)

CALLBACK_PREFIX = "eventlot"
LOTTERY_FILE = "data/event_lotteries.json"
DRAFT_KEY = "event_lottery_draft"
JOIN_KEY = "event_lottery_join"
MAX_PRIZES = 20
MAX_TARGETS = 10
MAX_PARTICIPANT_MANAGERS = 20
MAX_COMMENT_RULES = 10
PRIVATE_CHAT_ID_RE = re.compile(r"^-\d{5,20}$")

# Entry-verification challenges are intentionally separate from the group join
# verification module: a challenge belongs to one lottery and never changes a
# member's permissions. New methods can be added without changing the lottery
# record schema by extending ENTRY_VERIFICATION_METHODS.
ENTRY_VERIFICATION_METHODS = {"numeric": "🧮 数字验证"}
ENTRY_VERIFICATION_CHALLENGE_TTL = 5 * 60
_entry_verification_challenges: dict[tuple[str, int], dict] = {}


def _file(context: ContextTypes.DEFAULT_TYPE) -> str:
    return get_bot_path(context, LOTTERY_FILE)


def _load(context: ContextTypes.DEFAULT_TYPE) -> dict:
    data = load_json(_file(context))
    if not isinstance(data, dict):
        data = {}
    records = data.get("lotteries")
    if not isinstance(records, dict):
        records = {}
    data["lotteries"] = records
    return data


def _save(context: ContextTypes.DEFAULT_TYPE, data: dict) -> None:
    save_json(_file(context), data)


def _record(data: dict, lottery_id: str) -> Optional[dict]:
    record = data.get("lotteries", {}).get(str(lottery_id))
    return record if isinstance(record, dict) else None


def _new_lottery(chat_id: int, owner_id: int) -> dict:
    now = int(time.time())
    return {
        "id": uuid.uuid4().hex[:12],
        "chat_id": int(chat_id),
        "owner_id": int(owner_id),
        "status": "draft",  # draft / scheduled / open / drawn
        "name": "",
        "description": "",
        "publish_at": 0,  # 0 means publish immediately
        "draw_at": 0,
        "prizes": [],
        "requirements": {
            "username": False,
            "min_messages": 0,
            "password": "",
            # A rule is {"key": "...", "value": "..."}. Each rule must
            # be matched in a comment posted to the lottery group.
            "comment_rules": [],
        },
        # Verification is a separate entry gate. Keep it as an object so more
        # methods (captcha, channel code, etc.) can be introduced later.
        "entry_verification": {"enabled": False, "method": "numeric"},
        # target items: {target: "@public_name", password: "optional"}
        "follow_targets": [],
        # extra locations to receive the published lottery post.
        "sync_targets": [],
        "cover": None,
        "participants": {},
        # Users specifically allowed to directly add participants for this lottery.
        "participant_managers": {},
        # Per-lottery messages counted only after this lottery is published.
        # chat_id -> user_id -> count
        "message_counts": {},
        "published_at": 0,
        "drawn_at": 0,
        "created_at": now,
        "updated_at": now,
    }


def _display_target(value: str) -> str:
    return str(value or "").strip() or "未设置"


def _normalize_target(value: str) -> str:
    target = str(value or "").strip()
    if target.startswith("https://t.me/"):
        target = "@" + target.rsplit("/", 1)[-1]
    elif target.startswith("t.me/"):
        target = "@" + target.rsplit("/", 1)[-1]
    if PRIVATE_CHAT_ID_RE.fullmatch(target):
        return target
    if target and not target.startswith("@"):
        target = "@" + target.lstrip("@")
    return target if len(target) > 1 else ""


def _target_join_url(item: dict) -> str:
    """Return a safe public URL or the stored private-chat invite link."""
    target = str((item or {}).get("target") or "").strip()
    if PRIVATE_CHAT_ID_RE.fullmatch(target):
        invite_link = str((item or {}).get("invite_link") or "").strip()
        return invite_link if invite_link.startswith("https://t.me/") else ""
    return f"https://t.me/{target.lstrip('@')}" if target.startswith("@") else ""


def _target_api_ref(target: str):
    return int(target) if PRIVATE_CHAT_ID_RE.fullmatch(str(target or "")) else target


def _target_items(value: str) -> list[str]:
    result = []
    for raw in str(value or "").replace("，", ",").replace("\n", ",").split(","):
        item = _normalize_target(raw)
        if item and item not in result:
            result.append(item)
    return result


def _entry_verification_settings(record: dict) -> dict:
    """Read/migrate the per-lottery verification configuration."""
    raw = record.get("entry_verification") if isinstance(record, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    method = str(raw.get("method") or "numeric").strip().lower()
    if method not in ENTRY_VERIFICATION_METHODS:
        method = "numeric"
    normalized = {"enabled": bool(raw.get("enabled", False)), "method": method}
    if isinstance(record, dict):
        record["entry_verification"] = normalized
    return normalized


def _entry_verification_method_label(method: str) -> str:
    return ENTRY_VERIFICATION_METHODS.get(method, ENTRY_VERIFICATION_METHODS["numeric"])


def _entry_verification_passed(record: dict, user_id: int) -> bool:
    participants = record.get("participants") if isinstance(record, dict) else None
    entry = participants.get(str(user_id)) if isinstance(participants, dict) else None
    if isinstance(entry, dict) and bool(entry.get("manual_override", False)):
        return True
    verification = entry.get("entry_verification") if isinstance(entry, dict) else None
    settings = _entry_verification_settings(record)
    return bool(
        isinstance(verification, dict)
        and verification.get("passed_at")
        and verification.get("method") == settings["method"]
    )


def _reset_entry_verification_for_participants(record: dict) -> None:
    """Require a fresh check when the manager enables the entry gate."""
    participants = record.get("participants") if isinstance(record, dict) else None
    if not isinstance(participants, dict):
        return
    for entry in participants.values():
        if not isinstance(entry, dict):
            continue
        # Directly added participants remain exempt when verification settings
        # change; they must never be downgraded by an unrelated requirement.
        if entry.get("manual_override"):
            entry["eligible"] = True
            continue
        entry.pop("entry_verification", None)
        entry["eligible"] = False


def _entry_verification_challenge_key(lottery_id: str, user_id: int) -> tuple[str, int]:
    return str(lottery_id), int(user_id)


def _create_numeric_entry_verification_challenge(record: dict, user_id: int):
    """Create a small, button-based numeric challenge for this lottery entry."""
    if random.choice((True, False)):
        left = random.randint(1, 99)
        right = random.randint(1, 100 - left)
        operator, answer = "+", left + right
    else:
        left = random.randint(1, 100)
        right = random.randint(1, left)
        operator, answer = "-", left - right

    options = {answer}
    while len(options) < 4:
        options.add(random.randint(0, 100))
    options = list(options)
    random.shuffle(options)
    token = secrets.token_hex(4)
    lottery_id = str(record["id"])
    _entry_verification_challenges[_entry_verification_challenge_key(lottery_id, user_id)] = {
        "answer": answer,
        "options": set(options),
        "token": token,
        "expires_at": int(time.time()) + ENTRY_VERIFICATION_CHALLENGE_TTL,
    }
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                str(option),
                callback_data=(
                    f"{CALLBACK_PREFIX}:entry_verify:{lottery_id}:{user_id}:{token}:{option}"
                ),
            )
            for option in options[index:index + 2]
        ]
        for index in range(0, len(options), 2)
    ])
    return f"{left} {operator} {right} = ?", keyboard


def _comment_rule_signature(key: str, value: str) -> str:
    """Return a stable identifier for one configured comment rule."""
    return f"{str(key).strip()}\x1f{str(value).strip()}"


def _comment_rules(requirements: dict) -> list[dict]:
    """Read and normalize comment rules stored on the main requirements."""
    if not isinstance(requirements, dict):
        return []
    raw_rules = requirements.get("comment_rules")
    normalized = []
    seen = set()
    for item in raw_rules if isinstance(raw_rules, list) else []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()[:80]
        value = str(item.get("value") or "").strip()[:80]
        signature = _comment_rule_signature(key, value)
        if not key or not value or signature in seen:
            continue
        seen.add(signature)
        normalized.append({"key": key, "value": value})
    requirements["comment_rules"] = normalized
    return normalized


def _matched_comment_rule_signatures(record: dict, user_id: int) -> set[str]:
    matches = record.get("comment_rule_matches") if isinstance(record, dict) else None
    values = matches.get(str(user_id)) if isinstance(matches, dict) else None
    return {str(value) for value in values if isinstance(value, str)} if isinstance(values, list) else set()


def _comment_rules_completed(record: dict, user_id: int) -> bool:
    requirements = record.get("requirements") if isinstance(record, dict) else None
    rules = _comment_rules(requirements)
    if not rules:
        return True
    completed = _matched_comment_rule_signatures(record, user_id)
    return all(_comment_rule_signature(rule["key"], rule["value"]) in completed for rule in rules)


def _record_comment_rule_matches(record: dict, user_id: int, text: str) -> bool:
    """Save rules whose key and value occur in the same lottery-group comment."""
    requirements = record.get("requirements") if isinstance(record, dict) else None
    rules = _comment_rules(requirements)
    if not rules:
        return False
    comment = str(text or "").casefold()
    if not comment:
        return False
    matched = [
        _comment_rule_signature(rule["key"], rule["value"])
        for rule in rules
        if rule["key"].casefold() in comment and rule["value"].casefold() in comment
    ]
    if not matched:
        return False
    by_user = record.setdefault("comment_rule_matches", {})
    completed = by_user.setdefault(str(user_id), [])
    if not isinstance(completed, list):
        completed = []
        by_user[str(user_id)] = completed
    changed = False
    for signature in matched:
        if signature not in completed:
            completed.append(signature)
            changed = True
    return changed


def _remove_comment_rule_matches(record: dict, key: str, value: str) -> None:
    """Do not retain a completed result when a rule is deleted and re-added."""
    matches = record.get("comment_rule_matches") if isinstance(record, dict) else None
    if not isinstance(matches, dict):
        return
    signature = _comment_rule_signature(key, value)
    for user_id, completed in list(matches.items()):
        if not isinstance(completed, list):
            continue
        matches[user_id] = [item for item in completed if item != signature]


def _comment_rules_text(record: dict) -> str:
    requirements = record.setdefault("requirements", {})
    rules = _comment_rules(requirements)
    lines = [
        "💬 <b>参与评论规则</b>",
        "参与者需在抽奖发布群发送评论；每条规则的 key 和 value 必须同时出现在同一条评论中。",
        "全部规则均满足后，才算完成“参与评论”条件。",
        "",
    ]
    if not rules:
        lines.append("暂未设置规则。")
    else:
        for index, rule in enumerate(rules, 1):
            lines.append(
                f"{index}. key：<code>{html.escape(rule['key'])}</code> ｜ "
                f"值：<code>{html.escape(rule['value'])}</code>"
            )
    return "\n".join(lines)


def _comment_rules_keyboard(record: dict) -> InlineKeyboardMarkup:
    lid = record["id"]
    rules = _comment_rules(record.setdefault("requirements", {}))
    rows = []
    for index, rule in enumerate(rules):
        label = f"🗑 删除：{rule['key'][:12]} = {rule['value'][:12]}"
        rows.append([InlineKeyboardButton(
            label, callback_data=f"{CALLBACK_PREFIX}:comment_rule_delete:{lid}:{index}"
        )])
    if len(rules) < MAX_COMMENT_RULES:
        rows.append([InlineKeyboardButton(
            "➕ 添加评论规则", callback_data=f"{CALLBACK_PREFIX}:comment_rule_add:{lid}"
        )])
    rows.append([InlineKeyboardButton(
        "⬅️ 返回参与条件", callback_data=f"{CALLBACK_PREFIX}:req_main:{lid}"
    )])
    return InlineKeyboardMarkup(rows)


def _target_requirements(item: dict) -> dict:
    """Read per-follow-target requirements and migrate the legacy password field."""
    raw = item.get("requirements") if isinstance(item, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    return {
        "username": bool(raw.get("username", False)),
        "min_messages": max(0, int(raw.get("min_messages", 0) or 0)),
        "password": str(raw.get("password") or item.get("password") or "").strip()[:80],
    }


def _set_target_requirements(item: dict, requirements: dict) -> dict:
    normalized = {
        "username": bool(requirements.get("username", False)),
        "min_messages": max(0, int(requirements.get("min_messages", 0) or 0)),
        "password": str(requirements.get("password") or "").strip()[:80],
    }
    item["requirements"] = normalized
    # Keep old saved records compatible, but all new settings use requirements.
    item.pop("password", None)
    return normalized


def _target_conditions_text(item: dict) -> list[str]:
    req = _target_requirements(item)
    lines = []
    if req["username"]:
        lines.append("└ 🪪 必须设置用户名")
    if _target_is_group(item):
        if req["min_messages"] > 0:
            lines.append(f"└ 🎟️ 本群发言大于 {req['min_messages']} 条")
        if req["password"]:
            lines.append(f"└ 🔑 发送口令﹝{req['password']}﹞")
    return lines


def _target_chat_type(item: dict) -> str:
    value = str((item or {}).get("chat_type") or "").lower()
    return value if value in {"channel", "group", "supergroup"} else ""


def _target_is_group(item: dict) -> bool:
    return _target_chat_type(item) in {"group", "supergroup"}


async def _get_admin_target_chat(context: ContextTypes.DEFAULT_TYPE, target: str):
    """Return the target chat only when this bot is an administrator there."""
    try:
        chat = await context.bot.get_chat(_target_api_ref(target))
        me = await context.bot.get_me()
        member = await context.bot.get_chat_member(chat.id, me.id)
    except Exception:
        return None
    status = getattr(member, "status", "")
    status = getattr(status, "value", status)
    if str(status).lower() not in {"creator", "owner", "administrator"}:
        return None
    return chat


async def _refresh_target_chat_type(context: ContextTypes.DEFAULT_TYPE, item: dict) -> bool:
    """Populate target type for older lottery records created before this field."""
    target = str((item or {}).get("target") or "")
    if not target:
        return False
    try:
        chat = await context.bot.get_chat(_target_api_ref(target))
    except Exception:
        return False
    chat_type = getattr(chat, "type", "")
    chat_type = getattr(chat_type, "value", chat_type)
    chat_type = str(chat_type).lower()
    if chat_type not in {"channel", "group", "supergroup"}:
        return False
    chat_id = getattr(chat, "id", None)
    changed = item.get("chat_type") != chat_type
    if chat_id is not None and str(item.get("chat_id") or "") != str(int(chat_id)):
        item["chat_id"] = int(chat_id)
        changed = True
    if not changed:
        return False
    item["chat_type"] = chat_type
    # Channels cannot enforce chat activity or a group password.
    if chat_type == "channel":
        req = _target_requirements(item)
        req["min_messages"] = 0
        req["password"] = ""
        _set_target_requirements(item, req)
    return True


def _timestamp_text(ts: int, context: ContextTypes.DEFAULT_TYPE) -> str:
    if not ts:
        return "未设置"
    return bot_datetime_from_timestamp(ts, context).strftime("%Y-%m-%d %H:%M")


def _participation_condition_lines(record: dict) -> list[str]:
    """Render the configured entry requirements for lottery/result cards."""
    req = record.get("requirements") if isinstance(record.get("requirements"), dict) else {}
    lines = []
    has_conditions = False
    if req.get("username"):
        lines.append("🪪 必须设置用户名")
        has_conditions = True
    min_messages = int(req.get("min_messages", 0) or 0)
    if min_messages > 0:
        lines.append(f"🎟️ 发言数大于 {min_messages} 条")
        has_conditions = True
    password = str(req.get("password") or "").strip()
    if password:
        lines.append(f"🔑 发送口令﹝{html.escape(password)}﹞")
        has_conditions = True
    comment_rules = _comment_rules(req)
    if comment_rules:
        lines.append("💬 参与评论（每条规则的 key 和值需在同一条评论中出现）")
        for rule in comment_rules:
            lines.append(
                f"└ key：{html.escape(rule['key'])} ｜ 值：{html.escape(rule['value'])}"
            )
        has_conditions = True
    entry_verification = _entry_verification_settings(record)
    if entry_verification["enabled"]:
        lines.append(f"🛡️ 参与前需完成{_entry_verification_method_label(entry_verification['method'])}")
        has_conditions = True
    for item in record.get("follow_targets", []) or []:
        if not isinstance(item, dict):
            continue
        target = _display_target(item.get("target"))
        lines.append(f"🎫 加入-{html.escape(target.lstrip('@'))}")
        lines.extend(_target_conditions_text(item))
        has_conditions = True
    if not has_conditions:
        lines.append("无额外条件")
    return lines


def _lottery_description_and_time_lines(
    record: dict, context: ContextTypes.DEFAULT_TYPE
) -> list[str]:
    return [
        "📜 <b>抽奖说明</b>",
        html.escape(str(record.get("description") or "未填写")),
        "",
        "📆 <b>开奖时间</b>",
        (
            f"• {get_bot_timezone(context)} "
            f"{_timestamp_text(int(record.get('draw_at', 0) or 0), context)}"
        ),
    ]


def _format_lottery(record: dict, context: ContextTypes.DEFAULT_TYPE) -> str:
    name = html.escape(str(record.get("name") or "未命名抽奖"))
    lines = [f"🎰 <b>{name}</b>", "", "📮 <b>参与条件</b>"]
    lines.extend(_participation_condition_lines(record))

    lines.extend(["", "🎁 <b>奖品名单</b>"])
    prizes = record.get("prizes", []) or []
    if prizes:
        for prize in prizes:
            if isinstance(prize, dict):
                prize_name = html.escape(str(prize.get("name") or "未命名奖品"))
                lines.append(
                    f"💰️ {prize_name} × <b>{int(prize.get('count', 1) or 1)}</b>"
                )
    else:
        lines.append("暂未设置奖品")

    lines.extend(["", *_lottery_description_and_time_lines(record, context)])
    return "\n".join(lines)


def _format_draw_result(
    record: dict, context: ContextTypes.DEFAULT_TYPE, winners: list[dict]
) -> str:
    """Build the full winner card sent to the original group and winners."""
    name = html.escape(str(record.get("name") or "抽奖"))
    lines = [f"🎉 <b>{name} 开奖结果</b>", "", "📮 <b>参与条件</b>"]
    lines.extend(_participation_condition_lines(record))
    lines.extend(["", "🏆 <b>开奖结果</b>"])
    if winners:
        for index, item in enumerate(winners, start=1):
            prize = html.escape(str(item.get("prize") or "未命名奖品"))
            winner_name = html.escape(str(item.get("name") or "用户"))
            lines.append(f"{index}. 🎁 {prize}：{winner_name}")
    else:
        lines.append("暂无符合条件的参与者。")
    lines.extend(["", *_lottery_description_and_time_lines(record, context)])
    return "\n".join(lines)


def _editor_text(record: dict, context: ContextTypes.DEFAULT_TYPE) -> str:
    req = record.get("requirements", {})
    return "\n".join([
        "🎰 抽奖编辑",
        f"状态：{_status_label(record)}",
        f"名称：{record.get('name') or '未设置'}",
        f"说明：{'已设置' if record.get('description') else '未设置'}",
        f"发布时间：{'立即发布' if not record.get('publish_at') else _timestamp_text(int(record['publish_at']), context)}",
        f"开奖时间：{_timestamp_text(int(record.get('draw_at', 0) or 0), context)}",
        f"奖品：{len(record.get('prizes', []))} 项",
        f"用户名：{'需要' if req.get('username') else '不要求'} | 发言：{int(req.get('min_messages', 0) or 0)} 条 | 口令：{'已设置' if req.get('password') else '未设置'}",
        f"参与评论：{len(_comment_rules(req))} 条规则",
        f"参与前验证：{_entry_verification_method_label(_entry_verification_settings(record)['method']) if _entry_verification_settings(record)['enabled'] else '未开启'}",
        f"关注目标：{len(record.get('follow_targets', []))} 个 | 同步位置：{len(record.get('sync_targets', []))} 个",
        f"封面：{'已设置' if isinstance(record.get('cover'), dict) else '未设置'}",
    ])


def _editor_keyboard(record: dict) -> InlineKeyboardMarkup:
    lid = record["id"]
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✏️ 抽奖名称", callback_data=f"{CALLBACK_PREFIX}:field:{lid}:name"),
            InlineKeyboardButton("📜 抽奖说明", callback_data=f"{CALLBACK_PREFIX}:field:{lid}:description"),
        ],
        [
            InlineKeyboardButton("📅 发布时间", callback_data=f"{CALLBACK_PREFIX}:field:{lid}:publish_at"),
            InlineKeyboardButton("⏰ 开奖时间", callback_data=f"{CALLBACK_PREFIX}:field:{lid}:draw_at"),
        ],
        [
            InlineKeyboardButton("🎁 奖品设置", callback_data=f"{CALLBACK_PREFIX}:prizes:{lid}"),
            InlineKeyboardButton("📮 参与条件", callback_data=f"{CALLBACK_PREFIX}:requirements:{lid}"),
        ],
        [InlineKeyboardButton("🛡️ 参与前验证", callback_data=f"{CALLBACK_PREFIX}:entry_verification:{lid}")],
        [
            InlineKeyboardButton("🖼 抽奖封面", callback_data=f"{CALLBACK_PREFIX}:field:{lid}:cover"),
            InlineKeyboardButton("📢 关注频道/群组", callback_data=f"{CALLBACK_PREFIX}:targets:{lid}:follow"),
        ],
        [
            InlineKeyboardButton("📍 同步位置", callback_data=f"{CALLBACK_PREFIX}:targets:{lid}:sync"),
            InlineKeyboardButton("👁 抽奖预览", callback_data=f"{CALLBACK_PREFIX}:preview:{lid}"),
        ],
        [
            InlineKeyboardButton("✅ 完成发布", callback_data=f"{CALLBACK_PREFIX}:publish:{lid}"),
            InlineKeyboardButton("❌ 放弃草稿", callback_data=f"{CALLBACK_PREFIX}:discard:{lid}"),
        ],
        [InlineKeyboardButton("⬅️ 返回抽奖列表", callback_data=f"{CALLBACK_PREFIX}:dashboard:{record['chat_id']}")],
    ])


def _prizes_keyboard(record: dict) -> InlineKeyboardMarkup:
    lid = record["id"]
    rows = []
    for index, prize in enumerate(record.get("prizes", [])):
        if isinstance(prize, dict):
            rows.append([InlineKeyboardButton(
                f"🗑 {prize.get('name', '未命名')} × {prize.get('count', 1)}",
                callback_data=f"{CALLBACK_PREFIX}:prize_delete:{lid}:{index}",
            )])
    if len(record.get("prizes", [])) < MAX_PRIZES:
        rows.append([InlineKeyboardButton("➕ 添加奖品", callback_data=f"{CALLBACK_PREFIX}:prize_add:{lid}")])
    rows.append([InlineKeyboardButton("⬅️ 返回编辑", callback_data=f"{CALLBACK_PREFIX}:edit:{lid}")])
    return InlineKeyboardMarkup(rows)


def _entry_verification_text(record: dict) -> str:
    settings = _entry_verification_settings(record)
    status = "已开启" if settings["enabled"] else "未开启"
    return "\n".join([
        "🛡️ 参与前验证",
        f"状态：{status}",
        f"验证方式：{_entry_verification_method_label(settings['method'])}",
        "开启后，用户点击“参与抽奖”会先在机器人私聊完成验证，再进入报名页。",
    ])


def _entry_verification_keyboard(record: dict) -> InlineKeyboardMarkup:
    lid = record["id"]
    settings = _entry_verification_settings(record)
    method_label = _entry_verification_method_label(settings["method"])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"{'✅' if settings['enabled'] else '🚫'} 开启参与前验证",
            callback_data=f"{CALLBACK_PREFIX}:entry_verify_toggle:{lid}",
        )],
        [InlineKeyboardButton(
            f"验证方式：{method_label}",
            callback_data=f"{CALLBACK_PREFIX}:entry_verify_method:{lid}",
        )],
        [InlineKeyboardButton("⬅️ 返回编辑", callback_data=f"{CALLBACK_PREFIX}:edit:{lid}")],
    ])


def _entry_verification_methods_text(record: dict) -> str:
    settings = _entry_verification_settings(record)
    return "\n".join([
        "🛡️ 选择验证方式",
        f"当前方式：{_entry_verification_method_label(settings['method'])}",
        "目前可用数字验证；新增验证方式会显示在此处。",
    ])


def _entry_verification_methods_keyboard(record: dict) -> InlineKeyboardMarkup:
    lid = record["id"]
    current_method = _entry_verification_settings(record)["method"]
    rows = [
        [InlineKeyboardButton(
            f"{'✅' if method == current_method else '▫️'} {label}",
            callback_data=f"{CALLBACK_PREFIX}:entry_verify_select:{lid}:{method}",
        )]
        for method, label in ENTRY_VERIFICATION_METHODS.items()
    ]
    rows.append([InlineKeyboardButton(
        "⬅️ 返回参与前验证", callback_data=f"{CALLBACK_PREFIX}:entry_verification:{lid}"
    )])
    return InlineKeyboardMarkup(rows)


def _requirements_keyboard(record: dict) -> InlineKeyboardMarkup:
    """Requirements for the primary lottery group."""
    lid = record["id"]
    req = record.get("requirements", {})
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"{'✅' if req.get('username') else '🚫'} 必须有用户名",
            callback_data=f"{CALLBACK_PREFIX}:req_toggle:{lid}:username",
        )],
        [
            InlineKeyboardButton("🎟️ 发言条数", callback_data=f"{CALLBACK_PREFIX}:field:{lid}:min_messages"),
            InlineKeyboardButton("🔑 参与口令", callback_data=f"{CALLBACK_PREFIX}:field:{lid}:password"),
        ],
        [InlineKeyboardButton(
            f"💬 参与评论（{len(_comment_rules(req))} 条规则）",
            callback_data=f"{CALLBACK_PREFIX}:comment_rules:{lid}",
        )],
        [InlineKeyboardButton("⬅️ 返回群组选择", callback_data=f"{CALLBACK_PREFIX}:requirements:{lid}")],
    ])


def _requirements_selector_keyboard(record: dict) -> InlineKeyboardMarkup:
    lid = record["id"]
    rows = [[InlineKeyboardButton(
        "🎰 抽奖发布群（参与条件）",
        callback_data=f"{CALLBACK_PREFIX}:req_main:{lid}",
    )]]
    for index, item in enumerate(record.get("follow_targets", []) or []):
        if isinstance(item, dict):
            rows.append([InlineKeyboardButton(
                f"📢 {str(item.get('target') or '未设置')[:45]}",
                callback_data=f"{CALLBACK_PREFIX}:target_requirements:{lid}:{index}",
            )])
    rows.append([InlineKeyboardButton("⬅️ 返回编辑", callback_data=f"{CALLBACK_PREFIX}:edit:{lid}")])
    return InlineKeyboardMarkup(rows)


def _target_requirements_keyboard(record: dict, index: int) -> InlineKeyboardMarkup:
    lid = record["id"]
    item = (record.get("follow_targets") or [])[index]
    req = _target_requirements(item)
    rows = [[InlineKeyboardButton(
        f"{'✅' if req['username'] else '🚫'} 必须有用户名",
        callback_data=f"{CALLBACK_PREFIX}:target_req_toggle:{lid}:{index}:username",
    )]]
    if _target_is_group(item):
        rows.append([
            InlineKeyboardButton("🎟️ 本群发言条数", callback_data=f"{CALLBACK_PREFIX}:target_req_field:{lid}:{index}:min_messages"),
            InlineKeyboardButton("🔑 本群参与口令", callback_data=f"{CALLBACK_PREFIX}:target_req_field:{lid}:{index}:password"),
        ])
    rows.append([InlineKeyboardButton("⬅️ 返回群组选择", callback_data=f"{CALLBACK_PREFIX}:requirements:{lid}")])
    return InlineKeyboardMarkup(rows)


def _target_requirements_text(record: dict, index: int) -> str:
    item = (record.get("follow_targets") or [])[index]
    req = _target_requirements(item)
    is_group = _target_is_group(item)
    target_type = "群组" if is_group else "频道"
    lines = [
        f"📢 {item.get('target') or '未设置'} 的参与条件",
        f"类型：{target_type}",
        f"用户名：{'需要' if req['username'] else '不要求'}",
    ]
    if is_group:
        lines.extend([
            f"本群发言：{req['min_messages']} 条",
            f"口令：{'已设置' if req['password'] else '未设置'}",
        ])
    else:
        lines.append("频道不支持设置发言条数和参与口令。")
    return "\n".join(lines)


def _sync_targets_keyboard(record: dict) -> InlineKeyboardMarkup:
    lid = record["id"]
    rows = []
    sync_values = list(record.get("sync_targets", []) or [])
    for index, value in enumerate(sync_values):
        rows.append([InlineKeyboardButton(
            f"🗑 {value}", callback_data=f"{CALLBACK_PREFIX}:target_delete:{lid}:sync:{index}"
        )])
    for index, item in enumerate(record.get("follow_targets", []) or []):
        if not isinstance(item, dict):
            continue
        target = str(item.get("target") or "")
        if target and target not in sync_values:
            rows.append([InlineKeyboardButton(
                f"➕ 同步到 {target}", callback_data=f"{CALLBACK_PREFIX}:sync_pick:{lid}:{index}"
            )])
    rows.append([InlineKeyboardButton("⬅️ 返回编辑", callback_data=f"{CALLBACK_PREFIX}:edit:{lid}")])
    return InlineKeyboardMarkup(rows)


def _targets_keyboard(record: dict, kind: str) -> InlineKeyboardMarkup:
    if kind == "sync":
        return _sync_targets_keyboard(record)
    lid = record["id"]
    rows = []
    for index, item in enumerate(record.get("follow_targets", []) or []):
        if not isinstance(item, dict):
            continue
        target = str(item.get("target") or "未设置")
        rows.append([
            InlineKeyboardButton(
                f"⚙️ {target}", callback_data=f"{CALLBACK_PREFIX}:target_requirements:{lid}:{index}"
            ),
            InlineKeyboardButton(
                "🗑 删除", callback_data=f"{CALLBACK_PREFIX}:target_delete:{lid}:follow:{index}"
            ),
        ])
    if len(record.get("follow_targets", []) or []) < MAX_TARGETS:
        rows.append([InlineKeyboardButton("➕ 添加关注频道/群组", callback_data=f"{CALLBACK_PREFIX}:target_add:{lid}:follow")])
    rows.append([InlineKeyboardButton("⬅️ 返回编辑", callback_data=f"{CALLBACK_PREFIX}:edit:{lid}")])
    return InlineKeyboardMarkup(rows)


def _start_editor_input_stage(query, context: ContextTypes.DEFAULT_TYPE, lottery_id: str, field: str) -> None:
    """Remember the bot prompt so it can be removed after a successful input."""
    prompt = query.message
    context.user_data[DRAFT_KEY] = {
        "id": lottery_id,
        "field": field,
        "prompt_chat_id": getattr(getattr(prompt, "chat", None), "id", None),
        "prompt_message_id": getattr(prompt, "message_id", None),
    }


async def _delete_editor_input_prompt(context: ContextTypes.DEFAULT_TYPE, stage: dict) -> None:
    """Remove the temporary “please enter …” message and its back button."""
    chat_id = stage.get("prompt_chat_id")
    message_id = stage.get("prompt_message_id")
    if chat_id is None or message_id is None:
        return
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as exc:
        # The setting itself has succeeded; deletion is best effort only.
        print(f"删除抽奖输入提示失败 chat={chat_id} message={message_id}: {exc}")


async def _can_manage(context: ContextTypes.DEFAULT_TYPE, user_id: int, chat_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
    except Exception:
        return False
    return getattr(member, "status", "") in {"creator", "owner", "administrator"}


def _participant_managers(record: dict) -> dict:
    """Return the per-lottery delegated participant-manager mapping."""
    raw = record.get("participant_managers") if isinstance(record, dict) else None
    if not isinstance(raw, dict):
        raw = {}
        if isinstance(record, dict):
            record["participant_managers"] = raw
    return raw


def _is_participant_manager(record: dict, user_id: int) -> bool:
    return str(user_id) in _participant_managers(record)


async def _can_manage_participants(
    context: ContextTypes.DEFAULT_TYPE, record: dict, user_id: int
) -> bool:
    return _is_participant_manager(record, user_id) or await _can_manage(
        context, user_id, int(record["chat_id"])
    )


def _actor_snapshot(user) -> dict:
    return {
        "user_id": int(user.id),
        "name": str(getattr(user, "full_name", "") or getattr(user, "first_name", "") or user.id)[:80],
        "username": str(getattr(user, "username", "") or "")[:64],
    }


def _actor_label(actor: dict) -> str:
    if not isinstance(actor, dict):
        return "未知管理员"
    name = str(actor.get("name") or actor.get("user_id") or "未知管理员")
    username = str(actor.get("username") or "").strip().lstrip("@")
    return f"{name}（@{username}）" if username else name


def _resolve_known_lottery_user(record: dict, value: str) -> dict:
    """Resolve an ID or a username from members recorded for this lottery group.

    Telegram's Bot API cannot reliably look up arbitrary personal @usernames.
    Usernames are therefore resolved from the bot's observed group-user records;
    a numeric Telegram ID is always accepted and may have an unknown display name.
    """
    text = str(value or "").strip()
    if not text:
        raise ValueError("请输入用户ID或用户名。")
    users = load_users(int(record["chat_id"]))
    participants = record.get("participants") if isinstance(record.get("participants"), dict) else {}

    if re.fullmatch(r"\d{1,20}", text):
        user_id = int(text)
        info = users.get(str(user_id), {}) if isinstance(users, dict) else {}
        existing = participants.get(str(user_id), {}) if isinstance(participants, dict) else {}
        info = info if isinstance(info, dict) else {}
        existing = existing if isinstance(existing, dict) else {}
        return {
            "user_id": user_id,
            "name": str(info.get("full_name") or existing.get("name") or f"用户 {user_id}")[:80],
            "username": str(info.get("username") or existing.get("username") or "")[:64],
        }

    username = text.lstrip("@").lower()
    if not re.fullmatch(r"[a-z0-9_]{5,32}", username):
        raise ValueError("用户名格式不正确；请输入 @用户名 或纯数字用户ID。")
    matches = []
    for user_id, info in (users.items() if isinstance(users, dict) else []):
        if not isinstance(info, dict):
            continue
        candidates = [str(info.get("username") or "").lower().lstrip("@")]
        candidates.extend(str(item).lower().lstrip("@") for item in info.get("username_history", []) or [])
        if username in candidates:
            try:
                resolved_id = int(user_id)
            except (TypeError, ValueError):
                continue
            matches.append({
                "user_id": resolved_id,
                "name": str(info.get("full_name") or info.get("username") or resolved_id)[:80],
                "username": str(info.get("username") or username)[:64],
            })
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError("找到多个同名用户名记录，请改用用户ID。")
    raise ValueError("未找到该用户名。请确认用户曾在抽奖发布群发言，或直接输入用户ID。")


def _participant_manager_text(record: dict) -> str:
    managers = _participant_managers(record)
    lines = ["👮 <b>可添加抽奖用户的管理人员</b>", f"🎰 {html.escape(str(record.get('name') or '未命名抽奖'))}"]
    if not managers:
        lines.extend(["", "暂未设置管理人员。"])
    else:
        lines.append("")
        for index, item in enumerate(managers.values(), start=1):
            if not isinstance(item, dict):
                continue
            name = html.escape(str(item.get("name") or item.get("user_id") or "未知用户"))
            username = str(item.get("username") or "").strip().lstrip("@")
            username_text = f" ｜ @{html.escape(username)}" if username else ""
            lines.append(f"{index}. {name}{username_text} ｜ <code>{item.get('user_id')}</code>")
    lines.extend(["", "被指定的管理人员只能直接添加参与用户，不能编辑抽奖或开奖。"])
    return "\n".join(lines)


def _participant_manager_keyboard(record: dict) -> InlineKeyboardMarkup:
    lid = record["id"]
    rows = []
    for item in _participant_managers(record).values():
        if not isinstance(item, dict):
            continue
        user_id = int(item.get("user_id", 0) or 0)
        if user_id:
            label = str(item.get("name") or item.get("username") or user_id)[:40]
            rows.append([InlineKeyboardButton(
                f"🗑 移除 {label}", callback_data=f"{CALLBACK_PREFIX}:participant_manager_remove:{lid}:{user_id}"
            )])
    if len(_participant_managers(record)) < MAX_PARTICIPANT_MANAGERS:
        rows.append([InlineKeyboardButton(
            "➕ 添加管理人员", callback_data=f"{CALLBACK_PREFIX}:participant_manager_add:{lid}"
        )])
    rows.append([InlineKeyboardButton("⬅️ 返回参与用户", callback_data=f"{CALLBACK_PREFIX}:participants:{lid}:1")])
    return InlineKeyboardMarkup(rows)


def _participant_manager_card_text(record: dict) -> str:
    return "\n".join([
        "👮 <b>抽奖参与用户管理</b>",
        f"🎰 {html.escape(str(record.get('name') or '未命名抽奖'))}",
        "你可以直接添加参与用户；通过此方式添加的用户无需完成参与条件。",
    ])


def _participant_manager_card_keyboard(record: dict) -> InlineKeyboardMarkup:
    lid = record["id"]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ 添加参与抽奖用户", callback_data=f"{CALLBACK_PREFIX}:manual_add:{lid}")],
        [InlineKeyboardButton("👥 查看参与用户", callback_data=f"{CALLBACK_PREFIX}:participants:{lid}:1")],
    ])


async def _send_participant_manager_card(context: ContextTypes.DEFAULT_TYPE, record: dict, manager: dict) -> bool:
    try:
        await context.bot.send_message(
            chat_id=int(manager["user_id"]),
            text=_participant_manager_card_text(record),
            parse_mode="HTML",
            reply_markup=_participant_manager_card_keyboard(record),
        )
        return True
    except Exception:
        return False


def _records_for_chat(data: dict, chat_id: int) -> list[dict]:
    records = [
        item for item in data.get("lotteries", {}).values()
        if isinstance(item, dict) and int(item.get("chat_id", 0) or 0) == int(chat_id)
    ]
    return sorted(records, key=lambda item: int(item.get("created_at", 0) or 0), reverse=True)


def _status_label(record: dict) -> str:
    return {
        "draft": "📝 草稿",
        "scheduled": "🕒 待发布",
        "open": "🟢 进行中",
        "drawn": "🏆 已开奖",
    }.get(str(record.get("status") or ""), "未知")


def _dashboard_text(records: list[dict], *, history: bool = False) -> str:
    title = "📚 历史抽奖" if history else "🎉 独立活动抽奖"
    lines = [title, "", "独立于积分抽奖和发言抽奖。"]
    shown = records if history else records[:6]
    if not shown:
        lines.append("\n暂无抽奖记录。")
    else:
        lines.append("")
        for record in shown:
            lines.append(f"{_status_label(record)} · {record.get('name') or '未命名抽奖'}")
    return "\n".join(lines)


def _dashboard_keyboard(chat_id: int, records: list[dict], *, history: bool = False) -> InlineKeyboardMarkup:
    rows = []
    for record in (records if history else records[:6]):
        rows.append([InlineKeyboardButton(
            f"{_status_label(record)} {str(record.get('name') or '未命名')[:35]}",
            callback_data=f"{CALLBACK_PREFIX}:view:{record['id']}",
        )])
    if not history:
        rows.append([InlineKeyboardButton("➕ 创建独立抽奖", callback_data=f"{CALLBACK_PREFIX}:create:{chat_id}")])
        rows.append([InlineKeyboardButton("📚 查看历史抽奖", callback_data=f"{CALLBACK_PREFIX}:history:{chat_id}")])
    rows.append([InlineKeyboardButton("⬅️ 返回群设置", callback_data=f"gcfg:open:{chat_id}")])
    return InlineKeyboardMarkup(rows)


async def open_lottery_creator(query, context: ContextTypes.DEFAULT_TYPE, chat_id_str: str, user_id: int):
    try:
        chat_id = int(chat_id_str)
    except (TypeError, ValueError):
        return await query.answer("群ID无效。", show_alert=True)
    if not await _can_manage(context, user_id, chat_id):
        return await query.answer("你不是该群管理员，无法管理抽奖。", show_alert=True)
    data = _load(context)
    records = _records_for_chat(data, chat_id)
    await query.answer()
    return await query.edit_message_text(
        _dashboard_text(records),
        reply_markup=_dashboard_keyboard(chat_id, records),
    )


async def _create_lottery(query, context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int):
    data = _load(context)
    record = _new_lottery(chat_id, user_id)
    data["lotteries"][record["id"]] = record
    _save(context, data)
    await query.answer("已创建抽奖草稿；草稿不会自动发布。")
    return await query.edit_message_text(_editor_text(record, context), reply_markup=_editor_keyboard(record))


def _time_input_prompt(field: str, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Build time-entry guidance using this bot's configured local clock."""
    now = bot_now(context)
    timezone_name = get_bot_timezone(context)
    now_text = now.strftime("%Y-%m-%d %H:%M")
    if field == "publish_at":
        example = (now + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M")
        return (
            "请输入发布时间，或点击下方“⚡ 立即发布”。\n"
            f"当前机器人时间（{timezone_name}）：{now_text}\n"
            f"定时示例（5 分钟后）：{example}"
        )
    example = (now + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
    return (
        "请输入未来开奖时间，格式 YYYY-MM-DD HH:MM。\n"
        f"当前机器人时间（{timezone_name}）：{now_text}\n"
        f"可填写示例（1 小时后）：{example}"
    )


def _parse_time(value: str, context: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    text = str(value or "").strip()
    if text in {"立即", "现在"}:
        return 0
    try:
        naive = datetime.strptime(text, "%Y-%m-%d %H:%M")
        local = naive.replace(tzinfo=bot_now(context).tzinfo)
        timestamp = int(local.timestamp())
    except Exception:
        return None
    return timestamp if timestamp > int(time.time()) else None


async def _send_lottery_content(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id,
    record: dict,
    *,
    reply_markup=None,
    text: Optional[str] = None,
    allow_long_cover_text: bool = False,
):
    """Send text with the configured cover, optionally splitting long result cards.

    Normal lottery publications remain a single cover/caption post.  Draw-result
    cards can be longer because they include every winner; those cards retain
    the cover and send their complete text as a following message when needed.
    """
    text = _format_lottery(record, context) if text is None else str(text)
    cover = record.get("cover")
    if not isinstance(cover, dict):
        return await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

    if len(text) > 1024 and not allow_long_cover_text:
        raise ValueError("设置封面时，抽奖文案不能超过 1024 个字符。请缩短抽奖说明后再预览或发布。")

    payload_type = str(cover.get("type") or "").lower()
    file_id = cover.get("file_id")
    if not file_id:
        raise ValueError("抽奖封面文件无效，请重新上传图片或视频。")
    caption = text
    if len(text) > 1024:
        caption = f"🎉 <b>{html.escape(str(record.get('name') or '抽奖'))} 开奖结果</b>"
    common = {
        "chat_id": chat_id,
        "caption": caption,
        "parse_mode": "HTML",
        "reply_markup": reply_markup,
    }
    if payload_type == "photo":
        sent = await context.bot.send_photo(photo=file_id, **common)
    elif payload_type == "video":
        sent = await context.bot.send_video(video=file_id, **common)
    else:
        raise ValueError("抽奖封面仅支持图片或视频，请重新设置封面。")

    if len(text) > 1024:
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
    return sent


def _preview_markup(record: dict) -> InlineKeyboardMarkup:
    """Controls shown only beneath a manager's private lottery preview."""
    lottery_id = record["id"]
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "⬅️ 关闭返回", callback_data=f"{CALLBACK_PREFIX}:preview_close:{lottery_id}"
        ),
        InlineKeyboardButton(
            "✅ 完成发布", callback_data=f"{CALLBACK_PREFIX}:preview_publish:{lottery_id}"
        ),
    ]])


async def _close_preview_message(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Best-effort removal of the temporary private preview message."""
    message = getattr(query, "message", None)
    if not message:
        return
    try:
        await message.delete()
        return
    except Exception:
        pass
    chat_id = getattr(message, "chat_id", None) or getattr(getattr(message, "chat", None), "id", None)
    message_id = getattr(message, "message_id", None)
    if chat_id is None or message_id is None:
        return
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass


async def _lottery_deep_link(context: ContextTypes.DEFAULT_TYPE, lottery_id: str) -> str:
    username = str(getattr(context.bot, "username", "") or "").strip().lstrip("@")
    if not username:
        try:
            username = str((await context.bot.get_me()).username or "").strip().lstrip("@")
        except Exception:
            return ""
    return f"https://t.me/{username}?start=eventlot_{lottery_id}" if username else ""


async def _lottery_join_markup(context: ContextTypes.DEFAULT_TYPE, record: dict) -> InlineKeyboardMarkup:
    link = await _lottery_deep_link(context, str(record["id"]))
    if link:
        return InlineKeyboardMarkup([[InlineKeyboardButton("🎫 参与抽奖", url=link)]])
    # A bot without a username cannot use a deep link. Keep old posts usable.
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🎫 参与抽奖", callback_data=f"{CALLBACK_PREFIX}:join:{record['id']}")
    ]])


async def _send_lottery_post(context: ContextTypes.DEFAULT_TYPE, record: dict) -> int:
    markup = await _lottery_join_markup(context, record)
    targets = [str(record["chat_id"])] + [str(item) for item in record.get("sync_targets", [])]
    sent = 0
    for target in dict.fromkeys(targets):
        try:
            await _send_lottery_content(
                context,
                target,
                record,
                reply_markup=markup,
            )
            sent += 1
        except Exception as exc:
            print(f"抽奖发布失败 lottery={record['id']} target={target}: {exc}")
    return sent


async def _publish_if_due(context: ContextTypes.DEFAULT_TYPE, record: dict) -> bool:
    # Drafts are never eligible for the scheduler. A draft can exist forever
    # without posting anything until its owner explicitly presses 完成发布.
    now = int(time.time())
    if record.get("status") == "scheduled" and int(record.get("publish_at", 0) or 0) <= now:
        sent = await _send_lottery_post(context, record)
        if sent:
            record["status"] = "open"
            record["published_at"] = now
            record["message_counts"] = {}
            record["comment_rule_matches"] = {}
            return True
    return False


async def _draw_if_due(context: ContextTypes.DEFAULT_TYPE, record: dict) -> bool:
    if record.get("status") != "open" or not record.get("draw_at") or int(record["draw_at"]) > int(time.time()):
        return False
    participants = [
        item
        for item in (record.get("participants") or {}).values()
        if (
            isinstance(item, dict)
            and bool(item.get("eligible", True))
            and _comment_rules_completed(record, int(item.get("user_id", 0) or 0))
            and (
                not _entry_verification_settings(record)["enabled"]
                or _entry_verification_passed(record, int(item.get("user_id", 0) or 0))
            )
        )
    ]
    pool = list(participants)
    random.shuffle(pool)
    winners = []
    for prize in record.get("prizes", []):
        if not isinstance(prize, dict):
            continue
        for _ in range(max(0, int(prize.get("count", 1) or 1))):
            if not pool:
                break
            winner = pool.pop()
            winners.append({"prize": prize.get("name", "奖品"), "user_id": winner.get("user_id"), "name": winner.get("name", "用户")})
    result_text = _format_draw_result(record, context, winners)

    # Always publish the full result card to the group where the lottery was
    # created. Keep configured sync locations receiving the same announcement.
    targets = [str(record["chat_id"])] + [str(item) for item in record.get("sync_targets", [])]
    for target in dict.fromkeys(targets):
        try:
            await _send_lottery_content(
                context,
                target,
                record,
                text=result_text,
                allow_long_cover_text=True,
            )
        except Exception as exc:
            print(f"抽奖开奖通知失败 lottery={record['id']} target={target}: {exc}")

    # Winners receive the same complete result card privately. Telegram may
    # reject a private message before a user has started the bot; that failure
    # must not prevent publication of the group result or other winner notices.
    for user_id in dict.fromkeys(
        int(item.get("user_id", 0) or 0) for item in winners if item.get("user_id")
    ):
        try:
            await _send_lottery_content(
                context,
                user_id,
                record,
                text=result_text,
                allow_long_cover_text=True,
            )
        except Exception as exc:
            print(f"抽奖中奖私信失败 lottery={record['id']} user={user_id}: {exc}")
    record["status"] = "drawn"
    record["drawn_at"] = int(time.time())
    record["winners"] = winners
    return True


async def event_lottery_tick(context: ContextTypes.DEFAULT_TYPE):
    data = _load(context)
    changed = False
    for record in data.get("lotteries", {}).values():
        if not isinstance(record, dict):
            continue
        changed = await _publish_if_due(context, record) or changed
        changed = await _draw_if_due(context, record) or changed
    if changed:
        _save(context, data)


def _lottery_message_count(record: dict, chat_id: int, user_id: int) -> int:
    """Read only messages recorded after this lottery was published."""
    counts = record.get("message_counts") if isinstance(record.get("message_counts"), dict) else {}
    by_chat = counts.get(str(chat_id)) if isinstance(counts, dict) else {}
    value = by_chat.get(str(user_id), 0) if isinstance(by_chat, dict) else 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _lottery_counted_chat_ids(record: dict) -> set[int]:
    result = {int(record["chat_id"])}
    for item in record.get("follow_targets", []) or []:
        if not isinstance(item, dict):
            continue
        try:
            chat_id = int(item.get("chat_id"))
        except (TypeError, ValueError):
            continue
        if _target_is_group(item):
            result.add(chat_id)
    return result


def _activity_condition_threshold_reached(record: dict, chat_id: int, user_id: int) -> bool:
    """Whether this message may have completed a message-count condition."""
    requirements = record.get("requirements") if isinstance(record, dict) else None
    requirements = requirements if isinstance(requirements, dict) else {}
    if int(chat_id) == int(record.get("chat_id", 0) or 0):
        minimum = max(0, int(requirements.get("min_messages", 0) or 0))
        if minimum and _lottery_message_count(record, chat_id, user_id) == minimum + 1:
            return True
    for item in record.get("follow_targets", []) or []:
        if not isinstance(item, dict) or not _target_is_group(item):
            continue
        try:
            target_chat_id = int(item.get("chat_id"))
        except (TypeError, ValueError):
            continue
        requirements = _target_requirements(item)
        minimum = requirements["min_messages"]
        if (
            minimum
            and int(chat_id) == target_chat_id
            and _lottery_message_count(record, chat_id, user_id) == minimum + 1
        ):
            return True
    return False


async def _send_participation_completed_notice(
    context: ContextTypes.DEFAULT_TYPE, user, record: dict
) -> bool:
    """Privately notify a participant when they first complete every condition."""
    try:
        await context.bot.send_message(
            chat_id=user.id,
            text=(
                "🎉 <b>已完成全部参与条件</b>\n\n"
                f"您已获得「{html.escape(str(record.get('name') or '抽奖'))}」的抽奖资格，请等待开奖。"
            ),
            parse_mode="HTML",
        )
        return True
    except Exception:
        return False


async def event_lottery_message_counter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Count eligible group messages from the instant an open lottery is published."""
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user or getattr(user, "is_bot", False):
        return
    if chat.type not in {"group", "supergroup"}:
        return

    message_date = getattr(msg, "date", None)
    message_timestamp = int(message_date.timestamp()) if message_date else int(time.time())
    data = _load(context)
    changed = False
    for record in data.get("lotteries", {}).values():
        if not isinstance(record, dict) or record.get("status") != "open":
            continue
        published_at = int(record.get("published_at", 0) or 0)
        if not published_at or message_timestamp < published_at:
            continue
        if int(chat.id) not in _lottery_counted_chat_ids(record):
            continue
        counts = record.setdefault("message_counts", {})
        by_chat = counts.setdefault(str(chat.id), {})
        user_key = str(user.id)
        by_chat[user_key] = int(by_chat.get(user_key, 0) or 0) + 1
        changed = True
        matched_comment_rules = False
        if int(chat.id) == int(record["chat_id"]):
            matched_comment_rules = _record_comment_rule_matches(
                record, user.id, getattr(msg, "text", "") or ""
            )
            changed = matched_comment_rules or changed

        # A group message can complete either a comment rule or the final
        # required message-count threshold. Refresh just at those milestones
        # and send one private completion notice when every condition is met.
        participants = record.get("participants") or {}
        registration = participants.get(str(user.id)) if isinstance(participants, dict) else None
        should_refresh = matched_comment_rules or _activity_condition_threshold_reached(
            record, int(chat.id), user.id
        )
        if should_refresh and isinstance(registration, dict):
            eligible, _status_text = await _build_participation_status(context, user, record)
            if eligible and not registration.get("completion_notified_at"):
                if await _send_participation_completed_notice(context, user, record):
                    registration["completion_notified_at"] = int(time.time())
                    changed = True
    if changed:
        _save(context, data)


def _registration(record: dict, user) -> dict:
    participants = record.setdefault("participants", {})
    user_id = str(user.id)
    entry = participants.get(user_id)
    if not isinstance(entry, dict):
        entry = {
            "user_id": user.id,
            "name": user.full_name,
            "username": user.username,
            "registered_at": int(time.time()),
            "eligible": False,
            "passed_passwords": [],
        }
        participants[user_id] = entry
    else:
        entry["name"] = user.full_name
        entry["username"] = user.username
        entry.setdefault("registered_at", int(time.time()))
        entry.setdefault("eligible", False)
        if not isinstance(entry.get("passed_passwords"), list):
            entry["passed_passwords"] = []
    return entry


def _required_passwords(record: dict) -> list[str]:
    passwords = [str(record.get("requirements", {}).get("password") or "").strip()]
    passwords.extend(
        _target_requirements(item)["password"]
        for item in record.get("follow_targets", []) or []
        if isinstance(item, dict) and _target_is_group(item)
    )
    return list(dict.fromkeys(value for value in passwords if value))


async def _build_participation_status(
    context: ContextTypes.DEFAULT_TYPE,
    user,
    record: dict,
    *,
    submitted_password: str = "",
) -> tuple[bool, str]:
    """Register a user and render every completed/pending lottery requirement."""
    # Older records did not persist target chat_type. Refresh it before
    # evaluating group-only conditions such as talk count and password.
    for item in record.get("follow_targets", []) or []:
        if isinstance(item, dict):
            await _refresh_target_chat_type(context, item)

    registration = _registration(record, user)
    required_passwords = _required_passwords(record)
    passed = {
        str(value).strip()
        for value in registration.get("passed_passwords", [])
        if str(value).strip()
    }
    candidate = str(submitted_password or "").strip()
    if candidate and candidate in required_passwords:
        passed.add(candidate)
    registration["passed_passwords"] = sorted(passed)

    # A participant added by an authorized lottery manager is deliberately
    # exempt from every normal condition, including entry verification.
    if registration.get("manual_override"):
        registration["eligible"] = True
        registration["checked_at"] = int(time.time())
        participants = record.get("participants") or {}
        registered_count = sum(1 for item in participants.values() if isinstance(item, dict))
        eligible_count = sum(
            1 for item in participants.values()
            if isinstance(item, dict) and bool(item.get("eligible", False))
        )
        draw_time = _timestamp_text(int(record.get("draw_at", 0) or 0), context)
        lines = [
            "<b>您已成功报名抽奖（管理员直接添加）</b>",
            "",
            "⭐ 管理员已为您直接添加参与资格，无需完成其他参与条件。",
            f"添加人：{html.escape(_actor_label(registration.get('added_by')))}",
            "",
            f"🎰 <b>{record.get('name') or '抽奖'}</b>",
            "",
            "📜 <b>抽奖详情</b>",
            str(record.get("description") or "未填写"),
            "",
            "🎁 <b>奖品名单</b>",
        ]
        prizes = record.get("prizes", []) or []
        if prizes:
            for prize in prizes:
                if isinstance(prize, dict):
                    lines.append(
                        f"💰️ {prize.get('name', '未命名奖品')} × <b>{int(prize.get('count', 1) or 1)}</b>"
                    )
        else:
            lines.append("暂未设置奖品")
        lines.extend([
            "",
            f"💁 参与情况：{eligible_count}达标 / {registered_count}报名",
            "",
            f"📅 开奖时间：（{get_bot_timezone(context)}）",
            draw_time,
        ])
        return True, "\n".join(lines)

    eligible = True
    condition_lines = []
    entry_verification = _entry_verification_settings(record)
    if entry_verification["enabled"]:
        verified = _entry_verification_passed(record, user.id)
        condition_lines.append(
            f"🛡️ {_entry_verification_method_label(entry_verification['method'])} {'✅' if verified else '❌'}"
        )
        eligible = eligible and verified
    main_req = record.get("requirements") if isinstance(record.get("requirements"), dict) else {}
    has_main_requirements = bool(
        main_req.get("username")
        or int(main_req.get("min_messages", 0) or 0) > 0
        or str(main_req.get("password") or "").strip()
        or bool(_comment_rules(main_req))
    )
    if has_main_requirements:
        condition_lines.append("🎫 抽奖发布群 ✅")
        if main_req.get("username"):
            ok = bool(getattr(user, "username", None))
            condition_lines.append(f"└ 🪪 已设置用户名 {'✅' if ok else '❌'}")
            eligible = eligible and ok
        main_min = int(main_req.get("min_messages", 0) or 0)
        if main_min > 0:
            count = _lottery_message_count(record, int(record["chat_id"]), user.id)
            ok = count > main_min
            condition_lines.append(
                f"└ 🎟️ 发布后发言数大于 {main_min} 条 {'✅' if ok else '❌'}（{count}/{main_min}）"
            )
            eligible = eligible and ok
        main_password = str(main_req.get("password") or "").strip()
        if main_password:
            ok = main_password in passed
            condition_lines.append(f"└ 🔑 发送口令﹝{main_password}﹞ {'✅' if ok else '❌'}")
            eligible = eligible and ok
        comment_rules = _comment_rules(main_req)
        if comment_rules:
            completed = _matched_comment_rule_signatures(record, user.id)
            condition_lines.append("└ 💬 参与评论")
            for rule in comment_rules:
                signature = _comment_rule_signature(rule["key"], rule["value"])
                ok = signature in completed
                condition_lines.append(
                    f"　• key：{html.escape(rule['key'])} ｜ 值：{html.escape(rule['value'])} "
                    f"{'✅' if ok else '❌'}"
                )
                eligible = eligible and ok

    for item in record.get("follow_targets", []) or []:
        if not isinstance(item, dict):
            continue
        target = str(item.get("target") or "未设置")
        target_name = target.lstrip("@") or target
        joined = False
        chat = None
        try:
            chat = await context.bot.get_chat(_target_api_ref(target))
            member = await context.bot.get_chat_member(chat.id, user.id)
            status = getattr(member, "status", "")
            status = getattr(status, "value", status)
            joined = str(status).lower() not in {"left", "kicked"}
        except Exception:
            joined = False
        condition_lines.append(f"🎫 已加入-{target_name} {'✅' if joined else '❌'}")
        eligible = eligible and joined

        req = _target_requirements(item)
        if req["username"]:
            ok = bool(getattr(user, "username", None))
            condition_lines.append(f"└ 🪪 已设置用户名 {'✅' if ok else '❌'}")
            eligible = eligible and ok
        if _target_is_group(item):
            if req["min_messages"] > 0:
                count = _lottery_message_count(record, int(chat.id), user.id) if joined and chat else 0
                ok = count > req["min_messages"]
                condition_lines.append(
                    f"└ 🎟️ 发布后发言数大于 {req['min_messages']} 条 {'✅' if ok else '❌'}（{count}/{req['min_messages']}）"
                )
                eligible = eligible and ok
            if req["password"]:
                ok = req["password"] in passed
                condition_lines.append(f"└ 🔑 发送口令﹝{req['password']}﹞ {'✅' if ok else '❌'}")
                eligible = eligible and ok

    registration["eligible"] = bool(eligible)
    registration["checked_at"] = int(time.time())
    participants = record.get("participants") or {}
    registered_count = sum(1 for item in participants.values() if isinstance(item, dict))
    eligible_count = sum(
        1 for item in participants.values()
        if isinstance(item, dict) and bool(item.get("eligible", False))
    )
    status_title = "达标" if eligible else "未达标"
    status_note = "✅ 您已完成全部激活任务，可以参与抽奖！" if eligible else "❌ 您还需要完成激活任务才能抽奖！"
    draw_time = _timestamp_text(int(record.get("draw_at", 0) or 0), context)
    lines = [
        f"<b>您已成功报名抽奖（{status_title}）</b>",
        "",
        status_note,
        "",
        f"🎰 <b>{record.get('name') or '抽奖'}</b>",
        "",
        *(condition_lines or ["无需额外参与条件。"]),
    ]

    # Once the user is eligible, include the actual lottery information rather
    # than only the activation checklist.
    if eligible:
        lines.extend(["", "📜 <b>抽奖详情</b>", str(record.get("description") or "未填写")])
        lines.extend(["", "🎁 <b>奖品名单</b>"])
        prizes = record.get("prizes", []) or []
        if prizes:
            for prize in prizes:
                if isinstance(prize, dict):
                    lines.append(
                        f"💰️ {prize.get('name', '未命名奖品')} × <b>{int(prize.get('count', 1) or 1)}</b>"
                    )
        else:
            lines.append("暂未设置奖品")

    lines.extend([
        "",
        f"💁 参与情况：{eligible_count}达标 / {registered_count}报名",
        "",
        f"📅 开奖时间：（{get_bot_timezone(context)}）",
        draw_time,
    ])
    return bool(eligible), "\n".join(lines)


PARTICIPANT_PAGE_SIZE = 20


def _participant_entries(record: dict, *, eligible_only: bool = False) -> list[dict]:
    participants = record.get("participants") if isinstance(record.get("participants"), dict) else {}
    entries = [item for item in participants.values() if isinstance(item, dict)]
    if eligible_only:
        entries = [item for item in entries if bool(item.get("eligible", False))]
    return sorted(
        entries,
        key=lambda item: int(item.get("registered_at", 0) or 0),
        reverse=True,
    )


def _participants_text(
    record: dict, context: ContextTypes.DEFAULT_TYPE, page: int, *, eligible_only: bool = False
) -> tuple[str, int]:
    """Build one safe, paginated manager-facing participant list."""
    all_entries = _participant_entries(record)
    entries = _participant_entries(record, eligible_only=eligible_only)
    total = len(all_entries)
    eligible = sum(bool(item.get("eligible", False)) for item in all_entries)
    total_pages = max(1, (len(entries) + PARTICIPANT_PAGE_SIZE - 1) // PARTICIPANT_PAGE_SIZE)
    page = max(1, min(page, total_pages))
    visible = entries[(page - 1) * PARTICIPANT_PAGE_SIZE : page * PARTICIPANT_PAGE_SIZE]
    lines = [
        "👥 <b>参与用户</b>",
        f"🎰 {html.escape(str(record.get('name') or '未命名抽奖'))}",
        f"已报名：{total} 人 ｜ 达标：{eligible} 人",
        f"筛选：{'仅展示已达标' if eligible_only else '全部参与用户'}（当前 {len(entries)} 人）",
        f"第 {page}/{total_pages} 页",
    ]
    if not visible:
        lines.extend(["", "暂无符合当前筛选条件的用户。"])
    else:
        lines.append("")
        for index, entry in enumerate(visible, start=(page - 1) * PARTICIPANT_PAGE_SIZE + 1):
            try:
                user_id = int(entry.get("user_id"))
            except (TypeError, ValueError):
                user_id = 0
            name = html.escape(str(entry.get("name") or "未知用户")[:24])
            name_text = f'<a href="tg://user?id={user_id}">{name}</a>' if user_id else name
            username = str(entry.get("username") or "").strip().lstrip("@")
            username_text = f" ｜ @{html.escape(username)}" if username else ""
            registered_at = int(entry.get("registered_at", 0) or 0)
            registered_text = _timestamp_text(registered_at, context) if registered_at else "时间未知"
            lines.append(
                f"{index}. {'✅ 达标' if entry.get('eligible', False) else '⏳ 未达标'} {name_text}{username_text}"
            )
            lines.append(f"   <code>{user_id}</code> ｜ 报名于 {registered_text}")
            if entry.get("manual_override"):
                lines.append(
                    f"   ⭐ 管理员直接添加（免条件）｜由 {html.escape(_actor_label(entry.get('added_by')))} 添加"
                )
    return "\n".join(lines), page


def _participants_keyboard(
    record: dict,
    page: int,
    *,
    eligible_only: bool = False,
    can_manage_delegates: bool = False,
    return_to_manager_panel: bool = False,
) -> InlineKeyboardMarkup:
    all_entries = _participant_entries(record)
    entries = _participant_entries(record, eligible_only=eligible_only)
    eligible_count = sum(bool(item.get("eligible", False)) for item in all_entries)
    total_pages = max(1, (len(entries) + PARTICIPANT_PAGE_SIZE - 1) // PARTICIPANT_PAGE_SIZE)
    filter_value = "eligible" if eligible_only else "all"
    rows = [[InlineKeyboardButton(
        f"{'⬅️ 查看全部' if eligible_only else '✅ 仅展示已达标'}（{len(all_entries) if eligible_only else eligible_count}）",
        callback_data=f"{CALLBACK_PREFIX}:participants:{record['id']}:1:{'all' if eligible_only else 'eligible'}",
    )]]
    navigation = []
    if page > 1:
        navigation.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"{CALLBACK_PREFIX}:participants:{record['id']}:{page - 1}:{filter_value}"))
    if page < total_pages:
        navigation.append(InlineKeyboardButton("➡️ 下一页", callback_data=f"{CALLBACK_PREFIX}:participants:{record['id']}:{page + 1}:{filter_value}"))
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton("➕ 添加参与抽奖用户", callback_data=f"{CALLBACK_PREFIX}:manual_add:{record['id']}")])
    if can_manage_delegates:
        rows.append([InlineKeyboardButton("👮 添加用户管理人员", callback_data=f"{CALLBACK_PREFIX}:participant_managers:{record['id']}")])
    back_action = "manager_panel" if return_to_manager_panel else "view"
    back_label = "⬅️ 返回管理面板" if return_to_manager_panel else "⬅️ 返回抽奖详情"
    rows.append([InlineKeyboardButton(back_label, callback_data=f"{CALLBACK_PREFIX}:{back_action}:{record['id']}")])
    return InlineKeyboardMarkup(rows)


def _participation_status_keyboard(record: dict) -> InlineKeyboardMarkup:
    rows = []
    for item in record.get("follow_targets", []) or []:
        if not isinstance(item, dict):
            continue
        url = _target_join_url(item)
        if not url:
            continue
        label = str(item.get("title") or item.get("target") or "频道/群组")
        rows.append([InlineKeyboardButton(f"📢 关注/加入 {label[:48]}", url=url)])
    rows.append([
        InlineKeyboardButton("🔄 刷新参与条件", callback_data=f"{CALLBACK_PREFIX}:join_refresh:{record['id']}")
    ])
    return InlineKeyboardMarkup(rows)


async def event_lottery_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return
    parts = query.data.split(":")
    if len(parts) < 2:
        return
    action = parts[1]
    lottery_id = parts[2] if len(parts) > 2 else ""
    data = _load(context)

    if action in {"dashboard", "history", "create", "back"}:
        try:
            chat_id = int(lottery_id)
        except (TypeError, ValueError):
            return await query.answer("群ID无效。", show_alert=True)
        if action == "back":
            await query.answer()
            return await query.edit_message_text("已返回群设置。")
        if not await _can_manage(context, query.from_user.id, chat_id):
            return await query.answer("无管理权限。", show_alert=True)
        records = _records_for_chat(data, chat_id)
        if action == "dashboard":
            await query.answer()
            return await query.edit_message_text(_dashboard_text(records), reply_markup=_dashboard_keyboard(chat_id, records))
        if action == "history":
            await query.answer()
            return await query.edit_message_text(_dashboard_text(records, history=True), reply_markup=_dashboard_keyboard(chat_id, records, history=True))
        return await _create_lottery(query, context, chat_id, query.from_user.id)

    record = _record(data, lottery_id)
    if not record:
        return await query.answer("抽奖不存在或已删除。", show_alert=True)

    if action == "entry_verify":
        if record.get("status") != "open":
            return await query.answer("抽奖尚未开放或已结束。", show_alert=True)
        if not query.message or query.message.chat.type != "private":
            return await query.answer("请在机器人私聊中完成验证。", show_alert=True)
        try:
            target_user_id = int(parts[3])
            token = parts[4]
            selected_answer = int(parts[5])
        except (IndexError, TypeError, ValueError):
            return await query.answer("验证题无效，请重新点击参与抽奖。", show_alert=True)
        if query.from_user.id != target_user_id:
            return await query.answer("这不是你的验证题。", show_alert=True)
        key = _entry_verification_challenge_key(lottery_id, target_user_id)
        challenge = _entry_verification_challenges.get(key)
        if (
            not isinstance(challenge, dict)
            or int(challenge.get("expires_at", 0) or 0) < int(time.time())
            or challenge.get("token") != token
            or selected_answer not in challenge.get("options", set())
        ):
            _entry_verification_challenges.pop(key, None)
            return await query.answer("验证已过期，请重新点击参与抽奖。", show_alert=True)
        if selected_answer != challenge.get("answer"):
            return await query.answer("答案不正确，请重新选择。", show_alert=True)

        registration = _registration(record, query.from_user)
        registration["entry_verification"] = {
            "method": _entry_verification_settings(record)["method"],
            "passed_at": int(time.time()),
        }
        _entry_verification_challenges.pop(key, None)
        context.user_data[JOIN_KEY] = {"id": lottery_id, "source_chat_id": int(record["chat_id"])}
        _eligible, status_text = await _build_participation_status(context, query.from_user, record)
        _save(context, data)
        await query.answer("验证成功！")
        return await query.edit_message_text(
            status_text,
            parse_mode="HTML",
            reply_markup=_participation_status_keyboard(record),
        )

    if action == "time_immediate":
        if not await _can_manage(context, query.from_user.id, int(record["chat_id"])):
            return await query.answer("无管理权限。", show_alert=True)
        record["publish_at"] = 0
        record["updated_at"] = int(time.time())
        _save(context, data)
        stage = context.user_data.get(DRAFT_KEY)
        if isinstance(stage, dict) and stage.get("id") == lottery_id and stage.get("field") == "publish_at":
            context.user_data.pop(DRAFT_KEY, None)
        await query.answer("已设置为立即发布。")
        return await query.edit_message_text(
            _editor_text(record, context),
            reply_markup=_editor_keyboard(record),
        )

    if action == "clone":
        if not await _can_manage(context, query.from_user.id, int(record["chat_id"])):
            return await query.answer("无管理权限。", show_alert=True)
        clone = _new_lottery(int(record["chat_id"]), query.from_user.id)
        clone["name"] = (str(record.get("name") or "未命名抽奖") + "（副本）")[:80]
        clone["description"] = str(record.get("description") or "")
        clone["draw_at"] = 0
        clone["prizes"] = [dict(item) for item in record.get("prizes", []) if isinstance(item, dict)]
        clone["requirements"] = {
            **dict(record.get("requirements") or {}),
            "comment_rules": [
                dict(rule) for rule in _comment_rules(record.get("requirements") or {})
            ],
        }
        clone["entry_verification"] = dict(_entry_verification_settings(record))
        clone["follow_targets"] = [
            {**item, "requirements": dict(_target_requirements(item))}
            for item in record.get("follow_targets", [])
            if isinstance(item, dict)
        ]
        clone["sync_targets"] = list(record.get("sync_targets", []) or [])
        clone["cover"] = dict(record["cover"]) if isinstance(record.get("cover"), dict) else None
        # Publication schedule, participants and winners are deliberately reset.
        clone["publish_at"] = 0
        clone["status"] = "draft"
        data["lotteries"][clone["id"]] = clone
        _save(context, data)
        await query.answer("已根据历史设置创建草稿，不会自动发布。")
        return await query.edit_message_text(_editor_text(clone, context), reply_markup=_editor_keyboard(clone))

    if action == "view":
        if not await _can_manage(context, query.from_user.id, int(record["chat_id"])):
            return await query.answer("无查看抽奖详情权限。", show_alert=True)
        text = _format_lottery(record, context) + f"\n\n状态：{_status_label(record)}"
        rows = []
        if record.get("status") in {"draft", "scheduled", "open"}:
            rows.append([InlineKeyboardButton("✏️ 编辑抽奖", callback_data=f"{CALLBACK_PREFIX}:edit:{record['id']}")])
            rows.append([InlineKeyboardButton("🗑 删除抽奖", callback_data=f"{CALLBACK_PREFIX}:delete:{record['id']}")])
        participant_count = len(_participant_entries(record))
        rows.append([InlineKeyboardButton(
            f"👥 查看参与用户（{participant_count}）",
            callback_data=f"{CALLBACK_PREFIX}:participants:{record['id']}:1",
        )])
        rows.append([InlineKeyboardButton("📋 使用此设置新建", callback_data=f"{CALLBACK_PREFIX}:clone:{record['id']}")])
        rows.append([InlineKeyboardButton("⬅️ 返回抽奖列表", callback_data=f"{CALLBACK_PREFIX}:dashboard:{record['chat_id']}")])
        await query.answer()
        return await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
    if action == "participants":
        is_group_manager = await _can_manage(context, query.from_user.id, int(record["chat_id"]))
        if not (is_group_manager or _is_participant_manager(record, query.from_user.id)):
            return await query.answer("无查看参与用户权限。", show_alert=True)
        try:
            page = int(parts[3]) if len(parts) >= 4 else 1
        except (TypeError, ValueError):
            page = 1
        eligible_only = len(parts) >= 5 and parts[4] == "eligible"
        text, page = _participants_text(record, context, page, eligible_only=eligible_only)
        await query.answer()
        return await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=_participants_keyboard(
                record,
                page,
                eligible_only=eligible_only,
                can_manage_delegates=is_group_manager,
                return_to_manager_panel=not is_group_manager,
            ),
            disable_web_page_preview=True,
        )
    if action == "manager_panel":
        if not await _can_manage_participants(context, record, query.from_user.id):
            return await query.answer("无管理权限。", show_alert=True)
        await query.answer()
        return await query.edit_message_text(
            _participant_manager_card_text(record),
            parse_mode="HTML",
            reply_markup=_participant_manager_card_keyboard(record),
        )
    if action == "manual_add":
        if not await _can_manage_participants(context, record, query.from_user.id):
            return await query.answer("无添加参与用户权限。", show_alert=True)
        _start_editor_input_stage(query, context, lottery_id, "manual_participant")
        context.user_data[DRAFT_KEY]["participant_manager_input"] = True
        context.user_data[DRAFT_KEY]["return_to"] = "participants"
        await query.answer()
        return await query.edit_message_text(
            "请输入要直接参与抽奖的用户ID或用户名。\n"
            "用户名需是机器人已记录的抽奖发布群成员；使用用户ID可直接添加。\n"
            "该用户会免除所有参与条件。",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ 返回参与用户", callback_data=f"{CALLBACK_PREFIX}:participants:{lottery_id}:1")
            ]]),
        )
    if action == "participant_managers":
        if not await _can_manage(context, query.from_user.id, int(record["chat_id"])):
            return await query.answer("无管理权限。", show_alert=True)
        await query.answer()
        return await query.edit_message_text(
            _participant_manager_text(record), parse_mode="HTML", reply_markup=_participant_manager_keyboard(record)
        )
    if action == "participant_manager_add":
        if not await _can_manage(context, query.from_user.id, int(record["chat_id"])):
            return await query.answer("无管理权限。", show_alert=True)
        if len(_participant_managers(record)) >= MAX_PARTICIPANT_MANAGERS:
            return await query.answer(f"最多只能设置 {MAX_PARTICIPANT_MANAGERS} 名管理人员。", show_alert=True)
        _start_editor_input_stage(query, context, lottery_id, "participant_manager_add")
        context.user_data[DRAFT_KEY]["return_to"] = "participant_managers"
        await query.answer()
        return await query.edit_message_text(
            "请输入可添加抽奖用户的管理人员的用户ID或用户名。\n"
            "添加后会向该用户私聊发送本抽奖管理卡片。",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ 返回管理人员", callback_data=f"{CALLBACK_PREFIX}:participant_managers:{lottery_id}")
            ]]),
        )
    if action == "participant_manager_remove" and len(parts) >= 4:
        if not await _can_manage(context, query.from_user.id, int(record["chat_id"])):
            return await query.answer("无管理权限。", show_alert=True)
        _participant_managers(record).pop(str(parts[3]), None)
        record["updated_at"] = int(time.time())
        _save(context, data)
        await query.answer("已移除管理人员")
        return await query.edit_message_text(
            _participant_manager_text(record), parse_mode="HTML", reply_markup=_participant_manager_keyboard(record)
        )

    if action in {"discard", "delete"}:
        if not await _can_manage(context, query.from_user.id, int(record["chat_id"])):
            return await query.answer("无管理权限。", show_alert=True)
        if action == "discard" and record.get("status") != "draft":
            return await query.answer("草稿以外的抽奖请使用删除操作。", show_alert=True)
        data["lotteries"].pop(record["id"], None)
        _save(context, data)
        records = _records_for_chat(data, int(record["chat_id"]))
        note = "草稿已放弃，不会发布。" if action == "discard" else "抽奖已删除；已发出的历史消息不会自动撤回。"
        await query.answer(note)
        return await query.edit_message_text(_dashboard_text(records), reply_markup=_dashboard_keyboard(int(record["chat_id"]), records))

    if action == "edit":
        if not await _can_manage(context, query.from_user.id, int(record["chat_id"])):
            return await query.answer("无管理权限。", show_alert=True)
        await query.answer()
        return await query.edit_message_text(_editor_text(record, context), reply_markup=_editor_keyboard(record))
    if action == "entry_verification":
        if not await _can_manage(context, query.from_user.id, int(record["chat_id"])):
            return await query.answer("无管理权限。", show_alert=True)
        await query.answer()
        return await query.edit_message_text(
            _entry_verification_text(record),
            reply_markup=_entry_verification_keyboard(record),
        )
    if action == "entry_verify_toggle":
        if not await _can_manage(context, query.from_user.id, int(record["chat_id"])):
            return await query.answer("无管理权限。", show_alert=True)
        settings = _entry_verification_settings(record)
        settings["enabled"] = not settings["enabled"]
        if settings["enabled"]:
            _reset_entry_verification_for_participants(record)
        record["updated_at"] = int(time.time())
        _save(context, data)
        await query.answer("已开启参与前验证" if settings["enabled"] else "已关闭参与前验证")
        return await query.edit_message_text(
            _entry_verification_text(record),
            reply_markup=_entry_verification_keyboard(record),
        )
    if action == "entry_verify_method":
        if not await _can_manage(context, query.from_user.id, int(record["chat_id"])):
            return await query.answer("无管理权限。", show_alert=True)
        await query.answer()
        return await query.edit_message_text(
            _entry_verification_methods_text(record),
            reply_markup=_entry_verification_methods_keyboard(record),
        )
    if action == "entry_verify_select" and len(parts) >= 4:
        if not await _can_manage(context, query.from_user.id, int(record["chat_id"])):
            return await query.answer("无管理权限。", show_alert=True)
        method = parts[3]
        if method not in ENTRY_VERIFICATION_METHODS:
            return await query.answer("该验证方式不可用。", show_alert=True)
        settings = _entry_verification_settings(record)
        if settings["method"] != method:
            settings["method"] = method
            _reset_entry_verification_for_participants(record)
            record["updated_at"] = int(time.time())
            _save(context, data)
        await query.answer("已设置验证方式")
        return await query.edit_message_text(
            _entry_verification_text(record),
            reply_markup=_entry_verification_keyboard(record),
        )
    if action == "field":
        if not await _can_manage(context, query.from_user.id, int(record["chat_id"])):
            return await query.answer("无管理权限。", show_alert=True)
        field = parts[3] if len(parts) > 3 else ""
        _start_editor_input_stage(query, context, lottery_id, field)
        # These fields are opened from the primary participation-conditions
        # panel. Keep that page as the return destination after input.
        if field in {"min_messages", "password"}:
            context.user_data[DRAFT_KEY]["return_to"] = "main_requirements"
        prompts = {
            "name": "请输入抽奖名称。",
            "description": "请输入抽奖说明。",
            "publish_at": _time_input_prompt("publish_at", context),
            "draw_at": _time_input_prompt("draw_at", context),
            "min_messages": "请输入最低发言条数；输入 0 表示不限制。",
            "password": "请输入参与口令；发送“清空”清除口令。",
            "cover": "请发送抽奖封面（图片或视频），或发送“清空”不设置封面。",
        }
        rows = []
        if field == "publish_at":
            rows.append([InlineKeyboardButton(
                "⚡ 立即发布",
                callback_data=f"{CALLBACK_PREFIX}:time_immediate:{lottery_id}",
            )])
        if field in {"min_messages", "password"}:
            rows.append([InlineKeyboardButton(
                "⬅️ 返回参与条件", callback_data=f"{CALLBACK_PREFIX}:req_main:{lottery_id}"
            )])
        else:
            rows.append([InlineKeyboardButton(
                "⬅️ 返回编辑", callback_data=f"{CALLBACK_PREFIX}:edit:{lottery_id}"
            )])
        await query.answer()
        return await query.edit_message_text(
            prompts.get(field, "请输入设置内容。"),
            reply_markup=InlineKeyboardMarkup(rows),
        )
    if action == "prizes":
        if not await _can_manage(context, query.from_user.id, int(record["chat_id"])):
            return await query.answer("无管理权限。", show_alert=True)
        await query.answer()
        return await query.edit_message_text("🎁 奖品设置", reply_markup=_prizes_keyboard(record))
    if action == "prize_add":
        _start_editor_input_stage(query, context, lottery_id, "prize_add")
        await query.answer()
        return await query.edit_message_text("请输入奖品，格式：奖品名称 x 数量\n例如：手机 x 2", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ 返回奖品设置", callback_data=f"{CALLBACK_PREFIX}:prizes:{lottery_id}")]]))
    if action == "prize_delete" and len(parts) >= 4:
        index = int(parts[3])
        if 0 <= index < len(record.get("prizes", [])):
            record["prizes"].pop(index)
            record["updated_at"] = int(time.time())
            _save(context, data)
        await query.answer("已删除奖品")
        return await query.edit_message_text("🎁 奖品设置", reply_markup=_prizes_keyboard(record))
    if action == "requirements":
        await query.answer()
        return await query.edit_message_text(
            "📮 请选择要设置参与条件的群组：",
            reply_markup=_requirements_selector_keyboard(record),
        )
    if action == "req_main":
        stage = context.user_data.get(DRAFT_KEY)
        if (
            isinstance(stage, dict)
            and stage.get("id") == lottery_id
            and stage.get("field") in {"min_messages", "password"}
        ):
            context.user_data.pop(DRAFT_KEY, None)
        await query.answer()
        return await query.edit_message_text(
            "🎰 抽奖发布群的参与条件",
            reply_markup=_requirements_keyboard(record),
        )
    if action == "req_toggle" and len(parts) >= 4:
        key = parts[3]
        if key == "username":
            record.setdefault("requirements", {})["username"] = not bool(record["requirements"].get("username"))
            _save(context, data)
        await query.answer("已更新")
        return await query.edit_message_text("🎰 抽奖发布群的参与条件", reply_markup=_requirements_keyboard(record))
    if action == "comment_rules":
        if not await _can_manage(context, query.from_user.id, int(record["chat_id"])):
            return await query.answer("无管理权限。", show_alert=True)
        stage = context.user_data.get(DRAFT_KEY)
        if (
            isinstance(stage, dict)
            and stage.get("id") == lottery_id
            and stage.get("field") == "comment_rule_add"
        ):
            context.user_data.pop(DRAFT_KEY, None)
        await query.answer()
        return await query.edit_message_text(
            _comment_rules_text(record),
            parse_mode="HTML",
            reply_markup=_comment_rules_keyboard(record),
        )
    if action == "comment_rule_add":
        if not await _can_manage(context, query.from_user.id, int(record["chat_id"])):
            return await query.answer("无管理权限。", show_alert=True)
        rules = _comment_rules(record.setdefault("requirements", {}))
        if len(rules) >= MAX_COMMENT_RULES:
            return await query.answer(f"最多只能设置 {MAX_COMMENT_RULES} 条评论规则。", show_alert=True)
        _start_editor_input_stage(query, context, lottery_id, "comment_rule_add")
        context.user_data[DRAFT_KEY]["return_to"] = "comment_rules"
        await query.answer()
        return await query.edit_message_text(
            "请输入评论规则，格式：key | 值\n"
            "示例：飞机号 | iwoai\n\n"
            "用户需在抽奖发布群的同一条评论中同时包含 key 和值。",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ 返回评论规则", callback_data=f"{CALLBACK_PREFIX}:comment_rules:{lottery_id}")
            ]]),
        )
    if action == "comment_rule_delete" and len(parts) >= 4:
        if not await _can_manage(context, query.from_user.id, int(record["chat_id"])):
            return await query.answer("无管理权限。", show_alert=True)
        try:
            index = int(parts[3])
        except (TypeError, ValueError):
            return await query.answer("评论规则无效。", show_alert=True)
        requirements = record.setdefault("requirements", {})
        rules = _comment_rules(requirements)
        if not 0 <= index < len(rules):
            return await query.answer("评论规则不存在。", show_alert=True)
        removed = rules.pop(index)
        _remove_comment_rule_matches(record, removed["key"], removed["value"])
        record["updated_at"] = int(time.time())
        _save(context, data)
        await query.answer("已删除评论规则")
        return await query.edit_message_text(
            _comment_rules_text(record),
            parse_mode="HTML",
            reply_markup=_comment_rules_keyboard(record),
        )

    if action == "target_requirements" and len(parts) >= 4:
        index = int(parts[3])
        values = record.get("follow_targets", []) or []
        if not 0 <= index < len(values) or not isinstance(values[index], dict):
            return await query.answer("关注目标不存在。", show_alert=True)
        if await _refresh_target_chat_type(context, values[index]):
            _save(context, data)
        await query.answer()
        return await query.edit_message_text(
            _target_requirements_text(record, index),
            reply_markup=_target_requirements_keyboard(record, index),
        )
    if action == "target_req_toggle" and len(parts) >= 5:
        index, key = int(parts[3]), parts[4]
        values = record.get("follow_targets", []) or []
        if not 0 <= index < len(values) or not isinstance(values[index], dict):
            return await query.answer("关注目标不存在。", show_alert=True)
        req = _target_requirements(values[index])
        if key == "username":
            req["username"] = not req["username"]
            _set_target_requirements(values[index], req)
            _save(context, data)
        await query.answer("已更新")
        return await query.edit_message_text(
            _target_requirements_text(record, index),
            reply_markup=_target_requirements_keyboard(record, index),
        )
    if action == "target_req_field" and len(parts) >= 5:
        index, field = int(parts[3]), parts[4]
        values = record.get("follow_targets", []) or []
        if field not in {"min_messages", "password"} or not 0 <= index < len(values) or not isinstance(values[index], dict):
            return await query.answer("关注目标不存在。", show_alert=True)
        if not _target_is_group(values[index]):
            return await query.answer("频道不能设置发言条数或参与口令，只有群组可以设置。", show_alert=True)
        stage_field = f"target_req_{field}"
        _start_editor_input_stage(query, context, lottery_id, stage_field)
        context.user_data[DRAFT_KEY]["target_index"] = index
        context.user_data[DRAFT_KEY]["return_to"] = "target_requirements"
        prompt = "请输入该群最低发言条数；输入 0 表示不限制。" if field == "min_messages" else "请输入该群参与口令；发送“清空”清除口令。"
        await query.answer()
        return await query.edit_message_text(
            prompt,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ 返回群组条件", callback_data=f"{CALLBACK_PREFIX}:target_requirements:{lottery_id}:{index}")
            ]]),
        )
    if action == "targets" and len(parts) >= 4:
        kind = parts[3]
        await query.answer()
        title = "📢 关注频道/群组（点目标可设置该群参与条件）" if kind == "follow" else "📍 同步位置（从关注列表中选择）"
        return await query.edit_message_text(title, reply_markup=_targets_keyboard(record, kind))
    if action == "target_add" and len(parts) >= 4:
        kind = parts[3]
        if kind == "sync":
            await query.answer()
            return await query.edit_message_text("📍 同步位置（从关注列表中选择）", reply_markup=_targets_keyboard(record, "sync"))
        _start_editor_input_stage(query, context, lottery_id, "target_follow")
        await query.answer()
        return await query.edit_message_text(
            "请输入要关注的频道/群组：\n"
            "• 公开目标：@laoshibangdan 或 t.me/zhaolaoshiqun\n"
            "• 私有目标：发送频道/群组 ID，例如 -1001234567890\n\n"
            "机器人必须是目标管理员；私有目标还需要创建邀请链接的权限。",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ 返回关注列表", callback_data=f"{CALLBACK_PREFIX}:targets:{lottery_id}:follow")
            ]]),
        )
    if action == "sync_pick" and len(parts) >= 4:
        index = int(parts[3])
        follows = record.get("follow_targets", []) or []
        if not 0 <= index < len(follows) or not isinstance(follows[index], dict):
            return await query.answer("关注目标不存在。", show_alert=True)
        target = str(follows[index].get("target") or "")
        values = record.setdefault("sync_targets", [])
        if target and target not in values:
            if len(values) >= MAX_TARGETS:
                return await query.answer(f"最多只能设置 {MAX_TARGETS} 个同步位置。", show_alert=True)
            values.append(target)
            _save(context, data)
        await query.answer("已添加同步位置")
        return await query.edit_message_text("📍 同步位置（从关注列表中选择）", reply_markup=_targets_keyboard(record, "sync"))
    if action == "target_delete" and len(parts) >= 5:
        kind, index = parts[3], int(parts[4])
        key = "follow_targets" if kind == "follow" else "sync_targets"
        values = record.get(key, [])
        if 0 <= index < len(values):
            removed = values.pop(index)
            if kind == "follow":
                target = str((removed or {}).get("target") if isinstance(removed, dict) else removed)
                record["sync_targets"] = [item for item in record.get("sync_targets", []) if item != target]
            _save(context, data)
        await query.answer("已移除")
        title = "📢 关注频道/群组（点目标可设置该群参与条件）" if kind == "follow" else "📍 同步位置（从关注列表中选择）"
        return await query.edit_message_text(title, reply_markup=_targets_keyboard(record, kind))
    if action == "preview":
        if not await _can_manage(context, query.from_user.id, int(record["chat_id"])):
            return await query.answer("无管理权限。", show_alert=True)
        await query.answer()
        try:
            return await _send_lottery_content(
                context,
                query.message.chat_id,
                record,
                reply_markup=_preview_markup(record),
            )
        except Exception as exc:
            return await query.message.reply_text(f"❌ 无法预览：{exc}")
    if action == "preview_close":
        if not await _can_manage(context, query.from_user.id, int(record["chat_id"])):
            return await query.answer("无管理权限。", show_alert=True)
        await query.answer("预览已关闭")
        await _close_preview_message(query, context)
        return
    if action in {"publish", "preview_publish"}:
        if not await _can_manage(context, query.from_user.id, int(record["chat_id"])):
            return await query.answer("无管理权限。", show_alert=True)
        is_preview_publish = action == "preview_publish"
        if not record.get("name") or not record.get("prizes") or not record.get("draw_at"):
            return await query.answer("请至少设置名称、奖品和开奖时间。", show_alert=True)
        if record.get("status") in {"open", "drawn"}:
            return await query.answer("该抽奖已经发布，不能重复发布。", show_alert=True)
        record["updated_at"] = int(time.time())
        if record.get("publish_at"):
            record["status"] = "scheduled"
            _save(context, data)
            notice = "已安排定时发布"
        else:
            sent = await _send_lottery_post(context, record)
            if not sent:
                return await query.answer("发布失败，请确认机器人可在目标位置发言。", show_alert=True)
            record["status"] = "open"
            record["published_at"] = int(time.time())
            record["message_counts"] = {}
            record["comment_rule_matches"] = {}
            _save(context, data)
            notice = "已立即发布"
        if is_preview_publish:
            await query.answer(f"{notice}，预览已关闭")
            await _close_preview_message(query, context)
            return
        await query.answer(notice)
        return await query.edit_message_text(_editor_text(record, context), reply_markup=_editor_keyboard(record))
    if action == "join":
        if record.get("status") != "open":
            return await query.answer("抽奖尚未开放或已结束。", show_alert=True)
        link = await _lottery_deep_link(context, lottery_id)
        if link:
            # Telegram opens the deep link directly from the callback response;
            # do not leave an extra prompt/button in the lottery group.
            return await query.answer(url=link)
        return await query.answer("机器人未设置用户名，暂时无法跳转私聊。", show_alert=True)
    if action == "join_refresh":
        if record.get("status") != "open":
            return await query.answer("抽奖尚未开放或已结束。", show_alert=True)
        await query.answer("已刷新")
        _eligible, status_text = await _build_participation_status(context, query.from_user, record)
        _save(context, data)
        return await query.edit_message_text(
            status_text,
            parse_mode="HTML",
            reply_markup=_participation_status_keyboard(record),
        )



async def _handle_editor_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    stage = context.user_data.get(DRAFT_KEY)
    if not isinstance(stage, dict) or not update.message or not update.effective_user:
        return False
    if not update.effective_chat or update.effective_chat.type != "private":
        return False
    data = _load(context)
    record = _record(data, stage.get("id"))
    is_group_manager = bool(record) and await _can_manage(context, update.effective_user.id, int(record["chat_id"]))
    is_participant_manager = bool(record) and _is_participant_manager(record, update.effective_user.id)
    can_handle_stage = is_group_manager or (
        bool(stage.get("participant_manager_input")) and is_participant_manager
    )
    if not record or not can_handle_stage:
        context.user_data.pop(DRAFT_KEY, None)
        await update.message.reply_text("❌ 抽奖不存在或你没有管理权限。")
        return True
    field = str(stage.get("field") or "")
    text = (update.message.text or "").strip()
    if text in {"取消", "返回"}:
        context.user_data.pop(DRAFT_KEY, None)
        await _delete_editor_input_prompt(context, stage)
        if stage.get("return_to") == "comment_rules":
            await update.message.reply_text(
                _comment_rules_text(record),
                parse_mode="HTML",
                reply_markup=_comment_rules_keyboard(record),
            )
            return True
        if stage.get("return_to") == "main_requirements":
            await update.message.reply_text(
                "🎰 抽奖发布群的参与条件",
                reply_markup=_requirements_keyboard(record),
            )
            return True
        if stage.get("return_to") == "target_requirements":
            index = int(stage.get("target_index", -1))
            values = record.get("follow_targets", []) or []
            if 0 <= index < len(values) and isinstance(values[index], dict):
                await update.message.reply_text(
                    _target_requirements_text(record, index),
                    reply_markup=_target_requirements_keyboard(record, index),
                )
                return True
        await update.message.reply_text(_editor_text(record, context), reply_markup=_editor_keyboard(record))
        return True

    req = record.setdefault("requirements", {})
    try:
        if field == "name":
            if not text:
                raise ValueError("抽奖名称不能为空。")
            record["name"] = text[:80]
        elif field == "description":
            if not text:
                raise ValueError("抽奖说明不能为空。")
            record["description"] = text[:3500]
        elif field == "publish_at":
            parsed = _parse_time(text, context)
            if parsed is None:
                raise ValueError("请输入“立即”或未来时间 YYYY-MM-DD HH:MM。")
            record["publish_at"] = parsed
        elif field == "draw_at":
            parsed = _parse_time(text, context)
            if not parsed:
                raise ValueError("请输入未来开奖时间 YYYY-MM-DD HH:MM。")
            record["draw_at"] = parsed
        elif field == "min_messages":
            if not text.isdigit():
                raise ValueError("发言条数必须是非负整数。")
            req["min_messages"] = min(100000, int(text))
        elif field == "password":
            req["password"] = "" if text in {"清空", "关闭"} else text[:80]
        elif field == "cover":
            if text in {"清空", "关闭"}:
                record["cover"] = None
            else:
                cover = build_message_payload(update.message)
                if cover.get("type") not in {"photo", "video"}:
                    raise ValueError("抽奖封面仅支持图片或视频。")
                record["cover"] = cover
        elif field == "prize_add":
            match = re.fullmatch(r"(.+?)\s*[xX×]\s*(\d+)\s*", text)
            if not match:
                raise ValueError("格式：奖品名称 x 数量，例如：手机 x 2")
            name, count_raw = (match.group(1).strip(), match.group(2))
            if not name or int(count_raw) <= 0:
                raise ValueError("奖品格式或数量不正确。")
            if len(record.get("prizes", [])) >= MAX_PRIZES:
                raise ValueError(f"最多只能设置 {MAX_PRIZES} 项奖品。")
            record.setdefault("prizes", []).append({"name": name[:80], "count": int(count_raw)})
        elif field == "comment_rule_add":
            parts = re.split(r"[|｜]", text, maxsplit=1)
            if len(parts) != 2:
                raise ValueError("格式：key | 值，例如：飞机号 | iwoai")
            key, value = (part.strip()[:80] for part in parts)
            if not key or not value:
                raise ValueError("key 和值都不能为空。")
            rules = _comment_rules(req)
            if len(rules) >= MAX_COMMENT_RULES:
                raise ValueError(f"最多只能设置 {MAX_COMMENT_RULES} 条评论规则。")
            signature = _comment_rule_signature(key, value)
            if any(_comment_rule_signature(item["key"], item["value"]) == signature for item in rules):
                raise ValueError("相同的评论规则已经存在。")
            rules.append({"key": key, "value": value})
        elif field == "manual_participant":
            target = _resolve_known_lottery_user(record, text)
            user_id = str(target["user_id"])
            existing = record.setdefault("participants", {}).get(user_id, {})
            existing = existing if isinstance(existing, dict) else {}
            record.setdefault("participants", {})[user_id] = {
                **existing,
                **target,
                "registered_at": int(existing.get("registered_at", 0) or time.time()),
                "eligible": True,
                "manual_override": True,
                "added_by": _actor_snapshot(update.effective_user),
                "added_at": int(time.time()),
            }
        elif field == "participant_manager_add":
            target = _resolve_known_lottery_user(record, text)
            managers = _participant_managers(record)
            if str(target["user_id"]) in managers:
                raise ValueError("该用户已经是本抽奖的管理人员。")
            if len(managers) >= MAX_PARTICIPANT_MANAGERS:
                raise ValueError(f"最多只能设置 {MAX_PARTICIPANT_MANAGERS} 名管理人员。")
            managers[str(target["user_id"])] = {
                **target,
                "added_by": _actor_snapshot(update.effective_user),
                "added_at": int(time.time()),
            }
        elif field == "target_follow":
            if len(record.get("follow_targets", [])) >= MAX_TARGETS:
                raise ValueError(f"最多只能设置 {MAX_TARGETS} 个关注目标。")
            target = _normalize_target(text)
            if not target:
                raise ValueError("请输入公开 @频道/@群组，或私有频道/群组 ID。")
            target_chat = await _get_admin_target_chat(context, target)
            if target_chat is None:
                raise ValueError("机器人不是该频道或群组的管理员，无法添加为关注目标。")
            chat_type = getattr(target_chat, "type", "")
            chat_type = getattr(chat_type, "value", chat_type)
            chat_type = str(chat_type).lower()
            if chat_type not in {"channel", "group", "supergroup"}:
                raise ValueError("仅支持频道、群组或超级群组作为关注目标。")
            values = record.setdefault("follow_targets", [])
            if any(item.get("target") == target for item in values if isinstance(item, dict)):
                raise ValueError("该关注目标已经存在。")
            invite_link = ""
            if PRIVATE_CHAT_ID_RE.fullmatch(target):
                try:
                    invite = await context.bot.create_chat_invite_link(
                        chat_id=int(target), name="抽奖关注",
                    )
                    invite_link = str(getattr(invite, "invite_link", "") or "").strip()
                    if not invite_link:
                        raise ValueError("未获得邀请链接")
                except Exception as exc:
                    raise ValueError(
                        "无法为私有目标创建邀请链接；请确认机器人拥有邀请用户的管理员权限。"
                    ) from exc
            values.append({
                "target": target,
                "title": str(getattr(target_chat, "title", "") or getattr(target_chat, "username", "") or target)[:128],
                "chat_id": int(target_chat.id),
                "chat_type": chat_type,
                "invite_link": invite_link,
                "requirements": {"username": False, "min_messages": 0, "password": ""},
            })
        elif field in {"target_req_min_messages", "target_req_password"}:
            index = int(stage.get("target_index", -1))
            values = record.get("follow_targets", []) or []
            if not 0 <= index < len(values) or not isinstance(values[index], dict):
                raise ValueError("关注目标不存在。")
            if not _target_is_group(values[index]):
                raise ValueError("频道不能设置发言条数或参与口令，只有群组可以设置。")
            target_req = _target_requirements(values[index])
            if field == "target_req_min_messages":
                if not text.isdigit():
                    raise ValueError("发言条数必须是非负整数。")
                target_req["min_messages"] = min(100000, int(text))
            else:
                target_req["password"] = "" if text in {"清空", "关闭"} else text[:80]
            _set_target_requirements(values[index], target_req)
        else:
            raise ValueError("未知抽奖设置项。")
    except Exception as exc:
        await update.message.reply_text(f"❌ {exc}")
        return True

    record["updated_at"] = int(time.time())
    _save(context, data)
    context.user_data.pop(DRAFT_KEY, None)
    await _delete_editor_input_prompt(context, stage)
    if stage.get("return_to") == "comment_rules":
        await update.message.reply_text(
            _comment_rules_text(record),
            parse_mode="HTML",
            reply_markup=_comment_rules_keyboard(record),
        )
        return True
    if stage.get("return_to") == "main_requirements":
        await update.message.reply_text(
            "🎰 抽奖发布群的参与条件",
            reply_markup=_requirements_keyboard(record),
        )
        return True
    if stage.get("return_to") == "target_requirements":
        index = int(stage.get("target_index", -1))
        values = record.get("follow_targets", []) or []
        if 0 <= index < len(values) and isinstance(values[index], dict):
            await update.message.reply_text(
                _target_requirements_text(record, index),
                reply_markup=_target_requirements_keyboard(record, index),
            )
            return True
    if stage.get("return_to") == "participants":
        participants_text, page = _participants_text(record, context, 1)
        await update.message.reply_text(
            participants_text,
            parse_mode="HTML",
            reply_markup=_participants_keyboard(
                record,
                page,
                can_manage_delegates=is_group_manager,
                return_to_manager_panel=not is_group_manager,
            ),
        )
        return True
    if stage.get("return_to") == "participant_managers":
        manager = _participant_managers(record).get(str(target["user_id"]))
        delivered = await _send_participant_manager_card(context, record, manager)
        notice = "已添加管理人员，并已发送抽奖管理卡片。" if delivered else (
            "已添加管理人员，但无法私聊发送管理卡片；请让该用户先私聊机器人并发送 /start。"
        )
        await update.message.reply_text(notice)
        await update.message.reply_text(
            _participant_manager_text(record),
            parse_mode="HTML",
            reply_markup=_participant_manager_keyboard(record),
        )
        return True
    await update.message.reply_text(_editor_text(record, context), reply_markup=_editor_keyboard(record))
    return True


async def event_lottery_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Open a lottery registration/status card from t.me/<bot>?start=eventlot_<id>."""
    if not update.message or not update.effective_user or not update.effective_chat:
        return
    if update.effective_chat.type != "private" or not context.args:
        return
    parameter = str(context.args[0] or "")
    if not parameter.startswith("eventlot_"):
        return
    lottery_id = parameter[len("eventlot_"):]
    data = _load(context)
    record = _record(data, lottery_id)
    if not record or record.get("status") != "open":
        await update.message.reply_text("❌ 该抽奖不存在、尚未开放或已经结束。")
        raise ApplicationHandlerStop

    context.user_data[JOIN_KEY] = {"id": lottery_id, "source_chat_id": int(record["chat_id"])}
    verification = _entry_verification_settings(record)
    if verification["enabled"] and not _entry_verification_passed(record, update.effective_user.id):
        if verification["method"] == "numeric":
            question, keyboard = _create_numeric_entry_verification_challenge(
                record, update.effective_user.id
            )
            _save(context, data)
            await update.message.reply_text(
                "🛡️ 请先完成抽奖参与前验证。\n\n"
                f"验证方式：{_entry_verification_method_label(verification['method'])}\n"
                f"题目：{question}\n请选择正确答案：",
                reply_markup=keyboard,
            )
            raise ApplicationHandlerStop
        await update.message.reply_text("❌ 该抽奖的验证方式暂不可用，请联系管理员。")
        raise ApplicationHandlerStop

    _eligible, status_text = await _build_participation_status(context, update.effective_user, record)
    _save(context, data)
    await update.message.reply_text(
        status_text,
        parse_mode="HTML",
        reply_markup=_participation_status_keyboard(record),
    )
    raise ApplicationHandlerStop


async def event_lottery_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _handle_editor_input(update, context):
        # Prevent the participant-management value from being forwarded by the
        # generic private two-way bot after the lottery flow has consumed it.
        raise ApplicationHandlerStop
    if not update.effective_user or not update.effective_chat or not update.message:
        return

    chat = update.effective_chat
    code = (update.message.text or "").strip()
    data = _load(context)
    state = context.user_data.get(JOIN_KEY)
    record = _record(data, state.get("id")) if isinstance(state, dict) else None

    # A matching password in the original lottery group can also resume the
    # flow when the user has not opened the private card in this session yet.
    if not isinstance(record, dict) and chat.type in {"group", "supergroup"}:
        matches = [
            item
            for item in data.get("lotteries", {}).values()
            if isinstance(item, dict)
            and item.get("status") == "open"
            and int(item.get("chat_id", 0) or 0) == int(chat.id)
            and code in _required_passwords(item)
        ]
        if len(matches) == 1:
            record = matches[0]
            context.user_data[JOIN_KEY] = {
                "id": record["id"],
                "source_chat_id": int(record["chat_id"]),
            }

    if not isinstance(record, dict) or record.get("status") != "open":
        if isinstance(state, dict):
            context.user_data.pop(JOIN_KEY, None)
        return

    # Do not allow a password sent in either the group or private chat to
    # bypass the optional entry-verification gate. In private chat, show a
    # fresh numeric question so the user can continue without reopening the
    # deep link; group messages remain silent as before.
    verification = _entry_verification_settings(record)
    if verification["enabled"] and not _entry_verification_passed(record, update.effective_user.id):
        if chat.type == "private" and verification["method"] == "numeric":
            question, keyboard = _create_numeric_entry_verification_challenge(
                record, update.effective_user.id
            )
            _save(context, data)
            await update.message.reply_text(
                "🛡️ 请先完成抽奖参与前验证。\n\n"
                f"验证方式：{_entry_verification_method_label(verification['method'])}\n"
                f"题目：{question}\n请选择正确答案：",
                reply_markup=keyboard,
            )
            raise ApplicationHandlerStop
        return

    is_private = chat.type == "private"
    is_lottery_group = int(chat.id) == int(record["chat_id"])
    # Ordinary messages remain silent. A matching participation password may be
    # sent either to the bot privately or in the original lottery group.
    if not (is_private or is_lottery_group) or code not in _required_passwords(record):
        return

    _eligible, status_text = await _build_participation_status(
        context,
        update.effective_user,
        record,
        submitted_password=code,
    )
    _save(context, data)

    if is_private:
        await update.message.reply_text(
            status_text,
            parse_mode="HTML",
            reply_markup=_participation_status_keyboard(record),
        )
        raise ApplicationHandlerStop

    # The matching code was sent in the lottery group. Keep the detailed card
    # in the bot private chat and leave only a minimal confirmation in-group.
    try:
        await context.bot.send_message(
            chat_id=update.effective_user.id,
            text=status_text,
            parse_mode="HTML",
            reply_markup=_participation_status_keyboard(record),
        )
        return await update.message.reply_text("✅ 已收到参与口令，报名进度已更新，请到机器人私聊查看。")
    except Exception:
        return await update.message.reply_text(
            "✅ 已收到参与口令。请先点击抽奖消息的“参与抽奖”进入机器人私聊查看报名进度。"
        )


def register_event_lottery_handlers(app):
    # Run before the generic verification /start handler so eventlot deep links
    # open the private participation card instead of the ordinary welcome text.
    app.add_handler(
        CommandHandler("start", event_lottery_start, filters=filters.ChatType.PRIVATE),
        group=-100,
    )
    app.add_handler(CallbackQueryHandler(event_lottery_callback, pattern=rf"^{CALLBACK_PREFIX}:"))
    # Take private lottery-management input before bot.py's group=0
    # bidirectional-forward handler. Unrelated messages still fall through.
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (~filters.COMMAND), event_lottery_text), group=-18)
    # This must run before the general group text interceptors. In particular,
    # member-tag handling also occupies group -19; PTB invokes only one matching
    # handler per group, which previously prevented lottery messages from being
    # counted at all.
    app.add_handler(
        MessageHandler(filters.ChatType.GROUPS & filters.TEXT & (~filters.COMMAND), event_lottery_message_counter),
        group=-29,
    )
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT & (~filters.COMMAND), event_lottery_text), group=-18)
    app.job_queue.run_repeating(event_lottery_tick, interval=60, first=60, name="event_lottery_tick")
