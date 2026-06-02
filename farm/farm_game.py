import random
from telegram import Update, User, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CommandHandler, CallbackQueryHandler, ContextTypes

from command_router import FEATURE_MANOR, feature_required, register_command
from farm.crop_price import CROP_PEICE
from utils import (
    FARM_DATA_FILE,
    apply_reward,
    format_reward_text,
    safe_reply,
    load_json,
    save_json,
)
from farm.crop_config import CROP_CONFIG  # 作物数据
from info.economy import get_user_data, change_balance, save_user_data  # 操作金币
from farm.inventory import change_item

from copy import deepcopy
from datetime import datetime
import time
import os


BASE_EXPAND_COST = 1000  # 基础扩建费用
MAX_LAND_COUNT = 24  # 最大土地块数
EXPAND_COOLDOWN = 600  # 扩建冷却时间，单位秒


EMPTY_LAND_TEMPLATE = {
    "crop": None,
    "planted_time": None,
    "watered": False,
    "fertilized": False,
    "sprayed": False,
    "yield_left": 0,
    "stolen_by": [],  # 记录地块被谁偷过
}


def create_empty_land():
    return deepcopy(EMPTY_LAND_TEMPLATE)


def create_farmland(size: int):
    return [create_empty_land() for _ in range(size)]


def _get_farm_action_flags(farm):
    has_water = False
    has_fertilize = False
    has_spray = False
    has_harvest = False

    for land in farm.get("land", []):
        crop = land.get("crop")
        planted_time = land.get("planted_time")
        if not crop or not planted_time:
            continue

        stage = get_growth_stage(land, crop)
        if stage == "苗期" and not land.get("watered"):
            has_water = True
        elif stage == "花期" and not land.get("fertilized"):
            has_fertilize = True
        elif stage == "果期" and not land.get("sprayed"):
            has_spray = True
        elif stage == "成熟":
            has_harvest = True

    return has_water, has_fertilize, has_spray, has_harvest


def build_farm_panel_keyboard(farm=None, view_mode="self", target_user_id=None, actor_id=None):
    has_water = has_fertilize = has_spray = has_harvest = True
    if farm:
        has_water, has_fertilize, has_spray, has_harvest = _get_farm_action_flags(farm)

    target_user_id = str(target_user_id or "")
    actor_id = str(actor_id or target_user_id or "")
    if view_mode == "friend":
        status_callback = f"farm_ui:status:friend:{target_user_id}:{actor_id}"
        action_prefix = f"farm_ui:action:friend:{target_user_id}:{actor_id}"
    else:
        status_callback = f"farm_ui:status:self:{target_user_id}:{actor_id}"
        action_prefix = f"farm_ui:action:self:{target_user_id}:{actor_id}"
        plant_callback = f"farm_ui:plant:self:{target_user_id}:{actor_id}"
        expand_callback = f"farm_ui:expand:self:{target_user_id}:{actor_id}"
    close_callback = f"farm_ui:close:{view_mode}:{target_user_id}:{actor_id}"

    action_buttons = []
    if has_water:
        action_buttons.append(
            InlineKeyboardButton("💧 浇水", callback_data=f"{action_prefix}:water")
        )
    if has_fertilize:
        action_buttons.append(
            InlineKeyboardButton("🌿 施肥", callback_data=f"{action_prefix}:fertilize")
        )
    if has_spray:
        action_buttons.append(
            InlineKeyboardButton("🐛 杀虫", callback_data=f"{action_prefix}:spray")
        )
        
    # 收获有问题暂时注视掉
    # if has_harvest:
    #     action_buttons.append(
    #         InlineKeyboardButton("🌾 收获", callback_data=f"farm_ui:harvest:{view_mode}:{target_user_id}:{actor_id}")
    #     )

    rows = []
    if action_buttons:
        rows.append(action_buttons)
    if view_mode != "friend":
        rows.append(
            [
                InlineKeyboardButton("🌱 种植", callback_data=plant_callback),
                InlineKeyboardButton("🧱 扩建", callback_data=expand_callback),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton("🔄 刷新", callback_data=status_callback),
            # InlineKeyboardButton("📖 命令", callback_data="farm_ui:help"),
            InlineKeyboardButton("❌ 关闭", callback_data=close_callback)   
        ]
    )
    # rows.append([InlineKeyboardButton("❌ 关闭", callback_data=close_callback)])
    return InlineKeyboardMarkup(rows)


def build_farm_view_keyboard(farm=None, view_mode="self", target_user_id=None, actor_id=None):
    return build_farm_panel_keyboard(farm, view_mode=view_mode, target_user_id=target_user_id, actor_id=actor_id)


def build_farm_plant_quantity_keyboard(crop_name: str, max_count: int, owner_id: str, actor_id: str):
    rows = [[InlineKeyboardButton("全部", callback_data=f"farm_ui:plantqty:{crop_name}:{owner_id}:all:{actor_id}")]]
    row = []
    for count in range(1, max_count + 1):
        row.append(
            InlineKeyboardButton(
                str(count), callback_data=f"farm_ui:plantqty:{crop_name}:{owner_id}:{count}:{actor_id}"
            )
        )
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ 返回", callback_data=f"farm_ui:plant:self:{owner_id}:{actor_id}")])
    return InlineKeyboardMarkup(rows)


