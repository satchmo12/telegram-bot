import time
import json
import random
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes
# from command_router import register_command
from command_router import register_command
from utils import COMPANY_FILE, group_allowed, load_json, save_json, safe_reply
from info.economy import change_balance, get_balance
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler
from utils import load_json, save_json, group_allowed, safe_reply
from info.economy import change_balance, get_balance
from datetime import datetime, timedelta


BASE_COST = 2000
BASE_INCOME = 200
MAX_LEVEL = 30
BASE_LIMIT = 3  # 初始招聘人数
RECRUIT_UPGRADE_STEP = 5  # 每升 5 级增加一个名额
SALARY_LOG = "data/salary_log.json"
SALARY_AMOUNT = int(BASE_INCOME * 2)  # 每人固定工资

COMPANY_TYPES = {
    "科技": 1.0,
    "餐饮": 0.9,
    "娱乐": 1.1,
    "金融": 1.2,
    "制造": 1.0,
    "教育": 0.95,
    "医药": 1.05,
    "物流": 0.85,
    "房地产": 1.15,
    "农业": 0.8,
    "能源": 1.1,
    "旅游": 0.9,
    "互联网": 1.25,
    "游戏": 1.2,
    "时尚": 1.05,
}

EVENTS = [
    {"type": "positive", "msg": "你获得了一笔天使投资！", "reward": 300},
    {
        "type": "positive",
        "msg": "与明星合作带来了大量曝光，收益翻倍！",
        "multiplier": 2,
    },
    {"type": "positive", "msg": "获得了国家补贴。", "reward": 500},
    {"type": "negative", "msg": "工厂遭遇火灾，损失惨重。", "penalty": 200},
    {"type": "negative", "msg": "员工罢工，暂时无法产出收益。", "block_income": True},
    {"type": "negative", "msg": "数据泄露事件，公司声誉受损。", "penalty": 150},
]


def get_company_data():
    return load_json(COMPANY_FILE)


def save_company_data(data):
    save_json(COMPANY_FILE, data)


def trigger_event():
    if random.random() > 0.25:
        return None
    event = random.choice(EVENTS)
    return {
        "msg": event["msg"],
        "reward": event.get("reward", 0),
        "penalty": event.get("penalty", 0),
        "multiplier": event.get("multiplier", 1),
        "block_income": event.get("block_income", False),
    }


def calculate_company_income(company: dict) -> int:
    """
    根据公司等级和员工人数计算总收入。
    - 基础收入 + 成员带来的额外收入 + 等级加成
    """
    base_income = company.get("income", 100)
    members = company.get("members", {})
    member_count = len(members)
    level = company.get("level", 1)

    # 每个员工提供基础收入的10%
    member_bonus = base_income * 0.1 * member_count

    # 等级加成：每级 +5%
    level_bonus = (base_income + member_bonus) * (0.05 * (level - 1))

    total_income = base_income + member_bonus + level_bonus
    return int(total_income)


@register_command("公司注册")
def start_company(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)
    data = get_company_data()
    group_data = data.setdefault(chat_id, {})

    if user_id in group_data:
        return safe_reply(update, context, "你已经拥有一家公司，不能再创建。")

    if len(context.args) < 2:
        return safe_reply(update, context, "用法：公司注册 [类型] [公司名称] 查看类型，发送公司类型，可直接写 互联网")

    company_type = context.args[0]
    name = " ".join(context.args[1:])

    if company_type not in COMPANY_TYPES:
        return safe_reply(
            update, context, f"公司类型无效，可选：{'，'.join(COMPANY_TYPES.keys())}"
        )

    balance = get_balance(chat_id, user_id)
    if balance < BASE_COST:
        return safe_reply(update, context, f"金币不足，开公司需要 {BASE_COST} 金币。")

    change_balance(chat_id, user_id, -BASE_COST)
    group_data[user_id] = {
        "name": name,
        "type": company_type,
        "level": 1,
        "income": int(BASE_INCOME * COMPANY_TYPES[company_type]),
        "last_collected": int(time.time()),
        "owner": user_id,
        "bossname": update.effective_user.full_name,
        "recruiting": False,
        "recruit_limit": 3,
        "members": {},
        "recruit_list": {},
        "events": [],
    }
    save_company_data(data)
    return safe_reply(
        update, context, f"🎉 成功开设「{name}」（{company_type}公司），开始经营吧！"
    )



