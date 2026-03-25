from utils import load_json, save_json

MUTE_LIST_FILE = "data/muted_users.json"


def _load_data() -> dict:
    data = load_json(MUTE_LIST_FILE)
    return data if isinstance(data, dict) else {}


def _save_data(data: dict):
    save_json(MUTE_LIST_FILE, data)


def add_mute(chat_id: str, user_id: int, name: str = "", source: str = ""):
    data = _load_data()
    bucket = data.setdefault(chat_id, {})
    bucket[str(user_id)] = {
        "name": name or "",
        "source": source or "",
    }
    _save_data(data)


def remove_mute(chat_id: str, user_id: int):
    data = _load_data()
    bucket = data.get(chat_id)
    if not isinstance(bucket, dict):
        return
    if str(user_id) in bucket:
        bucket.pop(str(user_id), None)
        if not bucket:
            data.pop(chat_id, None)
        _save_data(data)


def list_mutes(chat_id: str) -> dict:
    data = _load_data()
    bucket = data.get(chat_id)
    return bucket if isinstance(bucket, dict) else {}
