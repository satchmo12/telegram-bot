import asyncio
import os
import json
import time
from functools import wraps
from typing import Optional
from contextvars import ContextVar
from telegram import Bot, Message, Update, User
import telegram
from telegram.ext import ContextTypes
from telegram.constants import ParseMode
from pypinyin import lazy_pinyin
from threading import Lock
from config import BOT_REPLY
from telegram.ext import Application
import shutil

BOT_ID = None
ADMIN_CACHE = {}  # {chat_id: {"admins": set(), "timestamp": float}}

bot_reply = BOT_REPLY

# ===== 文件路径 =====
DATA_DIR = "data"
CON_DATA_DIR = "config_data"


ZHENXINHUA_FILE = os.path.join(CON_DATA_DIR, "zhenxinhua.txt")
GROUP_LIST_FILE = os.path.join(DATA_DIR, "groups.json")
ADMIN_WHITELIST_FILE = os.path.join(CON_DATA_DIR, "admin_whitelist.txt")
QA_FILE = os.path.join(DATA_DIR, "qa.json")
IDIOM_FILE = os.path.join(CON_DATA_DIR, "idiom.json")
IDIOM_EXTRA_FILE = os.path.join(CON_DATA_DIR, "chengyu_extra.json")

INFO_FILE = os.path.join(DATA_DIR, "info.json")
MARRY_FILE = os.path.join(DATA_DIR, "marriages.json")
PET_FILE = os.path.join(DATA_DIR, "pet.json")
SLAVE_FILE = os.path.join(DATA_DIR, "slaves.json")
BAG_DATA_FILE = os.path.join(DATA_DIR, "bag.json")

BANK_FILE = os.path.join(DATA_DIR, "bank.json")
INTEREST_LOG_FILE = os.path.join(DATA_DIR, "bank_interest_log.json")

INVENTORY_DATA_FILE = os.path.join(DATA_DIR, "inventory.json")
LOTTERY_FILE = os.path.join(DATA_DIR, "lottery.json")

COOLDOWN_FILE = os.path.join(DATA_DIR, "cooldown.json")

COOLDOWN_FILE = os.path.join(DATA_DIR, "action_cooldowns.json")

ACTIONS_FILE = "config_data/action_config.json"
WARNINGS_FILE = os.path.join(DATA_DIR, "warnings.json")


RED_PACKET_FILE = os.path.join(DATA_DIR, "red_packet.json")

COMPANY_FILE = os.path.join(DATA_DIR, "company.json")
RECRUIT_FILE = os.path.join(DATA_DIR, "recruit.json")

CHECKIN_FILE = os.path.join(DATA_DIR, "checkin.json")

GARDEN_DATA_FILE = os.path.join(DATA_DIR, "garden_info.json")

MANAGER_FILE = os.path.join(DATA_DIR, "manager.json")
FARM_DATA_FILE = os.path.join(DATA_DIR, "farm_info.json")

ANIMALS_DATA_FILE = os.path.join(DATA_DIR, "animals_info.json")

# JSON 文件路径
AD_KEYWORDS_FILE = os.path.join(CON_DATA_DIR, "ad_keywords.json")

WHITELIST_FILE = os.path.join(CON_DATA_DIR, "ad_whitelist.json")

QA_FILE = os.path.join(DATA_DIR, "qa.json")
RE_FILE = os.path.join(DATA_DIR, "reply.json")

ACTIVITY_FILE = os.path.join(DATA_DIR, "economy_activity.json")

SYSTEM_SHOP_FILE = os.path.join(DATA_DIR, "system_shop.json")

ORDERS_FILE = os.path.join(DATA_DIR, "orders.json")

INSULT_FILE = os.path.join(DATA_DIR, "insult_words.json")
PRAISE_FILE = os.path.join(DATA_DIR, "praise_words.json")

FORWARD_MAP_FILE = os.path.join(DATA_DIR, "forward_map.json")

