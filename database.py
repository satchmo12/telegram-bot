import sqlite3
from datetime import datetime

conn = sqlite3.connect("global_dick.db", check_same_thread=False)
cursor = conn.cursor()

# 用户表
cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    length INTEGER DEFAULT 10,
    last_sign TEXT,
    anonymous INTEGER DEFAULT 0
)
""")

# 兼容旧数据库
try:
    cursor.execute("""
        ALTER TABLE users
        ADD COLUMN first_name TEXT
    """)
except sqlite3.OperationalError:
    pass

# 排行榜索引
cursor.execute("""
CREATE INDEX IF NOT EXISTS idx_users_length
ON users(length DESC)
""")

conn.commit()


def get_user(user_id, username=None, first_name=None):
    cursor.execute(
        "SELECT * FROM users WHERE user_id=?",
        (user_id,)
    )
    user = cursor.fetchone()

    # 新用户
    if not user:
        cursor.execute(
            """
            INSERT INTO users
            (user_id, username, first_name)
            VALUES (?, ?, ?)
            """,
            (user_id, username, first_name)
        )
        conn.commit()

        return (
            user_id,
            username,
            first_name,
            10,
            None,
            0,
        )

    # 同步最新资料
    if (
        (username is not None and user[1] != username)
        or
        (first_name is not None and user[2] != first_name)
    ):
        cursor.execute(
            """
            UPDATE users
            SET username=?, first_name=?
            WHERE user_id=?
            """,
            (username, first_name, user_id)
        )
        conn.commit()

        cursor.execute(
            "SELECT * FROM users WHERE user_id=?",
            (user_id,)
        )
        user = cursor.fetchone()

    return user


def update_length(user_id, amount):
    cursor.execute(
        """
        UPDATE users
        SET length = MAX(length + ?, 0)
        WHERE user_id=?
        """,
        (amount, user_id)
    )
    conn.commit()


def set_sign(user_id):
    cursor.execute(
        """
        UPDATE users
        SET last_sign=?
        WHERE user_id=?
        """,
        (str(datetime.date(datetime.now())), user_id)
    )
    conn.commit()