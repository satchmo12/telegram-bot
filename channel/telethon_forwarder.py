import asyncio
import os
from typing import Dict, List, Any, TYPE_CHECKING
import re
import warnings
import difflib

warnings.filterwarnings(
    "ignore",
    message="Using async sessions support is an experimental feature",
    category=UserWarning,
)

from utils import (
    load_json,
    is_super_admin,
    save_json,
    set_runtime_bot_name,
    get_runtime_bot_name,
    get_sessions_dir_by_bot,
    is_shared_session_name,
)
from channel.channel_forwarder import _is_active_subscription
from channel.telethon_login import _get_api_creds

try:
    from telethon import TelegramClient, events
    from telethon.tl.types import (
        MessageEntityBlockquote,
        MessageEntitySpoiler,
        MessageEntityUrl,
        MessageEntityTextUrl,
    )
    try:
        from telethon.extensions import html as telethon_html
    except Exception:
        telethon_html = None
    try:
        from telethon.extensions import markdown as telethon_markdown
    except Exception:
        telethon_markdown = None
except Exception:  # pragma: no cover - 运行时缺依赖
    TelegramClient = None
    events = None
    MessageEntityBlockquote = None
    MessageEntitySpoiler = None
    MessageEntityUrl = None
    MessageEntityTextUrl = None
    telethon_html = None
    telethon_markdown = None

if TYPE_CHECKING:
    from telethon import TelegramClient as TelethonClient


SESSION_CLIENTS_BY_BOT: Dict[str, Dict[str, "TelethonClient"]] = {}
SESSION_RULES_BY_BOT: Dict[str, Dict[str, List[dict]]] = {}
FORWARD_TASKS: Dict[str, asyncio.Task] = {}
DEBUG_FORWARD = False
HISTORY_REQUESTS_FILE = "data/history_forward_requests.json"
HISTORY_STATE_FILE = "data/history_forward_state.json"

LINK_RE = re.compile(r"(?i)(https?://[^\s)\]}>]+|t\.me/[^\s)\]}>]+|www\.[^\s)\]}>]+)")


def _entity_urls(entities) -> list:
    urls = []
    for ent in entities or []:
        if MessageEntityTextUrl and isinstance(ent, MessageEntityTextUrl):
            url = getattr(ent, "url", None)
            if isinstance(url, str) and url:
                urls.append(url)
        elif getattr(ent, "url", None):
            url = getattr(ent, "url", None)
            if isinstance(url, str) and url:
                urls.append(url)
    return urls


def _has_link(text: str, entities=None) -> bool:
    for url in _entity_urls(entities):
        if url:
            return True
    if not text:
        return False
    if LINK_RE.search(text):
        return True
    return False


def _should_skip_by_links(rule: dict, text: str, entities=None) -> bool:
    if rule.get("skip_links"):
        return _has_link(text or "", entities)
    return False


def _join_with_suffix(text: str, suffix: str) -> str:
    base = text or ""
    extra = suffix or ""
    if not extra:
        return base
    if not base:
        return extra.strip("\n")
    # Preserve original formatting inside base; only trim trailing newlines in base
    # and leading newlines in suffix, then join with a single blank line.
    base = re.sub(r"\n+\Z", "", base)
    extra = re.sub(r"\A\n+", "", extra)
    return f"{base}\n\n{extra}"


def _is_subscripted_generics_error(err: Exception) -> bool:
    return "Subscripted generics cannot be used with class and instance checks" in str(err)


def _normalize_entities(entities):
    if not entities:
        return None
    if isinstance(entities, list):
        return entities
    try:
        return list(entities)
    except Exception:
        return None


async def _send_message_safe(client, target_id, text: str, *, entities=None, file=None):
    normalized = _normalize_entities(entities)
    try:
        if normalized:
            return await client.send_message(
                target_id, text, file=file, formatting_entities=normalized
            )
        return await client.send_message(target_id, text, file=file)
    except TypeError as e:
        if normalized and _is_subscripted_generics_error(e):
            return await client.send_message(target_id, text, file=file)
        raise


async def _send_file_safe(client, target_id, files, *, caption: str = "", entities=None):
    normalized = _normalize_entities(entities)
    try:
        if normalized:
            return await client.send_file(
                target_id, files, caption=caption, formatting_entities=normalized
            )
        return await client.send_file(target_id, files, caption=caption)
    except TypeError as e:
        if normalized and _is_subscripted_generics_error(e):
            return await client.send_file(target_id, files, caption=caption)
        raise


def _caption_to_parse_mode(text: str, entities):
    if not text or not entities:
        return None, None
    try:
        if telethon_html:
            return telethon_html.unparse(text, entities), "html"
    except Exception:
        pass
    try:
        if telethon_markdown:
            return telethon_markdown.unparse(text, entities), "md"
    except Exception:
        pass
    return None, None


async def _send_album_with_caption(client, target_id, files, *, caption: str = "", entities=None):
    normalized = _normalize_entities(entities)
    if normalized and caption:
        parsed_caption, parse_mode = _caption_to_parse_mode(caption, normalized)
        if parsed_caption and parse_mode:
            return await client.send_file(
                target_id,
                files,
                caption=parsed_caption,
                parse_mode=parse_mode,
            )
    await _send_file_safe(client, target_id, files, caption=caption or "", entities=normalized)


