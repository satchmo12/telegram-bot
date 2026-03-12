import asyncio
import random
import re
import time
import difflib
from typing import Optional
from telegram import Update
from telegram.ext import (
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ParseMode
from command_router import register_command
from game.voice_reply import tts_voice_reply
from utils import (
    AD_KEYWORDS_FILE,
    BOT_OWNER_ID,
    GROUP_LIST_FILE,
    get_group_whitelist,
    INSULT_FILE,
    JOKE_FILE,
    PRAISE_FILE,
    get_first_pinyin,
    get_last_pinyin,
    group_allowed,
    load_json,
    save_json,
    safe_reply,
)

from PIL import Image, ImageFilter, ImageEnhance
import os
import cv2 as cv
import numpy as np

# ---------------- 配置 ----------------
DATA_FILE = "data/learned_pairs.json"  # 存储学习问答对

MAX_PAIR_WINDOW_SECONDS = 600  # 前后消息时间差，超过则不学习
MIN_TEXT_LEN, MAX_TEXT_LEN = 2, 60  # 可学习文本长度
MAX_BOT_QUESTION_LEN = 300  # 回复机器人时，被回复内容允许更长
MIN_SUPPORT = 1  # 最小学习次数，达到才会回复
SIMILARITY_THRESHOLD = 0.88  # 查找相似问题的匹配阈值
REPLY_PROB = 0.8  # 触发回复概率（1.0 = 总是回复）
CHAT_COOLDOWN_SECONDS = 10  # 两次回复间隔（秒）
BLOCK_PATTERNS = [r"https?://", r"t\.me/", r"@\w+"]  # 不学习/回复的内容正则
START_KEYWORDS = ["学说话"]  # 开启学习模式关键词
STOP_KEYWORDS = ["停止学习", "别学了"]  # 关闭学习模式关键词

REPLY_START_KEYWORDS = ["开启回复", "自动回复"]  # 开启关键词
REPLY_STOP_KEYWORDS = ["停止回复", "关闭回复"]  # 关闭关键词
GROUP_KEY_LEARNING = "learning_enabled"
GROUP_KEY_REPLY = "reply_enabled"
GROUP_KEY_ACTIVE_SPEAK = "active_speak_enabled"
GROUP_KEY_ACTIVE_INTERVAL = "active_speak_interval_min"
GROUP_KEY_AD_PUSH_ENABLED = "ad_push_enabled"
GROUP_KEY_AD_PUSH_MODE = "ad_push_mode"
GROUP_KEY_AD_PUSH_INTERVAL = "ad_push_interval_min"
GROUP_KEY_AD_PUSH_TEXT = "ad_push_text"
GROUP_KEY_AD_PUSH_TIMES = "ad_push_times"
ACTIVE_SPEAK_DEFAULT_INTERVAL_MIN = 120
ACTIVE_SPEAK_MIN_INTERVAL_MIN = 1
ACTIVE_SPEAK_MAX_INTERVAL_MIN = 1440
AD_PUSH_MODE_INTERVAL = "interval"
AD_PUSH_MODE_FIXED = "fixed"
AD_PUSH_DEFAULT_INTERVAL_MIN = 120
AD_PUSH_MIN_INTERVAL_MIN = 5
AD_PUSH_MAX_INTERVAL_MIN = 1440

JOKE_CACHE = {}
last_active_speak_ts = {}
last_ad_push_ts = {}
last_ad_push_slot = {}

# ---------------- 数据存储 ----------------

last_msg = {}  # 记录各群最后一条消息，用于学习前后消息关系
last_reply_ts = {}  # 记录各群上次回复时间，用于控制冷却


def get_memory() -> dict:
    data = load_json(DATA_FILE)
    return data if isinstance(data, dict) else {}


def get_group_toggle(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: str,
    key: str,
    *,
    default: bool,
) -> bool:
    """
    从 groups.json 读取开关；若未配置则按默认值写回。
    """
    groups = get_group_whitelist(context)
    cfg = groups.get(chat_id, {})
    value = cfg.get(key, None)
    if isinstance(value, bool):
        return value

    cfg[key] = default
    groups[chat_id] = cfg
    save_json(GROUP_LIST_FILE, groups)
    return default


def set_group_toggle(
    context: ContextTypes.DEFAULT_TYPE, chat_id: str, key: str, value: bool
):
    groups = get_group_whitelist(context)
    cfg = groups.get(chat_id, {})
    cfg[key] = bool(value)
    groups[chat_id] = cfg
    save_json(GROUP_LIST_FILE, groups)


def get_group_int_config(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: str,
    key: str,
    *,
    default: int,
    min_value: int,
    max_value: int,
) -> int:
    groups = get_group_whitelist(context)
    cfg = groups.get(chat_id, {})
    value = cfg.get(key, None)
    if isinstance(value, int):
        value = max(min_value, min(max_value, value))
    else:
        value = default
    cfg[key] = value
    groups[chat_id] = cfg
    save_json(GROUP_LIST_FILE, groups)
    return value


def get_runtime_chat_key(context: ContextTypes.DEFAULT_TYPE, chat_id: str) -> str:
    bot_name = context.application.bot_data.get("name", "default")
    return f"{bot_name}:{chat_id}"


# ---------------- 工具函数 ----------------
def normalize(text: str) -> str:
    """规范化文本：小写、去掉网址、重复字符、尾部标点"""
    t = text.strip().lower()
    t = re.sub(r"https?://\S+|t\.me/\S+|@\S+", "", t)
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"(.)\1{2,}", r"\1\1", t)
    t = re.sub(r"ha{2,}", "ha", t)
    t = re.sub(r"[。！!？?]+$", "", t)
    return t


