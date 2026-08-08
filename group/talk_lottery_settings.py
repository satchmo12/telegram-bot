"""发言抽奖设置 UI / callback / 输入处理。

这里只处理发言抽奖自己的奖池，不再复用积分抽奖奖池。
"""

from __future__ import annotations

import html
from typing import Callable, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from game.talk_lottery_core import (
    RATE_MAX,
    RATE_MIN,
    STOCK_MIN,
    add_prize,
    delete_prize,
    get_prize,
    list_prizes,
    update_prize,
)
from utils import GROUP_LIST_FILE, get_group_whitelist, save_json

CALLBACK_PREFIX = "gcfg"
STAGE_ADD = "talk_lottery_prize_add"
STAGE_EDIT = "talk_lottery_prize_edit"
STAGE_TRIGGER_RATE = "talk_lottery_trigger_rate"

TRIGGER_RATE_MIN = 1
TRIGGER_RATE_MAX = 100
TRIGGER_RATE_DEFAULT = 100

def _toggle_text(enabled: bool) -> str:
    return "✅ 开启" if enabled else "🚫 关闭"


def build_settings_text(chat_id: str, cfg: dict) -> str:
    prizes = list_prizes(chat_id)
    return "\n".join(
        [
            "🎰 发言抽奖设置",
            f"发言抽奖：{_toggle_text(bool(cfg.get('talk_lottery_enabled', False)))}",
            f"触发概率：{int(cfg.get('talk_lottery_trigger_rate', TRIGGER_RATE_DEFAULT) or TRIGGER_RATE_DEFAULT)}%",
            f"奖池设置：{len(prizes)} 个奖品",
        ]
    )


def build_settings_keyboard(chat_id: str, cfg: dict) -> InlineKeyboardMarkup:
    enabled = bool(cfg.get("talk_lottery_enabled", False))
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"{'✅' if enabled else '🚫'} 发言抽奖",
                    callback_data=f"{CALLBACK_PREFIX}:toggle:{chat_id}:talk_lottery_enabled",
                )
            ],
            [
                InlineKeyboardButton(
                        "🎯 触发概率",
                        callback_data=f"{CALLBACK_PREFIX}:talk_lottery_trigger_rate:{chat_id}",
                    ),
                ],
            [
                InlineKeyboardButton(
                    "🎁 奖池设置",
                    callback_data=f"{CALLBACK_PREFIX}:talk_lottery_prizes:{chat_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ 返回群设置",
                    callback_data=f"{CALLBACK_PREFIX}:open:{chat_id}",
                )
            ],
        ]
    )


def build_prizes_text(chat_id: str) -> str:
    prizes = list_prizes(chat_id)
    lines = ["🎁 发言抽奖奖池：", ""]
    if not prizes:
        lines.append("暂无奖品。")
    else:
        for idx, prize in enumerate(prizes, start=1):
            lines.append(
                f"{idx}. {html.escape(str(prize.get('name', '未命名')))} | "
                f"概率 {int(prize.get('rate', 0) or 0)} | "
                f"数量 {int(prize.get('stock', 0) or 0)}"
            )
    return "\n".join(lines)


def build_prizes_keyboard(chat_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "➕ 添加奖品",
                    callback_data=f"{CALLBACK_PREFIX}:talk_lottery_prize_add:{chat_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "✏️ 修改奖品",
                    callback_data=f"{CALLBACK_PREFIX}:talk_lottery_prize_edit_menu:{chat_id}",
                ),
                InlineKeyboardButton(
                    "🗑 删除奖品",
                    callback_data=f"{CALLBACK_PREFIX}:talk_lottery_prize_delete_menu:{chat_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "⬅️ 返回",
                    callback_data=f"{CALLBACK_PREFIX}:talk_lottery_back:{chat_id}",
                )
            ],
        ]
    )


def _prize_pick_keyboard(chat_id: str, mode: str) -> InlineKeyboardMarkup:
    prizes = list_prizes(chat_id)

    rows = [
        [
            InlineKeyboardButton(
                f"{'✏️' if mode == 'edit' else '🗑'} "
                f"奖品 {idx}",
                callback_data=(
                    f"{CALLBACK_PREFIX}:"
                    f"talk_lottery_prize_{mode}_pick:"
                    f"{chat_id}:{idx}"
                ),
            )
        ]
        for idx, prize in enumerate(prizes, start=1)
    ]

    rows.append(
        [
            InlineKeyboardButton(
                "⬅️ 返回",
                callback_data=(
                    f"{CALLBACK_PREFIX}:talk_lottery_prizes:{chat_id}"
                ),
            )
        ]
    )

    return InlineKeyboardMarkup(rows)


