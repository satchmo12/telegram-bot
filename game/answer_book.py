import random
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

from command_router import register_command
from utils import load_json

ANSWER_BOOK_FILE = "config_data/all.json"

ANSWER_BOOK = [
    "相信自己，你的直觉不会错。",
    "勇敢迈出第一步，答案就在前方。",
    "耐心等待，时机会到来。",
    "不要害怕失败，经验是成长的宝藏。",
    "向内心倾听，那里藏着真理。",
    "机会正在悄悄靠近，准备好迎接吧。",
    "保持乐观，困难只是暂时的。",
    "你的努力终将开花结果。",
    "放松心情，一切都会水到渠成。",
    "相信缘分，它会指引你的方向。",
    "现在不是放弃的时候，继续努力。",
    "每一次挑战，都是成长的机会。",
    "内心的声音值得你去听。",
    "拥抱变化，迎接新的开始。",
    "坚持梦想，未来会更好。",
    "失败是成功的垫脚石。",
    "你比自己想象的更坚强。",
    "脚踏实地，一步步前进。",
    "不要害怕未知，勇敢尝试。",
    "专注当下，未来自然明朗。",
    "用心感受生活的美好。",
    "相信时间的力量，一切都会好。",
    "学会放下，给自己一个新的开始。",
    "保持好奇心，探索无限可能。",
    "善待自己，你值得被爱。",
    "让心灵保持纯净，迎接幸福。",
    "今天的努力是明天的收获。",
    "对自己温柔一点，慢慢来。",
    "和积极的人在一起，能量倍增。",
    "勇敢表达，沟通让问题迎刃而解。",
    "未来充满希望，别忘了微笑。",
    "学会感恩，生活会更美好。",
    "不要把事情想得太复杂。",
    "每一次尝试，都是胜利。",
    "相信自己有改变世界的力量。",
    "给自己时间，成长需要过程。",
    "调整心态，迎接新的挑战。",
    "你的付出不会被辜负。",
    "活在当下，享受生活的美。",
    "用微笑面对困难。",
    "相信自己的选择。",
    "生活的答案就在行动中。",
    "每天都是新的开始。",
    "付出终有回报。",
    "保持耐心，奇迹会出现。",
    "用心聆听，发现更多可能。",
    "别害怕失败，勇敢前行。",
    "让梦想照进现实。",
    "你拥有改变的力量。",
    "不忘初心，方得始终。",
    "每天进步一点点。",
    "心怀希望，未来可期。",
    "用爱和善意对待世界。",
    "自信是成功的第一步。",
    "接受不完美，拥抱真实。",
    "努力才有资格幸运。",
    "勇气源于内心的信念。",
    "把握现在，未来才会精彩。",
    "相信自己，奇迹会发生。",
    "付出总有回响。",
    "坚定信念，迎接挑战。",
    "用行动创造奇迹。",
    "人生没有白走的路。",
    "相信过程，享受成长。",
    "让每一天都有意义。",
    "用心生活，幸福自然来。",
    "保持微笑，迎接未来。",
    "相信时间会治愈一切。",
    "做最好的自己。",
    "梦想不会辜负努力的人。",
    "不怕慢，就怕停。",
    "你比你想象的更勇敢。",
    "成功属于坚持的人。",
    "每天都是新起点。",
    "内心强大，一切皆有可能。",
    "选择积极，选择快乐。",
    "坚持就是胜利。",
    "保持好心态，迎难而上。",
    "把握机会，勇敢尝试。",
    "用爱填满生活。",
    "每个结束都是新的开始。",
    "相信自己的直觉。",
    "放下过去，拥抱未来。",
    "梦想就在前方，别回头。",
    "用心感受生活的美好。",
    "你拥有无限可能。",
    "每天都是成长的机会。",
    "用行动改变命运。",
    "心态决定一切。",
    "积极面对，笑对人生。",
    "做自己想成为的人。",
    "相信爱，世界更美好。",
    "生活因你而精彩。",
]

@register_command("答案之书")
async def answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    question = " ".join(context.args).strip()
    if not question:
        await update.message.reply_text(
            "🕯️ 你现在犹豫不决，想找内心的答案。\n"
            "请在命令后输入你的问题，例如：\n"
            "答案之书 我该如何选择未来？"
        )
        return
    # answer = random.choice(ANSWER_BOOK)
    data_list = load_json(ANSWER_BOOK_FILE)
    selected = random.choice(data_list)
    answer = selected["chinese"]
    
    await update.message.reply_text(
        f"🔮 你的问题是：{question}\n\n✨ 答案之书为你指引：\n{answer}"
    )
@register_command("来点鸡汤")  
async def chicken_soup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer = random.choice(ANSWER_BOOK)
    await update.message.reply_text(
        f"{answer}"
    )
    
def register_answer_book_handlers(app):
    app.add_handler(CommandHandler("answer", answer))
    app.add_handler(CommandHandler("chicken_soup", chicken_soup))


