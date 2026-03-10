import aiohttp
import os
from typing import List, Dict

OPENROUTER_API_KEY = os.getenv(
    "OPENROUTER_API_KEY",
    "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",
)
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "mistralai/mistral-7b-instruct")
FALLBACK_MODELS = [
    "openrouter/auto",
    "deepseek/deepseek-chat",
    "google/gemini-2.0-flash-lite",
]


async def ask_ai(messages: List[Dict[str, str]]) -> str:
    if not OPENROUTER_API_KEY:
        return "未配置 OPENROUTER_API_KEY，无法使用 AI 聊天。"

    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "yourdomain.com",  # 可以随便填
        "X-Title": "TelegramBotAI"
    }
    timeout = aiohttp.ClientTimeout(total=30)
    models = []
    if OPENROUTER_MODEL:
        models.append(OPENROUTER_MODEL)
    for m in FALLBACK_MODELS:
        if m not in models:
            models.append(m)

    last_error = ""
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for model in models:
                data = {
                    "model": model,
                    "messages": messages,
                    "temperature": 0.5,
                }
                async with session.post(url, headers=headers, json=data) as resp:
                    res = await resp.json()
                    if resp.status == 200:
                        try:
                            return res["choices"][0]["message"]["content"]
                        except Exception:
                            last_error = f"模型 {model} 返回结构异常：{res}"
                            continue

                    err_msg = str(res.get("error", {}).get("message", ""))
                    last_error = f"模型 {model} 不可用：{err_msg}"
                    if "model_not_available" in str(res):
                        continue

                    # 非模型不可用错误，直接返回，避免无意义重试
                    return f"AI 请求失败：{last_error}"
    except Exception as e:
        return f"AI 请求失败：{e}"

    return f"AI 暂时不可用：{last_error}"