async def _send_text_split(client, target_id, text: str, *, entities=None, limit: int = 4096):
    payload = text or ""
    if len(payload) <= limit:
        await _send_message_safe(client, target_id, payload, entities=entities)
        return
    first = payload[:limit]
    rest = payload[limit:]
    await _send_message_safe(client, target_id, first, entities=entities)
    await _send_message_safe(client, target_id, rest)


def _load_session_owners() -> dict:
    data = load_json("data/telethon_session_owners.json")
    return data if isinstance(data, dict) else {}


def _can_use_rule(user_id: str, username: str) -> bool:
    if not user_id:
        return False
    if is_super_admin(user_id):
        return True
    return _is_active_subscription(user_id, username)


def _is_owner_for_session(owners: dict, session_name: str, user_id: str, username: str) -> bool:
    if is_shared_session_name(session_name):
        return False
    sessions = owners.get("sessions", {}) if isinstance(owners, dict) else {}
    record = sessions.get(session_name)
    if not isinstance(record, dict):
        return False
    owner_id = str(record.get("owner_id", "") or "")
    owner_username = str(record.get("owner_username", "") or "").lstrip("@").lower()
    return bool((owner_id and owner_id == str(user_id)) or (username and owner_username == username))


def _match_filter(message, filter_type: str) -> bool:
    ftype = (filter_type or "all").lower()
    if ftype == "all":
        return True
    has_gif = bool(getattr(message, "animation", None)) or (
        getattr(message, "document", None)
        and getattr(message.document, "mime_type", "") == "image/gif"
    )
    if ftype == "text":
        return not (getattr(message, "photo", None) or getattr(message, "video", None) or has_gif)
    if ftype == "photo":
        return bool(getattr(message, "photo", None) or has_gif)
    if ftype == "video":
        return bool(getattr(message, "video", None) or has_gif)
    return True


def _clear_links(text: str) -> str:
    if not text:
        return text
    t = text
    t = re.sub(r"(?i)https?://\S+", "", t)
    t = re.sub(r"(?i)t\.me/\S+", "", t)
    t = re.sub(r"@[\w_]{3,}", "", t)
    # 压缩多余空白
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t.strip()


def _apply_replace(text: str, pairs: List[dict]) -> str:
    if not text or not pairs:
        return text
    out = text
    for p in pairs:
        if not isinstance(p, dict):
            continue
        src = str(p.get("from", "") or "")
        dst = str(p.get("to", "") or "")
        if not src:
            continue
        out = out.replace(src, dst)
    return out


def _apply_cut(text: str, cut_rule) -> str:
    if not text or not cut_rule:
        return text
    if isinstance(cut_rule, str):
        rules = [cut_rule]
    else:
        rules = [r for r in (cut_rule or []) if r]
    out = text
    for rule in rules:
        if not rule:
            continue
        if "|" in rule:
            start, end = rule.split("|", 1)
        else:
            start, end = "", rule
        if start:
            idx = out.find(start)
            if idx >= 0:
                out = out[idx + len(start) :]
        if end:
            idx = out.find(end)
            if idx >= 0:
                out = out[:idx]
    return out


def _clone_entity(ent, offset: int, length: int):
    try:
        data = dict(getattr(ent, "__dict__", {}) or {})
        data.pop("offset", None)
        data.pop("length", None)
        if MessageEntityBlockquote and isinstance(ent, MessageEntityBlockquote):
            data.setdefault("collapsed", bool(getattr(ent, "collapsed", False)))
        return ent.__class__(offset=offset, length=length, **data)
    except Exception:
        try:
            if MessageEntityBlockquote and isinstance(ent, MessageEntityBlockquote):
                return ent.__class__(
                    offset=offset, length=length, collapsed=bool(getattr(ent, "collapsed", False))
                )
            return ent.__class__(offset=offset, length=length)
        except Exception:
            return None


def _apply_cut_with_entities(text: str, entities, cut_rule) -> tuple[str, List[Any]]:
    if not text or not cut_rule:
        return text, list(entities or [])
    if isinstance(cut_rule, str):
        rules = [cut_rule]
    else:
        rules = [r for r in (cut_rule or []) if r]
    out_text = text
    out_entities = list(entities or [])
    for rule in rules:
        if not rule:
            continue
        if "|" in rule:
            start, end = rule.split("|", 1)
        else:
            start, end = "", rule
        start_idx = 0
        end_idx = len(out_text)
        if start:
            idx = out_text.find(start)
            if idx >= 0:
                start_idx = idx + len(start)
        if end:
            idx = out_text.find(end, start_idx)
            if idx >= 0:
                end_idx = idx
        if start_idx == 0 and end_idx == len(out_text):
            continue
        new_text = out_text[start_idx:end_idx]
        new_entities = []
        for ent in out_entities:
            ent_start = getattr(ent, "offset", 0)
            ent_len = getattr(ent, "length", 0)
            ent_end = ent_start + ent_len
            # overlap with slice [start_idx, end_idx)
            overlap_start = max(ent_start, start_idx)
            overlap_end = min(ent_end, end_idx)
            if overlap_start >= overlap_end:
                continue
            new_offset = overlap_start - start_idx
            new_length = overlap_end - overlap_start
            cloned = _clone_entity(ent, new_offset, new_length)
            if cloned is not None:
                new_entities.append(cloned)
        out_text = new_text
        out_entities = new_entities
    return out_text, out_entities


