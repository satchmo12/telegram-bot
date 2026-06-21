from asyncio.log import logger
import json
import os
import random
from telegram import Update, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CommandHandler, ContextTypes, CallbackQueryHandler
from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest, Forbidden
import re
import time
from pypinyin import lazy_pinyin
from command_router import register_command, get_matched_command
from utils import (
    WARNINGS_FILE,
    BOT_USER_FILE,
    FORWARD_MAP_FILE,
    SHARED_SESSION_NAME,
    is_admin,
    is_super_admin,
    get_session_path,
    load_json,
    safe_reply,
)

import datetime
from pathlib import Path
import aiohttp


import asyncio
from playwright.async_api import async_playwright
import string
from telethon.errors import FloodWaitError
from telethon.tl.functions.account import CheckUsernameRequest
import itertools

OUTPUT_FILE = "found.txt"
_USERNAME_CHECK_COOLDOWN: dict[int, float] = {}

INPUT_FILE = "data/found.txt"
SCAN_STATE_FILE = "data/scan_state.json"
AUTO_SCAN_CHAT_ID = 6085551760
MASTER_BOT_NAME = str(os.getenv("MASTER_BOT_NAME", "")).strip()
CONCURRENCY = 300   # 可以根据机器调 100~500

def load_usernames():
    path = Path(INPUT_FILE)

    if not path.exists():
        raise FileNotFoundError(
            f"{INPUT_FILE} 不存在"
        )

    usernames = []

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            username = line.strip()

            if not username:
                continue

            usernames.append(username)

    return usernames