def looks_ok(text: str) -> bool:
    """判断文本是否适合学习或回复"""
    return looks_ok_with_limit(text, max_len=MAX_TEXT_LEN)


def looks_ok_with_limit(text: str, *, max_len: int) -> bool:
    """判断文本是否适合学习或回复（可指定最大长度）"""
    if not text:
        return False
    if len(text) < MIN_TEXT_LEN or len(text) > max_len:
        return False
    if any(re.search(p, text, flags=re.I) for p in BLOCK_PATTERNS):
        return False
    if re.fullmatch(r"[\W_]+", text):
        return False
    return True


def find_similar_key(nq: str) -> Optional[str]:
    """
    在 memory 中查找与 nq 最相似的已学习问题
    返回相似问题的 key 或 None
    """
    memory = get_memory()
    if nq in memory:
        return nq
    best, best_ratio = None, 0.0
    for k in memory.keys():
        if abs(len(k) - len(nq)) > 10:
            continue
        r = difflib.SequenceMatcher(a=k, b=nq).ratio()
        if r > best_ratio:
            best_ratio, best = r, k
    return best if best_ratio >= SIMILARITY_THRESHOLD else None


def add_pair(q_text: str, a_text: str):
    """将一对问答加入 memory，并保存到磁盘"""
    memory = get_memory()
    nq = normalize(q_text)
    if nq not in memory:
        memory[nq] = {"answers": {}, "total": 0}
    answers = memory[nq]["answers"]
    answers[a_text] = answers.get(a_text, 0) + 1
    memory[nq]["total"] += 1
    save_json(DATA_FILE, memory)


def weighted_choice(answers_dict: dict) -> str:
    """根据权重随机选择答案"""
    texts, weights = zip(*answers_dict.items())
    return random.choices(texts, weights=weights, k=1)[0]


def _normalize_keywords(raw_keywords) -> list[str]:
    keywords = []
    if isinstance(raw_keywords, list):
        keywords = raw_keywords
    elif isinstance(raw_keywords, dict):
        # 兼容多群配置：{chat_id: [kw1, kw2, ...]}
        for _, value in raw_keywords.items():
            if isinstance(value, list):
                keywords.extend(value)
    else:
        return []

    return [
        kw.strip().lower()
        for kw in keywords
        if isinstance(kw, str) and kw.strip()
    ]


def _contains_ad(text: str, keywords: list[str]) -> bool:
    if not text:
        return False
    t = text.lower()
    return any(kw in t for kw in keywords)


def remove_mosaic_command1(image_path: str, output_dir: str = "downloads") -> str:
    """
    去马赛克/增强模糊区域：
    输入图片路径，返回处理后的图片路径（临时文件夹）。

    参数:
        image_path: 原始图片路径
        output_dir: 输出目录（默认 downloads/）

    返回:
        处理后的图片路径
    """
    if not os.path.exists(image_path):
        raise FileNotFoundError(f"图片不存在: {image_path}")

    # 确保输出目录存在
    os.makedirs(output_dir, exist_ok=True)

    # 打开图片
    img = Image.open(image_path)

    # 尝试增强模糊区域
    enhanced_img = img.filter(
        ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3)
    )
    enhancer = ImageEnhance.Contrast(enhanced_img)
    enhanced_img = enhancer.enhance(2)

    # 生成输出路径
    base_name = os.path.basename(image_path)
    name, ext = os.path.splitext(base_name)
    output_path = os.path.join(output_dir, f"{name}_enhanced{ext}")

    # 保存处理后的图片
    enhanced_img.save(output_path)

    return output_path