def _process_text(text: str, rule: dict) -> str:
    if text is None:
        return text
    t = text
    include_words = rule.get("include_words") or []
    block_words = rule.get("block_words") or []
    if include_words:
        if not any(w and w in t for w in include_words):
            return ""
    if block_words:
        if any(w and w in t for w in block_words):
            if DEBUG_FORWARD:
                hit = next((w for w in block_words if w and w in t), "")
                rname = str(rule.get("name", "") or "")
                print(f"⛔ 跳过: 命中屏蔽词 {hit} (rule={rname})")
            return ""
    t = _apply_replace(t, rule.get("replace_words") or [])
    t = _apply_cut(t, rule.get("cut_words", ""))
    if rule.get("clear_links"):
        t = _clear_links(t)
    suffix = str(rule.get("suffix", "") or "")
    if suffix:
        t = _join_with_suffix(t, suffix)
    if t == "" and not include_words and not block_words and not rule.get("clear_links") and not rule.get("cut_words") and not rule.get("replace_words") and not suffix:
        return None
    return t


def _process_text_with_entities(text: str, rule: dict, entities) -> tuple[str, List[Any]]:
    if text is None:
        return text, []
    t = text
    original_entities = list(entities or [])
    include_words = rule.get("include_words") or []
    block_words = rule.get("block_words") or []
    if include_words:
        if not any(w and w in t for w in include_words):
            return "", []
    if block_words:
        if any(w and w in t for w in block_words):
            if DEBUG_FORWARD:
                hit = next((w for w in block_words if w and w in t), "")
                rname = str(rule.get("name", "") or "")
                print(f"⛔ 跳过: 命中屏蔽词 {hit} (rule={rname})")
            return "", []
    if rule.get("replace_words") or rule.get("clear_links"):
        processed = _process_text(t, rule)
        # If text unchanged, keep original entities to preserve formatting.
        if processed == t:
            return t, list(entities or [])
        if processed is None:
            return None, []
        new_entities = _remap_entities_by_diff(t, processed, entities)
        if processed is not None:
            processed, new_entities = _truncate_with_entities(
                processed, new_entities, max_len=len(processed)
            )
        return processed, new_entities
    if rule.get("cut_words"):
        t, entities = _apply_cut_with_entities(t, entities, rule.get("cut_words", ""))
    suffix = str(rule.get("suffix", "") or "")
    if suffix:
        t = _join_with_suffix(t, suffix)
    if t == text and original_entities:
        return t, original_entities
    if t == "" and not include_words and not block_words and not rule.get("cut_words") and not suffix:
        return None, []
    return t, list(entities or [])


def _needs_processing(rule: dict) -> bool:
    return any(
        [
            rule.get("include_words"),
            rule.get("block_words"),
            rule.get("replace_words"),
            rule.get("cut_words"),
            rule.get("clear_links"),
            rule.get("suffix"),
        ]
    )


def _get_message_text(msg) -> str:
    if msg is None:
        return ""
    for attr in ("message", "raw_text", "text"):
        val = getattr(msg, attr, None)
        if isinstance(val, str) and val != "":
            return val
    return ""


def _has_fold_entities(entities) -> bool:
    if not entities:
        return False
    for ent in entities:
        if MessageEntityBlockquote and isinstance(ent, MessageEntityBlockquote):
            return True
        if MessageEntitySpoiler and isinstance(ent, MessageEntitySpoiler):
            return True
    return False


def _format_entities(entities) -> str:
    if not entities:
        return "[]"
    out = []
    for ent in entities:
        try:
            etype = ent.__class__.__name__
            off = int(getattr(ent, "offset", 0))
            length = int(getattr(ent, "length", 0))
            extra = ""
            if etype == "MessageEntityBlockquote":
                extra = f", collapsed={bool(getattr(ent, 'collapsed', False))}"
            out.append(f"{etype}(offset={off}, length={length}{extra})")
        except Exception:
            out.append(repr(ent))
    return "[" + ", ".join(out) + "]"


def _debug_print_fold_compare(kind: str, raw_text: str, raw_entities, processed_text: str, processed_entities):
    if not DEBUG_FORWARD:
        return
    print("🔎 折叠块对比开始")
    print(f"类型: {kind}")
    print(f"原内容: {raw_text!r}")
    print(f"原格式: {_format_entities(raw_entities)}")
    print(f"转发内容: {processed_text!r}")
    print(f"转发格式: {_format_entities(processed_entities)}")
    print("🔎 折叠块对比结束")


