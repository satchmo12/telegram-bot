# game/action_handler.py
import json
import logging
import os
import random
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, User
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes
from command_router import FEATURE_FRIENDS, feature_required, register_command
from info.economy import ensure_user_exists
from telegram.helpers import mention_html
from datetime import datetime
from utils import (
    ACTIONS_FILE,
    COOLDOWN_FILE,
    INFO_FILE,
    group_allowed,
    is_super_admin,
    safe_reply,
)
import random, time
from info.economy import change_user_attribute
from utils import load_json, save_json

ADD_ACTION_CB_PREFIX = "aa"
ADD_ACTION_DRAFT_KEY = "add_action_draft"
ATTR_CHOICES = [
    ("balance", "金币"),
    ("charm", "魅力"),
    ("stamina", "体力"),
    ("mood", "心情"),
    ("hunger", "饥饿"),
]
COST_VALUE_CHOICES = [1, 2, 5, 10, 20, 50, 100]
EFFECT_VALUE_CHOICES = [1, 2, 5, 10, 20, 50, 100]
COOLDOWN_CHOICES = [0, 60, 120, 300, 600, 1800, 3600]
ATTR_LABEL_MAP = dict(ATTR_CHOICES)


def get_actions() -> dict:
    data = load_json(ACTIONS_FILE)
    return data if isinstance(data, dict) else {}


def _new_add_action_draft(name: str, chat_id: str) -> dict:
    return {
        "name": name.strip(),
        "chat_id": str(chat_id),
        "type": 1,
        "cost_attr": None,
        "cost_value": 0,
        "effect_attr": "charm",
        "effect_sign": 1,
        "effect_value": 1,
        "cooldown": 120,
    }


def _set_draft(context: ContextTypes.DEFAULT_TYPE, draft: dict):
    context.user_data[ADD_ACTION_DRAFT_KEY] = draft


def _get_draft(context: ContextTypes.DEFAULT_TYPE) -> dict:
    data = context.user_data.get(ADD_ACTION_DRAFT_KEY, {})
    return data if isinstance(data, dict) else {}


def _clear_draft(context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop(ADD_ACTION_DRAFT_KEY, None)


def _kb(rows):
    return InlineKeyboardMarkup(rows)


def _format_attr(attr_key: str) -> str:
    return ATTR_LABEL_MAP.get(attr_key, attr_key)


def _build_mode_keyboard():
    return _kb(
        [
            [
                InlineKeyboardButton(
                    "👤 对自己操作", callback_data=f"{ADD_ACTION_CB_PREFIX}:mode:1"
                ),
                InlineKeyboardButton(
                    "🎯 回复别人操作", callback_data=f"{ADD_ACTION_CB_PREFIX}:mode:2"
                ),
            ],
            [InlineKeyboardButton("❌ 取消", callback_data=f"{ADD_ACTION_CB_PREFIX}:cancel")],
        ]
    )


def _build_attr_keyboard(action: str, allow_skip: bool = False):
    rows = []
    row = []
    for key, label in ATTR_CHOICES:
        row.append(
            InlineKeyboardButton(
                label, callback_data=f"{ADD_ACTION_CB_PREFIX}:{action}:{key}"
            )
        )
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if allow_skip:
        rows.append(
            [
                InlineKeyboardButton(
                    "⏭️ 不扣除", callback_data=f"{ADD_ACTION_CB_PREFIX}:{action}:none"
                )
            ]
        )
    rows.append([InlineKeyboardButton("❌ 取消", callback_data=f"{ADD_ACTION_CB_PREFIX}:cancel")])
    return _kb(rows)


def _build_value_keyboard(action: str, values: list[int]):
    rows = []
    row = []
    for val in values:
        row.append(
            InlineKeyboardButton(
                str(val), callback_data=f"{ADD_ACTION_CB_PREFIX}:{action}:{val}"
            )
        )
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("❌ 取消", callback_data=f"{ADD_ACTION_CB_PREFIX}:cancel")])
    return _kb(rows)


def _build_sign_keyboard():
    return _kb(
        [
            [
                InlineKeyboardButton(
                    "➕ 增加", callback_data=f"{ADD_ACTION_CB_PREFIX}:sign:1"
                ),
                InlineKeyboardButton(
                    "➖ 减少", callback_data=f"{ADD_ACTION_CB_PREFIX}:sign:-1"
                ),
            ],
            [InlineKeyboardButton("❌ 取消", callback_data=f"{ADD_ACTION_CB_PREFIX}:cancel")],
        ]
    )