def remove_mosaic_command2(image_path: str, output_dir: str = "downloads") -> str:
    """
    去马赛克 / 模糊增强（实验性）
    ⚠️ 不能真实还原，只是增强可读性
    """

    if not os.path.exists(image_path):
        raise FileNotFoundError(f"图片不存在: {image_path}")

    os.makedirs(output_dir, exist_ok=True)

    # 打开图片（统一转 RGB，避免 PNG / RGBA 问题）
    img = Image.open(image_path).convert("RGB")

    # -------- 1️⃣ 轻度去噪（防止后面锐化炸点） --------
    img = img.filter(ImageFilter.MedianFilter(size=3))

    # -------- 2️⃣ 第一次轻锐化 --------
    img = img.filter(ImageFilter.UnsharpMask(radius=1.5, percent=120, threshold=3))

    # -------- 3️⃣ 提升对比度 --------
    img = ImageEnhance.Contrast(img).enhance(1.4)

    # -------- 4️⃣ 提升清晰度（对文字类马赛克有点用） --------
    img = ImageEnhance.Sharpness(img).enhance(1.6)

    # -------- 5️⃣ 第二次重锐化（核心） --------
    img = img.filter(ImageFilter.UnsharpMask(radius=2.5, percent=180, threshold=5))

    # -------- 6️⃣ 可选：小幅放大（对马赛克“块”有帮助） --------
    w, h = img.size
    if max(w, h) < 2000:  # 防止超大图炸内存
        img = img.resize((int(w * 1.2), int(h * 1.2)), Image.BICUBIC)

    # -------- 保存 --------
    base_name = os.path.basename(image_path)
    name, ext = os.path.splitext(base_name)
    output_path = os.path.join(output_dir, f"{name}_enhanced{ext}")

    img.save(output_path, quality=95, subsampling=0)

    return output_path


def remove_mosaic_command(image_path: str, output_dir: str = "downloads") -> str:
    # 1️⃣ 创建输出目录
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    # 2️⃣ 读取原图
    img = cv.imread(image_path)
    if img is None:
        raise FileNotFoundError(f"图像不存在或无法读取: {image_path}")

    # 3️⃣ 自动生成 mask（检测亮色区域作为干扰）
    gray = cv.cvtColor(img, cv.COLOR_BGR2GRAY)
    _, mask = cv.threshold(gray, 200, 255, cv.THRESH_BINARY)  # 阈值可调整
    kernel = np.ones((3, 3), np.uint8)
    mask = cv.dilate(mask, kernel, iterations=1)

    # 4️⃣ 修复图像（去马赛克/水印）
    repaired = cv.inpaint(img, mask, 3, cv.INPAINT_TELEA)

    # 5️⃣ 去噪 & 对比度增强
    denoised = cv.fastNlMeansDenoisingColored(repaired, None, 10, 10, 7, 21)
    lab = cv.cvtColor(denoised, cv.COLOR_BGR2LAB)
    l, a, b = cv.split(lab)
    clahe = cv.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    l = clahe.apply(l)
    enhanced = cv.merge((l, a, b))
    enhanced = cv.cvtColor(enhanced, cv.COLOR_LAB2BGR)

    # 6️⃣ 保存输出文件，统一命名为 <原图名>_enhanced.jpg
    base_name = os.path.splitext(os.path.basename(image_path))[0]
    output_path = os.path.join(output_dir, f"{base_name}_enhanced.jpg")
    cv.imwrite(output_path, enhanced)

    return output_path


# ---------------- 命令与消息处理 ----------------
@group_allowed
@register_command("给我骂")
async def insult_someone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """给指定用户发送骂人消息"""
    if not update.message or not update.message.reply_to_message:
        return await safe_reply(update, context, "请回复你要骂的用户。")

    target_user = update.message.reply_to_message.from_user
    # 🚫 如果回复的是机器人（包括本 bot）
    owner_id = context.application.bot_data.get("owner_id", BOT_OWNER_ID)
    if target_user.id == owner_id:
        return  # 直接不执行、不提示（或你也可以提示）

    chat_id = str(update.effective_chat.id)
    args = context.args
    try:
        times = int(args[0]) if args else 1
        times = max(1, min(times, 10))
    except Exception:
        times = 1

    data = load_json(INSULT_FILE)
    if chat_id not in data:
        data[chat_id] = []

    insult_list = data[chat_id] if data[chat_id] else ["你死！", "别惹我！", "小心点！"]
    target_user = update.message.reply_to_message.from_user

    for i in range(times):
        mention = (
            f'<a href="tg://user?id={target_user.id}">{target_user.first_name}</a>'
        )
        insult_text = f"{mention}，{random.choice(insult_list)}"

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=insult_text,
            reply_to_message_id=update.message.reply_to_message.message_id,
            parse_mode=ParseMode.HTML,
        )
        if i != times - 1:
            await asyncio.sleep(10)


