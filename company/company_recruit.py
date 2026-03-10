
# 发布招聘

import json
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler
from command_router import register_command
from utils import COMPANY_FILE, RECRUIT_FILE, load_json, save_json, safe_reply, group_allowed
from datetime import datetime




def get_company(chat_id, user_id):
    data = load_json(COMPANY_FILE)
    return data.get(chat_id, {}).get(user_id)




from utils import load_json, save_json, safe_reply


@register_command("发布招聘")
async def publish_recruit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)

    company = get_company(chat_id, user_id)
    if not company or company.get("owner") != user_id:
        return await safe_reply(
            update, context, "❌ 你没有公司或不是老板，不能发布招聘。"
        )

    if not context.args:
        return await safe_reply(update, context, "❗ 用法：/publish_recruit <招聘人数>")

    try:
        recruit_count = int(context.args[0])
        if recruit_count <= 0:
            return await safe_reply(update, context, "❌ 招聘人数必须是正整数。")
    except ValueError:
        return await safe_reply(update, context, "❌ 招聘人数格式错误，请输入数字。")

    recruit_data = load_json(RECRUIT_FILE)
    recruit_data.setdefault(chat_id, {})

    recruit_data[chat_id][user_id] = {
        "company_name": company.get("name"),
        "owner": user_id,
        "recruit_count": recruit_count,
        "current_count": 0,
        "company_id": user_id,
        "auto_accept": False,
    }
    save_json(RECRUIT_FILE, recruit_data)
    await safe_reply(
        update,
        context,
        f"✅ 公司【{company.get('name')}】已发布招聘，招聘人数：{recruit_count}，等待员工加入。",
    )


@register_command("应聘")
async def join_company(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)
    user_name = user.full_name

    if not context.args:
        return await safe_reply(update, context, "❗用法：/join_company <公司名称>")

    company_name = " ".join(context.args).strip()
    company_data = load_json(COMPANY_FILE)
    group_companies = company_data.get(chat_id, {})

    # ✅ 检查该用户是否已加入任意公司
    for cid, company in group_companies.items():
        members = company.get("members", {})
        if user_id in members:
            return await safe_reply(
                update,
                context,
                f"⚠️ 你已经加入了公司【{company.get('name', '未知')}】，无法重复加入。",
            )

    # ✅ 查找目标公司
    target_company = None
    for owner_id, company in group_companies.items():
        if company.get("name") == company_name:
            target_company = company
            break

    if not target_company:
        return await safe_reply(update, context, f"❌ 未找到公司【{company_name}】。")

    # ✅ 判断招聘人数限制
    recruit_limit = target_company.get("recruit_limit", 3)
    members = target_company.setdefault("members", {})
    if len(members) >= recruit_limit:
        return await safe_reply(update, context, "⚠️ 该公司已满员，无法加入。")

    # ✅ 加入公司
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    members[user_id] = {"name": user_name, "joined_at": now_str}

    save_json(COMPANY_FILE, company_data)
    await safe_reply(update, context, f"🎉 你已成功加入公司【{company_name}】！")


@register_command("解聘")
async def fire_employee(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    owner_id = str(user.id)  # 老板 ID

    if not context.args:
        return await safe_reply(update, context, "❗用法：/fire <成员ID或名字的一部分>")

    target_input = " ".join(context.args).strip()
    company_data = load_json(COMPANY_FILE)
    group_companies = company_data.get(chat_id, {})

    # ✅ 检查老板是否有公司
    my_company = group_companies.get(owner_id)
    if not my_company:
        return await safe_reply(update, context, "⚠️ 你还没有公司，无法解聘。")

    # ✅ 检查是不是老板
    if str(user.id) != owner_id:
        return await safe_reply(update, context, "⚠️ 只有公司老板才能解聘员工。")

    members = my_company.get("members", {})
    if not members:
        return await safe_reply(update, context, "⚠️ 你的公司没有员工可解聘。")

    # ✅ 查找匹配成员（支持 ID 或名字模糊匹配）
    target_user_id = None
    for uid, info in members.items():
        if uid == target_input or target_input in info.get("name", ""):
            target_user_id = uid
            break

    if not target_user_id:
        return await safe_reply(update, context, f"❌ 未找到匹配的员工：{target_input}")

    # ✅ 移除成员
    fired_name = members[target_user_id]["name"]
    del members[target_user_id]

    save_json(COMPANY_FILE, company_data)
    await safe_reply(update, context, f"🛑 你已解聘员工【{fired_name}】。")


@register_command("公司员工", "我的员工")
async def list_company_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)

    company_data = load_json(COMPANY_FILE)
    company = company_data.get(chat_id, {}).get(user_id)

    if not company:
        return await safe_reply(update, context, "你还没有公司，无法查看员工。")

    members = company.get("members", {})
    if not members:
        return await safe_reply(update, context, "公司目前没有员工。")

    lines = [f"🏢 公司【{company.get('name')}】员工列表："]
    for mid, info in members.items():
        lines.append(f"- {info['name']}（ID: {mid}），加入时间：{info['joined_at']}")

    await safe_reply(update, context, "\n".join(lines))


# 📋 查看可入职公司
@register_command("招聘列表")
async def list_recruits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    recruit_data = load_json(RECRUIT_FILE)

    group_recruits = recruit_data.get(chat_id, {})
    if not group_recruits:
        return await safe_reply(update, context, "❌ 当前没有公司在招聘。")

    text = "📃 当前招聘公司列表：\n"
    for owner_id, info in group_recruits.items():
        text += f"- {info['company_name']}（老板 ID: {owner_id}）\n"

    await safe_reply(update, context, text)


# 🚫 老板关闭招聘
@register_command("关闭招聘")
async def close_recruit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)

    recruit_data = load_json(RECRUIT_FILE)
    group_recruits = recruit_data.get(chat_id, {})
    if user_id not in group_recruits:
        return await safe_reply(update, context, "❌ 你没有在招聘。")

    del group_recruits[user_id]
    save_json(RECRUIT_FILE, recruit_data)

    await safe_reply(update, context, "✅ 已关闭招聘信息。")
    
# 后期可以单独拿出来
@register_command("辞职")
async def resign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = str(update.effective_chat.id)
    user_id = str(user.id)  # 员工自己

    company_data = load_json(COMPANY_FILE)
    group_companies = company_data.get(chat_id, {})

    # 查找用户所在公司
    found_company_id = None
    for owner_id, company in group_companies.items():
        members = company.get("members", {})
        if user_id in members:
            found_company_id = owner_id
            break

    if not found_company_id:
        return await safe_reply(update, context, "⚠️ 你目前没有加入任何公司，无法辞职。")

    # 移除自己
    company = group_companies[found_company_id]
    resigned_name = company["members"][user_id]["name"]
    del company["members"][user_id]

    save_json(COMPANY_FILE, company_data)
    await safe_reply(update, context, f"🛑 你已成功从公司【{company['name']}】辞职。")



def register_recruit_handlers(app):
    app.add_handler(CommandHandler("publish_recruit", publish_recruit))
    app.add_handler(CommandHandler("list_recruits", list_recruits))
    app.add_handler(CommandHandler("apply_company", join_company))
    app.add_handler(CommandHandler("fire_employee", fire_employee))
    app.add_handler(CommandHandler("close_recruit", close_recruit))
    app.add_handler(CommandHandler("list_company_members", list_company_members))
    # 辞职
    app.add_handler(CommandHandler("resign", resign))
