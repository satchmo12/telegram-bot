
import random
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler
from command_router import register_command
from utils import COMPANY_FILE, load_json, save_json, safe_reply, group_allowed
from info.economy import change_balance, get_user_data



@register_command("公司上市")
async def ipo_company(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    chat_id = str(update.effective_chat.id)
    args = context.args

    if len(args) < 1:
        return await safe_reply(update, context,  "❌ 用法：公司上市 <公司名>")

    company_name = args[0]
    data = load_json(COMPANY_FILE)
    
        # 找公司代码示例：
    company_id = None
    for cid, cdata in data.get(chat_id, {}).items():
        if cdata.get("name") == company_name:
            company_id = cid
            break
        
    company = data.get(chat_id, {}).get(company_id)

    if not company:
        return await safe_reply(update, context,  "❌ 公司不存在。")
    if company.get("owner") != user_id:
        return await safe_reply(update, context,  "❌ 你不是这家公司的老板。")
    if company.get("listed"):
        return await safe_reply(update, context,  "❌ 公司已经上市了。")
    
    base_price = 10
    level = company.get("level", 1)
    member_count = len(company.get("members", []))
    income = company.get("income", 0)
    price = base_price + level * 2  + member_count * 0.5 + income / 1000
    price = price * (1 + random.uniform(-0.05, 0.05))  # ±5%随机调整
    price = max(price, 1)  # 最低1金币
    price = round(price, 2)  # ✅ 保留两位小数

    company["listed"] = True
    company["stock"] = {
        "total": 1000,
        "available": 1000,
        "price": price,
        "holders": {}
    }

    data.setdefault(chat_id, {})[company_id] = company
    save_json(COMPANY_FILE, data)
    await safe_reply(update, context,  f"✅ 公司 {company['name']} 成功上市，每股 {price:.2f} 金币，共1000股。")

@register_command("股票购买")
async def buy_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    chat_id = str(update.effective_chat.id)
    args = context.args

    if len(args) < 2 or not args[1].isdigit():
        return await safe_reply(update, context,  "❌ 用法：股票购买 <公司名称> <股数>")
    


    company_name, count = args[0], int(args[1])
    data = load_json(COMPANY_FILE)
    user_info = get_user_data(chat_id, user_id)
    
    # 找公司代码示例：
    company_id = None
    for cid, cdata in data.get(chat_id, {}).items():
        if cdata.get("name") == company_name:
            company_id = cid
            break

    company = data.get(chat_id, {}).get(company_id)
    if not company or not company.get("listed"):
        return await safe_reply(update, context,  "❌ 公司不存在或未上市。")

    stock_info = company["stock"]
    if stock_info["available"] < count:
        return await safe_reply(update, context,  "❌ 股票数量不足。")

    cost = int(count * stock_info["price"])  # ✅ 整数
    if user_info["balance"] < cost:
        return await safe_reply(update, context,  f"❌ 你需要 {cost} 金币，但只有 {user_info['balance']}")

    change_balance(chat_id, user_id, -cost)
    stock_info["available"] -= count
    stock_info["holders"][user_id] = stock_info["holders"].get(user_id, 0) + count

    save_json(COMPANY_FILE, data)
    await safe_reply(update, context,  f"✅ 成功购买 {count} 股 {company['name']} 的股票，共花费 {cost} 金币。")

@register_command("我的股票")
async def my_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    chat_id = str(update.effective_chat.id)
    data = load_json(COMPANY_FILE)
    holdings = []

    for cid, company in data.get(chat_id, {}).items():
        stock = company.get("stock", {})
        if user_id in stock.get("holders", {}):
            count = stock["holders"][user_id]
            holdings.append(f"🏢 {company['name']} (ID: {cid})\n📦 股票数: {count}\n💰 当前价: {stock.get('price', 0):.2f} 金币")

    if not holdings:
        return await safe_reply(update, context,  "📭 你还没有持有任何股票。")

    text = "<b>📊 你的股票持仓：</b>\n\n" + "\n\n".join(holdings)
    await safe_reply(update, context,  text, True)

@register_command("股票列表")
async def list_stocks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    data = load_json(COMPANY_FILE)
    listed_companies = []

    for cid, company in data.get(chat_id, {}).items():
        if company.get("listed"):
            stock = company.get("stock", {})
            listed_companies.append(
                f"🏢 <b>{company['name']}</b> (ID: <code>{cid}</code>)\n"
                f"💰 单价: {stock.get('price', 0):.2f} 金币\n"
                f"📦 可购股数: {stock.get('available', 0)}\n"
            )

    if not listed_companies:
        return await safe_reply(update, context,  "🚫 当前没有已上市的公司可供购买。")

    text = "<b>📈 当前可购买股票列表：</b>\n\n" + "\n".join(listed_companies)
    await safe_reply(update, context,  text, True)
    
@register_command("股票出售")
async def sell_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    chat_id = str(update.effective_chat.id)
    args = context.args

    if len(args) < 2 or not args[1].isdigit():
        return await safe_reply(update, context,  "❌ 用法：股票出售 <公司名称> <股数>")

    company_name = args[0]
    count = int(args[1])
    if count <= 0:
        return await safe_reply(update, context,  "❌ 卖出股数必须大于0")

    data = load_json(COMPANY_FILE)
    companies = data.get(chat_id, {})

    # 根据名称查找公司（取第一个匹配的）
    company_id = None
    for cid, cdata in companies.items():
        if cdata.get("name") == company_name:
            company_id = cid
            break

    if not company_id:
        return await safe_reply(update, context,  f"❌ 未找到名称为『{company_name}』的公司。")

    company = companies[company_id]
    if not company.get("listed"):
        return await safe_reply(update, context,  "❌ 公司未上市，无法交易股票。")

    stock = company.get("stock", {})
    user_shares = stock.get("holders", {}).get(user_id, 0)
    if user_shares < count:
        return await safe_reply(update, context,  f"❌ 你只有 {user_shares} 股，无法卖出 {count} 股。")

    price = stock.get("price", 0)
    gain = int(price * count)  # ✅ 保留两位小数

    # 返还金币，减少持股，增加可购股数
    change_balance(chat_id, user_id, gain)
    stock["holders"][user_id] -= count
    if stock["holders"][user_id] == 0:
        del stock["holders"][user_id]
    stock["available"] += count

    save_json(COMPANY_FILE, data)
    await safe_reply(update, context,  f"✅ 成功卖出 {count} 股『{company_name}』，获得 {gain} 金币。")


def calculate_new_price(company: dict) -> float:
    current_price = company["stock"]["price"]
    level = company.get("level", 1)
    member_count = len(company.get("members", []))
    income = company.get("income", 0)
    holders_count = len(company["stock"].get("holders", {}))

    # 计算活跃度得分，代表公司综合实力对股价的正向影响
    activity_score = (level * 2) + (member_count * 1.5) + (holders_count * 1) + (income / 1000)
    # activity_score = 0
    # 随机波动因子，范围 [-3%, +3%]
    random_factor = random.uniform(-0.03, 0.03)

    if random_factor > 0:
        # 总涨跌幅 = 活跃度影响 + 随机影响
        growth_rate = activity_score * 0.005 + random_factor  # 活跃度因子缩小到最大约0.05
    else:
        growth_rate = -activity_score * 0.005 + random_factor  # 活跃度因子缩小到最大约0.05

    # 计算新价格，四舍五入，最低为1金币
    new_price = max(1, round(current_price * (1 + growth_rate), 2)) 
    
    return new_price

async def update_stock_prices_job(context: ContextTypes.DEFAULT_TYPE):
    data = load_json(COMPANY_FILE)
    updated = 0

    for chat_id, companies in data.items():
        for cid, company in companies.items():
            stock = company.get("stock")
            if not company.get("listed") or not stock:
                continue
            
            old_price = company["stock"]["price"]
            new_price = round(old_price * random.uniform(0.6, 1.5), 2)  # 保留2位小数
            # calculate_new_price(company)
            
            company["stock"]["price"] = new_price
            company["stock"].setdefault("history", []).append(new_price)
            
            # 限制历史记录长度
            company["stock"]["history"] = company["stock"]["history"][-20:]
            
            updated += 1

    save_json(COMPANY_FILE, data)
    # print(f"[股票更新] 更新了 {updated} 家公司的股票价格。")
    
@register_command("股价历史")
async def stock_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    args = context.args

    if len(args) < 1:
        return await safe_reply(update, context,  "❌ 用法：股价历史 <公司名称>")

    company_name = args[0]
    data = load_json(COMPANY_FILE)
    companies = data.get(chat_id, {})

    # 按名称查找公司
    company = None
    for cdata in companies.values():
        if cdata.get("name") == company_name:
            company = cdata
            break

    if not company:
        return await safe_reply(update, context,  f"❌ 未找到公司『{company_name}』。")

    stock = company.get("stock", {})
    history = stock.get("history", [])

    if not history:
        return await safe_reply(update, context,  "📭 该公司暂无股价历史记录。")

    # 格式化输出历史价格（只显示最近20条）
    lines = [f"📈 {company_name} 历史股价（最近 {len(history)} 次更新）："]
    for i, price in enumerate(history[-20:], start=1):
        lines.append(f"{i}. {price:.2f} 金币")

    text = "\n".join(lines)
    await safe_reply(update, context,  text)


def register_company_ipo_handlers(app):
    app.add_handler(CommandHandler("ipo_company", ipo_company))
    app.add_handler(CommandHandler("list_stocks", list_stocks))
    app.add_handler(CommandHandler("buy_stock", buy_stock))
    app.add_handler(CommandHandler("sell_stock", sell_stock))
    app.add_handler(CommandHandler("my_stock", my_stock))
    app.add_handler(CommandHandler("stock_history", stock_history))
    
    
    