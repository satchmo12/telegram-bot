import logging
from typing import Iterable, Optional, Set


TOP_LEVEL_FEATURES: Set[str] = {
    "economy",
    "entertainment",
    "group",
    "private_forward",
    "channel",
    "my_bot",
    "game_hub",
}
# 顶层模块：
# economy=经济系统总开关
# entertainment=娱乐玩法总开关
# group=群功能总开关
# private_forward=私聊转发（双向机器人）
# channel=频道功能（频道克隆/配置/搬运/转发）
# my_bot=文本互动/学习回复
# game_hub=大型玩法集合（农场/花园/牧场/背包/婚姻/宠物/奴隶/工作/动作/绑架/保镖）

ECONOMY_FEATURES: Set[str] = {
    "lottery_betting",
    "market_price",
}
# 经济功能：
# lottery_betting=彩票下注
# market_price=行情价格
# economy=统一管理：经济信息/银行/价格/公司经营/公司上市/公司招聘/彩票下注

GROUP_FEATURES: Set[str] = set()
# 群功能：
# group=统一管理：群设置/群管理/邀请统计/入群验证/签到/群互动/存图/统计/菜单/强制关注

FEATURE_LABELS = {
    "economy": "经济系统总开关",
    "entertainment": "娱乐玩法总开关",
    "group": "群功能总开关",
    "lottery_betting": "彩票下注",
    "market_price": "行情价格",
    "my_bot": "文本互动/学习回复",
    "game_hub": "大型玩法集合",
    "private_forward": "私聊转发（双向机器人）",
    "channel": "频道功能",
}

# 仅包含运行时通过 BOT_FEATURES_/BOT_DISABLE_FEATURES_ 控制的功能键。
# 像 manor、friends 这类群内细粒度开关不在这里维护。
ALL_FEATURES: Set[str] = (
    TOP_LEVEL_FEATURES
    | ECONOMY_FEATURES
    | GROUP_FEATURES
)


def parse_feature_list(raw: str) -> Set[str]:
    if not raw:
        return set()
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def sanitize_features(
    features: Iterable[str], *, source_name: str = "BOT_FEATURES", warn_unknown: bool = True
) -> Set[str]:
    result: Set[str] = set()
    for feature in features:
        key = (feature or "").strip().lower()
        if not key:
            continue
        if key not in ALL_FEATURES:
            if warn_unknown:
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
