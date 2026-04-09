from datetime import datetime

from info.economy import change_points
from utils import POINTS_REWARD_LOG_FILE, load_json, save_json

TALK_POINTS_AMOUNT_MIN = 1
TALK_POINTS_AMOUNT_MAX = 100
TALK_POINTS_DAILY_LIMIT_MIN = 1
TALK_POINTS_DAILY_LIMIT_MAX = 10000
TALK_POINTS_MIN_LENGTH_MIN = 1
TALK_POINTS_MIN_LENGTH_MAX = 200
INVITE_POINTS_AMOUNT_MIN = 1
INVITE_POINTS_AMOUNT_MAX = 500
INVITE_POINTS_DAILY_LIMIT_MIN = 1
INVITE_POINTS_DAILY_LIMIT_MAX = 10000


def _today_key() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


def _clamp(value, min_value: int, max_value: int, default: int) -> int:
    try:
        value = int(value)
    except Exception:
        value = default
    return max(min_value, min(max_value, value))


def get_talk_points_config(cfg: dict) -> dict:
    cfg = cfg if isinstance(cfg, dict) else {}
    return {
        "enabled": bool(cfg.get("talk_points_enabled", False)),
        "amount": _clamp(cfg.get("talk_points_amount", 1), TALK_POINTS_AMOUNT_MIN, TALK_POINTS_AMOUNT_MAX, 1),
        "daily_limit": _clamp(
            cfg.get("talk_points_daily_limit", 20),
            TALK_POINTS_DAILY_LIMIT_MIN,
            TALK_POINTS_DAILY_LIMIT_MAX,
            20,
        ),
        "min_length": _clamp(
            cfg.get("talk_points_min_length", 5),
            TALK_POINTS_MIN_LENGTH_MIN,
            TALK_POINTS_MIN_LENGTH_MAX,
            5,
        ),
    }


def get_invite_points_config(cfg: dict) -> dict:
    cfg = cfg if isinstance(cfg, dict) else {}
    return {
        "enabled": bool(cfg.get("invite_points_enabled", False)),
        "amount": _clamp(
            cfg.get("invite_points_amount", 100),
            INVITE_POINTS_AMOUNT_MIN,
            INVITE_POINTS_AMOUNT_MAX,
            100,
        ),
        "daily_limit": _clamp(
            cfg.get("invite_points_daily_limit", 500),
            INVITE_POINTS_DAILY_LIMIT_MIN,
            INVITE_POINTS_DAILY_LIMIT_MAX,
            500,
        ),
    }


def _load_reward_log() -> dict:
    data = load_json(POINTS_REWARD_LOG_FILE)
    return data if isinstance(data, dict) else {}


def _save_reward_log(data: dict):
    save_json(POINTS_REWARD_LOG_FILE, data)


def _get_user_day_log(data: dict, chat_id: str, user_id: str) -> dict:
    today = _today_key()
    return (
        data.setdefault(str(chat_id), {})
        .setdefault(today, {})
        .setdefault(str(user_id), {"talk": 0, "invite": 0})
    )


def award_talk_points(chat_id: str, user_id: str, text: str, cfg: dict) -> int:
    settings = get_talk_points_config(cfg)
    if not settings["enabled"]:
        return 0
    text = (text or "").strip()
    if not text or text.startswith("/"):
        return 0
    if len(text) < settings["min_length"]:
        return 0

    data = _load_reward_log()
    day_log = _get_user_day_log(data, str(chat_id), str(user_id))
    awarded_today = int(day_log.get("talk", 0) or 0)
    remaining = settings["daily_limit"] - awarded_today
    if remaining <= 0:
        return 0

    awarded = min(settings["amount"], remaining)
    if awarded <= 0:
        return 0
    change_points(chat_id, user_id, awarded)
    day_log["talk"] = awarded_today + awarded
    _save_reward_log(data)
    return awarded


def award_invite_points(chat_id: str, inviter_id: int, invitee_ids: list[int], cfg: dict) -> int:
    settings = get_invite_points_config(cfg)
    if not settings["enabled"]:
        return 0
    if not invitee_ids:
        return 0

    valid_invitees = [uid for uid in invitee_ids if int(uid) != int(inviter_id)]
    if not valid_invitees:
        return 0

    data = _load_reward_log()
    day_log = _get_user_day_log(data, str(chat_id), str(inviter_id))
    awarded_today = int(day_log.get("invite", 0) or 0)
    remaining = settings["daily_limit"] - awarded_today
    if remaining <= 0:
        return 0

    max_rewarded_invites = remaining // settings["amount"]
    if max_rewarded_invites <= 0:
        return 0

    rewarded_invites = min(len(valid_invitees), max_rewarded_invites)
    awarded = rewarded_invites * settings["amount"]
    if awarded <= 0:
        return 0
    change_points(chat_id, inviter_id, awarded)
    day_log["invite"] = awarded_today + awarded
    _save_reward_log(data)
    return awarded
