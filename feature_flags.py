import logging
from typing import Iterable, Optional, Set


ALL_FEATURES: Set[str] = {
    # Top-level modules
    "economy",
    "entertainment",
    "simulation",
    "group",
    "niuniu",
    # Economy
    "lottery_betting",
    "economy_info",
    "economy_bank",
    "market_price",
    "company_business",
    "company_ipo",
    "company_recruit",
    # Entertainment
    "dress",
    "chengyu",
    "five",
    "qa",
    "truth",
    "dice",
    "lottery_game",
    "voice_reply",
    "answer_book",
    "ai_chat",
    "beauty",
    "ssc",
    # Simulation
    "my_bot",
    "work",
    "action",
    "game_hub",
    "kidnap",
    "guard",
    # Group
    "group_setting",
    "admin",
    "invite_stats",
    "verification",
    "checkin",
    "group_care",
    "group_media_tools",
    "save_photos",
    "talk_stats",
    "user_tracker",
    "menu",
}


def parse_feature_list(raw: str) -> Set[str]:
    if not raw:
        return set()
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def sanitize_features(
    features: Iterable[str], *, source_name: str = "BOT_FEATURES"
) -> Set[str]:
    result: Set[str] = set()
    for feature in features:
        key = (feature or "").strip().lower()
        if not key:
            continue
        if key not in ALL_FEATURES:
            logging.warning("未知功能开关 [%s]: %s（将忽略）", source_name, feature)
            continue
        result.add(key)
    return result


def is_feature_enabled(app, feature_name: str, default: bool = True) -> bool:
    feature_key = (feature_name or "").strip().lower()
    if not feature_key:
        return default

    enabled_features: Optional[Set[str]] = app.bot_data.get("enabled_features")
    if enabled_features is None:
        return default
    return feature_key in enabled_features