def _build_offset_map_by_diff(old_text: str, new_text: str) -> List[Any]:
    if old_text is None or new_text is None:
        return []
    mapping = [None] * len(old_text)
    matcher = difflib.SequenceMatcher(None, old_text, new_text, autojunk=False)
    for tag, a1, a2, b1, b2 in matcher.get_opcodes():
        if tag == "equal":
            for i in range(a2 - a1):
                mapping[a1 + i] = b1 + i
        elif tag == "replace":
            overlap = min(a2 - a1, b2 - b1)
            for i in range(overlap):
                mapping[a1 + i] = b1 + i
        # delete/insert -> no direct mapping for removed/added chars
    return mapping


def _remap_entities_by_diff(old_text: str, new_text: str, entities) -> List[Any]:
    if not entities:
        return []
    mapping = _build_offset_map_by_diff(old_text or "", new_text or "")
    if not mapping:
        return []
    new_entities = []
    new_len = len(new_text or "")
    for ent in entities:
        try:
            start = int(getattr(ent, "offset", 0))
            length = int(getattr(ent, "length", 0))
        except Exception:
            continue
        if length <= 0:
            continue
        if start < 0:
            start = 0
        end = start + length
        if end > len(mapping):
            end = len(mapping)
        if start >= end:
            continue
        mapped = [m for m in mapping[start:end] if m is not None]
        if not mapped:
            continue
        new_start = min(mapped)
        new_end = max(mapped) + 1
        if new_start < 0:
            new_start = 0
        if new_end > new_len:
            new_end = new_len
        new_length = new_end - new_start
        if new_length <= 0:
            continue
        cloned = _clone_entity(ent, new_start, new_length)
        if cloned is not None:
            new_entities.append(cloned)
    try:
        new_entities.sort(key=lambda e: int(getattr(e, "offset", 0)))
    except Exception:
        pass
    return new_entities


def _has_url(text: str, entities) -> bool:
    if entities:
        for ent in entities:
            if MessageEntityUrl and isinstance(ent, MessageEntityUrl):
                return True
            if MessageEntityTextUrl and isinstance(ent, MessageEntityTextUrl):
                return True
    if text:
        return bool(re.search(r"https?://\S+|t\.me/\S+", text, flags=re.I))
    return False