def build_farm_status_text(farm, coins: int, title: str = "🏡 农场面板") -> str:
    return build_farm_status_text_with_name(farm, coins, title)


def build_farm_status_text_with_name(farm, coins: int, title: str) -> str:
    now = int(time.time())
    text_lines = [title, f"💰 金币: {coins}", f"🌱 土地状况："]
    

    for idx, land in enumerate(farm["land"], 1):
        crop = land["crop"]
        planted_time = land["planted_time"]
        if crop:
            crop_info = CROP_CONFIG.get(crop)
            grow_time = crop_info["grow_time"] if crop_info else 0
            elapsed = now - planted_time
            progress = min(100, int(elapsed / grow_time * 100)) if grow_time > 0 else 0
            stage = get_growth_stage(land, crop)
            action = get_action_stage(land, stage)
            remain = grow_time - elapsed
            minutes = max(0, int(remain // 60))
            text_lines.append(
                f"  地块{idx}: {crop} - {stage} - {action} -⏳{minutes} m后成熟"
            )
        else:
            text_lines.append(f"  地块{idx}: 空闲")

    text_lines.append(f"🧱 农场扩建等级: {farm.get('expansions', 1)}")
    text_lines.append("点击下方按钮进行操作。")
    return "\n".join(text_lines)


def build_friend_farm_text(friend_name: str, farm) -> str:
    now = int(time.time())
    text_lines = [f"🌾 {friend_name}的农场", "🌱 土地状况："]

    for idx, land in enumerate(farm.get("land", []), 1):
        crop = land.get("crop")
        planted_time = land.get("planted_time")
        if crop:
            crop_info = CROP_CONFIG.get(crop)
            grow_time = crop_info["grow_time"] if crop_info else 0
            elapsed = now - planted_time
            stage = get_growth_stage(land, crop)
            action = get_action_stage(land, stage)
            remain = grow_time - elapsed
            minutes = max(0, int(remain // 60))
            text_lines.append(f"  地块{idx}: {crop} - {stage} - {action} -⏳{minutes} m后成熟")
        else:
            text_lines.append(f"  地块{idx}: 空闲")

    text_lines.append("点击下方按钮进行操作。")
    return "\n".join(text_lines)


def build_friend_farm_view_text(friend_name: str, farm) -> str:
    return build_friend_farm_text(friend_name, farm)


def _message_first_line(update: Update, default: str = "") -> str:
    message = update.effective_message
    if message and message.text:
        return message.text.splitlines()[0]
    return default


async def render_friend_farm(update: Update, context: ContextTypes.DEFAULT_TYPE, friend_id: str, friend_name: str):
    chat_id = str(update.effective_chat.id)
    farm_data = load_json(FARM_DATA_FILE)
    if chat_id not in farm_data or friend_id not in farm_data[chat_id]:
        query = update.callback_query
        if query:
            return await query.edit_message_text(f"❌ 好友 {friend_name} 还没有农场数据。")
        return await safe_reply(update, context, f"❌ 好友 {friend_name} 还没有农场数据。")

    farm = farm_data[chat_id][friend_id]
    land_list = farm.get("land", [])
    if not land_list:
        query = update.callback_query
        if query:
            return await query.edit_message_text(f"❌ 好友 {friend_name} 的农场没有种植任何作物。")
        return await safe_reply(update, context, f"❌ 好友 {friend_name} 的农场没有种植任何作物。")

    text = build_friend_farm_view_text(friend_name, farm)
    keyboard = build_farm_view_keyboard(farm, view_mode="friend", target_user_id=friend_id, actor_id=str(update.effective_user.id))
    query = update.callback_query
    if query:
        return await query.edit_message_text(text, reply_markup=keyboard)
    return await safe_reply(update, context, text, reply_markup=keyboard, auto_delete_seconds=0)


async def render_target_farm(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    target_user_id: str,
    title: str,
    view_mode: str = "self",
    actor_id: str = None,
):
    chat_id = str(update.effective_chat.id)
    farm_data = load_json(FARM_DATA_FILE)
    if chat_id not in farm_data or target_user_id not in farm_data[chat_id]:
        query = update.callback_query
        if query:
            return await query.edit_message_text("❌ 该农场数据不存在。")
        return await safe_reply(update, context, "❌ 该农场数据不存在。")

    farm = farm_data[chat_id][target_user_id]
    user_data = get_user_data(chat_id, target_user_id)
    coins = user_data.get("balance", 0)
    text = build_farm_status_text(farm, coins, title)
    keyboard = build_farm_view_keyboard(farm, view_mode=view_mode, target_user_id=target_user_id, actor_id=actor_id)
    query = update.callback_query
    if query:
        return await query.edit_message_text(text, reply_markup=keyboard)
    return await safe_reply(update, context, text, reply_markup=keyboard, auto_delete_seconds=0)


async def farm_ui_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    await query.answer()
    parts = (query.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    view_mode = parts[2] if len(parts) > 2 else ""
    target_user_id = parts[3] if len(parts) > 3 else ""
    actor_id = parts[4] if len(parts) > 4 else ""
    sub_action = parts[5] if len(parts) > 5 else ""

    if action == "plantqty":
        crop_name = parts[2] if len(parts) > 2 else ""
        owner_id = parts[3] if len(parts) > 3 else str(update.effective_user.id)
        count_text = parts[4] if len(parts) > 4 else ""
        actor_id = parts[5] if len(parts) > 5 else str(update.effective_user.id)
        if actor_id != str(update.effective_user.id):
            return await query.answer("只能操作自己打开的面板。", show_alert=True)
        return await plant_crop_by_button(update, context, crop_name, count_text, owner_id, actor_id)

    if actor_id and actor_id != str(update.effective_user.id):
        return await query.answer("只能操作自己打开的面板。", show_alert=True)

    if action == "status":
        if target_user_id:
            first_line = (query.message.text or "").splitlines()[0] if query.message and query.message.text else ""
            title = first_line if first_line else f"🏡 {update.effective_user.full_name}的农场"
            return await render_target_farm(update, context, target_user_id, title, view_mode=view_mode or "self", actor_id=actor_id or str(update.effective_user.id))
        return await status(update, context)
    if action == "action" and sub_action in {"water", "fertilize", "spray"}:
        context.user_data["farm_action"] = sub_action
        context.user_data["farm_action_target_id"] = target_user_id or str(update.effective_user.id)
        context.user_data["farm_action_actor_id"] = actor_id or str(update.effective_user.id)
        return await perform_all_action(update, context)
    if action == "plant":
        chat_id = str(update.effective_chat.id)
        user_id = str(target_user_id or update.effective_user.id)
        farm_data = load_json(FARM_DATA_FILE)
        if chat_id not in farm_data or user_id not in farm_data[chat_id]:
            return await query.edit_message_text("❌ 你还没有农场，请先使用 我的农场 创建一个。")

        farm = farm_data[chat_id][user_id]
        empty_lands = [land for land in farm.get("land", []) if land.get("crop") is None]
        if not empty_lands:
            return await query.edit_message_text("❌ 所有土地都已种满，请先收获或扩建。", reply_markup=build_farm_panel_keyboard(farm))

        crop_buttons = []
        row = []
        for name, info in CROP_CONFIG.items():
            row.append(
                InlineKeyboardButton(
                    f"{name}-{info['seed_cost']}",
                    callback_data=f"farm_ui:plantcrop:{name}:{user_id}:{actor_id or update.effective_user.id}",
                )
            )
            if len(row) == 4:
                crop_buttons.append(row)
                row = []
        if row:
            crop_buttons.append(row)
        crop_buttons.append([InlineKeyboardButton("⬅️ 返回", callback_data=f"farm_ui:status:self:{user_id}")])
        return await query.edit_message_text("🌱 请选择要种植的作物：", reply_markup=InlineKeyboardMarkup(crop_buttons))
    if action == "plantcrop":
        crop_name = view_mode
        owner_id = target_user_id or str(update.effective_user.id)
        return await show_plant_quantity_menu(update, context, crop_name, owner_id, actor_id or str(update.effective_user.id))

    if action == "help":
        text = (
            "📖 我的农场命令：\n"
            "我的农场｜种植 <作物>｜收获/农场收获｜出售 <作物> <数量>（不填参数=全部）｜种子商店｜我的库存｜开垦地块/农场扩建｜浇水｜施肥｜杀虫｜好友农场｜偷菜"
        )
        return await query.edit_message_text(text, reply_markup=build_farm_panel_keyboard())

    if action == "shop":
        crops_list = "\n".join(
            [
                f"{name}：价格：{info['seed_cost']}  生长时间：{format_duration(info['grow_time'])} "
                f"售价：{info['sell_price']}  产量：{info['max_yield']}"
                for name, info in CROP_CONFIG.items()
            ]
        )
        text = f"🛒 种子列表：\n{crops_list}"
        return await query.edit_message_text(text, reply_markup=build_farm_panel_keyboard())

    if action == "harvest":
        if target_user_id:
            await farm_harvest_target(update, context, target_user_id, view_mode=view_mode or "self")
            title = _message_first_line(update, f"🏡 {update.effective_user.full_name}的农场")
            return await render_target_farm(
                update,
                context,
                target_user_id,
                title,
                view_mode=view_mode or "self",
                actor_id=actor_id or str(update.effective_user.id),
            )
        await farm_harvest(update, context)
        return await status(update, context)
    if action == "close":
        if actor_id and actor_id != str(update.effective_user.id):
            return await query.answer("只能关闭自己打开的农场面板。", show_alert=True)
        context.user_data.pop("farm_view_mode", None)
        context.user_data.pop("friend_farm_target_id", None)
        context.user_data.pop("friend_farm_target_name", None)
        context.user_data.pop("farm_action_target_id", None)
        return await query.message.delete()
    if action == "expand":
        if target_user_id and target_user_id != str(update.effective_user.id):
            return await query.answer("只能扩建自己的农场。", show_alert=True)
        await expand_land(update, context)
        return await status(update, context)

    return await query.edit_message_text("❓ 未知操作", reply_markup=build_farm_panel_keyboard())


async def show_plant_quantity_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE, crop_name: str, owner_id: str, actor_id: str
):
    query = update.callback_query
    if not query:
        return

    chat_id = str(update.effective_chat.id)
    user_id = str(owner_id or update.effective_user.id)
    farm_data = load_json(FARM_DATA_FILE)
    if chat_id not in farm_data or user_id not in farm_data[chat_id]:
        return await query.edit_message_text("❌ 你还没有农场，请先使用 我的农场 创建一个。")

    farm = farm_data[chat_id][user_id]
    if crop_name not in CROP_CONFIG:
        return await query.edit_message_text("❌ 不存在该作物，请重新选择。", reply_markup=build_farm_panel_keyboard(farm))

    crop_info = CROP_CONFIG[crop_name]
    empty_lands = [i for i, land in enumerate(farm["land"]) if land["crop"] is None]
    if not empty_lands:
        return await query.edit_message_text("❌ 所有土地都已种满，请先收获或扩建。", reply_markup=build_farm_panel_keyboard(farm))

    user_data = get_user_data(chat_id, user_id)
    max_can_plant = user_data["balance"] // crop_info["seed_cost"]
    max_count = min(len(empty_lands), max_can_plant)
    if max_count <= 0:
        return await query.edit_message_text("❌ 没有足够空地或金币来种植。", reply_markup=build_farm_panel_keyboard(farm))

    text = f"🌱 请选择种植数量：\n作物：{crop_name}\n最多可种：{max_count}"
    return await query.edit_message_text(
        text,
        reply_markup=build_farm_plant_quantity_keyboard(crop_name, max_count, user_id, actor_id),
    )


async def plant_crop_by_button(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    crop_name: str,
    count_text: str,
    owner_id: str,
    actor_id: str,
):
    query = update.callback_query
    if not query:
        return

    chat_id = str(update.effective_chat.id)
    user_id = str(owner_id or update.effective_user.id)
    farm_data = load_json(FARM_DATA_FILE)
    if chat_id not in farm_data or user_id not in farm_data[chat_id]:
        return await query.edit_message_text("❌ 你还没有农场，请先使用 我的农场 创建一个。")

    farm = farm_data[chat_id][user_id]
    if crop_name not in CROP_CONFIG:
        return await query.edit_message_text("❌ 不存在该作物，请重新选择。", reply_markup=build_farm_panel_keyboard(farm))

    crop_info = CROP_CONFIG[crop_name]
    empty_lands = [i for i, land in enumerate(farm["land"]) if land["crop"] is None]
    if not empty_lands:
        return await query.edit_message_text("❌ 所有土地都已种满，请先收获或扩建。", reply_markup=build_farm_panel_keyboard(farm))

    user_data = get_user_data(chat_id, user_id)
    max_can_plant = min(len(empty_lands), user_data["balance"] // crop_info["seed_cost"])
    if max_can_plant <= 0:
        return await query.edit_message_text("❌ 没有足够空地或金币来种植。", reply_markup=build_farm_panel_keyboard(farm))

    if count_text == "all":
        to_plant_count = max_can_plant
    else:
        try:
            to_plant_count = int(count_text)
        except ValueError:
            return await query.edit_message_text("❌ 种植数量无效。", reply_markup=build_farm_panel_keyboard(farm))

    to_plant_count = min(to_plant_count, max_can_plant)
    if to_plant_count <= 0:
        return await query.edit_message_text("❌ 没有足够空地或金币来种植。", reply_markup=build_farm_panel_keyboard(farm))

    now = int(time.time())
    for i in empty_lands[:to_plant_count]:
        farm["land"][i]["crop"] = crop_name
        farm["land"][i]["planted_time"] = now
        farm["land"][i]["yield_left"] = crop_info["max_yield"]
        change_balance(chat_id, user_id, -crop_info["seed_cost"])

    save_json(FARM_DATA_FILE, farm_data)
    return await render_target_farm(
        update,
        context,
        user_id,
        f"🏡 {update.effective_user.full_name}的农场",
        view_mode="self",
        actor_id=actor_id,
    )


@register_command("我的农场")
@feature_required(FEATURE_MANOR)
async def start_farm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)

    # 加载农场数据
    data = load_json(FARM_DATA_FILE)
    if chat_id not in data:
        data[chat_id] = {}

    # 如果已存在农场
    if user_id in data[chat_id]:
        return await status(update, context)

    land = create_farmland(2)

    # 初始化农场（不包含金币）
    data[chat_id][user_id] = {
        "land": land,
        "expansions": 1,
        "last_expand_time": int(time.time()),
    }

    save_json(FARM_DATA_FILE, data)
    await safe_reply(
        update,
        context,
        "✅ 农场创建成功！你拥有了 2 块土地。\n使用 种植 【作物名称】 开始吧！",
    )


@register_command("种植")
@feature_required(FEATURE_MANOR)
async def plant_crop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)
    args = context.args

    farm_data = load_json(FARM_DATA_FILE)
    if chat_id not in farm_data or user_id not in farm_data[chat_id]:
        return await safe_reply(
            update, context, "❌ 你还没有农场，请先使用 我的农场 创建一个。"
        )

    farm = farm_data[chat_id][user_id]

    if not args:
        crops = "\n".join(
            [f"{name} - {info['seed_cost']}金币" for name, info in CROP_CONFIG.items()]
        )
        return await safe_reply(
            update,
            context,
            f"🌱 请选择你要种植的作物：\n{crops}\n\n用法：`种植 作物名 [地块编号或全部]`",
        )

    crop_name = args[0]
    if crop_name not in CROP_CONFIG:
        return await safe_reply(update, context, "❌ 不存在该作物，请重新输入。")

    crop_info = CROP_CONFIG[crop_name]
    now = int(time.time())

    user_data = get_user_data(chat_id, user_id)
    empty_lands = [i for i, land in enumerate(farm["land"]) if land["crop"] is None]

    if len(args) == 2 and args[1] in ["全部", "all"]:
        max_can_plant = user_data["balance"] // crop_info["seed_cost"]
        to_plant_count = min(len(empty_lands), max_can_plant)

        if to_plant_count == 0:
            return await safe_reply(update, context, "❌ 没有足够空地或金币来种植。")

        for i in empty_lands[:to_plant_count]:
            farm["land"][i]["crop"] = crop_name
            farm["land"][i]["planted_time"] = now
            farm["land"][i]["yield_left"] = crop_info["max_yield"]
            change_balance(chat_id, user_id, -crop_info["seed_cost"])

        save_json(FARM_DATA_FILE, farm_data)
        return await safe_reply(
            update,
            context,
            f"✅ 成功在 {to_plant_count} 块地种植 {crop_name}！将在 {crop_info['grow_time']} 秒后成熟。",
        )

    # 如果指定了数字编号
    if len(args) == 2:
        try:
            land_index = int(args[1]) - 1
            if land_index < 0 or land_index >= len(farm["land"]):
                return await safe_reply(
                    update,
                    context,
                    f"❌ 地块编号无效，请输入 1 到 {len(farm['land'])} 之间的数字。",
                )
        except ValueError:
            return await safe_reply(update, context, "❌ 地块编号必须是数字或“全部”。")

        if farm["land"][land_index]["crop"] is not None:
            return await safe_reply(
                update, context, f"❌ 地块 {land_index+1} 已有作物。"
            )

        if user_data["balance"] < crop_info["seed_cost"]:
            return await safe_reply(update, context, "❌ 金币不足，无法种植。")

        change_balance(chat_id, user_id, -crop_info["seed_cost"])

        farm["land"][land_index]["crop"] = crop_name
        farm["land"][land_index]["planted_time"] = now
        farm["land"][land_index]["yield_left"] = crop_info["max_yield"]

        save_json(FARM_DATA_FILE, farm_data)
        return await safe_reply(
            update,
            context,
            f"✅ 成功在地块 {land_index + 1} 种植 {crop_name}！将在 {crop_info['grow_time']} 秒后成熟。",
        )

    # 自动找空地种植一块
    if not empty_lands:
        return await safe_reply(
            update, context, "❌ 所有土地都已种满，请等作物成熟或 扩建农场。"
        )

    if user_data["balance"] < crop_info["seed_cost"]:
        return await safe_reply(update, context, "❌ 金币不足，无法种植。")

    land_index = empty_lands[0]
    change_balance(chat_id, user_id, -crop_info["seed_cost"])

    farm["land"][land_index]["crop"] = crop_name
    farm["land"][land_index]["planted_time"] = now
    farm["land"][land_index]["yield_left"] = crop_info["max_yield"]

    save_json(FARM_DATA_FILE, farm_data)
    return await safe_reply(
        update,
        context,
        f"✅ 成功在地块 {land_index + 1} 种植 {crop_name}！将在 {crop_info['grow_time']} 秒后成熟。",
    )


@register_command("农场状态", "查看农场")
@feature_required(FEATURE_MANOR)
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)

    farm_data = load_json(FARM_DATA_FILE)
    if chat_id not in farm_data or user_id not in farm_data[chat_id]:
        return await safe_reply(
            update, context, "❌ 你还没有农场，请先用 我的农场 创建。"
        )

    farm = farm_data[chat_id][user_id]

    user_data = get_user_data(chat_id, user_id)
    coins = user_data.get("balance", 0)

    title = f"🏡 {user.full_name}的农场"
    context.user_data["farm_view_mode"] = "self"
    context.user_data.pop("friend_farm_target_id", None)
    context.user_data.pop("friend_farm_target_name", None)
    text = build_farm_status_text(farm, coins, title)
    keyboard = build_farm_view_keyboard(farm, view_mode="self", target_user_id=user_id, actor_id=user_id)

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard)
        return

    await safe_reply(update, context, text, reply_markup=keyboard, auto_delete_seconds=0)


@register_command("农场扩建", "扩建农场", "开垦地块")
@feature_required(FEATURE_MANOR)
async def expand_land(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # TODO: 扣金币扩建土地，增加土地数量
    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)

    farm_data = load_json(FARM_DATA_FILE)
    if chat_id not in farm_data or user_id not in farm_data[chat_id]:
        return await safe_reply(
            update, context, "❌ 你还没有农场，请先使用 我的农场 创建。"
        )

    farm = farm_data[chat_id][user_id]

    user_data = get_user_data(chat_id, user_id)
    balance = user_data.get("balance", 0)

    expand_times = farm.get("expansions", 1)  # 当前扩建次数（包括初始）
    current_land_count = len(farm["land"])

    # 扩建上限判断
    if current_land_count >= MAX_LAND_COUNT:
        return await safe_reply(
            update,
            context,
            f"❌ 你的土地已达到最大数量 {MAX_LAND_COUNT} 块，无法继续扩建。",
        )

    # 计算本次扩建费用，费用递增
    cost = BASE_EXPAND_COST * expand_times
    cost_time = EXPAND_COOLDOWN * expand_times
    if balance < cost:
        return await safe_reply(
            update, context, f"❌ 金币不足，扩建土地需要 {cost} 金币。"
        )

    # 扩建冷却时间限制
    last_expand_time = farm_data.get("last_expand_time", 0)

    now = int(time.time())
    if now - last_expand_time < cost_time:
        remain = cost_time - (now - last_expand_time)
        return await safe_reply(update, context, f"⏳ 请等待 {remain} 秒后再扩建。")

    # 扣金币
    change_balance(chat_id, user_id, -cost)

    # 增加空地块
    farm["land"].append(create_empty_land())

    # 扩建等级+1
    farm["expansions"] = expand_times + 1

    # 更新扩建时间
    farm["last_expand_time"] = now

    save_json(FARM_DATA_FILE, farm_data)

    # 计算下一次扩建费用
    next_cost = BASE_EXPAND_COST * farm["expansions"]
    next_time = EXPAND_COOLDOWN * expand_times
    await safe_reply(
        update,
        context,
        f"✅ 开垦地块成功！你现在拥有 {len(farm['land'])} 块土地。\n"
        f"下一次扩建将需要 {next_cost} 金币。\n"
        f"扩建冷却时间为 {next_time} 秒，请合理安排。",
    )


def format_duration(seconds):
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    return f"{hours}小时{minutes}分" if hours else f"{minutes}分"


@register_command("种子商店")
@feature_required(FEATURE_MANOR)
async def show_shop(update: Update, context: ContextTypes.DEFAULT_TYPE):

    crops_list = "\n".join(
        [
            f"{name}："
            f"价格：{info['seed_cost']}  "
            f"生长时间：{format_duration(info['grow_time'])} \n"
            f"售价：{info['sell_price']}  "
            f"产量：{info['max_yield']}  "
            for name, info in CROP_CONFIG.items()
        ]
    )

    text = "种子列表：\n" f"{crops_list}\n\n"
    await safe_reply(update, context, text)


def get_growth_stage(land, crop_name):
    if not land.get("crop") or not land.get("planted_time"):
        return "空地"

    base_time = CROP_CONFIG.get(crop_name, {}).get("grow_time", 3600)  # 默认1小时
    elapsed = int(time.time()) - land["planted_time"]
    progress = elapsed / base_time

    if progress >= 1:
        return "成熟"
    elif progress >= 0.75:
        return "果期"
    elif progress >= 0.5:
        return "花期"
    elif progress >= 0.25:
        return "苗期"
    else:
        return "种子"


def steal_crop(crop_data, crop_name):
    config = CROP_CONFIG.get(crop_name)
    if not config:
        return 0

    max_yield = config["max_yield"]
    min_owner_yield = int(config.get("min_owner_yield", max(0, max_yield // 2)))
    max_steal_once = int(config.get("max_steal_once", 1))

    current_left = crop_data.get("yield_left", max_yield)
    stealable = max(0, current_left - min_owner_yield)
    actual_steal = min(max_steal_once, stealable)

    crop_data["yield_left"] = current_left - actual_steal
    return actual_steal


@register_command("好友农场")
@feature_required(FEATURE_MANOR)
async def friendfarm_by_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.reply_to_message:
        await safe_reply(update, context, "❌ 请回复好友的一条消息来查看他的农场状态。")
        return

    friend = update.message.reply_to_message.from_user
    chat_id = str(update.effective_chat.id)
    friend_id = str(friend.id)
    friend_name = friend.full_name

    farm_data = load_json(FARM_DATA_FILE)
    if chat_id not in farm_data or friend_id not in farm_data[chat_id]:
        await safe_reply(update, context, f"❌ 好友 {friend_name} 还没有农场数据。")
        return

    await render_friend_farm(update, context, friend_id, friend_name)


def get_action_stage(land, stage):
    if not land.get("crop") or not land.get("planted_time"):
        return ""
    if not land["watered"] and stage == "苗期":
        return "可浇水"
    elif not land["fertilized"] and stage == "花期":
        return "可施肥"
    elif not land["sprayed"] and stage == "果期":
        return "可杀虫"
    elif stage == "成熟":
        return "可收获"
    else:
        return ""


@register_command("浇水", "施肥", "杀虫")
@feature_required(FEATURE_MANOR)
async def perform_all_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    farm_data = load_json(FARM_DATA_FILE)
    actor = update.effective_user
    message = update.effective_message
    query = update.callback_query
    sub_action = context.user_data.pop("farm_action", "")
    target_user_id = str(context.user_data.pop("farm_action_target_id", "") or "")
    actor_id = str(context.user_data.pop("farm_action_actor_id", "") or actor.id)

    is_stealing = False  # 是否为偷取行为
    # 判断目标农场
    if target_user_id:
        is_self = target_user_id == str(actor_id)
        if chat_id not in farm_data or target_user_id not in farm_data[chat_id]:
            await safe_reply(
                update, context, f"❌ {'你还' if is_self else '对方还'}没有农场数据。"
            )
            return
        target_user = None
    elif message and message.reply_to_message:
        target_user = message.reply_to_message.from_user
        is_self = target_user.id == actor.id
        is_stealing = True
    else:
        target_user = actor
        is_self = True

    if not target_user_id:
        target_user_id = str(target_user.id)

    if chat_id not in farm_data or target_user_id not in farm_data[chat_id]:
        await safe_reply(
            update, context, f"❌ {'你还' if is_self else '对方还'}没有农场数据。"
        )
        return

    farm = farm_data[chat_id][target_user_id]
    text = ""
    if sub_action == "water":
        text = "浇水"
    elif sub_action == "fertilize":
        text = "施肥"
    elif sub_action == "spray":
        text = "杀虫"
    elif message and message.text:
        text = message.text.strip()

    if text == "浇水":
        action = "watered"
    elif text == "施肥":
        action = "fertilized"
    elif text == "杀虫":
        action = "sprayed"
    else:
        return await safe_reply(update, context, "❌ 未知务农操作。")

    action_names = {"watered": "💧浇水", "fertilized": "🌿施肥", "sprayed": "🐛杀虫"}

    success_count = 0

    for i, land in enumerate(farm["land"]):
        if land.get("crop") and not land.get(action):
            crop = land.get("crop")
            stage = get_growth_stage(land, crop)

            crop_info = CROP_CONFIG[land["crop"]]
            grow_time = crop_info["grow_time"]

            if action == "watered" and stage == "苗期":
                land[action] = True
                land["planted_time"] -= grow_time * 0.1
                success_count += 1
            elif action == "fertilized" and stage == "花期":
                land[action] = True
                land["planted_time"] -= grow_time * 0.1
                success_count += 1
            elif action == "sprayed" and stage == "果期":
                land[action] = True
                success_count += 1

    if success_count > 0:
        save_json(FARM_DATA_FILE, farm_data)
        msg = f"{action_names.get(action)}成功！ ✅ 共作用于 {success_count} 块作物地。"
        if action == "sprayed":
            msg += f"\n杀虫效果显著，作物增产"
        else:
            msg += f"\n作物生长时间减少10%"

        if is_stealing:
            msg += lottery(chat_id, actor_id)

    else:
        if action == "fertilized":
            msg = "⚠️ 目前没有可施肥的地块。"
        elif action == "watered":
            msg = "⚠️ 目前没有可浇水的地块。"
        else:
            msg = "⚠️ 目前没有可杀虫的地块。"

    if query:
        title = _message_first_line(update, f"🏡 {actor.full_name}的农场")
        return await render_target_farm(
            update,
            context,
            target_user_id,
            title,
            view_mode="friend" if target_user_id != str(actor_id) else "self",
            actor_id=actor_id,
        )

    await safe_reply(update, context, msg)


# 我的农场
@register_command("农场命令")
# @feature_required(FEATURE_MANOR)
async def farm_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 我的农场命令：\n"
        "我的农场｜种植 <作物>｜收获/农场收获｜出售 <作物> <数量>（不填参数=全部）｜种子商店｜我的库存｜开垦地块/农场扩建｜浇水｜施肥｜杀虫｜好友农场｜偷菜"
    )
    await safe_reply(update, context, text)


# 给好友操作 增加
def lottery(chat_id, user_id):

    # 奖励池
    rewards = [
        {"text": "🎁 你获得了 {balance} 金币！", "balance": 50},
        {"text": "🎉 你获得了 {points} 积分！", "points": 5},
        {"text": "🍀 幸运女神眷顾你，幸运值 +{luck}", "luck": 10},
        {"text": "😢 祝你下次好运！心情 {mood}", "mood": -3},
        {"text": "💰 恭喜中大奖！金币 +{balance}", "balance": 100},
        {"text": "🍀 幸运值提升 +{luck}！", "luck": 5},
        {"text": "💪 体力恢复 {stamina} 点", "stamina": 10},
        {"text": "✨ 魅力值增加 {charm} 点", "charm": 3},
        {"text": "😊 心情提升 {mood} 点", "mood": 5},
    ]

    reward = random.choice(rewards)

    # 加载并更新用户数据
    user_data = get_user_data(chat_id, user_id)

    user_data = apply_reward(user_data, reward)

    save_user_data(chat_id, user_id, user_data)

    msg = format_reward_text(reward)

    return msg


@register_command("农场收获", "收获农场", "洗劫农场", "偷菜")
@feature_required(FEATURE_MANOR)
async def farm_harvest_target(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    target_user_id: str,
    view_mode: str = "self",
):
    actor = update.effective_user
    chat_id = str(update.effective_chat.id)
    actor_id = str(actor.id)
    farm_data = load_json(FARM_DATA_FILE)

    if chat_id not in farm_data or target_user_id not in farm_data[chat_id]:
        return

    if view_mode == "friend" and target_user_id != actor_id:
        friend_farm = farm_data[chat_id][target_user_id]
        for land in friend_farm.get("land", []):
            crop = land.get("crop")
            planted_time = land.get("planted_time")
            if not crop or not planted_time:
                continue

            if get_growth_stage(land, crop) != "成熟":
                continue

            stolen_by = land.setdefault("stolen_by", [])
            if actor_id in stolen_by:
                continue

            stolen_amount = steal_crop(land, crop)
            if stolen_amount <= 0:
                continue

            stolen_by.append(actor_id)
            change_item(chat_id, actor_id, crop, stolen_amount)

        save_json(FARM_DATA_FILE, farm_data)
        return

    farm = farm_data[chat_id][target_user_id]
    now = int(time.time())
    changed = False
    for i, land in enumerate(farm["land"]):
        crop = land.get("crop")
        planted_time = land.get("planted_time")
        if not crop or not planted_time:
            continue

        crop_info = CROP_CONFIG.get(crop)
        if not crop_info or now - planted_time < crop_info["grow_time"]:
            continue

        amount = land.get("yield_left", 10)
        if not land.get("sprayed"):
            amount -= 2
        change_item(chat_id, target_user_id, crop, amount)
        farm["land"][i] = create_empty_land()
        changed = True

    if changed:
        save_json(FARM_DATA_FILE, farm_data)


@register_command("农场收获", "收获农场", "洗劫农场", "偷菜")
@feature_required(FEATURE_MANOR)
async def farm_harvest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)
    message = update.effective_message

    farm_data = load_json(FARM_DATA_FILE)
    if chat_id not in farm_data or user_id not in farm_data[chat_id]:
        return await start_farm(update, context)
        # return await safe_reply(
        #     update, context, "❌ 你还没有农场，请先用 我的农场 创建。"
        # )

    msg_lines = []

    # 判断是否回复好友消息
    if message and message.reply_to_message:
    # if update.message.reply_to_message:
        # 回复好友 → 只偷菜，不收自己的
        target_user = message.reply_to_message.from_user
        thief_id = user_id
        friend_id = str(target_user.id)

        if thief_id == friend_id:
            await safe_reply(update, context, "❌ 不能偷你自己的农场！")
            return

        if target_user.is_bot:
            await safe_reply(update, context, "🤖 你不能偷机器人的农场！")
            return

        if chat_id not in farm_data or friend_id not in farm_data[chat_id]:
            await safe_reply(update, context, "❌ 对方还没有农场数据。")
            return

        friend_farm = farm_data[chat_id][friend_id]
        stolen_total = {}
        has_mature_crop = False
        has_stealable_crop = False

        for land in friend_farm.get("land", []):
            crop = land.get("crop")
            planted_time = land.get("planted_time")
            if not crop or not planted_time:
                continue

            stage = get_growth_stage(land, crop)
            if stage != "成熟":
                continue

            has_mature_crop = True
            stolen_by = land.setdefault("stolen_by", [])
            if thief_id in stolen_by:
                continue

            stolen_amount = steal_crop(land, crop)
            if stolen_amount <= 0:
                continue

            stolen_by.append(thief_id)
            change_item(chat_id, thief_id, crop, stolen_amount)
            stolen_total[crop] = stolen_total.get(crop, 0) + stolen_amount
            has_stealable_crop = True

        save_json(FARM_DATA_FILE, farm_data)

        if not has_mature_crop:
            await safe_reply(update, context, "😢 对方的作物都还没成熟，暂时偷不到哦。")
            return

        if not has_stealable_crop:
            await safe_reply(
                update, context, "😢 作物虽然成熟，但地主太抠，一个都不给你偷。"
            )
            return

        msg_lines.append(f"🕵️ 你偷偷从 {target_user.first_name} 的农场中偷到了：")
        for crop, amount in stolen_total.items():
            msg_lines.append(f"{crop} × {amount}")

    else:
        # 没有回复好友 → 收获自己的农场
        farm = farm_data[chat_id][user_id]
        now = int(time.time())
        harvested = {}
        changed = False

        for i, land in enumerate(farm["land"]):
            crop = land.get("crop")
            planted_time = land.get("planted_time")
            num = land.get("yield_left", 10)

            if not land.get("sprayed"):
                num = num - 2

            if crop and planted_time:
                crop_info = CROP_CONFIG.get(crop)
                if not crop_info:
                    continue

                grow_time = crop_info["grow_time"]
                if now - planted_time >= grow_time:
                    harvested[crop] = harvested.get(crop, 0) + num
                    farm["land"][i] = create_empty_land()
                    changed = True

        if not harvested:
            await safe_reply(update, context, "🌱 目前没有成熟的作物可以收获。")
            return

        for crop, cnt in harvested.items():
            change_item(chat_id, user_id, crop, cnt)

        if changed:
            save_json(FARM_DATA_FILE, farm_data)

        msg_lines.append("🌾 收获成功！获得：")
        for crop, cnt in harvested.items():
            msg_lines.append(f"{crop} × {cnt}")

    await safe_reply(update, context, "\n".join(msg_lines))


def register_farm_game_handlers(app):
    app.add_handler(CommandHandler("startfarm", start_farm))
    app.add_handler(CommandHandler("plant", plant_crop))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("farm_harvest", farm_harvest))

    app.add_handler(CommandHandler("expand", expand_land))
    app.add_handler(CommandHandler("shop", show_shop))

    # 操作别人的
    app.add_handler(CommandHandler("friendfarm", friendfarm_by_reply))
    app.add_handler(CommandHandler("farm", farm_help))
    app.add_handler(CallbackQueryHandler(farm_ui_callback, pattern=r"^farm_ui:"))