@register_command("我的公司")
def company_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)
    data = get_company_data()
    group_data = data.get(chat_id, {})
    info = group_data.get(user_id)

    if not info:
        return safe_reply(update, context, "你还没有公司，可用 /start_company 创建。")

    now = int(time.time())
    elapsed = max(0, now - info["last_collected"])
    hours = elapsed // 3600
    max_hours = min(hours, 24)

    income_ready = hours * info["income"]
    income = calculate_company_income(info)
    income_ready = max_hours * income
    num = len(info.get("members", []))

    text = (
        f"🏢 公司名称：{info['name']}\n"
        f"📦 类型：{info['type']}\n"
        f"📈 等级：{info['level']}\n"
        f"💰 基础产出：{info['income']} 金币\n"
        f"💰 员工人数：{num} 人\n"
        f"💰 每小时产出：{income} 金币\n"
        f"⌛ 可领取收益：{income_ready} 金币"
    )
    return safe_reply(update, context, text)


@register_command("公司收益")
def collect_income(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)
    data = get_company_data()
    group_data = data.get(chat_id, {})
    info = group_data.get(user_id)

    if not info:
        return safe_reply(update, context, "你还没有公司。")

    now = int(time.time())
    elapsed = max(0, now - info["last_collected"])
    hours = elapsed // 3600

    if hours <= 0:
        return safe_reply(update, context, "⏳ 暂无收益可领取，请稍后再试。")

    max_hours = min(hours, 24)
    income = calculate_company_income(info)
    income = max_hours * income

    # # ✅ 使用新逻辑计算收入

    logs = []

    event = trigger_event()
    if event:
        logs.append(f"📣 公司事件：{event['msg']}")
        if event["penalty"]:
            change_balance(chat_id, user_id, -event["penalty"])
            logs.append(f"❌ 损失 {event['penalty']} 金币")
        if event["reward"]:
            change_balance(chat_id, user_id, event["reward"])
            logs.append(f"✅ 额外获得 {event['reward']} 金币")
        if event["block_income"]:
            return safe_reply(
                update, context, "\n".join(logs + ["⛔ 本次收益被冻结，无法领取金币"])
            )
        if event["multiplier"] > 1:
            income *= event["multiplier"]
            logs.append(f"🔥 收益倍增至 {income} 金币")

    info["last_collected"] = now
    save_company_data(data)
    change_balance(chat_id, user_id, income)
    logs.append(f"✅ 成功领取 {income} 金币（{max_hours} 小时）")
    return safe_reply(update, context, "\n".join(logs))


@register_command("公司升级")
def upgrade_company(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)
    data = get_company_data()
    group_data = data.get(chat_id, {})
    info = group_data.get(user_id)

    if not info:
        return safe_reply(update, context, "你还没有公司。")

    level = info["level"]
    
    if level >= MAX_LEVEL:
        return safe_reply(update, context, f"公司已经满级")

    cost = (level + 1) * 1500
    balance = get_balance(chat_id, user_id)

    if balance < cost:
        return safe_reply(update, context, f"升级需要 {cost} 金币，余额不足。")

    change_balance(chat_id, user_id, -cost)
    info["level"] += 1
    info["income"] += int(BASE_INCOME * 0.3)
    # 每 5 级增加 1 个招聘名额
    info["recruit_limit"] = BASE_LIMIT + info["level"] // RECRUIT_UPGRADE_STEP

    save_company_data(data)

    return safe_reply(
        update, context, f"🚀 公司成功升级至 Lv.{info['level']}，产出提升！"
    )


def format_company_types():
    return "\n".join(
        [f"🏢 {name}：收益系数 ×{rate}" for name, rate in COMPANY_TYPES.items()]
    )