def _build_confirm_keyboard(overwrite: bool = False):
    if overwrite:
        return _kb(
            [
                [
                    InlineKeyboardButton(
                        "✅ 覆盖保存",
                        callback_data=f"{ADD_ACTION_CB_PREFIX}:confirm:overwrite",
                    ),
                    InlineKeyboardButton(
                        "❌ 取消", callback_data=f"{ADD_ACTION_CB_PREFIX}:cancel"
                    ),
                ]
            ]
        )
    return _kb(
        [
            [
                InlineKeyboardButton(
                    "✅ 确认保存", callback_data=f"{ADD_ACTION_CB_PREFIX}:confirm:new"
                ),
                InlineKeyboardButton("❌ 取消", callback_data=f"{ADD_ACTION_CB_PREFIX}:cancel"),
            ]
        ]
    )


def _draft_to_config(draft: dict) -> dict:
    cfg = {
        "type": int(draft.get("type", 1)),
        "cooldown": int(draft.get("cooldown", 120)),
        "success_rate": 1.0,
    }

    cost_attr = draft.get("cost_attr")
    cost_value = int(draft.get("cost_value", 0))
    if cost_attr and cost_value > 0:
        cfg[cost_attr] = -abs(cost_value)

    effect_attr = draft.get("effect_attr")
    effect_sign = int(draft.get("effect_sign", 1))
    effect_value = abs(int(draft.get("effect_value", 1)))
    effect_delta = effect_value * (1 if effect_sign >= 0 else -1)
    if int(draft.get("type", 1)) == 2:
        effect_attr = f"target_{effect_attr}"
    cfg[effect_attr] = effect_delta
    return cfg


def _build_preview_text(draft: dict) -> str:
    op_mode = "对自己操作" if int(draft.get("type", 1)) == 1 else "回复别人操作"
    cost_attr = draft.get("cost_attr")
    cost_text = "无"
    if cost_attr and int(draft.get("cost_value", 0)) > 0:
        cost_text = f"{_format_attr(cost_attr)} -{int(draft['cost_value'])}"

    effect_sign = "增加" if int(draft.get("effect_sign", 1)) >= 0 else "减少"
    effect_target = "自己" if int(draft.get("type", 1)) == 1 else "对方"
    effect_text = (
        f"{effect_target}{_format_attr(draft.get('effect_attr', 'charm'))} "
        f"{effect_sign} {abs(int(draft.get('effect_value', 1)))}"
    )
    cooldown = int(draft.get("cooldown", 120))
    return (
        f"🧩 行为名：{draft.get('name')}\n"
        f"🎯 操作类型：{op_mode}\n"
        f"🧾 扣除：{cost_text}\n"
        f"📈 操作结果：{effect_text}\n"
        f"⏱️ 冷却：{cooldown}s"
    )


# register_command


