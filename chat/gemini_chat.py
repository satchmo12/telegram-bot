import os
import time
from typing import Optional

import httpx
from telegram import Update
from telegram.ext import ContextTypes

from utils import load_json, save_json, safe_reply


_LAST_CALL_AT: dict[tuple[int, int], float] = {}
_CHAT_COOLDOWN_UNTIL: dict[int, float] = {}
AI_USERS_FILE = os.path.join("data", "ai_chat_users.json")


def _is_master_bot(context: ContextTypes.DEFAULT_TYPE) -> bool:
    master_name = str(os.getenv("MASTER_BOT_NAME", "")).strip()
    if not master_name:
        return False
    runtime_name = str(getattr(context, "application", None).bot_data.get("name", "")).strip()
    return runtime_name == master_name


def _extract_prompt(text: str, bot_username: str) -> str:
    t = (text or "").strip()
    if not t:
        return ""
    mention = f"@{(bot_username or '').lstrip('@')}".strip()
    if mention:
        t = t.replace(mention, "").strip()
    return t


async def _gemini_generate(text: str, api_key: str) -> str:
    # Generative Language API (Gemini / AI Studio)
    # 不同账号/区域可用模型名会不一致；404 时做模型名兜底重试。
    preferred_model = str(os.getenv("GEMINI_MODEL", "")).strip()
    model_candidates = [
        preferred_model,
        "gemini-2.5-flash",
        "gemini-2.5-flash-latest",
        "gemini-1.5-flash-latest",
        "gemini-1.5-flash",
        "gemini-2.0-flash",
        "gemini-1.5-pro-latest",
        "gemini-1.5-pro",
    ]
    model_candidates = [m for m in model_candidates if m]

    params = {"key": api_key}
    payload = {"contents": [{"role": "user", "parts": [{"text": text}]}]}

    timeout = httpx.Timeout(connect=8.0, read=25.0, write=8.0, pool=10.0)
    last_exc: Optional[Exception] = None
    async with httpx.AsyncClient(timeout=timeout) as client:
        for model in model_candidates:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            try:
                resp = await client.post(url, params=params, json=payload)
                if resp.status_code == 404:
                    last_exc = httpx.HTTPStatusError(
                        f"404 model not found: {model}", request=resp.request, response=resp
                    )
                    continue
                if resp.status_code == 429:
                    # 额度/频率限制：不再换模型重试，直接抛出给上层做冷却
                    resp.raise_for_status()
                resp.raise_for_status()
                data = resp.json()
                break
            except httpx.HTTPStatusError as e:
                last_exc = e
                # 404 尝试下一个模型，其它状态直接抛出
                if getattr(e.response, "status_code", None) == 404:
                    continue
                raise
            except Exception as e:
                last_exc = e
                raise
        else:
            raise last_exc or RuntimeError("Gemini 模型不可用（404）")

    candidates = data.get("candidates") or []
    if not candidates:
        return ""
    content = (candidates[0] or {}).get("content") or {}
    parts = content.get("parts") or []
    if not parts:
        return ""
    return str((parts[0] or {}).get("text") or "").strip()


def _get_user_ai_enabled(chat_id: str, user_id: int) -> bool:
    data = load_json(AI_USERS_FILE)
    if not isinstance(data, dict):
        return False
    chat_cfg = data.get(str(chat_id))
    if not isinstance(chat_cfg, dict):
        return False
    return bool(chat_cfg.get(str(user_id), False))


def _set_user_ai_enabled(chat_id: str, user_id: int, enabled: bool) -> None:
    data = load_json(AI_USERS_FILE)
    if not isinstance(data, dict):
        data = {}
    chat_key = str(chat_id)
    chat_cfg = data.get(chat_key)
    if not isinstance(chat_cfg, dict):
        chat_cfg = {}
    if enabled:
        chat_cfg[str(user_id)] = True
    else:
        chat_cfg.pop(str(user_id), None)
    data[chat_key] = chat_cfg
    save_json(AI_USERS_FILE, data)


async def handle_gemini_ai(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    仅主机器人：
    - 群里发送「开启ai/关闭ai」切换用户级开关
    - 开启后无需 @机器人，直接对话
    返回 True 表示已处理并回复；False 表示忽略。
    """
    if not update.message or not update.effective_chat or not update.effective_user:
        return False
    chat_type = update.effective_chat.type
    if chat_type not in {"group", "supergroup", "private"}:
        return False
    if not _is_master_bot(context):
        return False

    api_key = str(os.getenv("GEMINI_API_KEY", "")).strip()
    if not api_key:
        return False

    text = (update.message.text or "").strip()
    if not text:
        return False

    chat_key = (
        str(update.effective_chat.id)
        if chat_type in {"group", "supergroup"}
        else "private"
    )

    # 用户级开关
    if text == "开启ai":
        _set_user_ai_enabled(chat_key, int(update.effective_user.id), True)
        await safe_reply(update, context, "✅ 已开启 AI 对话（仅你本人）。")
        return True
    if text == "关闭ai":
        _set_user_ai_enabled(chat_key, int(update.effective_user.id), False)
        await safe_reply(update, context, "🚫 已关闭 AI 对话（仅你本人）。")
        return True

    user_id_int = int(update.effective_user.id)

    bot_username = str(getattr(context, "bot", None).username or "").strip().lstrip("@")
    has_mention = bool(bot_username) and f"@{bot_username}" in text
    ai_enabled = _get_user_ai_enabled(chat_key, user_id_int)
    if chat_type == "private":
        if not ai_enabled:
            return False
    elif not ai_enabled and not has_mention:
        return False

    prompt = _extract_prompt(text, bot_username) if has_mention else text
    if not prompt.strip():
        return True

    # 简单防刷：同一用户同一群 3 秒内最多 1 次
    key = (int(update.effective_chat.id), user_id_int)
    now = time.time()
    last = _LAST_CALL_AT.get(key, 0.0)
    if now - last < 3.0:
        return True
    _LAST_CALL_AT[key] = now

    chat_id_int = int(update.effective_chat.id)
    cooldown_until = _CHAT_COOLDOWN_UNTIL.get(chat_id_int, 0.0)
    if cooldown_until and now < cooldown_until:
        # 不提示也行，但给个轻提示避免用户以为没触发
        await safe_reply(update, context, "AI 正在冷却中，请稍后再试。")
        return True

    try:
        answer = await _gemini_generate(prompt, api_key)
    except httpx.HTTPStatusError as e:
        status = getattr(e.response, "status_code", None)
        if status == 429:
            # 群级别冷却，避免整个群疯狂触发导致一直 429
            _CHAT_COOLDOWN_UNTIL[chat_id_int] = time.time() + 30
            await safe_reply(update, context, "AI 请求过多（429），请 30 秒后再试。")
            return True
        print(f"[Warning] Gemini HTTP 错误: status={status} err={e}")
        await safe_reply(update, context, "AI 暂时不可用（网络/接口错误），稍后再试。")
        return True
    except Exception as e:
        # 不把异常细节回显给群里（可能包含 URL/参数）
        print(f"[Warning] Gemini 调用失败: {e}")
        await safe_reply(update, context, "AI 暂时不可用，请稍后再试。")
        return True

    if not answer:
        await safe_reply(update, context, "AI 没有返回内容。")
        return True

    # 直接回复用户消息，避免刷屏
    await safe_reply(update, context, answer, auto_delete_seconds=0)
    return True
