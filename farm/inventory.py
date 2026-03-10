from utils import INVENTORY_DATA_FILE, load_json, save_json, sort_shop



def get_user_inventory(chat_id: str, user_id: str) -> dict:
    data = load_json(INVENTORY_DATA_FILE)
    if chat_id not in data:
        data[chat_id] = {}
    if user_id not in data[chat_id]:
        data[chat_id][user_id] = {}
        save_json(INVENTORY_DATA_FILE, data)
    return data[chat_id][user_id]

def change_item(chat_id: str, user_id: str, item_name: str, delta: int) -> bool:
    data = load_json(INVENTORY_DATA_FILE)
    user_inv = data.setdefault(chat_id, {}).setdefault(user_id, {})

    cur_amount = user_inv.get(item_name, 0)
    new_amount = cur_amount + delta
    if new_amount < 0:
        return False  # 库存不足

    if new_amount == 0:
        user_inv.pop(item_name, None)
    else:
        user_inv[item_name] = new_amount
        user_inv = sort_shop(user_inv)
        

    save_json(INVENTORY_DATA_FILE, data)
    return True

def get_item_count(chat_id: str, user_id: str, item_name: str) -> int:
    data = load_json(INVENTORY_DATA_FILE)
    return data.get(chat_id, {}).get(user_id, {}).get(item_name, 0)

def save_user_inventory(chat_id: str, user_id: str, inv: dict):
    data = load_json(INVENTORY_DATA_FILE)
    
    if chat_id not in data:
        data[chat_id] = {}
    
    inv = sort_shop(inv)
    data[chat_id][user_id] = inv

    save_json(INVENTORY_DATA_FILE, data)