@group_allowed
@register_command("给我夸")
async def praise_someone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """给指定用户发送夸人消息"""
    if not update.message or not update.message.reply_to_message:
        return await safe_reply(update, context, "请回复你要夸的用户。")

    target_user = update.message.reply_to_message.from_user
    # 🚫 如果回复的是机器人（包括本 bot）
    if target_user.is_bot:
        return  # 不执行

    chat_id = str(update.effective_chat.id)
    args = context.args
    try:
        times = int(args[0]) if args else 1
        times = max(1, min(times, 10))  # 最多10条
    except Exception:
        times = 1

    # 加载夸人语句列表
    data = load_json(PRAISE_FILE)
    if chat_id not in data:
        data[chat_id] = []

    praise_list = (
        data[chat_id]
        if data[chat_id]
        else [
            "你真棒！",
            "你太优秀了！",
            "今天也很可爱呢！",
            "赞一个！",
            "你让人开心！",
        ]
    )

    for i in range(times):
        mention = (
            f'<a href="tg://user?id={target_user.id}">{target_user.first_name}</a>'
        )
        praise_text = f"{mention}，{random.choice(praise_list)}"

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=praise_text,
            reply_to_message_id=update.message.reply_to_message.message_id,
            parse_mode=ParseMode.HTML,
        )
        if i != times - 1:
            await asyncio.sleep(10)  # 间隔10秒发送下一条


async def _add_word(update: Update, context: ContextTypes.DEFAULT_TYPE, word_type: str):
    """
    通用添加词函数
    word_type: "insult" 或 "praise"
    """
    chat_id = str(update.effective_chat.id)
    file_path = INSULT_FILE if word_type == "insult" else PRAISE_FILE
    data = load_json(file_path)

    if chat_id not in data:
        data[chat_id] = []

    # 优先使用回复消息，否则使用命令参数
    if update.message.reply_to_message and update.message.reply_to_message.text:
        new_word = update.message.reply_to_message.text.strip()
    else:
        new_word = " ".join(context.args).strip()

    if not new_word:
        msg = "请回复一条消息或在命令后输入你想添加的词。"
        return await safe_reply(update, context, msg)

    if new_word in data[chat_id]:
        msg = "这个词已经在列表里了。"
        return await safe_reply(update, context, msg)

    data[chat_id].append(new_word)
    save_json(file_path, data)

    msg = f"已添加新{'骂词' if word_type=='insult' else '赞美词'}：{new_word}"
    await safe_reply(update, context, msg)


# 注册两个命令
@group_allowed
@register_command("添加脏话")
async def add_insult_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _add_word(update, context, word_type="insult")


@group_allowed
@register_command("添加赞美")
async def add_praise_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _add_word(update, context, word_type="praise")