JOKE_FILE = os.path.join(DATA_DIR, "joke_file.json")

WORD_FILE = os.path.join(CON_DATA_DIR, "word.txt")

# 私聊机器人用户
BOT_USER_FILE = os.path.join(CON_DATA_DIR, "bot_user.json")


IDIOM_TEXT = os.path.join(CON_DATA_DIR, "chengyu_words.txt")

# 群特别关心
SPECIAL_FOLLOW_FILE = os.path.join(DATA_DIR, "special_follow.json")


_cache_data = {}
_cache_timestamp = {}
_cache_lock = Lock()
CACHE_TTL = 300  # 缓存有效时间（秒）

BOT_OWNER_ID = 6085551760  # 默认主人ID（各机器人可在 TOKEN_CONFIG 中覆盖）
BOT_RUNTIME_NAME: ContextVar[str] = ContextVar("BOT_RUNTIME_NAME", default="")
BOT_OWNER_MAP = {}


def set_runtime_bot_name(bot_name: str):
    BOT_RUNTIME_NAME.set((bot_name or "").strip())


def get_runtime_bot_name() -> str:
    return (BOT_RUNTIME_NAME.get() or "").strip()


def set_bot_owner(bot_name: str, owner_id: int):
    if not bot_name:
        return
    try:
        BOT_OWNER_MAP[str(bot_name).strip()] = int(owner_id)
    except Exception:
        pass


def get_runtime_owner_id() -> int:
    bot_name = get_runtime_bot_name()
    if bot_name and bot_name in BOT_OWNER_MAP:
        return int(BOT_OWNER_MAP[bot_name])
    return int(BOT_OWNER_ID)


def get_bot_path(context: ContextTypes.DEFAULT_TYPE, path: str) -> str:
    """
    按当前机器人名构造隔离路径。
    支持传入:
    - data/xxx.json
    - config_data/xxx.json
    """
    if not context or not getattr(context, "application", None):
        return path

    bot_name = (context.application.bot_data.get("name") or "").strip()
    if not bot_name:
        return path

    norm = str(path).replace("\\", "/")
    if norm.startswith(f"{DATA_DIR}/{bot_name}/") or norm.startswith(
        f"{CON_DATA_DIR}/{bot_name}/"
    ):
        return path

    if norm.startswith(f"{DATA_DIR}/"):
        rel = norm[len(f"{DATA_DIR}/") :]
        return os.path.join(DATA_DIR, bot_name, rel)

    if norm.startswith(f"{CON_DATA_DIR}/"):
        rel = norm[len(f"{CON_DATA_DIR}/") :]
        return os.path.join(CON_DATA_DIR, bot_name, rel)

    return path


def _resolve_json_path(path: str) -> str:
    """
    将 JSON 路径按机器人隔离:
    - data/xxx.json        -> data/<bot_name>/xxx.json
    - config_data/xxx.json -> config_data/<bot_name>/xxx.json
    仅对 .json 文件生效。
    """
    if not isinstance(path, str) or not path.endswith(".json"):
        return path

    bot_name = get_runtime_bot_name()
    if not bot_name:
        return path

    norm = path.replace("\\", "/")
    if norm.startswith(f"{DATA_DIR}/{bot_name}/") or norm.startswith(
        f"{CON_DATA_DIR}/{bot_name}/"
    ):
        return path

    if norm.startswith(f"{DATA_DIR}/"):
        rel = norm[len(f"{DATA_DIR}/") :]
        return os.path.join(DATA_DIR, bot_name, rel)

    if norm.startswith(f"{CON_DATA_DIR}/"):
        rel = norm[len(f"{CON_DATA_DIR}/") :]
        return os.path.join(CON_DATA_DIR, bot_name, rel)

    return path


