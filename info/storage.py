# -*- coding: utf-8 -*-
"""Group-sharded storage for economy/profile data.

Legacy format: data/<bot>/info.json containing every group.
New format:    data/<bot>/info_groups/<chat_id>.json, one group per file.
"""
from __future__ import annotations

import gzip
import os
from typing import Iterator, Tuple

from utils import INFO_FILE, get_runtime_bot_name, load_json, save_json

INFO_GROUP_DIR = "data/info_groups"
MIGRATION_MARKER = f"{INFO_GROUP_DIR}/_migration.json"


def _group_file(chat_id) -> str:
    return f"{INFO_GROUP_DIR}/{str(chat_id)}.json"


def _runtime_legacy_path() -> str:
    bot_name = get_runtime_bot_name()
    return os.path.join("data", bot_name, "info.json") if bot_name else INFO_FILE


def _ensure_group_shape(data) -> dict:
    if not isinstance(data, dict):
        data = {}
    users = data.get("users")
    if not isinstance(users, dict):
        data["users"] = {}
    return data


def ensure_info_migrated() -> None:
    """One-time, per-bot split of legacy info.json into group files.

    The old JSON is gzip-archived after a successful split so normal reads no
    longer load a multi-megabyte document. The archive is retained for recovery.
    """
    marker = load_json(MIGRATION_MARKER)
    if isinstance(marker, dict) and marker.get("done"):
        return

    legacy = load_json(INFO_FILE)
    group_count = 0
    user_count = 0
    if isinstance(legacy, dict):
        for chat_id, group_data in legacy.items():
            if not isinstance(group_data, dict):
                continue
            path = _group_file(chat_id)
            existing = load_json(path)
            # Never overwrite a group already written by a prior partial migration.
            if isinstance(existing, dict) and isinstance(existing.get("users"), dict):
                group = _ensure_group_shape(existing)
            else:
                group = _ensure_group_shape(group_data)
                save_json(path, group)
            group_count += 1
            user_count += len(group.get("users", {}))

    save_json(
        MIGRATION_MARKER,
        {"done": True, "groups": group_count, "users": user_count},
    )

    # Archive only the physical legacy file. `load_json` already supplied the
    # data above; subsequent reads use group shards exclusively.
    legacy_path = _runtime_legacy_path()
    archive_path = f"{legacy_path}.gz"
    try:
        if os.path.exists(legacy_path) and not os.path.exists(archive_path):
            with open(legacy_path, "rb") as source, gzip.open(archive_path, "wb") as archive:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    archive.write(chunk)
            os.remove(legacy_path)
            print(
                f"✅ info.json 已按群拆分：{group_count} 个群、{user_count} 名用户；"
                f"旧数据已归档为 {archive_path}"
            )
    except Exception as exc:
        print(f"⚠️ info.json 分组迁移完成，但归档旧文件失败: {exc}")


def load_group_info(chat_id) -> dict:
    ensure_info_migrated()
    return _ensure_group_shape(load_json(_group_file(chat_id)))


def save_group_info(chat_id, data: dict) -> None:
    ensure_info_migrated()
    save_json(_group_file(chat_id), _ensure_group_shape(data))


def iter_group_infos() -> Iterator[Tuple[str, dict]]:
    ensure_info_migrated()
    bot_name = get_runtime_bot_name()
    base = os.path.join("data", bot_name, "info_groups") if bot_name else INFO_GROUP_DIR
    try:
        names = os.listdir(base)
    except OSError:
        return
    for name in sorted(names):
        if not name.endswith(".json") or name.startswith("_"):
            continue
        chat_id = name[:-5]
        yield chat_id, load_group_info(chat_id)
