import os
import json

from utils import ACTIVITY_FILE, load_json, save_json




# ============ 工资部分 ============

def get_salary(chat_id, user_id, date_str):
    data = load_json(ACTIVITY_FILE)
    return data.get(str(chat_id), {}).get("salary", {}).get(date_str, {}).get(str(user_id), 0)

def increment_salary_count(chat_id, user_id, date_str, amount):
    data = load_json(ACTIVITY_FILE)
    chat_id, user_id = str(chat_id), str(user_id)
    data.setdefault(chat_id, {}).setdefault("salary", {})
    data[chat_id]["salary"].setdefault(date_str, {})
    data[chat_id]["salary"][date_str][user_id] = amount
    save_json(ACTIVITY_FILE, data)

# ============ 打工部分 ============

def get_work_count(chat_id, user_id, date_str):
    data = load_json(ACTIVITY_FILE)
    return data.get(str(chat_id), {}).get("work", {}).get(date_str, {}).get(str(user_id), 0)

def increment_work_count(chat_id, user_id, date_str):
    data = load_json(ACTIVITY_FILE)
    chat_id, user_id = str(chat_id), str(user_id)
    data.setdefault(chat_id, {}).setdefault("work", {})
    data[chat_id]["work"].setdefault(date_str, {})
    data[chat_id]["work"][date_str][user_id] = get_work_count(chat_id, user_id, date_str) + 1
    save_json(ACTIVITY_FILE, data)
