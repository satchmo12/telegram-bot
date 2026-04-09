import random
import time
from typing import Optional
from uuid import uuid4

from info.economy import change_points, get_points, get_user_data
from utils import POINTS_LOTTERY_FILE, load_json, save_json

MAX_RECENT_WINNERS = 20
MAX_USER_WINS = 100
PRIZE_RATE_MIN = 1
PRIZE_RATE_MAX = 100
PRIZE_STOCK_MIN = 0
PRIZE_STOCK_MAX = 999999
LOTTERY_COST_MIN = 1
LOTTERY_COST_MAX = 999999


def _normalize_int(value, default: int, min_value: int, max_value: int) -> int:
    try:
        value = int(value)
    except Exception:
        value = default
    return max(min_value, min(max_value, value))


def get_points_lottery_config(cfg: dict) -> dict:
    cfg = cfg if isinstance(cfg, dict) else {}
    return {
        "enabled": bool(cfg.get("points_lottery_enabled", False)),
        "cost": _normalize_int(cfg.get("points_lottery_cost", 100), 100, LOTTERY_COST_MIN, LOTTERY_COST_MAX),
    }


def _load_all() -> dict:
    data = load_json(POINTS_LOTTERY_FILE)
    return data if isinstance(data, dict) else {}


def _save_all(data: dict):
    save_json(POINTS_LOTTERY_FILE, data)


def get_group_points_lottery(chat_id: str) -> dict:
    data = _load_all()
    group = data.setdefault(str(chat_id), {})
    group.setdefault("prizes", [])
    group.setdefault("wins", {})
    group.setdefault("recent_winners", [])
    return group


def save_group_points_lottery(chat_id: str, group_data: dict):
    data = _load_all()
    data[str(chat_id)] = group_data
    _save_all(data)


def list_prizes(chat_id: str) -> list[dict]:
    group = get_group_points_lottery(chat_id)
    prizes = group.get("prizes", [])
    return prizes if isinstance(prizes, list) else []


def add_prize(chat_id: str, name: str, rate: int, stock: int) -> dict:
    group = get_group_points_lottery(chat_id)
    prize = {
        "id": uuid4().hex[:10],
        "name": str(name).strip(),
        "rate": _normalize_int(rate, 1, PRIZE_RATE_MIN, PRIZE_RATE_MAX),
        "stock": _normalize_int(stock, 0, PRIZE_STOCK_MIN, PRIZE_STOCK_MAX),
    }
    group.setdefault("prizes", []).append(prize)
    save_group_points_lottery(chat_id, group)
    return prize


def update_prize(chat_id: str, prize_id: str, name: str, rate: int, stock: int) -> bool:
    group = get_group_points_lottery(chat_id)
    prizes = group.setdefault("prizes", [])
    for prize in prizes:
        if str(prize.get("id")) != str(prize_id):
            continue
        prize["name"] = str(name).strip()
        prize["rate"] = _normalize_int(rate, 1, PRIZE_RATE_MIN, PRIZE_RATE_MAX)
        prize["stock"] = _normalize_int(stock, 0, PRIZE_STOCK_MIN, PRIZE_STOCK_MAX)
        save_group_points_lottery(chat_id, group)
        return True
    return False


def delete_prize(chat_id: str, prize_id: str) -> bool:
    group = get_group_points_lottery(chat_id)
    prizes = group.setdefault("prizes", [])
    new_prizes = [p for p in prizes if str(p.get("id")) != str(prize_id)]
    if len(new_prizes) == len(prizes):
        return False
    group["prizes"] = new_prizes
    save_group_points_lottery(chat_id, group)
    return True


def get_prize(chat_id: str, prize_id: str) -> Optional[dict]:
    for prize in list_prizes(chat_id):
        if str(prize.get("id")) == str(prize_id):
            return prize
    return None


def _build_user_name(chat_id: str, user_id: int, fallback: str) -> str:
    user_data = get_user_data(chat_id, user_id)
    return user_data.get("name") or fallback or f"用户{user_id}"


def draw_points_lottery(chat_id: str, user_id: int, user_name: str, draw_count: int, cfg: dict) -> tuple[bool, str, list[dict]]:
    settings = get_points_lottery_config(cfg)
    if not settings["enabled"]:
        return False, "本群未开启积分抽奖。", []

    draw_count = _normalize_int(draw_count, 1, 1, 10)
    total_cost = settings["cost"] * draw_count
    current_points = get_points(chat_id, user_id)
    if current_points < total_cost:
        return False, f"积分不足，需要 {total_cost} 分，当前只有 {current_points} 分。", []

    group = get_group_points_lottery(chat_id)
    prizes = group.setdefault("prizes", [])
    valid_prizes = [
        p for p in prizes
        if str(p.get("name", "")).strip()
        and _normalize_int(p.get("stock", 0), 0, PRIZE_STOCK_MIN, PRIZE_STOCK_MAX) > 0
        and _normalize_int(p.get("rate", 0), 0, PRIZE_RATE_MIN, PRIZE_RATE_MAX) > 0
    ]
    if not valid_prizes:
        return False, "奖池为空，暂时无法抽奖。", []

    change_points(chat_id, user_id, -total_cost)

    results = []
    weighted_total = sum(_normalize_int(p.get("rate", 0), 0, PRIZE_RATE_MIN, PRIZE_RATE_MAX) for p in valid_prizes)
    no_win_weight = max(0, 100 - weighted_total)
    recent = group.setdefault("recent_winners", [])
    wins_map = group.setdefault("wins", {})
    user_wins = wins_map.setdefault(str(user_id), [])
    ts = int(time.time())

    for _ in range(draw_count):
        live_prizes = [p for p in prizes if int(p.get("stock", 0) or 0) > 0 and int(p.get("rate", 0) or 0) > 0]
        if not live_prizes:
            results.append({"win": False, "name": "谢谢参与"})
            continue
        total = sum(int(p.get("rate", 0) or 0) for p in live_prizes) + no_win_weight
        pick = random.randint(1, max(1, total))
        cursor = 0
        chosen = None
        for prize in live_prizes:
            cursor += int(prize.get("rate", 0) or 0)
            if pick <= cursor:
                chosen = prize
                break
        if chosen is None:
            results.append({"win": False, "name": "谢谢参与"})
            continue
        chosen["stock"] = max(0, int(chosen.get("stock", 0) or 0) - 1)
        result = {"win": True, "name": str(chosen.get("name", "未知奖品")), "prize_id": chosen.get("id")}
        results.append(result)
        user_wins.append({"name": result["name"], "ts": ts})
        recent.append({
            "user_id": int(user_id),
            "user_name": _build_user_name(chat_id, user_id, user_name),
            "prize_name": result["name"],
            "ts": ts,
        })

    wins_map[str(user_id)] = user_wins[-MAX_USER_WINS:]
    group["recent_winners"] = recent[-MAX_RECENT_WINNERS:]
    save_group_points_lottery(chat_id, group)
    return True, "", results


def get_user_wins(chat_id: str, user_id: int) -> list[dict]:
    group = get_group_points_lottery(chat_id)
    wins_map = group.get("wins", {})
    user_wins = wins_map.get(str(user_id), [])
    return user_wins if isinstance(user_wins, list) else []
