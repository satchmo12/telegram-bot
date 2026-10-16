"""Shared persisted settings for the private-chat ``/start`` welcome panel."""

from utils import load_json, save_json


START_WELCOME_FILE = "config_data/start_welcome.json"
START_WELCOME_EDIT_KEY = "start_welcome_editing"
START_WELCOME_RETURN_TO_SETTINGS_KEY = "start_welcome_return_to_settings"
START_REPLY_KEYBOARD_CONFIG_KEY = "show_reply_keyboard"


def load_start_welcome_config() -> dict:
    """Load the current bot's isolated ``/start`` settings."""
    data = load_json(START_WELCOME_FILE)
    return data if isinstance(data, dict) else {}


def start_reply_keyboard_enabled() -> bool:
    """Whether a normal private ``/start`` sends the bottom reply keyboard."""
    return bool(load_start_welcome_config().get(START_REPLY_KEYBOARD_CONFIG_KEY, True))


def set_start_reply_keyboard_enabled(enabled: bool) -> None:
    config = load_start_welcome_config()
    config[START_REPLY_KEYBOARD_CONFIG_KEY] = bool(enabled)
    save_json(START_WELCOME_FILE, config)


def reset_start_welcome_text() -> None:
    """Restore default welcome copy without changing the keyboard preference."""
    config = load_start_welcome_config()
    config.pop("text", None)
    config.pop("entities", None)
    save_json(START_WELCOME_FILE, config)


def save_start_welcome_text(text: str, entities: list[dict]) -> None:
    """Save welcome copy while preserving the independent keyboard setting."""
    config = load_start_welcome_config()
    config["text"] = text
    config["entities"] = entities
    save_json(START_WELCOME_FILE, config)