# ---------------- 自动学习 & 小雅开关 ----------------
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.effective_user:
        return

    #   忽略机器人消息（包括自己和其他机器人）
    if update.effective_user.is_bot:
        return

        # 忽略机器人消息（包括自己和其他机器人）
    if update.effective_user.is_bot:
        return

    chat_id = str(update.effective_chat.id)
    runtime_chat_key = get_runtime_chat_key(context, chat_id)
    user_id = update.effective_user.id
    msg = update.effective_message

    # ---------------- 安全检查群组 ----------------
    groups = get_group_whitelist(context)
    if chat_id not in groups:
        # 群组未注册，跳过学习/回复
        return

    # ---------------- 安全读取开关 ----------------
    def get_group_toggle_safe(chat_id: str, key: str, default: bool) -> bool:
        cfg = groups.get(chat_id)
        if cfg is None:
            return default
        value = cfg.get(key)
        return value if isinstance(value, bool) else default

    learning_enabled = get_group_toggle_safe(chat_id, GROUP_KEY_LEARNING, True)
    reply_enabled = get_group_toggle_safe(chat_id, GROUP_KEY_REPLY, False)

    memory = get_memory()

    # 忽略非文本消息
    if not msg.text:
        return

    # 将问答里的广告删掉
    # await cleaned_word()

    text = msg.text.strip()
    ts = time.time()

    # ---------------- 回复开关指令 ----------------
    # 开启自动回复
    if any(k in text for k in REPLY_START_KEYWORDS):
        if not reply_enabled:  # 默认关闭
            set_group_toggle(context, chat_id, GROUP_KEY_REPLY, True)
            reply_enabled = True
            await safe_reply(update, context, "✅ 自动回复已开启")
        return

    # 关闭自动回复
    if any(k in text for k in REPLY_STOP_KEYWORDS):
        if reply_enabled:  # 默认关闭
            set_group_toggle(context, chat_id, GROUP_KEY_REPLY, False)
            reply_enabled = False
            await safe_reply(update, context, "❌ 自动回复已关闭")
        return

    # ---------------- 开关指令 ----------------
    if any(k in text for k in START_KEYWORDS):
        if not learning_enabled:
            set_group_toggle(context, chat_id, GROUP_KEY_LEARNING, True)
            learning_enabled = True
            await safe_reply(update, context, "✅ 学习模式已开启")
        return

    if any(k in text for k in STOP_KEYWORDS):
        if learning_enabled:
            set_group_toggle(context, chat_id, GROUP_KEY_LEARNING, False)
            learning_enabled = False
            await safe_reply(update, context, "❌ 学习模式已关闭")
        return

    ad_keywords = _normalize_keywords(load_json(AD_KEYWORDS_FILE))
    # ---------------- 学习模式 ----------------
    if learning_enabled:
        # 规则1：用户直接回复机器人消息时，学习为「机器人消息 -> 用户回复」
        replied_msg = update.message.reply_to_message if update.message else None
        if (
            replied_msg
            and replied_msg.from_user
            and int(replied_msg.from_user.id) == int(context.bot.id)
            and replied_msg.text
        ):
            q_text = replied_msg.text.strip()
            a_text = text
            if (
                looks_ok_with_limit(q_text, max_len=MAX_BOT_QUESTION_LEN)
                and looks_ok(a_text)
                and not _contains_ad(q_text, ad_keywords)
                and not _contains_ad(a_text, ad_keywords)
            ):
                add_pair(q_text, a_text)

        # 规则2：默认前后句学习（原逻辑）
        prev = last_msg.get(runtime_chat_key)
        # 只处理前一条消息存在且为文本、不同用户、在时间窗口内
        if (
            prev
            and "text" in prev
            and prev["text"]
            and user_id != prev["user_id"]
            and ts - prev["ts"] <= MAX_PAIR_WINDOW_SECONDS
        ):
            if (
                looks_ok(prev["text"])
                and looks_ok(text)
                and not _contains_ad(prev["text"], ad_keywords)
                and not _contains_ad(text, ad_keywords)
            ):
                add_pair(prev["text"], text)

        # 更新当前消息为上一条消息
        last_msg[runtime_chat_key] = {"text": text, "user_id": user_id, "ts": ts}

    # ---------------- 回复逻辑 ----------------
    # if looks_ok(text):
    #     key = find_similar_key(normalize(text))
    #     if key and memory[key]["total"] >= MIN_SUPPORT:
    #         if ts - last_reply_ts.get(chat_id, 0) >= CHAT_COOLDOWN_SECONDS:
    #             if random.random() <= REPLY_PROB:
    #                 await safe_reply(
    #                     update, context, weighted_choice(memory[key]["answers"])
    #                 )
    #                 # safe_reply(update, context,weighted_choice(memory[key]["answers"]))
    #                 last_reply_ts[chat_id] = ts

    if reply_enabled and looks_ok(text):  # 默认关闭
        key = find_similar_key(normalize(text))
        if key and memory[key]["total"] >= MIN_SUPPORT:
            if ts - last_reply_ts.get(runtime_chat_key, 0) >= CHAT_COOLDOWN_SECONDS:
                if random.random() <= REPLY_PROB:
                    reply_text = weighted_choice(memory[key]["answers"])
                    if _contains_ad(reply_text, ad_keywords):
                        return
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id, text=reply_text
                    )
                    last_reply_ts[runtime_chat_key] = ts


async def cleaned_word():
    memory = get_memory()
    ad_keywords = _normalize_keywords(load_json(AD_KEYWORDS_FILE))

    cleaned_data = {}

    for question, info in memory.items():
        if _contains_ad(question, ad_keywords):
            continue

        answers = info.get("answers", {})
        # 过滤广告回答
        new_answers = {
            ans: cnt
            for ans, cnt in answers.items()
            if not _contains_ad(ans, ad_keywords)
        }
        if new_answers:
            cleaned_data[question] = {
                "answers": new_answers,
                "total": sum(new_answers.values()),
            }

    print(f"清理完成，{len(memory)}： 剩余问题数：{len(cleaned_data)}")
    save_json(DATA_FILE, cleaned_data)


@group_allowed
@register_command("讲段子", "讲笑话")
async def tell_joke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    chat_id = str(update.effective_chat.id)

    # 次数参数
    try:
        times = int(context.args[0]) if context.args else 1
        times = max(1, min(times, 10))
    except Exception:
        times = 1

    data = load_json(JOKE_FILE)
    joke_list = data

    if not joke_list:
        joke_list = [
            "没有段子，但我依然坚持营业。",
            "你不写段子，我只能随机发挥。",
        ]

    # 初始化 / 重置随机池
    if chat_id not in JOKE_CACHE or not JOKE_CACHE[chat_id]:
        shuffled = joke_list.copy()
        random.shuffle(shuffled)
        JOKE_CACHE[chat_id] = shuffled

    for i in range(times):
        # 如果池子空了，重新洗牌
        if not JOKE_CACHE[chat_id]:
            shuffled = joke_list.copy()
            random.shuffle(shuffled)
            JOKE_CACHE[chat_id] = shuffled

        joke_text = JOKE_CACHE[chat_id].pop()

        await context.bot.send_message(chat_id=update.effective_chat.id, text=joke_text)

        if i != times - 1:
            await asyncio.sleep(10)


