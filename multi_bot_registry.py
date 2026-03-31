import logging
import os
import re
import time
import json
from typing import Optional

from feature_flags import ALL_FEATURES, parse_feature_list, sanitize_features

MANAGED_BOTS_FILE = "data/managed_bots.json"
LEGACY_MANAGED_BOTS_FILE = "data/小雅/managed_bots.json"
DEFAULT_OWNER_ID = 6085551760


def env_int(name: str, default: int) -> int:
    val = os.getenv(name)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        logging.warning("环境变量 %s 不是有效整数，使用默认值 %s", name, default)
        return default


def env_bool(name: str, default: bool = True) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _discover_bot_keys_from_env() -> list[str]:
    keys = []
    prefix = "BOT_TOKEN_"
    for k in os.environ.keys():
        if k.startswith(prefix) and len(k) > len(prefix):
            keys.append(k[len(prefix) :])
    return sorted(set(keys))


def load_env_bot_configs() -> list[dict]:
    configs = []
    for key in _discover_bot_keys_from_env():
        token = str(os.getenv(f"BOT_TOKEN_{key}", "")).strip()
        name = str(os.getenv(f"BOT_NAME_{key}", f"bot_{key.lower()}")).strip()
        owner_id = env_int(f"BOT_OWNER_{key}", DEFAULT_OWNER_ID)
        enabled = env_bool(f"BOT_ENABLE_{key}", True)
        raw_features = str(os.getenv(f"BOT_FEATURES_{key}", "")).strip()
        raw_disable_features = str(os.getenv(f"BOT_DISABLE_FEATURES_{key}", "")).strip()

        if not enabled:
            continue
        if ":" not in token or not name:
            logging.warning("跳过无效 token 配置: BOT_TOKEN_%s", key)
            continue

        if raw_features:
            enabled_features = sanitize_features(
                parse_feature_list(raw_features),
                source_name=f"BOT_FEATURES_{key}",
            )
        else:
            enabled_features = set(ALL_FEATURES)

        if raw_disable_features:
            disabled_features = sanitize_features(
                parse_feature_list(raw_disable_features),
                source_name=f"BOT_DISABLE_FEATURES_{key}",
            )
            enabled_features -= disabled_features

        configs.append(
            {
                "key": key,
                "token": token,
                "owner_id": owner_id,
                "name": name,
                "enabled": True,
                "enabled_features": sorted(enabled_features),
                "managed": False,
                "source_type": "env",
            }
        )
    return configs


