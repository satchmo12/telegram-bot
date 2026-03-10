# cooldown.py
import os
import json
import time

from utils import COOLDOWN_FILE, load_json, save_json



def is_kidnapped(user_data):
    return "kidnap" in user_data

def is_on_cooldown(chat_id, user_id, action, cooldown_seconds):
    cooldowns = load_json(COOLDOWN_FILE)
    now = int(time.time())
    user_cd = cooldowns.setdefault(str(chat_id), {}).setdefault(str(user_id), {})
    last = user_cd.get(action, 0)
    remaining = cooldown_seconds - (now - last)
    if remaining > 0:
        return True, remaining
    user_cd[action] = now
    save_json(COOLDOWN_FILE, cooldowns)
    return False, 0
  