@group_allowed
@register_command("加段子", "加笑话")
async def add_joke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """向当前群添加一条段子"""
    if not update.message:
        return

    chat_id = str(update.effective_chat.id)

    # 段子内容
    joke_text = " ".join(context.args).strip()
    if not joke_text:
        return await safe_reply(update, context, "段子内容不能为空。")

    data = load_json(JOKE_FILE)

    # 去重（可选）
    if joke_text in data:
        return await safe_reply(update, context, "这条段子已经存在啦～")

    data.append(joke_text)
    save_json(JOKE_FILE, data)

    # 🔄 刷新该群的随机池，保证新段子能被抽到
    if chat_id in JOKE_CACHE:
        JOKE_CACHE.pop(chat_id)

    await context.bot.send_message(
        chat_id=update.effective_chat.id, text="✅ 段子已添加！"
    )


@group_allowed
@register_command("群主动说话", "主动说话")
async def active_speak_control(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    if update.effective_chat.type in ["group", "supergroup"]:
        username = getattr(context.bot, "username", "") or ""
        if username:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup

            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton("👉 去私聊配置", url=f"https://t.me/{username}")]]
            )
            return await update.message.reply_text(
                "请在私聊里发送“群配置”修改主动说话设置，避免群里多机器人同时响应。",
                reply_markup=keyboard,
            )
        return await safe_reply(update, context, "请私聊机器人发送“群配置”修改主动说话设置。")

    chat_id = str(update.effective_chat.id)
    group_whitelist = get_group_whitelist(context)
    group_config = group_whitelist.get(chat_id, {})

    if not group_config.get("enabled", False):
        return await safe_reply(
            update, context, "⚠️ 本群尚未启用主功能，请先使用 /addgroup 启用。"
        )

    current_enabled = get_group_toggle(
        context, chat_id, GROUP_KEY_ACTIVE_SPEAK, default=False
    )
    current_interval = get_group_int_config(
        context,
        chat_id,
        GROUP_KEY_ACTIVE_INTERVAL,
        default=ACTIVE_SPEAK_DEFAULT_INTERVAL_MIN,
        min_value=ACTIVE_SPEAK_MIN_INTERVAL_MIN,
        max_value=ACTIVE_SPEAK_MAX_INTERVAL_MIN,
    )

    if not context.args:
        return await safe_reply(
            update,
            context,
            (
                f"🗣 主动说话：{'✅ 开启' if current_enabled else '🚫 关闭'}\n"
                f"⏱ 发送频率：每 {current_interval} 分钟\n"
                "用法：\n"
                "1) 群主动说话 开启/关闭\n"
                "2) 群主动说话 频率 30"
            ),
        )

    action = context.args[0].strip().lower()
    if action in {"开", "开启", "on", "true", "1"}:
        set_group_toggle(context, chat_id, GROUP_KEY_ACTIVE_SPEAK, True)
        return await safe_reply(update, context, "✅ 主动说话已开启")

    if action in {"关", "关闭", "off", "false", "0"}:
        set_group_toggle(context, chat_id, GROUP_KEY_ACTIVE_SPEAK, False)
        return await safe_reply(update, context, "❌ 主动说话已关闭")

    new_interval = None
    if action in {"频率", "间隔", "rate"} and len(context.args) > 1:
        try:
            new_interval = int(context.args[1])
        except Exception:
            pass
    elif action.isdigit():
        new_interval = int(action)

    if new_interval is not None:
        if (
            new_interval < ACTIVE_SPEAK_MIN_INTERVAL_MIN
            or new_interval > ACTIVE_SPEAK_MAX_INTERVAL_MIN
        ):
            return await safe_reply(
                update,
                context,
                f"❗频率范围：{ACTIVE_SPEAK_MIN_INTERVAL_MIN}~{ACTIVE_SPEAK_MAX_INTERVAL_MIN} 分钟",
            )
        group_config[GROUP_KEY_ACTIVE_INTERVAL] = new_interval
        group_whitelist[chat_id] = group_config
        save_json(GROUP_LIST_FILE, group_whitelist)
        return await safe_reply(
            update, context, f"✅ 已设置主动说话频率：每 {new_interval} 分钟"
        )

    await safe_reply(
        update,
        context,
        "❗参数错误。用法：群主动说话 开启/关闭 或 群主动说话 频率 30",
    )