@register_command("公司类型")
async def list_company_types(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "📋 可选公司类型及收益系数如下：\n\n" + format_company_types()
    await safe_reply(update, context, text)

import time
from datetime import datetime, timedelta

@register_command("发工资")
async def pay_salary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)

    company_data = load_json(COMPANY_FILE)
    salary_log = load_json(SALARY_LOG)

    company = None
    company_id = None

    # 找老板对应公司
    for cid, info in company_data.get(chat_id, {}).items():
        if info.get("owner") == user_id:
            company = info
            company_id = cid
            break

    if not company:
        return await safe_reply(
            update, context, "❌ 你不是任何公司的老板，无法发工资。"
        )

    members = company.get("members", {})
    if not members:
        return await safe_reply(update, context, "⚠️ 当前公司没有员工，无法发工资。")

    # 检查冷却时间（用时间戳）
    last_pay_ts = salary_log.get(chat_id, {}).get(company_id, {}).get("time")
    if last_pay_ts:
        last_pay_time = datetime.fromtimestamp(last_pay_ts)
        if datetime.now() - last_pay_time < timedelta(hours=24):
            remain = timedelta(hours=24) - (datetime.now() - last_pay_time)
            return await safe_reply(
                update, context, f"⌛ 你已经发过工资了，请 {remain} 后再试。"
            )

    # 计算总工资
    total_cost = len(members) * int(SALARY_AMOUNT)

    if get_balance(chat_id, user_id) < total_cost:
        return await safe_reply(
            update, context, f"❌ 余额不足，发工资需要 {total_cost} 金币。"
        )

    success_count = 0
    for member_id in members.keys():
        if member_id == user_id:
            # 跳过老板自己
            continue
        try:
            change_balance(chat_id, member_id, int(SALARY_AMOUNT))
            success_count += 1
        except Exception:
            continue

    # 扣老板的钱
    change_balance(chat_id, user_id, -total_cost)

    # 记录发工资时间（秒级时间戳）
    salary_log.setdefault(chat_id, {})[company_id] = {
        "time": int(time.time()),  # 时间戳
        "total": total_cost,
        "count": success_count,
    }
    save_json(SALARY_LOG, salary_log)

    await safe_reply(
        update,
        context,
        f"💰 成功向 {success_count} 名员工发放工资，总计 {total_cost} 金币！",
    )


@register_command("公司排行")
async def company_rank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    company_data = load_json(COMPANY_FILE).get(chat_id, {})

    if not company_data:
        return await safe_reply(update, context, "⚠️ 当前群没有任何公司数据。")
    
    
    # 按收入排序，倒序
    sorted_companies = sorted(
        company_data.items(), key=lambda x: x[1].get("income", 0), reverse=True
    )

    lines = ["🏆 公司收入排行榜 Top 10:"]
    for i, (cid, info) in enumerate(sorted_companies[:10], 1):
        name = info.get("name", "无名")
        owner = info.get("bossname") or info.get("owner", "未知")
        level = info.get("level", 1)
        base_income = info.get("income", 0)
        income = calculate_company_income(info)

        lines.append(
            f"{i}. {name} （老板: {owner}） - 等级: {level} - 收入: {income} 金币"
        )

    await safe_reply(update, context, "\n".join(lines))


@group_allowed
@register_command("公司功能", "公司命令")

async def company_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """📋公司功能用法说明！
公司注册 公司类型 公司信息 
公司收益 公司升级 公司功能
公司排行 

发布招聘 招聘列表
关闭招聘 公司员工 发工资
应聘 解聘

公司上市 股票列表 股价历史
股票购买 股票出售 我的股票

辞职
 
未开发功能
升职  投资 破产清算
"""
    await safe_reply(update, context, text)


def register_business_handlers(app):
    app.add_handler(CommandHandler("start_company", start_company))
    app.add_handler(CommandHandler("company_info", company_info))
    app.add_handler(CommandHandler("collect_income", collect_income))
    app.add_handler(CommandHandler("upgrade_company", upgrade_company))
    app.add_handler(CommandHandler("company_types", list_company_types))  # ✅ 添加新命令
    app.add_handler(CommandHandler("company_help", company_help))  # ✅ 添加新命令

    app.add_handler(CommandHandler("company_rank", company_rank))
    app.add_handler(CommandHandler("pay_salary", pay_salary))