async def open_settings_panel(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id_str: str,
    user_id: int,
    can_manage_group: Callable,
    parse_chat_id: Callable,
):
    chat_id = parse_chat_id(chat_id_str)
    if chat_id is None:
        return await query.answer("群ID无效", show_alert=True)
    if not await can_manage_group(context, user_id, chat_id):
        return await query.answer("你不是该群管理员，无法修改。", show_alert=True)

    data = get_group_whitelist(context)
    cfg = data.get(chat_id_str, {})
    if not isinstance(cfg, dict):
        cfg = {}
    return await query.edit_message_text(
        build_settings_text(chat_id_str, cfg),
        reply_markup=build_settings_keyboard(chat_id_str, cfg),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def handle_callback(
    action: str,
    parts: list[str],
    query,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    can_manage_group: Callable,
    parse_chat_id: Callable,
):
    if not action.startswith("talk_lottery_"):
        return False

    if len(parts) < 3:
        return True

    chat_id_str = parts[2]
    chat_id = parse_chat_id(chat_id_str)
    if chat_id is None:
        await query.answer("群ID无效", show_alert=True)
        return True

    if not await can_manage_group(context, user_id, chat_id):
        await query.answer("你不是该群管理员，无法修改。", show_alert=True)
        return True

    if action == "talk_lottery_menu":
        await query.answer()
        await open_settings_panel(
            query, context, chat_id_str, user_id, can_manage_group, parse_chat_id
        )
        return True
    
    if action == "talk_lottery_trigger_rate":
            current_rate = int(
                get_group_whitelist(context).get(chat_id_str, {}).get(
                    "talk_lottery_trigger_rate", TRIGGER_RATE_DEFAULT
                ) or TRIGGER_RATE_DEFAULT
            )
            context.user_data["group_setting_stage"] = STAGE_TRIGGER_RATE
            context.user_data["group_setting_chat_id"] = chat_id_str
            await query.answer()
            await query.edit_message_text(
                "请输入发言抽奖触发概率（1-100）：\n"
                f"当前：{current_rate}%\n\n"
                "例如发送：20，表示每条消息有 20% 概率触发抽奖。",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton(
                        "⬅️ 返回",
                        callback_data=f"{CALLBACK_PREFIX}:talk_lottery_menu:{chat_id_str}",
                    )]]
                ),
            )
            return True
        
    if action == "talk_lottery_prizes":
        await query.answer()
        return await query.edit_message_text(
            build_prizes_text(chat_id_str),
            reply_markup=build_prizes_keyboard(chat_id_str),
            parse_mode="HTML",
        )

    if action == "talk_lottery_prize_add":
        context.user_data["group_setting_stage"] = STAGE_ADD
        context.user_data["group_setting_chat_id"] = chat_id_str
        await query.answer()
        await query.edit_message_text(
            "请输入发言抽奖奖品信息：奖品名称 | 中奖率 | 奖品数量\n"
            "示例：iPhone15 | 5 | 1",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(
                    "⬅️ 返回",
                    callback_data=f"{CALLBACK_PREFIX}:talk_lottery_prizes:{chat_id_str}",
                )]]
            ),
        )
        return True

    if action in {
        "talk_lottery_prize_edit_menu",
        "talk_lottery_prize_delete_menu",
    }:


        try:
            prizes = list_prizes(chat_id_str)
           
            if not prizes:
      
                await query.answer(
                    "当前没有可操作的奖品。",
                    show_alert=True,
                )
                return True

            mode = "edit" if action == "talk_lottery_prize_edit_menu" else "delete"
  
            keyboard = _prize_pick_keyboard(chat_id_str, mode)


            await query.answer()

            
            await query.edit_message_text(
                text=(
                    f"请选择要"
                    f"{'修改' if mode == 'edit' else '删除'}"
                    f"的发言抽奖奖品："
                ) + build_prizes_text(chat_id_str),
                reply_markup=keyboard,
            )



        except Exception as exc:
            import traceback


            print(repr(exc))
            traceback.print_exc()

            try:
                await query.answer(
                    f"操作失败：{str(exc)[:150]}",
                    show_alert=True,
                )
            except Exception:
                pass

        return True

    if action in {
        "talk_lottery_prize_edit_pick",
        "talk_lottery_prize_delete_pick",
    }:
        if len(parts) < 4:
            await query.answer("奖品编号无效。", show_alert=True)
            return True

        try:
            prize_index = int(parts[3])
        except ValueError:
            await query.answer("奖品编号无效。", show_alert=True)
            return True

        # 获取当前群奖池
        prizes = list_prizes(chat_id_str)

        # 检查编号范围
        if prize_index < 1 or prize_index > len(prizes):
            await query.answer(
                "奖品不存在，请重新打开奖池。",
                show_alert=True,
            )
            return True

        # 根据编号找到真正的奖品
        prize = prizes[prize_index - 1]

        # 获取真正的奖品 ID
        prize_id = str(prize.get("id", ""))

        if not prize_id:
            await query.answer(
                "奖品ID无效。",
                show_alert=True,
            )
            return True

        if action == "talk_lottery_prize_edit_pick":
            context.user_data["group_setting_stage"] = STAGE_EDIT
            context.user_data["group_setting_chat_id"] = chat_id_str
            context.user_data["group_setting_prize_id"] = prize_id

            await query.answer()

            await query.edit_message_text(
                "请输入新的发言抽奖奖品信息：奖品名称 | 中奖率 | 奖品数量\n"
                f"当前：{prize.get('name')} | "
                f"{int(prize.get('rate', 0) or 0)} | "
                f"{int(prize.get('stock', 0) or 0)}",
                reply_markup=InlineKeyboardMarkup(
                    [[
                        InlineKeyboardButton(
                            "⬅️ 返回",
                            callback_data=(
                                f"{CALLBACK_PREFIX}:"
                                f"talk_lottery_prize_edit_menu:"
                                f"{chat_id_str}"
                            ),
                        )
                    ]]
                ),
            )

            return True

        # 删除奖品
        ok = delete_prize(chat_id_str, prize_id)

        await query.answer(
            "✅ 已删除" if ok else "奖品不存在",
            show_alert=False,
        )

        await query.edit_message_text(
            build_prizes_text(chat_id_str),
            reply_markup=build_prizes_keyboard(chat_id_str),
            parse_mode="HTML",
        )

        return True

    if action == "talk_lottery_back":
        context.user_data.pop("group_setting_stage", None)
        context.user_data.pop("group_setting_prize_id", None)
        await query.answer()
        return await open_settings_panel(
            query, context, chat_id_str, user_id, can_manage_group, parse_chat_id
        )

    return True


