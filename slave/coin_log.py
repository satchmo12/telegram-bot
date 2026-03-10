from datetime import datetime
from utils import load_json, save_json

LOG_FILE = "data/coin_logs.json"

def log_coin_change(user_id: int, change: int, action: str, note: str = ""):
    logs = load_json(LOG_FILE)
    if not isinstance(logs, list):
        logs = []

    logs.append({
        "user_id": user_id,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "change": change,
        "action": action,
        "note": note
    })

    save_json(LOG_FILE, logs)
