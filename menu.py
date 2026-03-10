from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CommandHandler, CallbackQueryHandler, ContextTypes
from command_router import register_command
from feature_flags import ALL_FEATURES
from utils import safe_reply

from farm.animals_game import animals_help
from farm.farm_game import farm_help
from farm.garden_game import garden_help
from slave.slave_game import slave_help  # 新增奴隶模块帮助


# 点击按钮的回调处理
async def menu_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()  # 必须应答 callback_query，否则按钮会一直显示“加载中”

    data = query.data  # 按钮传来的标识
    if data == "garden_help":
        await garden_help(update, context)
    elif data == "farm_help":
        await farm_help(update, context)
    elif data == "slave_help":
        await slave_help(update, context)
    elif data == "animals_help":
        await animals_help(update, context)

    
    


# 菜单命令，显示按钮
async def start_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🌱 花园帮助", callback_data="garden_help")],
        [InlineKeyboardButton("🚜 农场帮助", callback_data="farm_help")],
        [InlineKeyboardButton("🚜 牧场帮助", callback_data="animals_help")],
        [InlineKeyboardButton("👑 奴隶系统帮助", callback_data="slave_help")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "请选择你要查看的帮助内容：", reply_markup=reply_markup
    )


def build_feature_intro(context: ContextTypes.DEFAULT_TYPE) -> str:
    enabled = context.application.bot_data.get("enabled_features") or set(ALL_FEATURES)
    bot_name = context.application.bot_data.get("name", "当前机器人")

    top_features = []
    if "group" in enabled:
        top_features.append("群管理与群设置")
    if "economy" in enabled:
        top_features.append("经济系统（余额/签到/银行等）")
    if "simulation" in enabled:
        top_features.append("互动模拟玩法（学习回复/动作等）")
    if "entertainment" in enabled:
        top_features.append("娱乐功能（成语/问答/骰子等）")
    if "niuniu" in enabled:
        top_features.append("牛牛玩法（PK/纳妾/排行榜）")

    if not top_features:
        top_features.append("基础消息与管理功能")

    detail_map = {
        "group_setting": "群设置开关（群开关/群状态）",
        "admin": "群管命令（踢/禁言/解禁/锁群）",
        "invite_stats": "邀请统计与邀请链接",
        "verification": "入群验证",
        "talk_stats": "群聊发言统计",
        "economy_info": "余额与资料查询",
        "economy_bank": "银行存取款/贷款",
        "lottery_betting": "彩票投注",
        "company_business": "公司经营",
        "my_bot": "学说话与自动回复",
        "game_hub": "农场/花园/宠物等玩法",
        "truth": "真心话",
        "chengyu": "成语接龙",
        "qa": "问答学习",
        "voice_reply": "语音相关功能",
        "niuniu": "牛牛小游戏",
    }
    details = [detail_map[k] for k in detail_map if k in enabled]
    if not details:
        details = ["已启用基础功能（可在 .env 配置 BOT_FEATURES_* 扩展）"]

    lines = [
        f"🤖 {bot_name} 功能介绍",
        "",
        "✅ 当前主要能力：",
        *[f"• {x}" for x in top_features],
        "",
        "📌 已启用功能明细：",
        *[f"• {x}" for x in details[:20]],
        "",
        "💡 提示：不同机器人可以在 .env 里配置不同功能开关。",
    ]
    return "\n".join(lines)


@register_command("功能介绍", "机器人介绍", "功能")
async def feature_intro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply(update, context, build_feature_intro(context))


# 注册命令和按钮回调
def register_menu_handlers(app):
    app.add_handler(CommandHandler("start_menu", start_menu))
    app.add_handler(CommandHandler("features", feature_intro))
    app.add_handler(CommandHandler("intro", feature_intro))
    app.add_handler(CallbackQueryHandler(menu_button_handler))