async def handle_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    stage: str,
    chat_id_str: str,
):
    if stage not in {STAGE_ADD, STAGE_EDIT, STAGE_TRIGGER_RATE}:
        return False
    if not update.message:
        return True

    text = (update.message.text or "").strip()
    if text in {"取消", "返回"}:
        context.user_data.pop("group_setting_stage", None)
        context.user_data.pop("group_setting_chat_id", None)
        context.user_data.pop("group_setting_prize_id", None)
        await update.message.reply_text("✅ 已取消。")
        return True

    if stage == STAGE_TRIGGER_RATE:
        if not text.isdigit():
            await update.message.reply_text(
                f"❗ 请输入 1-{TRIGGER_RATE_MAX} 的整数。"
            )
            return True

        rate = int(text)
        if not TRIGGER_RATE_MIN <= rate <= TRIGGER_RATE_MAX:
            await update.message.reply_text(
                f"❗ 触发概率范围：{TRIGGER_RATE_MIN}-{TRIGGER_RATE_MAX}%"
            )
            return True

        data = get_group_whitelist(context)
        cfg = data.get(chat_id_str, {})
        if not isinstance(cfg, dict):
            cfg = {}
        cfg["talk_lottery_trigger_rate"] = rate
        data[chat_id_str] = cfg
        save_json(GROUP_LIST_FILE, data)

        context.user_data.pop("group_setting_stage", None)
        context.user_data.pop("group_setting_chat_id", None)
        context.user_data.pop("group_setting_prize_id", None)

        await update.message.reply_text(f"✅ 已设置发言抽奖触发概率：{rate}%")
        return True

    parts = [p.strip() for p in text.replace("｜", "|").split("|")]
    if len(parts) != 3:
        await update.message.reply_text("❗ 格式应为：奖品名称 | 中奖率 | 奖品数量")
        return True

    name, rate_raw, stock_raw = parts
    if not name:
        await update.message.reply_text("❗ 奖品名称不能为空。")
        return True
    if not rate_raw.isdigit() or not stock_raw.isdigit():
        await update.message.reply_text("❗ 中奖率和奖品数量必须是数字。")
        return True

    rate = int(rate_raw)
    stock = int(stock_raw)
    if not RATE_MIN <= rate <= RATE_MAX:
        await update.message.reply_text(f"❗ 中奖率范围：{RATE_MIN}-{RATE_MAX}")
        return True
    if stock < STOCK_MIN:
        await update.message.reply_text("❗ 奖品数量不能小于 0")
        return True

    try:
        if stage == STAGE_ADD:
            add_prize(chat_id_str, name, rate, stock)
            await update.message.reply_text(f"✅ 已添加发言抽奖奖品：{name}")
        else:
            prize_id = context.user_data.get("group_setting_prize_id")
            if not prize_id:
                await update.message.reply_text("❗ 未找到要修改的奖品。")
                return True
            ok = update_prize(chat_id_str, prize_id, name, rate, stock)
            if not ok:
                await update.message.reply_text("❗ 修改失败，奖品不存在。")
                return True
            await update.message.reply_text(f"✅ 已修改发言抽奖奖品：{name}")
    except ValueError as exc:
        await update.message.reply_text(f"❗ {exc}")
        return True

    context.user_data.pop("group_setting_stage", None)
    context.user_data.pop("group_setting_chat_id", None)
    context.user_data.pop("group_setting_prize_id", None)
    await update.message.reply_text(
        build_prizes_text(chat_id_str),
        reply_markup=build_prizes_keyboard(chat_id_str),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    return True
