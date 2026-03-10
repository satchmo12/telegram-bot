import sqlite3
from datetime import datetime

conn = sqlite3.connect("global_dick.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    length INTEGER DEFAULT 10,
    last_sign TEXT,
    anonymous INTEGER DEFAULT 0
)
""")

conn.commit()


def get_user(user_id, username=None):
    cursor.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    user = cursor.fetchone()

    if not user:
        cursor.execute(
            "INSERT INTO users (user_id, username) VALUES (?,?)",
            (user_id, username)
        )
        conn.commit()
        return get_user(user_id)

    # 用户已存在时，同步最新 username（用户可能改过用户名）
    if username is not None and user[1] != username:
        cursor.execute(
            "UPDATE users SET username=? WHERE user_id=?",
            (username, user_id)
        )
        conn.commit()
        cursor.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        user = cursor.fetchone()

    return user


def update_length(user_id, amount):
    cursor.execute(
        "UPDATE users SET length = MAX(length + ?, 0) WHERE user_id=?",
        (amount, user_id)
    )
    conn.commit()


def set_sign(user_id):
    cursor.execute(
        "UPDATE users SET last_sign=? WHERE user_id=?",
        (str(datetime.date(datetime.now())), user_id)
    )
    conn.commit()