def _merge_ranges(ranges: List[tuple]) -> List[tuple]:
    if not ranges:
        return []
    ranges = sorted(ranges, key=lambda r: r[0])
    merged = [ranges[0]]
    for start, end in ranges[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _get_protected_ranges(entities) -> List[tuple]:
    ranges = []
    if not entities:
        return ranges
    for ent in entities:
        if MessageEntityBlockquote and isinstance(ent, MessageEntityBlockquote):
            ranges.append((int(ent.offset), int(ent.offset + ent.length)))
        if MessageEntitySpoiler and isinstance(ent, MessageEntitySpoiler):
            ranges.append((int(ent.offset), int(ent.offset + ent.length)))
    return _merge_ranges(ranges)


def _strip_ranges(text: str, ranges: List[tuple]) -> str:
    if not ranges:
        return text
    out = []
    last = 0
    for start, end in ranges:
        out.append(text[last:start])
        last = end
    out.append(text[last:])
    return "".join(out)


def _process_text_preserve_folds(text: str, rule: dict, entities) -> tuple[str, List[Any]]:
    if text is None:
        return text, []
    original_text = text
    cut_rule = rule.get("cut_words", "")
    did_cut = False
    if cut_rule:
        text, entities = _apply_cut_with_entities(text, entities, cut_rule)
        original_text = text
        did_cut = True
    protected = _get_protected_ranges(entities)
    check_text = _strip_ranges(text, protected)
    include_words = rule.get("include_words") or []
    block_words = rule.get("block_words") or []
    if include_words:
        if not any(w and w in check_text for w in include_words):
            return "", []
    if block_words:
        if any(w and w in check_text for w in block_words):
            return "", []

    new_parts = []
    cursor = 0
    for start, end in protected:
        # unprotected
        if cursor < start:
            seg = text[cursor:start]
            seg = _apply_replace(seg, rule.get("replace_words") or [])
            if not did_cut:
                seg = _apply_cut(seg, cut_rule)
            if rule.get("clear_links"):
                seg = _clear_links(seg)
            new_parts.append(seg)
        # protected (fold/spoiler) — only apply replace words
        protected_text = text[start:end]
        protected_text = _apply_replace(protected_text, rule.get("replace_words") or [])
        if not did_cut:
            protected_text = _apply_cut(protected_text, cut_rule)
        new_parts.append(protected_text)
        cursor = end
    # tail
    if cursor < len(text):
        seg = text[cursor:]
        seg = _apply_replace(seg, rule.get("replace_words") or [])
        if not did_cut:
            seg = _apply_cut(seg, cut_rule)
        if rule.get("clear_links"):
            seg = _clear_links(seg)
        new_parts.append(seg)
    t = "".join(new_parts)
    suffix = str(rule.get("suffix", "") or "")
    if suffix:
        t = _join_with_suffix(t, suffix)
    if t == "" and not include_words and not block_words and not rule.get("clear_links") and not rule.get("cut_words") and not rule.get("replace_words") and not suffix:
        return None, []
    new_entities = _remap_entities_by_diff(original_text, t, entities)
    if t is not None:
        t, new_entities = _truncate_with_entities(t, new_entities, max_len=len(t))
    return t, new_entities


def _truncate_with_entities(text: str, entities, max_len: int = 1024) -> tuple[str, List[Any]]:
    if text is None:
        return text, entities or []
    if len(text) <= max_len:
        return text, entities or []
    t = text[:max_len]
    new_entities = []
    if entities:
        for ent in entities:
            try:
                off = int(ent.offset)
                length = int(ent.length)
            except Exception:
                continue
            if off >= max_len:
                continue
            if off + length > max_len:
                length = max_len - off
            # rebuild entity with truncated length
            try:
                if isinstance(ent, MessageEntityBlockquote):
                    new_entities.append(
                        ent.__class__(
                            offset=off,
                            length=length,
                            collapsed=bool(getattr(ent, "collapsed", False)),
                        )
                    )
                else:
                    new_entities.append(ent.__class__(offset=off, length=length))
            except Exception:
                pass
    return t, new_entities


def _parse_speed(value: str) -> float:
    if not value:
        return 0.5
    v = str(value).strip().lower()
    try:
        if v.endswith("ms"):
            return max(0.05, float(v[:-2]) / 1000.0)
        return max(0.05, float(v))
    except Exception:
        return 0.5


def _load_history_state() -> dict:
    data = load_json(HISTORY_STATE_FILE)
    if not isinstance(data, dict):
        data = {}
    data.setdefault("keys", {})
    return data


def _save_history_state(data: dict):
    save_json(HISTORY_STATE_FILE, data)


def _recent_ids_for_key(state: dict, key: str) -> List[int]:
    keys = state.get("keys", {})
    record = keys.get(key, {})
    ids = record.get("recent_ids", [])
    return ids if isinstance(ids, list) else []


def _append_recent_id(state: dict, key: str, msg_id: int, max_keep: int = 500):
    keys = state.setdefault("keys", {})
    record = keys.setdefault(key, {})
    ids = record.get("recent_ids", [])
    if not isinstance(ids, list):
        ids = []
    ids.append(int(msg_id))
    if len(ids) > max_keep:
        ids = ids[-max_keep:]
    record["recent_ids"] = ids
    keys[key] = record


def _get_history_max_id(state: dict, key: str) -> int:
    keys = state.get("keys", {})
    record = keys.get(key, {})
    try:
        return int(record.get("history_max_id", 0))
    except Exception:
        return 0


def _set_history_max_id(state: dict, key: str, msg_id: int):
    keys = state.setdefault("keys", {})
    record = keys.setdefault(key, {})
    try:
        current = int(record.get("history_max_id", 0))
    except Exception:
        current = 0
    if int(msg_id) > current:
        record["history_max_id"] = int(msg_id)
    keys[key] = record


FORWARD_USER_CONFIG_FILE = "data/forward_config_users_telethon.json"


def _collect_rules() -> Dict[str, List[dict]]:
    owners = _load_session_owners()
    user_config = load_json(FORWARD_USER_CONFIG_FILE)
    if not isinstance(user_config, dict):
        return {}
    users = user_config.get("users", {})
    if not isinstance(users, dict):
        return {}

    result: Dict[str, List[dict]] = {}
    for user_id, ucfg in users.items():
        if not isinstance(ucfg, dict):
            continue
        rules = ucfg.get("forward_rules")
        if not isinstance(rules, list):
            continue
        username = str(ucfg.get("username", "") or "").lstrip("@").lower()
        if not _can_use_rule(str(user_id), username):
            continue
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            if not bool(rule.get("enabled", True)):
                continue
            if str(rule.get("mode", "listen") or "listen").lower() != "listen":
                continue
            session_name = str(rule.get("session_name", "") or "").strip()
            if not session_name:
                continue
            if is_shared_session_name(session_name):
                continue
            if not _is_owner_for_session(owners, session_name, str(user_id), username):
                continue
            result.setdefault(session_name, []).append(rule)
    return result


async def _ensure_client(bot_name: str, session_name: str, api_id: int, api_hash: str):
    per_bot_clients = SESSION_CLIENTS_BY_BOT.setdefault(bot_name, {})
    client = per_bot_clients.get(session_name)
    if client:
        return client
    if is_shared_session_name(session_name):
        return None
    base = get_sessions_dir_by_bot(bot_name, session_name)
    os.makedirs(base, exist_ok=True)
    session_path = os.path.join(base, session_name)
    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        return None
    if DEBUG_FORWARD:
        print(f"✅ 协议号监听已启动: {session_name}")

    @client.on(events.NewMessage)
    async def _on_message(event):
        if getattr(event, "out", False):
            return
        if not getattr(event, "is_channel", False) or getattr(event, "is_group", False):
            return
        if getattr(event.message, "grouped_id", None):
            # 相册消息由 Album 处理
            return
        rules = SESSION_RULES_BY_BOT.get(bot_name, {}).get(session_name, [])
        if not rules:
            return
        chat_id = getattr(event, "chat_id", None)
        if chat_id is None:
            return
        if DEBUG_FORWARD:
            print(f"📥 监听到频道消息: session={session_name} source={chat_id}")
        message = event.message
        state = _load_history_state()
        state_changed = False
        for rule in rules:
            sources = set(rule.get("sources", []) or [])
            if sources and int(chat_id) not in sources:
                if DEBUG_FORWARD:
                    print("⛔ 跳过: 不在来源频道")
                continue
            if not _match_filter(message, rule.get("filter", "all")):
                if DEBUG_FORWARD:
                    print("⛔ 跳过: 类型过滤不匹配")
                continue
            targets = rule.get("targets", []) or []
            raw_text = _get_message_text(message)
            if _has_fold_entities(message.entities):
                processed_text, processed_entities = _process_text_preserve_folds(
                    raw_text, rule, message.entities
                )
                _debug_print_fold_compare(
                    "单条",
                    raw_text,
                    message.entities,
                    processed_text,
                    processed_entities,
                )
            else:
                if message.entities:
                    processed_text, processed_entities = _process_text_with_entities(
                        raw_text, rule, message.entities
                    )
                else:
                    processed_text = _process_text(raw_text, rule)
                    processed_entities = None
            if rule.get("skip_links") and _has_link(
                processed_text or "", processed_entities
            ):
                if DEBUG_FORWARD:
                    print("⛔ 跳过: 含链接或屏蔽域名")
                continue
            if processed_text == "":
                # If there's media but no text, still forward the media-only message.
                if not message.media or raw_text:
                    if DEBUG_FORWARD:
                        print("⛔ 跳过: 过滤后文本为空")
                    continue
            if processed_text is None:
                if not message.media:
                    if DEBUG_FORWARD:
                        print("⛔ 跳过: 无文本且无媒体")
                    continue
                processed_text = ""
            apply_processing = _needs_processing(rule)
            for target_id in targets:
                key = f"{session_name}:{chat_id}:{target_id}"
                try:
                    if message.media:
                        if apply_processing:
                            if processed_text and len(processed_text) > 1024:
                                # 方案1/3：单条文本（若文本里有链接，Telegram 会出预览）
                                await _send_message_safe(
                                    client,
                                    target_id,
                                    processed_text,
                                    entities=processed_entities,
                                )
                            else:
                                await _send_message_safe(
                                    client,
                                    target_id,
                                    processed_text or "",
                                    file=message.media,
                                    entities=processed_entities,
                                )
                        else:
                            if raw_text and len(raw_text) > 1024:
                                await _send_message_safe(
                                    client,
                                    target_id,
                                    raw_text,
                                    entities=message.entities,
                                )
                            else:
                                await _send_message_safe(
                                    client,
                                    target_id,
                                    raw_text,
                                    file=message.media,
                                    entities=message.entities,
                                )
                    else:
                        if apply_processing:
                            await _send_message_safe(
                                client,
                                target_id,
                                processed_text,
                                entities=processed_entities,
                            )
                        else:
                            await _send_message_safe(
                                client,
                                target_id,
                                raw_text,
                                entities=message.entities,
                            )
                    _append_recent_id(state, key, int(message.id))
                    _set_history_max_id(state, key, int(message.id))
                    state_changed = True
                    if DEBUG_FORWARD:
                        print(f"✅ 已转发: session={session_name} {chat_id} -> {target_id}")
                except Exception as e:
                    # 优先保留格式：caption 超长时不拆分，直接失败
                    print(f"⚠️ 协议号转发失败: {e} (来源: {chat_id} → 目标: {target_id})")
        if state_changed:
            _save_history_state(state)

    @client.on(events.Album)
    async def _on_album(event):
        if getattr(event, "out", False):
            return
        if not getattr(event, "is_channel", False) or getattr(event, "is_group", False):
            return
        rules = SESSION_RULES_BY_BOT.get(bot_name, {}).get(session_name, [])
        if not rules:
            return
        chat_id = getattr(event, "chat_id", None)
        if chat_id is None:
            return
        messages = event.messages or []
        if not messages:
            return
        if DEBUG_FORWARD:
            print(f"📥 监听到频道消息: session={session_name} source={chat_id} (相册)")

        caption_msg = None
        for m in messages:
            if m.message:
                caption_msg = m
                break
        if caption_msg is None:
            caption_msg = messages[0]
        raw_caption = _get_message_text(caption_msg)
        files = [m.media for m in messages if m.media]

        state = _load_history_state()
        state_changed = False
        for rule in rules:
            sources = set(rule.get("sources", []) or [])
            if sources and int(chat_id) not in sources:
                if DEBUG_FORWARD:
                    print("⛔ 跳过(相册): 不在来源频道")
                continue
            if not _match_filter(caption_msg, rule.get("filter", "all")):
                if DEBUG_FORWARD:
                    print("⛔ 跳过(相册): 类型过滤不匹配")
                continue
            targets = rule.get("targets", []) or []
            if _has_fold_entities(caption_msg.entities):
                processed_caption, processed_caption_entities = _process_text_preserve_folds(
                    raw_caption, rule, caption_msg.entities
                )
                _debug_print_fold_compare(
                    "相册",
                    raw_caption,
                    caption_msg.entities,
                    processed_caption,
                    processed_caption_entities,
                )
            else:
                if caption_msg.entities:
                    processed_caption, processed_caption_entities = _process_text_with_entities(
                        raw_caption, rule, caption_msg.entities
                    )
                else:
                    processed_caption = _process_text(raw_caption, rule)
                    processed_caption_entities = None
            if rule.get("skip_links") and _has_link(
                processed_caption or "", processed_caption_entities
            ):
                if DEBUG_FORWARD:
                    print("⛔ 跳过(相册): 含链接或屏蔽域名")
                continue
            if processed_caption == "":
                # If there's media but no caption, still forward the album.
                if not files or raw_caption:
                    if DEBUG_FORWARD:
                        print("⛔ 跳过(相册): 过滤后文本为空")
                    continue
            apply_processing = _needs_processing(rule)
            if not files:
                if DEBUG_FORWARD:
                    print("⛔ 跳过(相册): 无媒体文件")
                continue
            for target_id in targets:
                key = f"{session_name}:{chat_id}:{target_id}"
                try:
                    if apply_processing:
                        if processed_caption and len(processed_caption) > 1024:
                            await _send_message_safe(
                                client,
                                target_id,
                                processed_caption,
                                entities=processed_caption_entities,
                            )
                        else:
                            await _send_album_with_caption(
                                client,
                                target_id,
                                files,
                                caption=processed_caption or "",
                                entities=processed_caption_entities,
                            )
                    else:
                        if raw_caption and len(raw_caption) > 1024:
                            await _send_message_safe(
                                client,
                                target_id,
                                raw_caption,
                                entities=caption_msg.entities,
                            )
                        else:
                            await _send_album_with_caption(
                                client,
                                target_id,
                                files,
                                caption=raw_caption,
                                entities=caption_msg.entities,
                            )
                    _append_recent_id(state, key, int(caption_msg.id))
                    _set_history_max_id(state, key, int(caption_msg.id))
                    state_changed = True
                    if DEBUG_FORWARD:
                        print(f"✅ 已转发: session={session_name} {chat_id} -> {target_id}")
                except Exception as e:
                    # 优先保留格式：caption 超长时不拆分，直接失败
                    print(f"⚠️ 协议号转发失败: {e} (来源: {chat_id} → 目标: {target_id})")
        if state_changed:
            _save_history_state(state)

    per_bot_clients[session_name] = client
    return client


async def _refresh_sessions(app):
    bot_name = app.bot_data.get("name", "") or ""
    set_runtime_bot_name(bot_name)
    api_id, api_hash = _get_api_creds()
    if not api_id or not api_hash:
        return
    rules_by_session = _collect_rules()
    if DEBUG_FORWARD:
        if rules_by_session:
            summary = {k: len(v) for k, v in rules_by_session.items()}
            print(f"🧭 协议号规则刷新[{bot_name}]: {summary}")
        else:
            print(f"⚠️ 协议号规则为空[{bot_name}]")
    SESSION_RULES_BY_BOT[bot_name] = rules_by_session

    active_sessions = set(rules_by_session.keys())
    for session_name in active_sessions:
        await _ensure_client(bot_name, session_name, api_id, api_hash)

    # 清理不再需要的 session
    per_bot_clients = SESSION_CLIENTS_BY_BOT.setdefault(bot_name, {})
    for session_name in list(per_bot_clients.keys()):
        if session_name in active_sessions:
            continue
        client = per_bot_clients.pop(session_name, None)
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass


async def _process_history_requests():
    requests = load_json(HISTORY_REQUESTS_FILE)
    if not isinstance(requests, list) or not requests:
        return
    state = _load_history_state()
    remaining = []
    for req in requests:
        try:
            rule = req.get("rule", {}) if isinstance(req, dict) else {}
            session_name = str(rule.get("session_name", "") or "").strip()
            if not session_name:
                continue
            api_id, api_hash = _get_api_creds()
            if not api_id or not api_hash:
                continue
            bot_name = get_runtime_bot_name() or ""
            client = await _ensure_client(bot_name, session_name, api_id, api_hash)
            if not client:
                continue
            sources = rule.get("sources", []) or []
            targets = rule.get("targets", []) or []
            if not sources or not targets:
                continue
            source_id = int(sources[0])
            start_id = rule.get("start_id")
            end_id = rule.get("end_id")
            min_id = int(start_id) - 1 if start_id else None
            max_id = int(end_id) + 1 if end_id else None
            speed = _parse_speed(rule.get("speed", ""))

            # print(f"🚀 开始历史转发: session={session_name} source={source_id} targets={targets}")
            for target_id in targets:
                key = f"{session_name}:{source_id}:{target_id}"
                history_max_id = _get_history_max_id(state, key)
                effective_min_id = min_id
                if history_max_id:
                    effective_min_id = max(effective_min_id or 0, history_max_id)

                recent_ids = set(_recent_ids_for_key(state, key))
                current_gid = None
                current_group = []

                async def _flush_group(group_msgs):
                    if not group_msgs:
                        return
                    # 若整组都已发送，直接跳过
                    if all(int(m.id) in recent_ids for m in group_msgs):
                        return
                    caption_msg = None
                    for m in group_msgs:
                        if m.message:
                            caption_msg = m
                            break
                    if caption_msg is None:
                        caption_msg = group_msgs[0]
                    raw_caption = caption_msg.message or ""
                    if _should_skip_by_links(rule, raw_caption, caption_msg.entities):
                        return
                    if _has_fold_entities(caption_msg.entities):
                        processed_caption, processed_caption_entities = _process_text_preserve_folds(
                            raw_caption, rule, caption_msg.entities
                        )
                    else:
                        if caption_msg.entities:
                            processed_caption, processed_caption_entities = _process_text_with_entities(
                                raw_caption, rule, caption_msg.entities
                            )
                        else:
                            processed_caption = _process_text(raw_caption, rule)
                            processed_caption_entities = None
                    if processed_caption == "":
                        return
                    files = [m.media for m in group_msgs if m.media]
                    if not files:
                        return
                    try:
                        if processed_caption is None:
                            processed_caption = ""
                        if processed_caption and len(processed_caption) > 1024:
                            await _send_message_safe(
                                client,
                                target_id,
                                processed_caption,
                                entities=processed_caption_entities,
                            )
                        else:
                            await _send_album_with_caption(
                                client,
                                target_id,
                                files,
                                caption=processed_caption or "",
                                entities=processed_caption_entities,
                            )
                        max_id = max(int(m.id) for m in group_msgs)
                        for m in group_msgs:
                            mid = int(m.id)
                            _append_recent_id(state, key, mid)
                            recent_ids.add(mid)
                        _set_history_max_id(state, key, max_id)
                        await asyncio.sleep(speed)
                    except Exception as e:
                        print(f"⚠️ 历史转发失败: {e} (来源: {source_id} → 目标: {target_id})")

                async for message in client.iter_messages(
                    source_id, min_id=effective_min_id, max_id=max_id, reverse=True
                ):
                    gid = getattr(message, "grouped_id", None)
                    if gid:
                        if current_gid is None:
                            current_gid = gid
                        if gid != current_gid:
                            await _flush_group(current_group)
                            current_group = []
                            current_gid = gid
                        current_group.append(message)
                        continue

                    # 非相册消息，先把上一组相册发掉
                    if current_group:
                        await _flush_group(current_group)
                        current_group = []
                        current_gid = None

                    if int(message.id) in recent_ids:
                        continue
                    raw_text = message.message or ""
                    if _should_skip_by_links(rule, raw_text, message.entities):
                        continue
                    processed_entities = None
                    if message.entities:
                        processed_text, processed_entities = _process_text_with_entities(
                            raw_text, rule, message.entities
                        )
                    else:
                        processed_text = _process_text(raw_text, rule)
                    if processed_text == "":
                        continue
                    if processed_text is None:
                        if not message.media:
                            continue
                        processed_text = ""
                    try:
                        if message.media:
                            if processed_text and len(processed_text) > 1024:
                                await _send_text_split(
                                    client,
                                    target_id,
                                    processed_text,
                                    entities=processed_entities,
                                    limit=4096,
                                )
                                await _send_message_safe(
                                    client, target_id, "", file=message.media
                                )
                            else:
                                await _send_message_safe(
                                    client,
                                    target_id,
                                    processed_text or "",
                                    file=message.media,
                                    entities=processed_entities,
                                )
                        else:
                            await _send_text_split(
                                client,
                                target_id,
                                processed_text or "",
                                entities=processed_entities,
                                limit=4096,
                            )
                        _append_recent_id(state, key, int(message.id))
                        recent_ids.add(int(message.id))
                        _set_history_max_id(state, key, int(message.id))
                        await asyncio.sleep(speed)
                    except Exception as e:
                        print(f"⚠️ 历史转发失败: {e} (来源: {source_id} → 目标: {target_id})")

                if current_group:
                    await _flush_group(current_group)
            # print(f"✅ 历史转发完成: session={session_name} source={source_id}")
        except Exception as e:
            print(f"⚠️ 历史转发异常: {e}")
            remaining.append(req)
    _save_history_state(state)
    save_json(HISTORY_REQUESTS_FILE, remaining)


async def telethon_forwarder_loop(app):
    if TelegramClient is None or events is None:
        print("❗ Telethon 未安装，协议号自动转发未启动。")
        return
    while True:
        try:
            await _refresh_sessions(app)
            await _process_history_requests()
        except Exception as e:
            print(f"⚠️ 协议号自动转发刷新失败: {e}")
        await asyncio.sleep(30)


async def start_telethon_forwarder_job(context):
    app = context.application
    if app.bot_data.get("telethon_forwarder_started"):
        return
    app.bot_data["telethon_forwarder_started"] = True
    FORWARD_TASKS["main"] = asyncio.create_task(telethon_forwarder_loop(app))
