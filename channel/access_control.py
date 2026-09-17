from utils import load_json, save_json

CHANNEL_ACCESS_CONFIG_FILE = "config_data/channel_access_control.json"
# Older releases wrote this switch under data/. Keep reading it so existing
# cloned bots do not silently revert to subscription-required mode.
LEGACY_CHANNEL_ACCESS_CONFIG_FILE = "data/channel_access_control.json"
DEFAULT_REQUIRE_SUBSCRIPTION = True


def _load_channel_access_config() -> dict:
    data = load_json(CHANNEL_ACCESS_CONFIG_FILE)
    if isinstance(data, dict) and "require_subscription" in data:
        return data
    legacy = load_json(LEGACY_CHANNEL_ACCESS_CONFIG_FILE)
    if isinstance(legacy, dict) and "require_subscription" in legacy:
        return legacy
    return data if isinstance(data, dict) else {}


def is_channel_subscription_required() -> bool:
    data = _load_channel_access_config()
    value = data.get("require_subscription")
    if isinstance(value, bool):
        return value
    return DEFAULT_REQUIRE_SUBSCRIPTION


def set_channel_subscription_required(required: bool) -> None:
    save_json(CHANNEL_ACCESS_CONFIG_FILE, {"require_subscription": bool(required)})