# ===== 通用加载函数 =====
def safe_load_file(filepath):
    if not os.path.exists(filepath):
        return []
    with open(filepath, encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def _is_expired(path: str) -> bool:
    return (time.time() - _cache_timestamp.get(path, 0)) > CACHE_TTL


def load_json(path: str):
    """带缓存的 JSON 加载，支持 list 和 dict，自动隔离+自动复制默认配置"""

    original_path = path
    path = _resolve_json_path(path)
    # print(path)
    with _cache_lock:
        if path not in _cache_data or _is_expired(path):

            # ===== 如果隔离路径不存在 =====
            if not os.path.exists(path):

                # 判断是不是 config_data 下的文件
                bot_name = get_runtime_bot_name()
                norm = original_path.replace("\\", "/")

                if (
                    bot_name
                    and norm.startswith(f"{CON_DATA_DIR}/")
                    and not norm.startswith(f"{CON_DATA_DIR}/{bot_name}/")
                ):
                    # 原始文件路径
                    base_path = original_path

                    if os.path.exists(base_path):
                        os.makedirs(os.path.dirname(path), exist_ok=True)
                        shutil.copy2(base_path, path)
                        print(f"✅ 已为机器人复制默认配置: {path}")

                # 再次检查是否存在
                if not os.path.exists(path):
                    _cache_data[path] = {}
                    _cache_timestamp[path] = time.time()
                    return _cache_data[path]

            # ===== 正常读取 =====
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, (dict, list)):
                        _cache_data[path] = data
                    else:
                        _cache_data[path] = {}
            except:
                _cache_data[path] = {}

            _cache_timestamp[path] = time.time()

        return _cache_data[path]


def save_json(path: str, data):
    """保存 JSON（支持 list 和 dict），并更新缓存"""
    if not isinstance(data, (dict, list)):
        raise ValueError("save_json 只支持保存 dict 或 list")

    path = _resolve_json_path(path)
    with _cache_lock:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        _cache_data[path] = data
        _cache_timestamp[path] = time.time()


def invalidate_cache(path: str):
    """手动使缓存失效"""
    path = _resolve_json_path(path)
    with _cache_lock:
        _cache_data.pop(path, None)
        _cache_timestamp.pop(path, None)


# ===== 拼音处理 =====
def get_last_pinyin(word):
    return lazy_pinyin(word)[-1]


def get_first_pinyin(word):
    return lazy_pinyin(word)[0]


def load_qa():
    data = load_json(QA_FILE)
    return data if isinstance(data, dict) else {}


def save_qa(data):
    save_json(QA_FILE, data)


