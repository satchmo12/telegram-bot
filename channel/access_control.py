from utils import load_json, save_json

CHANNEL_ACCESS_CONFIG_FILE = "data/channel_access_control.json"
DEFAULT_REQUIRE_SUBSCRIPTION = True


def _load_channel_access_config() -> dict:
    data = load_json(CHANNEL_ACCESS_CONFIG_FILE)
    return data if isinstance(data, dict) else {}


def is_channel_subscription_required() -> bool:
    data = _load_channel_access_config()
    value = data.get("require_subscription")
    if isinstance(value, bool):
        return value
    return DEFAULT_REQUIRE_SUBSCRIPTION


def set_channel_subscription_required(required: bool) -> None:
    save_json(CHANNEL_ACCESS_CONFIG_FILE, {"require_subscription": bool(required)})