# /buy 回复某人购买奴隶
async def apply_action(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_chat.type not in ("group", "supergroup"):
        return

    # if not update.message or not update.message.reply_to_message:
    #     return await safe_reply(update, context,  "请回复你要购买的用户。")

    text = update.message.text.strip()
    if not text:
        return
    buyer = update.effective_user
    target_id = None

    chat_id = str(update.effective_chat.id)
    user_id = str(buyer.id)

    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
        target_id = str(update.message.reply_to_message.from_user.id)
        ensure_user_exists(chat_id, target_id, target.full_name)

    if user_id == target_id:
        return
        # return await safe_reply(update, context, "不能对自己操作")

    ensure_user_exists(chat_id, user_id, buyer.full_name)

    userData = load_json(INFO_FILE)
    user_info = userData.get(chat_id, {}).get("users", {}).get(user_id)

    result = apply_action_effects(chat_id, user_id, user_info, text, target_id)
    if result:
        await update.message.reply_text(result)
        return True

    return False


@register_command("添加行为")
@feature_required(FEATURE_FRIENDS)
async def add_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super_admin(update.effective_user.id):
        return await update.message.reply_text("❌ 此行为仅限高级管理员")

    args = update.message.text.split(" ", 2)
    # 兼容旧用法：添加行为 行为名 JSON配置
    if len(args) >= 3:
        name = args[1]
        try:
            config = json.loads(args[2])
        except json.JSONDecodeError:
            return await update.message.reply_text("⚠️ JSON 格式错误")

        actions = get_actions()
        actions[name] = config
        save_json(ACTIONS_FILE, actions)
        return await update.message.reply_text(f"✅ 已添加行为：{name}")

    if len(args) < 2 or not args[1].strip():
        return await update.message.reply_text("❗ 用法：添加行为 行为名")

    action_name = args[1].strip()
    draft = _new_add_action_draft(action_name, str(update.effective_chat.id))
    _set_draft(context, draft)
    await update.message.reply_text(
        f"开始配置行为「{action_name}」\n请选择操作类型：",
        reply_markup=_build_mode_keyboard(),
    )


async def add_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()

    if not update.effective_user or not is_super_admin(update.effective_user.id):
        return await query.edit_message_text("❌ 此行为仅限高级管理员")

    parts = query.data.split(":")
    if len(parts) < 2 or parts[0] != ADD_ACTION_CB_PREFIX:
        return

    draft = _get_draft(context)
    action = parts[1]
    if action == "cancel":
        _clear_draft(context)
        return await query.edit_message_text("已取消本次操作。")

    if not draft:
        return await query.edit_message_text("⚠️ 当前没有进行中的配置，请先发送：添加行为 行为名")

    if str(update.effective_chat.id) != str(draft.get("chat_id", "")):
        return await query.edit_message_text("⚠️ 请在发起配置的同一个群里继续操作。")

    if action == "mode" and len(parts) == 3:
        draft["type"] = 1 if parts[2] == "1" else 2
        _set_draft(context, draft)
        return await query.edit_message_text(
            f"{_build_preview_text(draft)}\n\n请选择扣除属性：",
            reply_markup=_build_attr_keyboard("cost_attr", allow_skip=True),
        )

    if action == "cost_attr" and len(parts) == 3:
        selected = parts[2]
        if selected == "none":
            draft["cost_attr"] = None
            draft["cost_value"] = 0
            _set_draft(context, draft)
            return await query.edit_message_text(
                f"{_build_preview_text(draft)}\n\n请选择操作结果的属性：",
                reply_markup=_build_attr_keyboard("effect_attr", allow_skip=False),
            )
        if selected not in ATTR_LABEL_MAP:
            return await query.edit_message_text("❌ 无效属性")
        draft["cost_attr"] = selected
        _set_draft(context, draft)
        return await query.edit_message_text(
            f"{_build_preview_text(draft)}\n\n请选择扣除数值：",
            reply_markup=_build_value_keyboard("cost_value", COST_VALUE_CHOICES),
        )

    if action == "cost_value" and len(parts) == 3:
        draft["cost_value"] = abs(int(parts[2]))
        _set_draft(context, draft)
        return await query.edit_message_text(
            f"{_build_preview_text(draft)}\n\n请选择操作结果的属性：",
            reply_markup=_build_attr_keyboard("effect_attr", allow_skip=False),
        )

    if action == "effect_attr" and len(parts) == 3:
        selected = parts[2]
        if selected not in ATTR_LABEL_MAP:
            return await query.edit_message_text("❌ 无效属性")
        draft["effect_attr"] = selected
        _set_draft(context, draft)
        return await query.edit_message_text(
            f"{_build_preview_text(draft)}\n\n请选择结果方向：",
            reply_markup=_build_sign_keyboard(),
        )

    if action == "sign" and len(parts) == 3:
        draft["effect_sign"] = 1 if parts[2] == "1" else -1
        _set_draft(context, draft)
        return await query.edit_message_text(
            f"{_build_preview_text(draft)}\n\n请选择结果数值：",
            reply_markup=_build_value_keyboard("effect_value", EFFECT_VALUE_CHOICES),
        )

    if action == "effect_value" and len(parts) == 3:
        draft["effect_value"] = abs(int(parts[2]))
        _set_draft(context, draft)
        return await query.edit_message_text(
            f"{_build_preview_text(draft)}\n\n请选择冷却时间（秒）：",
            reply_markup=_build_value_keyboard("cooldown", COOLDOWN_CHOICES),
        )

    if action == "cooldown" and len(parts) == 3:
        draft["cooldown"] = max(0, int(parts[2]))
        _set_draft(context, draft)
        actions = get_actions()
        overwrite = draft.get("name") in actions
        tip = "⚠️ 该行为已存在，确认覆盖？\n\n" if overwrite else ""
        return await query.edit_message_text(
            f"{tip}{_build_preview_text(draft)}",
            reply_markup=_build_confirm_keyboard(overwrite=overwrite),
        )

    if action == "confirm" and len(parts) == 3:
        mode = parts[2]
        actions = get_actions()
        name = draft.get("name")
        exists = name in actions
        if mode == "new" and exists:
            return await query.edit_message_text(
                "⚠️ 行为已存在，请点击“覆盖保存”或取消。",
                reply_markup=_build_confirm_keyboard(overwrite=True),
            )
        cfg = _draft_to_config(draft)
        actions[name] = cfg
        save_json(ACTIONS_FILE, actions)
        _clear_draft(context)
        return await query.edit_message_text(
            f"✅ 行为已保存：{name}\n\n配置如下：\n{json.dumps(cfg, ensure_ascii=False, indent=2)}"
        )


def apply_action_effects(chat_id, user_id, user_data, action_name, target_id=None):
    actions = get_actions()
    action = actions.get(action_name)
    if not action:
        return ""
    user_data = user_data or {}

    if int(action.get("type", 1)) == 2 and not target_id:
        return "⚠️ 该行为需要回复目标用户后再使用。"

    now = int(time.time())
    cooldown_data = load_json(COOLDOWN_FILE)
    user_cds = cooldown_data.setdefault(str(chat_id), {}).setdefault(str(user_id), {})

    # ⏳ 检查冷却时间
    last_used = user_cds.get(action_name, 0)
    if now - last_used < action.get("cooldown", 0):
        remaining = action["cooldown"] - (now - last_used)
        return f"⌛ 该行为正在冷却中，请 {remaining} 秒后再试。"

    # ⚠️ 检查扣除项是否足够（仅自己属性）
    for attr, effect in action.items():
        if attr in ("cooldown", "success_rate", "type"):
            continue
        if not isinstance(effect, (int, float)):
            continue
        if effect >= 0:
            continue
        if str(attr).startswith("target_"):
            continue
        current = user_data.get(attr, 0)
        need = abs(effect)
        if current < need:
            return f"💤 你的【{_format_attr(attr)}】不足（需要 {need}）"

    # 🎯 成功率判定
    success_rate = action.get("success_rate", 1.0)
    if random.random() > success_rate:
        user_cds[action_name] = now
        save_json(COOLDOWN_FILE, cooldown_data)
        return f"😵 很遗憾，{action_name} 失败了……"

    # ✅ 成功，执行效果
    log = [f"🎭 成功执行：{action_name}"]
    print("💪 成功执行行为", action_name)

    for attr, effect in action.items():
        if attr in ("cooldown", "success_rate", "type"):
            continue
        value = effect
        if effect == "random":
            value = random.randint(20, 100)
        if not isinstance(value, (int, float)):
            continue

        if str(attr).startswith("target_") and target_id:
            pure_attr = str(attr)[len("target_") :]
            change_user_attribute(chat_id, target_id, attr, value)
            log.append(f"🎯 对方{_format_attr(pure_attr)} {'+' if value >= 0 else ''}{value}")
            continue

        if attr in ATTR_LABEL_MAP:
            change_user_attribute(chat_id, user_id, attr, value)
            log.append(f"🧾 {_format_attr(attr)} {'+' if value >= 0 else ''}{value}")

    # 记录冷却
    user_cds[action_name] = now
    save_json(COOLDOWN_FILE, cooldown_data)
    return "\n".join(log)


# 冷却判断
def check_action_available(chat_id, user_id, user_data, action_name):
    action = get_actions().get(action_name)
    if not action:
        return False, f"❌ 未知行为：{action_name}"
    user_data = user_data or {}

    now = int(time.time())
    cooldown_data = load_json(COOLDOWN_FILE)
    user_cds = cooldown_data.setdefault(str(chat_id), {}).setdefault(str(user_id), {})

    # ⏳ 冷却判断
    last_used = user_cds.get(action_name, 0)
    cooldown = action.get("cooldown", 0)
    if now - last_used < cooldown:
        remaining = cooldown - (now - last_used)
        return False, f"⌛ 该行为正在冷却中，请 {remaining} 秒后再试。"

    # ⚠️ 扣除项判断（自己属性）
    for attr, effect in action.items():
        if attr in ("cooldown", "success_rate", "type"):
            continue
        if not isinstance(effect, (int, float)):
            continue
        if effect >= 0:
            continue
        if str(attr).startswith("target_"):
            continue
        current = user_data.get(attr, 0)
        need = abs(effect)
        if current < need:
            return False, f"💤 你的【{_format_attr(attr)}】不足（需要 {need}）"

    return True, "✅ 可执行"


@register_command("行为列表")
@feature_required(FEATURE_FRIENDS)
async def list_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    actions = get_actions()
    if not actions:
        return await update.message.reply_text("当前没有任何行为。")

    lines = ["🔧 可用行为列表："]
    for name, cfg in actions.items():
        desc = cfg.get("description") or cfg.get("desc") or ""
        cooldown = cfg.get("cooldown", 0)
        lines.append(f"- {name}  (冷却: {cooldown}s) {desc}")

    await update.message.reply_text("\n".join(lines))


# 注册命令
def register_action_handlers(app):
    app.add_handler(CommandHandler("apply_action", apply_action))
    app.add_handler(CommandHandler("add_action", add_action))
    app.add_handler(CommandHandler("list_actions", list_actions))  # 新增
    app.add_handler(
        CallbackQueryHandler(add_action_callback, pattern=rf"^{ADD_ACTION_CB_PREFIX}:")
    )