def load_idioms():
    if not os.path.exists(IDIOM_FILE):
        print("成语文件不存在")
        return []

    try:
        with open(IDIOM_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            print(f"读取成语数量: {len(data)}")
            return data

            # return data
    except Exception as e:
        print(f"读取成语出错: {e}")
        return []


def save_idioms(data):
    pass
    try:
        with open(IDIOM_EXTRA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"已保存成语数量: {len(data)}")
    except Exception as e:
        print(f"保存成语出错: {e}")


def sort_idioms_by_first_letter(words):
    return sorted(words, key=lambda w: lazy_pinyin(w)[0])


def load_chengyu_words():
    """
    加载成语词表（每行一个成语）
    返回: set[str]
    """
    if not os.path.exists(IDIOM_TEXT):
        print(f"成语词表不存在: {IDIOM_TEXT}")
        return set()

    words = set()
    try:
        with open(IDIOM_TEXT, "r", encoding="utf-8") as f:
            for line in f:
                word = line.strip()
                if word:
                    words.add(word)
        print(f"已加载成语数量: {len(words)}")
        return words
    except Exception as e:
        print(f"加载成语词表出错: {e}")
        return set()


from pypinyin import lazy_pinyin


def save_chengyu_words(words, path=IDIOM_TEXT):
    """
    保存成语词表（支持拼音顺序排序）
    words: set 或 list
    path: 保存文件路径
    """
    if not words:
        print("没有需要保存的成语")
        return

    try:
        # 先去重，再按拼音排序
        sorted_words = sorted(
            set(words), key=lambda w: lazy_pinyin(w)  # 按拼音列表排序
        )

        with open(path, "w", encoding="utf-8") as f:
            for w in sorted_words:
                f.write(w + "\n")

        print(f"已保存成语数量: {len(sorted_words)} （按拼音顺序排序）")
    except Exception as e:
        print(f"保存成语词表出错: {e}")


def find_idioms_by_first_char(char, idioms_data):
    return [i for i in idioms_data if i["word"].startswith(char)]


def is_valid_idiom(word, idioms_data):
    return any(i["word"] == word for i in idioms_data)


# ===== 白名单判断 =====
# 保留兼容变量，实际使用请走 get_group_whitelist()（支持多机器人隔离）
GROUP_WHITELIST = {}
SUPER_ADMINS = [int(uid) for uid in safe_load_file(ADMIN_WHITELIST_FILE)]


def get_group_whitelist(context: ContextTypes.DEFAULT_TYPE = None) -> dict:
    data = load_json(GROUP_LIST_FILE)
    if not isinstance(data, dict):
        return {}

    changed = False
    for chat_id, cfg in data.items():
        if not isinstance(cfg, dict):
            continue

        defaults = {
            "enabled": True,
            "bot_in_group": True,
            "verify": False,
            "silent": False,
            "ad_filter": False,
            "ad_push_enabled": False,
            "ad_push_mode": "interval",
            "ad_push_interval_min": 120,
            "ad_push_text": "",
            "ad_push_times": "",
            "manor": False,
            "welcome": False,
            "learning_enabled": True,
            "reply_enabled": False,
            "active_speak_enabled": False,
            "active_speak_interval_min": 120,
        }
        for key, val in defaults.items():
            if key not in cfg:
                cfg[key] = val
                changed = True

    if changed:
        save_json(GROUP_LIST_FILE, data)

    return data


def group_allowed(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type not in ["group", "supergroup"]:
            return

        chat_id = str(update.effective_chat.id)
        group_config = get_group_whitelist(context).get(chat_id)

        # 空值或类型不是 dict 时，直接跳过
        if not isinstance(group_config, dict):
            return

        if group_config.get("enabled", True):
            return await func(update, context)
        else:
            return

    return wrapper


def group_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat.type not in ["group", "supergroup"]:
            # await update.message.reply_text("❌ 此命令只能在群组中使用。")
            return
        return await func(update, context)

    return wrapper


# 高级管理员
def is_super_admin(user_id):
    return int(user_id) in SUPER_ADMINS or int(user_id) == get_runtime_owner_id()


delete_queue = asyncio.Queue()


async def delete_worker(bot: Bot):
    while True:
        msg = await delete_queue.get()
        try:
            await msg.delete()
        except Exception:
            pass
        await asyncio.sleep(0.1)  # 控制删除频率


# 安全回复
async def safe_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    html: bool = False,
    reply_markup=None,
):
    try:
        parse_mode = ParseMode.HTML if html else None
        if update.message and bot_reply:
            msg = await update.message.reply_text(
                text, parse_mode=parse_mode, reply_markup=reply_markup
            )
        else:
            msg = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
        # 后台启动删除任务，不阻塞 safe_reply
        if msg:
            context.job_queue.run_once(
                delete_message_job,
                60,
                chat_id=update.effective_chat.id,
                data=msg.message_id,
            )

            asyncio.create_task(delete_later(msg, delay=60))
        #     # await delete_queue.put(msg)  # 放入删除队列，不直接创建任务

        return msg  # 方便外部需要消息对象时用

    except telegram.error.TimedOut:
        print("[Warning] 回复消息时超时：", text)
    except Exception as e:
        print("[Error] 发送消息失败：", e)


async def delete_message_job(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    await context.bot.delete_message(chat_id=job.chat_id, message_id=job.data)


async def delete_later(msg: Message, delay: int):
    """后台延时删除消息"""
    try:
        await asyncio.sleep(delay)
        await msg.delete()
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print("[Warning] 删除消息失败：", e)


def format_reward_text(reward: dict) -> str:
    text = reward.get("text", "你获得了未知奖励")

    # 收集所有支持字段（仅用于格式化）
    format_values = {
        "balance": reward.get("balance", 0),
        "points": reward.get("points", 0),
        "luck": reward.get("luck", 0),
        "energy": reward.get("energy", 0),
        "charm": reward.get("charm", 0),
        "mood": reward.get("mood", 0),
        "stamina": reward.get("stamina", 0),
    }

    try:
        return text.format(**format_values)
    except KeyError as e:
        # 如果字段不匹配，显示原文
        return text


def apply_reward(user_data: dict, reward: dict):

    for key in ["balance", "points", "luck", "mood", "stamina", "charm", "hunger"]:
        if key in reward:
            user_data[key] = user_data.get(key, 0) + reward[key]

    return user_data


# ===== 加载词库（全局一次） =====
ZHENXINHUA_LIST = safe_load_file(ZHENXINHUA_FILE)


# 判断当前用户是否是管理员（含超级管理员）
@group_allowed
async def is_admin(update, context):
    user_id = update.effective_user.id

    if user_id in SUPER_ADMINS:
        return True

    admin_ids = await get_admin_ids(update.effective_chat.id, context)
    return user_id in admin_ids


def format_duration(seconds):
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    return f"{hours}小时{minutes}分" if hours else f"{minutes}分"


# 中文排序
def sort_shop(shop: dict) -> dict:
    """按中文拼音排序，安全兜底"""

    def safe_pinyin(word):
        try:
            # 转成字符串并去掉前后空格和不可见字符
            s = str(word).strip()
            s = "".join(c for c in s if not c.isspace())  # 去掉所有空白字符
            if not s:
                return ["zzz"]  # 空字符串排最后
            return lazy_pinyin(s)
        except Exception as e:
            print(f"safe_pinyin error: {word} -> {e}")
            return [str(word)]

    sorted_items = sorted(shop.items(), key=lambda x: safe_pinyin(x[0]))
    return dict(sorted_items)


# 通用操作


async def get_target_user(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> Optional[User]:
    # 1️⃣ 回复消息优先
    if update.message.reply_to_message:
        return update.message.reply_to_message.from_user

    # 2️⃣ 命令参数解析
    text = update.message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return None

    target = parts[1].strip()

    # 3️⃣ 如果是纯数字 ID
    if target.isdigit():
        try:
            member = await context.bot.get_chat_member(
                update.effective_chat.id, int(target)
            )
            return member.user
        except:
            return None

    # 4️⃣ 如果是 @username
    if target.startswith("@"):
        username = target[1:]
        try:
            # 遍历群成员（Telegram API 限制，这里只能尝试 get_chat_member）
            member = await context.bot.get_chat_member(
                update.effective_chat.id, username
            )
            return member.user
        except:
            return None

    return None


async def is_bot_admin(update, context):
    bot_id = context.bot.id
    admin_ids = await get_admin_ids(update.effective_chat.id, context)
    return bot_id in admin_ids


# 缓存版获取管理员 ID 集合
async def get_admin_ids(chat_id, context):
    now = time.time()

    bot_id = context.bot.id
    cache_key = (bot_id, chat_id)

    if (
        cache_key in ADMIN_CACHE
        and now - ADMIN_CACHE[cache_key]["timestamp"] < CACHE_TTL
    ):
        return ADMIN_CACHE[cache_key]["admins"]

    try:
        admins = await context.bot.get_chat_administrators(chat_id)
        admin_ids = {admin.user.id for admin in admins}

        ADMIN_CACHE[cache_key] = {"admins": admin_ids, "timestamp": now}

        return admin_ids

    except Exception as e:
        print(f"⚠️ 获取管理员列表失败: {e}")
        if cache_key in ADMIN_CACHE:
            return ADMIN_CACHE[cache_key]["admins"]
        return set()