def _pick_active_speak_text(memory: dict, ad_keywords: list[str]) -> Optional[str]:
    if not isinstance(memory, dict) or not memory:
        return None

    question_pool = []
    answer_pool = {}

    for q, info in memory.items():
        if isinstance(q, str) and looks_ok(q) and not _contains_ad(q, ad_keywords):
            question_pool.append(q)

        answers = info.get("answers", {}) if isinstance(info, dict) else {}
        if not isinstance(answers, dict):
            continue
        for ans, cnt in answers.items():
            if (
                isinstance(ans, str)
                and looks_ok(ans)
                and not _contains_ad(ans, ad_keywords)
            ):
                try:
                    cnt_num = int(cnt or 0)
                except Exception:
                    cnt_num = 1
                answer_pool[ans] = answer_pool.get(ans, 0) + max(1, cnt_num)

    if not question_pool and not answer_pool:
        return None

    choose_question = bool(question_pool) and (
        not answer_pool or random.random() < 0.5
    )
    if choose_question:
        return random.choice(question_pool)

    answers = list(answer_pool.keys())
    weights = [max(1, answer_pool[a]) for a in answers]
    return random.choices(answers, weights=weights, k=1)[0]


def _active_speak_offset_min(runtime_chat_key: str, interval_min: int) -> int:
    interval_min = max(1, int(interval_min))
    return abs(hash(runtime_chat_key)) % interval_min


def _active_speak_jitter_sec(runtime_chat_key: str) -> int:
    # 错峰发送：不同机器人/群延迟不同秒数，避免同秒说话
    return abs(hash(f"{runtime_chat_key}:jitter")) % 15


async def _send_active_speak_with_delay(bot, chat_id: int, text: str, delay_sec: int):
    if delay_sec > 0:
        await asyncio.sleep(delay_sec)
    await bot.send_message(chat_id=chat_id, text=text)