def _load_managed_data() -> dict:
    data = {}
    source_file = MANAGED_BOTS_FILE
    if not os.path.exists(source_file) and os.path.exists(LEGACY_MANAGED_BOTS_FILE):
        source_file = LEGACY_MANAGED_BOTS_FILE
    if os.path.exists(source_file):
        try:
            with open(source_file, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
            if source_file != MANAGED_BOTS_FILE and data:
                _save_managed_data(data)
        except Exception:
            logging.exception("读取托管机器人配置失败: %s", source_file)
    if not isinstance(data, dict):
        data = {}
    bots = data.get("bots")
    if not isinstance(bots, list):
        data["bots"] = []
    return data


def _save_managed_data(data: dict) -> None:
    os.makedirs(os.path.dirname(MANAGED_BOTS_FILE), exist_ok=True)
    with open(MANAGED_BOTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


def _normalize_name(name: str) -> str:
    return str(name or "").strip()


def _new_managed_key(name: str) -> str:
    raw = re.sub(r"[^a-zA-Z0-9_]+", "_", _normalize_name(name))
    raw = raw.strip("_").lower() or "managed_bot"
    return f"{raw}_{int(time.time())}"


def load_managed_bot_configs() -> list[dict]:
    data = _load_managed_data()
    results = []
    changed = False
    for item in data.get("bots", []):
        if not isinstance(item, dict):
            continue
        token = str(item.get("token", "")).strip()
        name = _normalize_name(item.get("name", ""))
        if ":" not in token or not name:
            continue
        features = item.get("enabled_features") or []
        if not isinstance(features, list):
            features = []
        enabled_features = sorted(
            sanitize_features(
                features,
                source_name=f"managed_bot:{name}",
                warn_unknown=False,
            )
        )
        if enabled_features != features:
            item["enabled_features"] = enabled_features
            changed = True
        if "auto_start" not in item:
            item["auto_start"] = bool(item.get("enabled", True))
            changed = True
        results.append(
            {
                "key": item.get("key") or _new_managed_key(name),
                "token": token,
                "owner_id": int(item.get("owner_id") or DEFAULT_OWNER_ID),
                "name": name,
                "enabled": bool(item.get("enabled", True)),
                "auto_start": bool(item.get("auto_start", item.get("enabled", True))),
                "enabled_features": enabled_features,
                "managed": True,
                "source_type": "managed",
                "clone_from": _normalize_name(item.get("clone_from", "")),
            }
        )
    if changed:
        _save_managed_data(data)
    return results


def load_all_bot_configs() -> list[dict]:
    configs = []
    seen_tokens = set()
    seen_names = set()
    for item in load_env_bot_configs() + load_managed_bot_configs():
        token = item["token"]
        name = item["name"]
        if token in seen_tokens:
            logging.warning("跳过重复 token 配置: %s", name)
            continue
        if name in seen_names:
            logging.warning("跳过重复机器人名称: %s", name)
            continue
        seen_tokens.add(token)
        seen_names.add(name)
        configs.append(item)
    return configs


def get_bot_config_by_name(name: str) -> Optional[dict]:
    target = _normalize_name(name)
    if not target:
        return None
    for item in load_all_bot_configs():
        if item.get("name") == target:
            return item
    return None


def get_managed_bot_by_name(name: str) -> Optional[dict]:
    target = _normalize_name(name)
    if not target:
        return None
    for item in load_managed_bot_configs():
        if item.get("name") == target:
            return item
    return None


def save_managed_bot(record: dict) -> dict:
    data = _load_managed_data()
    bots = data.get("bots", [])
    name = _normalize_name(record.get("name", ""))
    token = str(record.get("token", "")).strip()
    if not name:
        raise ValueError("机器人名称不能为空")
    if ":" not in token:
        raise ValueError("机器人 token 格式不正确")

    features = record.get("enabled_features") or []
    if not isinstance(features, list):
        features = list(features)
    normalized = {
        "key": record.get("key") or _new_managed_key(name),
        "name": name,
        "token": token,
        "owner_id": int(record.get("owner_id") or DEFAULT_OWNER_ID),
        "enabled": bool(record.get("enabled", True)),
        "auto_start": bool(record.get("auto_start", record.get("enabled", True))),
        "enabled_features": sorted(
            sanitize_features(
                features,
                source_name=f"managed_bot:{name}",
                warn_unknown=False,
            )
        ),
        "clone_from": _normalize_name(record.get("clone_from", "")),
    }

    replaced = False
    for idx, item in enumerate(bots):
        if not isinstance(item, dict):
            continue
        if _normalize_name(item.get("name", "")) == name:
            bots[idx] = normalized
            replaced = True
            break
    if not replaced:
        bots.append(normalized)
    data["bots"] = bots
    _save_managed_data(data)
    return normalized


def delete_managed_bot(name: str) -> bool:
    target = _normalize_name(name)
    if not target:
        return False
    data = _load_managed_data()
    bots = data.get("bots", [])
    new_bots = [
        item
        for item in bots
        if not (isinstance(item, dict) and _normalize_name(item.get("name", "")) == target)
    ]
    if len(new_bots) == len(bots):
        return False
    data["bots"] = new_bots
    _save_managed_data(data)
    return True


def update_managed_bot_features(name: str, features: list[str]) -> Optional[dict]:
    record = get_managed_bot_by_name(name)
    if not record:
        return None
    record["enabled_features"] = sorted(
        sanitize_features(
            features,
            source_name=f"managed_bot:{name}",
            warn_unknown=False,
        )
    )
    return save_managed_bot(record)


def update_managed_bot_auto_start(name: str, auto_start: bool) -> Optional[dict]:
    record = get_managed_bot_by_name(name)
    if not record:
        return None
    record["auto_start"] = bool(auto_start)
    return save_managed_bot(record)
