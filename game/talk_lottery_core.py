"""发言抽奖独立奖池。

与积分抽奖完全隔离：
- 数据文件独立
- CRUD 函数独立
- 不依赖积分抽奖配置
"""

from __future__ import annotations

import random
from uuid import uuid4
from typing import Optional

from utils import get_group_whitelist, load_json, save_json

TALK_LOTTERY_FILE = "data/talk_lottery.json"
RATE_MIN = 0
RATE_MAX = 100
STOCK_MIN = 0


def _load_data() -> dict:
    data = load_json(TALK_LOTTERY_FILE)
    if not isinstance(data, dict):
        data = {}
    return data


def _save_data(data: dict) -> None:
    save_json(TALK_LOTTERY_FILE, data)


def _chat_key(chat_id) -> str:
    return str(chat_id)


def list_prizes(chat_id) -> list[dict]:
    data = _load_data()
    prizes = data.get(_chat_key(chat_id), [])
    return [dict(item) for item in prizes if isinstance(item, dict)]


def get_prize(chat_id, prize_id: str) -> Optional[dict]:
    for prize in list_prizes(chat_id):
        if str(prize.get("id")) == str(prize_id):
            return prize
    return None


def add_prize(chat_id, name: str, rate: int, stock: int) -> dict:
    if not str(name).strip():
        raise ValueError("奖品名称不能为空")
    if not RATE_MIN <= int(rate) <= RATE_MAX:
        raise ValueError(f"中奖率范围：{RATE_MIN}-{RATE_MAX}")
    if int(stock) < STOCK_MIN:
        raise ValueError("奖品数量不能小于 0")

    data = _load_data()
    key = _chat_key(chat_id)
    prizes = data.setdefault(key, [])
    if not isinstance(prizes, list):
        prizes = []
        data[key] = prizes

    prize = {
        "id": uuid4().hex,
        "name": str(name).strip(),
        "rate": int(rate),
        "stock": int(stock),
    }
    prizes.append(prize)
    _save_data(data)
    return dict(prize)


def update_prize(chat_id, prize_id: str, name: str, rate: int, stock: int) -> bool:
    if not str(name).strip():
        raise ValueError("奖品名称不能为空")
    if not RATE_MIN <= int(rate) <= RATE_MAX:
        raise ValueError(f"中奖率范围：{RATE_MIN}-{RATE_MAX}")
    if int(stock) < STOCK_MIN:
        raise ValueError("奖品数量不能小于 0")

    data = _load_data()
    key = _chat_key(chat_id)
    prizes = data.get(key, [])
    if not isinstance(prizes, list):
        return False

    for prize in prizes:
        if isinstance(prize, dict) and str(prize.get("id")) == str(prize_id):
            prize["name"] = str(name).strip()
            prize["rate"] = int(rate)
            prize["stock"] = int(stock)
            _save_data(data)
            return True
    return False


def delete_prize(chat_id, prize_id: str) -> bool:
    data = _load_data()
    key = _chat_key(chat_id)
    prizes = data.get(key, [])
    if not isinstance(prizes, list):
        return False

    old_len = len(prizes)
    data[key] = [
        prize
        for prize in prizes
        if not (isinstance(prize, dict) and str(prize.get("id")) == str(prize_id))
    ]
    if len(data[key]) == old_len:
        return False

    _save_data(data)
    return True


def draw_prize(chat_id) -> Optional[dict]:
    """从当前有库存的奖品中按 rate 抽取一个。

    这里仅负责独立奖池的抽取，不负责积分扣除、用户记录等业务。
    """
    data = _load_data()
    key = _chat_key(chat_id)
    prizes = data.get(key, [])
    if not isinstance(prizes, list):
        return None

    available = [
        prize
        for prize in prizes
        if isinstance(prize, dict)
        and int(prize.get("stock", 0) or 0) > 0
        and int(prize.get("rate", 0) or 0) > 0
    ]
    if not available:
        return None

    total_rate = sum(int(prize.get("rate", 0) or 0) for prize in available)
    if total_rate <= 0:
        return None

    winner = random.uniform(0, total_rate)
    cursor = 0.0
    for prize in available:
        cursor += int(prize.get("rate", 0) or 0)
        if winner < cursor:
            prize["stock"] = max(0, int(prize.get("stock", 0) or 0) - 1)
            _save_data(data)
            return dict(prize)

    return None

async def handle_talk_lottery(update, context):

    msg = update.effective_message
    if not msg:
        return


    chat_id = update.effective_chat.id

    user = update.effective_user

    if not user:
        return

    data = get_group_whitelist(context)
    chat_id_str = str(chat_id)  # ⭐ 这里改成字符串

    cfg = data.get(chat_id_str, {})

    if not bool(cfg.get('talk_lottery_enabled', False)):
        return


    # # 触发概率
    # if random.randint(1,100) > group["trigger_rate"]:
    #     return
        # =========================
    # 发言抽奖触发概率
    # 默认 100%，兼容旧配置
    # =========================
    trigger_rate = int(
        cfg.get("talk_lottery_trigger_rate", 100) or 100
    )

    # 防止配置异常
    trigger_rate = max(1, min(100, trigger_rate))

    # 概率未命中，不触发抽奖
    if random.randint(1, 100) > trigger_rate:
        return


    prize = draw_prize(chat_id)


    if not prize:
        return

    await msg.reply_text(
        f"""
🎉 幸运事件！

👤 {user.first_name}
🎁 获得：
{prize['name']}
"""
    )