# ---------------- 定时群发 ----------------
async def speaking_to(context: ContextTypes.DEFAULT_TYPE):
    groups = load_json(GROUP_LIST_FILE)
    memory = get_memory()
    ad_keywords = _normalize_keywords(load_json(AD_KEYWORDS_FILE))
    now_ts = time.time()

    if not isinstance(groups, dict) or not groups:
        return

    # 复制快照，避免并发写 groups.json 时触发 "dictionary changed size during iteration"
    for chat_id, cfg in list(groups.items()):
        if not isinstance(cfg, dict):
            continue
        if not cfg.get("enabled", True):
            continue
        if not cfg.get("bot_in_group", True):
            continue
        if not bool(cfg.get(GROUP_KEY_ACTIVE_SPEAK, False)):
            continue

        interval_min = cfg.get(
            GROUP_KEY_ACTIVE_INTERVAL, ACTIVE_SPEAK_DEFAULT_INTERVAL_MIN
        )
        if not isinstance(interval_min, int):
            interval_min = ACTIVE_SPEAK_DEFAULT_INTERVAL_MIN
        interval_min = max(
            ACTIVE_SPEAK_MIN_INTERVAL_MIN,
            min(ACTIVE_SPEAK_MAX_INTERVAL_MIN, interval_min),
        )

        runtime_chat_key = get_runtime_chat_key(context, str(chat_id))
        current_minute = int(now_ts // 60)
        offset_min = _active_speak_offset_min(runtime_chat_key, interval_min)

        # 确定性错峰：按分钟槽位分散不同机器人/群的触发时机
        if current_minute % interval_min != offset_min:
            continue

        if (
            now_ts - last_active_speak_ts.get(runtime_chat_key, 0)
            < interval_min * 60
        ):
            continue

        try:
            text = _pick_active_speak_text(memory, ad_keywords)
            if not text:
                continue
            last_active_speak_ts[runtime_chat_key] = now_ts
            jitter_sec = _active_speak_jitter_sec(runtime_chat_key)
            await _send_active_speak_with_delay(
                context.bot,
                int(chat_id),
                text,
                jitter_sec,
            )
            # print(f"✅ 主动说话发送成功: {chat_id} ({interval_min}min)")
        except Exception as e:
            print(f"⚠️ 主动说话发送失败: {chat_id}, {e}")


def _parse_fixed_slots(raw: str) -> list[str]:
    if not raw:
        return []
    slots = []
    for part in str(raw).replace("，", ",").split(","):
        item = part.strip()
        if len(item) != 5 or item[2] != ":":
            continue
        hh, mm = item[:2], item[3:]
        if not (hh.isdigit() and mm.isdigit()):
            continue
        h, m = int(hh), int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            slots.append(f"{h:02d}:{m:02d}")
    return sorted(set(slots))


async def ad_push_to(context: ContextTypes.DEFAULT_TYPE):
    groups = load_json(GROUP_LIST_FILE)
    now_ts = time.time()
    current_hm = time.strftime("%H:%M")

    if not isinstance(groups, dict) or not groups:
        return

    # 复制快照，避免并发写 groups.json 时触发 "dictionary changed size during iteration"
    for chat_id, cfg in list(groups.items()):
        if not isinstance(cfg, dict):
            continue
        if not cfg.get("enabled", True):
            continue
        if not bool(cfg.get(GROUP_KEY_AD_PUSH_ENABLED, False)):
            continue

        text = str(cfg.get(GROUP_KEY_AD_PUSH_TEXT, "")).strip()
        if not text:
            continue

        runtime_chat_key = get_runtime_chat_key(context, str(chat_id))
        mode = str(cfg.get(GROUP_KEY_AD_PUSH_MODE, AD_PUSH_MODE_INTERVAL)).strip().lower()

        if mode == AD_PUSH_MODE_FIXED:
            slots = _parse_fixed_slots(str(cfg.get(GROUP_KEY_AD_PUSH_TIMES, "")))
            if not slots or current_hm not in slots:
                continue
            slot_key = f"{runtime_chat_key}:{current_hm}"
            if last_ad_push_slot.get(runtime_chat_key) == slot_key:
                continue
            try:
                await context.bot.send_message(chat_id=int(chat_id), text=text)
                last_ad_push_slot[runtime_chat_key] = slot_key
            except Exception as e:
                print(f"⚠️ 广告定时发送失败: {chat_id}, {e}")
            continue

        interval_min = cfg.get(GROUP_KEY_AD_PUSH_INTERVAL, AD_PUSH_DEFAULT_INTERVAL_MIN)
        if not isinstance(interval_min, int):
            interval_min = AD_PUSH_DEFAULT_INTERVAL_MIN
        interval_min = max(AD_PUSH_MIN_INTERVAL_MIN, min(AD_PUSH_MAX_INTERVAL_MIN, interval_min))

        current_minute = int(now_ts // 60)
        offset_min = abs(hash(f"{runtime_chat_key}:ad")) % interval_min
        if current_minute % interval_min != offset_min:
            continue
        if now_ts - last_ad_push_ts.get(runtime_chat_key, 0) < interval_min * 60:
            continue

        try:
            await context.bot.send_message(chat_id=int(chat_id), text=text)
            last_ad_push_ts[runtime_chat_key] = now_ts
        except Exception as e:
            print(f"⚠️ 广告间隔发送失败: {chat_id}, {e}")


@group_allowed
@register_command("去马赛克")
async def handle_remove_mosaic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message

    photo = None

    # 情况 1：命令消息本身带图片（caption）
    if msg.photo:
        photo = msg.photo[-1]

    # 情况 2：回复了一张图片
    elif msg.reply_to_message and msg.reply_to_message.photo:
        photo = msg.reply_to_message.photo[-1]

    if not photo:
        return await safe_reply(
            update, context, "请发送图片，或回复一张图片再使用 /去马赛克"
        )

    photo_file = await photo.get_file()

    os.makedirs("downloads", exist_ok=True)
    temp_input = f"downloads/{photo_file.file_id}.jpg"
    temp_output = f"downloads/{photo_file.file_id}_enhanced.jpg"

    await photo_file.download_to_drive(temp_input)

    try:
        remove_mosaic_command(temp_input)

        with open(temp_output, "rb") as f:
            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=f,
                reply_to_message_id=msg.message_id,
            )
    finally:
        if os.path.exists(temp_input):
            os.remove(temp_input)
        if os.path.exists(temp_output):
            os.remove(temp_output)


@group_allowed
@register_command("叫", "说")
async def add_joke(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.message:
        return

    chat_id = str(update.effective_chat.id)

    # 段子内容
    joke_text = " ".join(context.args).strip()
    if not joke_text:
        return

    await tts_voice_reply(update, context)


@group_allowed
@register_command("首拼音")
async def add_joke(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.message:
        return

    text = " ".join(context.args).strip()
    current_pinyin = get_first_pinyin(text)

    # 段子内
    await safe_reply(update, context, current_pinyin)


@group_allowed
@register_command("尾拼音")
async def add_joke(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.message:
        return

    text = " ".join(context.args).strip()
    current_pinyin = get_last_pinyin(text)

    # 段子内
    await safe_reply(update, context, current_pinyin)


# ---------------- 注册处理器 ----------------
def register_my_bot_handlers(app):
    """注册 Telegram 消息和命令处理器"""
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), on_text))
    app.add_handler(CommandHandler("insult_someone", insult_someone))
    app.add_handler(CommandHandler("add_insult_word", add_insult_word))