def load_scan_index():

    if not os.path.exists(
        SCAN_STATE_FILE
    ):
        return 0

    try:
        with open(
            SCAN_STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            return json.load(f).get(
                "index",
                0
            )
    except Exception:
        return 0
    
def save_scan_index(index):

    with open(
        SCAN_STATE_FILE,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            {"index": index},
            f
        )
        
def get_daily_candidates():

    usernames = load_usernames()

    if not usernames:
        return []

    daily_count = random.randint(
        80,
        250
    )

    start = load_scan_index()

    end = start + daily_count

    if end <= len(usernames):

        candidates = usernames[start:end]

    else:

        candidates = (
            usernames[start:]
            + usernames[:end % len(usernames)]
        )

    save_scan_index(
        end % len(usernames)
    )

    return candidates

async def auto_scan_job(context: ContextTypes.DEFAULT_TYPE):

    candidates = get_daily_candidates()

    if not candidates:
        return

    # ❗关键：丢到后台跑，不阻塞 event loop
    asyncio.create_task(
        run_scan_task(context, candidates)
    )

    # 立刻安排下一次（不要等扫描结束）
    await schedule_next_auto_scan(context.application)
 
async def run_scan_task(context, candidates):

    result_text = await build_username_check_result(
        context=context,
        keyword="自动扫描",
        candidates=candidates
    )

    await context.bot.send_message(
        chat_id=AUTO_SCAN_CHAT_ID,
        text=result_text
    )
 
async def schedule_next_auto_scan(application):

    next_run = get_next_run_time()

    application.job_queue.run_once(
        auto_scan_job,
        when=next_run,
        name="daily_auto_scan"
    )

    print(f"下次自动扫描时间: {next_run}")
    
def get_next_run_time():

    tomorrow = (
        datetime.datetime.now()
        + datetime.timedelta(days=1)
    )

    hour = random.randint(
        9,
        23
    )

    minute = random.randint(
        0,
        59
    )

    return tomorrow.replace(
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0
    )
    
@register_command("添加扫描库")
async def add_scan_library(update, context):
    msg = update.message

    if not msg.reply_to_message:
        await msg.reply_text("请回复包含用户名的消息。")
        return

    text = msg.reply_to_message.text or ""

    usernames = re.findall(r'@([A-Za-z0-9_]{5,32})', text)

    if not usernames:
        await msg.reply_text("未找到用户名。")
        return

    try:
        existing = {u.lower() for u in load_usernames()}
    except FileNotFoundError:
        existing = set()

    added = []

    for username in usernames:
        username = username.lower()

        if username not in existing:
            existing.add(username)
            added.append(username)

    with open(INPUT_FILE, "w", encoding="utf-8") as f:
        for username in sorted(existing):
            f.write(username + "\n")

    if added:
        await msg.reply_text(
            f"✅ 成功添加 {len(added)} 个用户名\n"
            + "\n".join(f"@{u}" for u in added)
        )
    else:
        await msg.reply_text("⚠️ 用户名已全部存在于扫描库中")
        
async def check_username(page, username: str):
    url = f"https://fragment.com/?query={username}"

    await page.goto(url, wait_until="domcontentloaded")

    # 等结果加载
    await page.wait_for_timeout(1000)

    # 判断是否存在 Unavailable
    # unavailable = await page.locator(
    #     ".tm-status-unavail"
    # ).count()

    # is_taken = unavailable > 0
    
    is_taken = False
    locator = page.locator(".tm-status-unavail")
    if await locator.count() > 0:
        text = (await locator.first.text_content() or "").strip()

        if text == "Unavailable":
            is_taken = True
            print("状态为 Unavailable")

    return username, not is_taken

async def check(session, sem, username):
    url = f"https://fragment.com/?query={username}"

    async with sem:
        try:
            async with session.get(url, timeout=10) as resp:

                if resp.status != 200:
                    return username, False

                html = await resp.text()

                # 只匹配真正的状态标签
                is_taken = 'tm-status-unavail">Unavailable<' in html

                return username,  is_taken

        except Exception as e:
            print(f"{username}: {e}")
            return username, False
        
        


async def worker(browser, usernames):
    page = await browser.new_page()

    results = []

    try:
        for username in usernames:
            result = await check_username(page, username)
            results.append(result)

            if result[1]:
                # print(f"[FOUND] {username}")
                pass
            else:
                print(f"[Unavailable] {username}")

    finally:
        await page.close()

    return results

def get_warnings_data() -> dict:
    data = load_json(WARNINGS_FILE)
    return data if isinstance(data, dict) else {}


def _normalize_group_target(raw: str):
    s = (raw or "").strip()
    if not s:
        return None
    s = re.sub(r"^https?://", "", s, flags=re.IGNORECASE)
    if s.startswith("t.me/") or s.startswith("telegram.me/"):
        s = s.split("/", 1)[1] if "/" in s else s
    s = s.strip()
    if s.startswith("@"):
        return s
    if s.lstrip("-").isdigit():
        try:
            return int(s)
        except Exception:
            return None
    return f"@{s}"


def _normalize_username(raw: str) -> str:
    s = (raw or "").strip()
    if s.startswith("@"):
        s = s[1:]
    return s.strip()


def _is_valid_tg_username(username: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", username or ""))


def _contains_chinese(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


def _to_pinyin(text: str) -> str:
    parts = lazy_pinyin(text or "")
    return "".join(parts).lower()


def _expand_alpha_wildcards(base: str, limit: int = 120) -> list[str]:
    """
    将字母位置按 a-z 做受控扩展。
    只扩展前两位字母位置，避免组合爆炸。
    """
    if not base:
        return []

    base = base.lower()
    alpha_positions = [idx for idx, ch in enumerate(base) if ch.isalpha()]
    if not alpha_positions:
        return [base]

    target_positions = alpha_positions[:2]
    results = []
    seen = set()
    letters = "abcdefghijklmnopqrstuvwxyz"

    def build(candidate_letters: tuple[str, ...]) -> str:
        chars = list(base)
        for pos, letter in zip(target_positions, candidate_letters):
            chars[pos] = letter
        return "".join(chars)

    if len(target_positions) == 1:
        for a in letters:
            value = build((a,))
            if value not in seen:
                seen.add(value)
                results.append(value)
            if len(results) >= limit:
                break
        return results

    for a in letters:
        for b in letters:
            value = build((a, b))
            if value not in seen:
                seen.add(value)
                results.append(value)
            if len(results) >= limit:
                return results

    return results


def _build_username_candidates(keyword: str) -> list[str]:
    raw = _normalize_username(keyword)
    if not raw:
        return []

    bases: list[str] = []
    if _contains_chinese(raw):
        pinyin_base = _to_pinyin(raw)
        if pinyin_base:
            bases.append(pinyin_base)
    else:
        cleaned = re.sub(r"[^A-Za-z0-9]", "", raw).lower()
        if cleaned:
            bases.append(cleaned)

    # 中文输入时，同时补一个“原样转小写后清洗”的备用基底，避免只命中拼音模式
    fallback_base = re.sub(r"[^A-Za-z0-9]", "", raw).lower()
    if fallback_base and fallback_base not in bases:
        bases.append(fallback_base)

    candidates: list[str] = []
    seen: set[str] = set()

    def push(value: str):
        value = re.sub(r"_+", "_", value).strip("_")
        if not value:
            return
        if not re.match(r"^[a-z][a-z0-9]{4,31}$", value):
            return
        if value in seen:
            return
        seen.add(value)
        candidates.append(value)

    for base in bases:
        is_five_char_third_repeat = (
            len(base) == 5
            and base[-1] == base[-2] == base[-3]
            and base.isalnum()
        )

        push(base)
        for variant in _expand_alpha_wildcards(base):
            push(variant)

        if is_five_char_third_repeat:
            continue

        if len(base) < 5:
            pad_char = base[-1:] or "a"
            push(base + pad_char * (5 - len(base)))
        if len(base) >= 5 and base[-1] == base[-2] == base[-3]:
            push(base[:2] + base[-1] * 3)

        suffixes = [
            "",
            # 三连号（最优先）
            "000","111", "222", "333", "444",
            "555", "666", "777", "888", "999",

            # 顺子
            "012", "123", "234", "345", "456",
            "567", "678", "789",

            # 倒顺
            "987", "876", "765", "654",
            "543", "432", "321",

            # 特殊
            "166", "168", "518", "520", "521", "588", "599", "618", "688","788", "899", "988", "998", "1314",
            
            "00","11", "22", "33", "44",
            "55", "66", "77", "88", "99",
        ]
        
        for suffix in suffixes:
            push(base + suffix)
            push(base + "_" + suffix if suffix else base)

        if len(base) >= 2:
            push(base[:2] + "_" + base[2:])
            push(base[:3] + "_" + base[3:] if len(base) > 3 else base)

        if len(base) >= 3:
            tail = base[-1]
            push(base[:2] + tail * 3)
            push(base[:1] + tail * 4)

    return candidates[:30] 





def _build_username_new(keyword: str, count=50) -> list[str]:
    raw = _normalize_username(keyword)
    if not raw:
        return []

    bases: list[str] = []

    # ========= 1. 中文 => 拼音 =========
    if _contains_chinese(raw):
        pinyin_base = _to_pinyin(raw)
        if pinyin_base:
            base = re.sub(r"[^a-z0-9]", "", pinyin_base.lower())
            if base:
                bases.append(base)
    else:
        cleaned = re.sub(r"[^A-Za-z0-9]", "", raw).lower()
        if cleaned:
            bases.append(cleaned)

    # fallback
    fallback_base = re.sub(r"[^A-Za-z0-9]", "", raw).lower()
    if fallback_base and fallback_base not in bases:
        bases.append(fallback_base)

    candidates: list[str] = []
    seen: set[str] = set()

    def push(value: str):
        value = re.sub(r"_+", "_", value).strip("_")
        if not value:
            return

        # 规范：不能以数字开头
        if re.match(r"^[0-9]", value):
            return

        # 必须以字母开头
        if not re.match(r"^[a-z][a-z0-9_]{4,31}$", value):
            return

        if value in seen:
            return

        seen.add(value)
        candidates.append(value)

    # ========= 2. 3位数字策略 =========
    triple_same = [str(i) * 3 for i in range(10)]
    double_same = [str(i) * 2 for i in range(10)]
    sequences = [
        "012","123","234","345","456","567","678","789",
        "987","876","765","654","543","432","321"
    ]
    fixed = ["520", "521", "1314", "168", "518", "588", "618", "688"]

    suffix_pool = triple_same + double_same + sequences + fixed

    # ========= 3. 字母/数字 pattern 扩展 =========
    def expand_pattern(base: str):
        """
        ababa -> 1-2-1-2-1
        abcab -> 1-2-3-1-2
        """
        pattern = []
        mapping = {}
        next_id = 1

        for ch in base:
            if ch not in mapping:
                mapping[ch] = str(next_id)
                next_id += 1
            pattern.append(mapping[ch])

        return "-".join(pattern)

    def apply_pattern_fill(pattern: str):
        """
        将 pattern 变成可替换结构，例如：
        1-2-1-2-1 -> a b a b a
        """
        parts = pattern.split("-")
        used = {}
        letters = "abcdefghijklmnopqrstuvwxyz0123456789"

        pool = iter(letters)
        for p in parts:
            if p not in used:
                used[p] = next(pool)
        return "".join(used[p] for p in parts)

    # ========= 4. 主逻辑 =========
    for base in bases:
        base = re.sub(r"[^a-z0-9]", "", base.lower())
        if not base:
            continue

        # 原始
        push(base)

        # pattern 版本（ababa）
        if len(base) >= 3:
            pattern = expand_pattern(base)
            pattern_variant = apply_pattern_fill(pattern)
            push(pattern_variant)

        # 中文 / 普通统一：suffix
        for suf in suffix_pool:
            push(base + suf)
            push(f"{base}{suf}")

        # 补长
        if len(base) < 5:
            pad = base[-1] if base else "a"
            push(base + pad * (5 - len(base)))

    return candidates[:count]

def get_pattern(s: str):
    mapping = {}
    pattern = []

    for ch in s:
        if ch not in mapping:
            mapping[ch] = len(mapping)
        pattern.append(mapping[ch])

    return pattern

def generate_by_structure(s: str, count=50):
    pattern = get_pattern(s)
    letters = string.ascii_lowercase + string.digits

    group_count = max(pattern) + 1
    results = []

    def dfs(group_idx, used, group_chars):
        if group_idx == group_count:
            # 根据模式还原字符串
            result = "".join(group_chars[idx] for idx in pattern)
            results.append(result)
            return

        for ch in letters:
            if ch in used:
                continue

            dfs(
                group_idx + 1,
                used | {ch},
                group_chars + [ch]
            )

            if len(results) >= count:
                return

    dfs(0, set(), [])
    return results[:count]


async def _check_username_available(context: ContextTypes.DEFAULT_TYPE, username: str) -> bool:
    try:
        from telethon import TelegramClient
        from telethon.tl.functions.account import CheckUsernameRequest
    except Exception:
        return False

    from channel.telethon_login import _get_api_creds

    api_id, api_hash = _get_api_creds()
    if not api_id or not api_hash:
        return False

    session_path = get_session_path(context, SHARED_SESSION_NAME)
    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return False
        return bool(await client(CheckUsernameRequest(username)))
    except Exception:
        return False
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass



async def _check_username_available_by_client(
    client,
    username: str
) -> bool:

    try:
        return bool(
            await client(
                CheckUsernameRequest(username)
            )
        )

    except FloodWaitError as e:
        wait_seconds = e.seconds

        logger.warning(
            f"用户名检测触发 FloodWait: {wait_seconds}s"
        )

        await asyncio.sleep(
            wait_seconds + random.randint(60, 300)
        )

        return False

    except Exception as e:
        # logger.exception(
        #     f"检测用户名失败 @{username}: {e}"
        # )
        return False
    

@register_command("注册", "注册用户名", "创建用户名", "设置用户名")
async def register_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user or not update.effective_chat:
        return
    if not is_super_admin(update.effective_user.id):
        return await safe_reply(update, context, "🚫 你不是超级管理员，无法执行此命令。")
    if update.effective_chat.type not in {"group", "supergroup", "channel"}:
        return await safe_reply(update, context, "请在目标群或频道中使用：注册用户名 <用户名>")
    if not context.args:
        return await safe_reply(update, context, "用法：注册用户名 <用户名>")

    username = _normalize_username(context.args[0])
    if not _is_valid_tg_username(username):
        return await safe_reply(
            update,
            context,
            "用户名格式不正确，需以字母开头，长度 5-32 位，只能包含字母、数字和下划线。",
        )

    now = time.time()
    last_call = _USERNAME_CHECK_COOLDOWN.get(int(update.effective_user.id), 0.0)
    if now - last_call < 10:
        wait_seconds = int(10 - (now - last_call))
        return await safe_reply(
            update, context, f"请稍后再试，剩余冷却 {wait_seconds} 秒。", auto_delete_seconds=0
        )
    _USERNAME_CHECK_COOLDOWN[int(update.effective_user.id)] = now

    try:
        from telethon import TelegramClient
        from telethon.tl.functions.channels import UpdateUsernameRequest
    except Exception:
        return await safe_reply(update, context, "❗ Telethon 未安装，请先安装依赖。")

    from channel.telethon_login import _get_api_creds

    api_id, api_hash = _get_api_creds()
    if not api_id or not api_hash:
        return await safe_reply(update, context, "❗ 未配置 Telethon API 信息。")

    session_path = get_session_path(context, SHARED_SESSION_NAME)
    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return await safe_reply(update, context, "❗ 协议号未登录，请先登录可用的小号。")

        entity = await client.get_entity(update.effective_chat.id)
        if update.effective_chat.type == "group":
            return await safe_reply(update, context, "❗ 普通群不能设置用户名，请先升级为超级群。")

        await client(UpdateUsernameRequest(entity, username))
        await safe_reply(
            update,
            context,
            f"✅ 用户名设置成功：@{username}",
            auto_delete_seconds=0,
        )
    except Exception as e:
        await safe_reply(update, context, f"❌ 用户名设置失败：{e}", auto_delete_seconds=0)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


@register_command("创建频道", "创建群组", "创建超级群")
async def create_channel_or_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    if not is_super_admin(update.effective_user.id):
        return await safe_reply(update, context, "🚫 你不是超级管理员，无法执行此命令。")
    if update.effective_chat.type != "private":
        return await safe_reply(update, context, "请私聊机器人使用：创建频道 <标题> [用户名]")
    if not context.args:
        return await safe_reply(update, context, "用法：创建频道 <标题> [用户名]")

    title = str(context.args[0]).strip()
    username = _normalize_username(context.args[1]) if len(context.args) > 1 else ""
    if not title:
        return await safe_reply(update, context, "请输入有效的标题。")
    if username and not _is_valid_tg_username(username):
        return await safe_reply(
            update,
            context,
            "用户名格式不正确，需以字母开头，长度 5-32 位，只能包含字母、数字和下划线。",
        )

    now = time.time()
    last_call = _USERNAME_CHECK_COOLDOWN.get(int(update.effective_user.id), 0.0)
    if now - last_call < 15:
        wait_seconds = int(15 - (now - last_call))
        return await safe_reply(
            update, context, f"请稍后再试，剩余冷却 {wait_seconds} 秒。", auto_delete_seconds=0
        )
    _USERNAME_CHECK_COOLDOWN[int(update.effective_user.id)] = now

    command_name = get_matched_command(update.message.text or "") or ""
    is_channel = command_name == "创建频道"

    try:
        from telethon import TelegramClient
        from telethon.tl.functions.channels import CreateChannelRequest, UpdateUsernameRequest
    except Exception:
        return await safe_reply(update, context, "❗ Telethon 未安装，请先安装依赖。")

    from channel.telethon_login import _get_api_creds

    api_id, api_hash = _get_api_creds()
    if not api_id or not api_hash:
        return await safe_reply(update, context, "❗ 未配置 Telethon API 信息。")

    session_path = get_session_path(context, SHARED_SESSION_NAME)
    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return await safe_reply(update, context, "❗ 协议号未登录，请先登录可用的小号。")

        created = await client(
            CreateChannelRequest(
                title=title,
                about=f"Created by bot: {title}",
                megagroup=not is_channel,
            )
        )
        entity = getattr(created, "chats", [None])[0]
        if not entity:
            return await safe_reply(update, context, "❌ 创建失败：未返回频道/群组实体。", auto_delete_seconds=0)

        final_username = username
        if final_username:
            try:
                await client(UpdateUsernameRequest(entity, final_username))
            except Exception as e:
                await safe_reply(
                    update,
                    context,
                    f"⚠️ 已创建成功，但用户名设置失败：{e}",
                    auto_delete_seconds=0,
                )
                final_username = ""

        chat_id = getattr(entity, "id", "")
        kind = "频道" if not getattr(entity, "megagroup", False) else "超级群"
        msg = [
            f"✅ 创建成功：{kind}",
            f"标题：{title}",
            f"ID：<code>{chat_id}</code>",
        ]
        if final_username:
            msg.append(f"用户名：@{final_username}")
        await safe_reply(update, context, "\n".join(msg), html=True, auto_delete_seconds=0)
    except Exception as e:
        await safe_reply(update, context, f"❌ 创建失败：{e}", auto_delete_seconds=0)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


@register_command("协议号查询", "查询协议号", "查协议号", "查询用户名", "用户名查询")
async def query_protocol_id_by_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user or not update.effective_chat:
        return
    if not is_super_admin(update.effective_user.id):
        return await safe_reply(update, context, "🚫 你不是超级管理员，无法执行此命令。")
    if not context.args:
        return await safe_reply(update, context, "用法：协议号查询 @用户名")

    username = _normalize_username(context.args[0])
    if not username:
        return await safe_reply(update, context, "请输入有效的用户名。")

    try:
        from telethon import TelegramClient
    except Exception:
        return await safe_reply(update, context, "❗ Telethon 未安装，请先安装依赖。")

    from channel.telethon_login import _get_api_creds

    api_id, api_hash = _get_api_creds()
    if not api_id or not api_hash:
        return await safe_reply(update, context, "❗ 未配置 Telethon API 信息。")

    session_path = get_session_path(context, SHARED_SESSION_NAME)
    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return await safe_reply(update, context, "❗ 协议号未登录，请先登录可用的小号。")

        entity = await client.get_entity(username)
        user_id = getattr(entity, "id", None)
        if not user_id:
            return await safe_reply(update, context, "未查询到该用户名对应的协议号。")

        display_name = getattr(entity, "first_name", "") or getattr(entity, "title", "") or "未知用户"
        resolved_username = _normalize_username(getattr(entity, "username", "") or username)
        await safe_reply(
            update,
            context,
            f"👤 用户：{display_name}\n"
            f"@{resolved_username}\n"
            f"🆔 协议号：<code>{user_id}</code>",
            html=True,
        )
    except Exception as e:
        await safe_reply(update, context, f"查询失败：{e}")
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


@register_command("检测", "检测用户名", "查用户名")
async def detect_username_candidates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    if not is_super_admin(update.effective_user.id):
        return await safe_reply(update, context, "🚫 你不是超级管理员，无法执行此命令。")

    msg = update.message

    if not msg.reply_to_message:
        if not context.args:
            return await safe_reply(update, context, "用法：检测 美女")
        else:
            keyword = " ".join(context.args).strip()
            candidates = _build_username_candidates(keyword)

    else:
        text = msg.reply_to_message.text or ""
        usernames = re.findall(r'@([A-Za-z0-9_]{5,32})', text)
        if not usernames:
            await msg.reply_text("未找到用户名。")
            return
        candidates = usernames
        keyword = "无"

    now = time.time()
    last_call = _USERNAME_CHECK_COOLDOWN.get(int(update.effective_user.id), 0.0)
    if now - last_call < 20:
        wait_seconds = int(20 - (now - last_call))
        return await safe_reply(update, context, f"请稍后再试，剩余冷却 {wait_seconds} 秒。", auto_delete_seconds=0)
    _USERNAME_CHECK_COOLDOWN[int(update.effective_user.id)] = now

    if not candidates:
        return await safe_reply(update, context, "请输入有效的中文或字母关键词。", auto_delete_seconds=0)
    
    result_text = await build_username_check_result(
        context=context,
        keyword=keyword,
        candidates=candidates
    )

    await safe_reply(
        update,
        context,
        result_text,
        auto_delete_seconds=0
    )

@register_command("扫描")
async def scan_username_candidates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    if not is_super_admin(update.effective_user.id):
        return await safe_reply(update, context, "🚫 你不是超级管理员，无法执行此命令。")
    
    candidates = load_usernames()
    
    result_text = await build_username_check_result(
        context=context,
        keyword="扫描",
        candidates=candidates
    )

    await safe_reply(
        update,
        context,
        result_text,
        auto_delete_seconds=0
    )

async def build_username_check_result(
    context,
    keyword: str,
    candidates: list[str],
    max_available: int = 15,
) -> str:

    from telethon import TelegramClient
    from channel.telethon_login import _get_api_creds

    api_id, api_hash = _get_api_creds()

    session_path = get_session_path(context, SHARED_SESSION_NAME)

    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()

    try:
        if not await client.is_user_authorized():
            return "❌ Session 未登录"

        available_count = 0
        checked = 0

        for username in candidates:

            checked += 1
            print(f"🔎 正在扫描: @{username}")
            
            await asyncio.sleep(random.uniform(5, 12))
            
            is_available = await _check_username_available_by_client(
                client,
                username
            )
            
            await asyncio.sleep(random.uniform(0.1, 0.3))


            # ❌ 不可用：直接跳过（不输出）
            if not is_available:
                continue

            # ✅ 可用：立刻输出
            text = f"✅ 可注册用户名：@{username}"

            await context.bot.send_message(
                chat_id=AUTO_SCAN_CHAT_ID,
                text=text
            )

            available_count += 1

            # 达到上限停止
            if available_count >= max_available:
                break
            
            
            
        # 最后只发总结（可选）
        await context.bot.send_message(
            chat_id=AUTO_SCAN_CHAT_ID,
            text=f"🔎 扫描完成：检查 {checked} 个，可用 {available_count} 个"
        )

        return "OK"

    finally:
        await client.disconnect()
        
@register_command("用户名")
async def dao_username_candidates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return

    if not is_super_admin(update.effective_user.id):
        return await safe_reply(update, context, "🚫 你不是超级管理员，无法执行此命令。")

    if not context.args:
        return await safe_reply(update, context, "用法：用户名 美女 用户名 aabbb", auto_delete_seconds=0)

    keyword = context.args[0].strip()
    # keyword = " ".join(context.args).strip()
    
    count = 500
    
    if len(context.args) >= 2:
        try:
            count = int(context.args[1])
        except ValueError:
            return await safe_reply(
                update,
                context,
                "数量必须是数字"
            )
            
    if _contains_chinese(keyword):
        candidates = _build_username_new(keyword, count)
    else:
        if len(keyword) < 5:
            candidates = generate_all_candidates(keyword)
        else:
            candidates = generate_by_structure(keyword, count)
        

    if not candidates:
        return await safe_reply(update, context, "请输入有效的中文或字母关键词。", auto_delete_seconds=0)


    usernames = candidates

    print(f"加载 {len(usernames)} 个用户名")

    sem = asyncio.Semaphore(CONCURRENCY)

    connector = aiohttp.TCPConnector(limit=CONCURRENCY)

    found_usernames = []
     
    async with aiohttp.ClientSession(connector=connector) as session:

        found_count = 0
        tasks = []

        for u in usernames:
            tasks.append(check(session, sem, u))

            # 分批执行，避免 80万 task 占内存
            if len(tasks) >= 2000:
                results = await asyncio.gather(*tasks)
                tasks = []

                for username, ok in results:
                    if ok:
                
                        found_usernames.append(username)
                        found_count += 1

                print("批次完成，当前命中:", found_count)

        # 处理剩余
        if tasks:
            results = await asyncio.gather(*tasks)

            for username, ok in results:
                if ok:
                    found_usernames.append(username)
                    found_count += 1
        
    # 返回结果
    msg = (
        f"🔎 关键词：{keyword}\n"
        f"生成数量：{len(found_usernames)}\n"
        f"示例：\n" +
        "\n".join(f"@{x}" for x in found_usernames)
    )

    await safe_reply(update, context, msg, auto_delete_seconds=0) 

def generate_all_candidates(keyword):
    chars = string.ascii_lowercase

    target_len = max(5, len(keyword))
    remain = target_len - len(keyword)

    results = set()

    # 所有前后分配方式
    for left_len in range(remain + 1):
        right_len = remain - left_len

        left_space = itertools.product(chars, repeat=left_len)
        right_space = itertools.product(chars, repeat=right_len)

        for left in left_space:
            for right in right_space:
                s = ''.join(left) + keyword + ''.join(right)
                results.add(s)

    return list(results)
    

def parse_duration(arg: str) -> datetime.timedelta:
    if arg.endswith("h"):
        return datetime.timedelta(hours=int(arg[:-1]))
    elif arg.endswith("d"):
        return datetime.timedelta(days=int(arg[:-1]))
    else:
        return datetime.timedelta(minutes=int(arg))

def register_auto_scan_jobs(app, is_main_bot):

    if not is_main_bot:
        return

    next_run = get_next_run_time()
    print(next_run)
    app.job_queue.run_once(
        auto_scan_job,
        when=next_run,
        name="daily_auto_scan"
    )
    
    logger.info(f"自动扫描已注册: {next_run}")
    
def register_auto_scan_handlers(app):

    bot_name = str(app.bot_data.get("name", "")).strip()
    register_auto_scan_jobs(app, is_main_bot=bot_name == MASTER_BOT_NAME)