"""Collected-teacher roster and category browsing for publication channels.

This module is intentionally bound to the legacy ``publish_setting`` facade at
startup.  Keeping the facade preserves existing imports and callback routes
while moving the cohesive roster feature out of the large publication module.
"""
from __future__ import annotations

import time
import types
import uuid
from typing import Optional

from pypinyin import lazy_pinyin
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity, Update
from telegram.ext import ContextTypes


COLLECTION_DISPLAY_CHANNEL_OPTIONS = {
    "main": "主频道",
    "backup": "备用频道",
}


def _collection_display_channel_mode(config: dict) -> str:
    mode = str((config or {}).get("collection_display_channel") or "main").lower()
    return mode if mode in COLLECTION_DISPLAY_CHANNEL_OPTIONS else "main"


def _collection_display_channel_id(config: dict) -> Optional[int]:
    """Resolve which mirrored channel supplies public roster links.

    The publication main channel remains authoritative for reports and comment
    routing. This setting only controls the channel/message links emitted by
    ``老师列表``.
    """
    main_channel_id = _as_int((config or {}).get("channel_id"))
    if _collection_display_channel_mode(config) == "backup":
        return _backup_channel_id(config or {}, main_channel_id) or main_channel_id
    return main_channel_id


def _collection_display_channel_label(config: dict) -> str:
    mode = _collection_display_channel_mode(config)
    channel_id = _collection_display_channel_id(config)
    label = COLLECTION_DISPLAY_CHANNEL_OPTIONS[mode]
    if mode == "backup" and channel_id == _as_int((config or {}).get("channel_id")):
        return f"{label}（未设置，已回退主频道）"
    return f"{label}：{channel_id if channel_id is not None else '未设置'}"


def _collection_display_labels(config: dict) -> list[str]:
    """Parse fields used for each collected-post roster item.

    ``#`` is supported as a display field and reads every keyword extracted by
    the special ``#`` keyword rule (for example ``#成都 #高新区``).
    """
    raw = str((config or {}).get("collection_display_label") or "艺名")
    labels = []
    for label in re.split(r"[+＋,，、\n]+", raw):
        label = label.strip()[:30]
        if label and label not in labels:
            labels.append(label)
    return labels or ["艺名"]


def _collection_display_value(post: dict, config: dict) -> str:
    parts = []
    for label in _collection_display_labels(config):
        values = _checkin_field_values(post, label, recover_from_keyword_index=False)
        if values:
            parts.append("、".join(_clean_keyword_value(item) for item in values))
    if parts:
        return "".join(parts)[:100]
    return "收录帖子"


def _latest_collection_posts_by_contact(posts: list[dict], config: dict) -> list[dict]:
    """Keep the newest collected post for each Telegram contact.

    Reposting the same ``联系`` / ``联系方式`` username updates the teacher's
    roster entry instead of leaving an older post in the ``老师`` display.
    Posts without a recognized Telegram contact remain visible.
    """
    ordered_posts = sorted(
        posts,
        key=lambda post: (
            int(post.get("created_at", 0) or 0),
            int(post.get("message_id", 0) or 0),
        ),
        reverse=True,
    )
    selected_posts = []
    seen_contacts = set()
    for post in ordered_posts:
        contacts = _checkin_contact_keys(post, config)
        if contacts and contacts.intersection(seen_contacts):
            continue
        selected_posts.append(post)
        seen_contacts.update(contacts)
    return selected_posts


def _collection_checkin_field_fallbacks(config: dict) -> dict[tuple[int, int], dict[str, list[str]]]:
    """Return saved post fields keyed by the primary channel/message pair.

    ``publish_keyword_map.json`` is rebuilt wholesale after a Telegram edit.
    The check-in store also records the extracted fields, and is therefore a
    useful fallback when an edit update omits one field (for example ``城市``)
    even though the original post had already been indexed correctly.
    """
    target_channel_id = _collection_display_channel_id(config)
    fallbacks: dict[tuple[int, int], dict[str, list[str]]] = {}
    for saved_post in _load_checkin_posts().get("posts", {}).values():
        if not isinstance(saved_post, dict):
            continue
        channel_id = _as_int(saved_post.get("channel_id"))
        message_id = _as_int(saved_post.get("message_id"))
        if channel_id is None or message_id is None or message_id <= 0:
            continue
        if target_channel_id is not None and channel_id != target_channel_id:
            continue
        saved_fields = saved_post.get("fields")
        if not isinstance(saved_fields, dict):
            continue
        fields: dict[str, list[str]] = {}
        for raw_label, raw_values in saved_fields.items():
            label = str(raw_label or "").strip()
            if not label or not isinstance(raw_values, list):
                continue
            values = [str(value).strip() for value in raw_values if str(value).strip()]
            if values:
                fields[label] = values
        if fields:
            fallbacks[(channel_id, message_id)] = fields
    return fallbacks


def _load_collection_display_posts(config: dict) -> list[dict]:
    """Rebuild the newest collected record for every current-channel contact.

    Keyword records remain authoritative.  Saved check-in fields only fill
    labels absent from the keyword index, so a deliberately changed city in an
    edited post still takes effect while a transiently omitted ``城市`` field no
    longer moves the teacher into ``未分类``.
    """
    target_channel_id = _collection_display_channel_id(config)
    # A process interruption during an older failover could leave the local
    # keyword/check-in indexes on the retired channel even though the backup
    # was successfully promoted. Repair that specific, recorded mirror path
    # before reading the current-main index below. The repair is idempotent
    # and is cached per runtime, so opening the roster does not repeatedly
    # rewrite storage.
    _repair_promoted_backup_indexes(config)

    posts: dict[tuple[int, int], dict] = {}
    for records in _load_keyword_map().values():
        for record in records if isinstance(records, list) else []:
            if not isinstance(record, dict):
                continue
            channel_id = _as_int(record.get("channel_id"))
            message_id = _as_int(record.get("channel_message_id"))
            if channel_id is None or message_id is None or message_id <= 0:
                continue
            if target_channel_id is not None and channel_id != target_channel_id:
                continue
            key = (channel_id, message_id)
            post = posts.setdefault(
                key,
                {
                    "channel_id": channel_id,
                    "message_id": message_id,
                    "created_at": 0,
                    "fields": {},
                },
            )
            label = str(record.get("label") or "").strip()
            value = str(record.get("raw") or record.get("key") or "").strip()
            if label and value:
                values = post["fields"].setdefault(label, [])
                if value not in values:
                    values.append(value)
            try:
                post["created_at"] = max(
                    int(post.get("created_at", 0) or 0),
                    int(record.get("created_at", 0) or 0),
                )
            except (TypeError, ValueError):
                pass

    checkin_field_fallbacks = _collection_checkin_field_fallbacks(config)
    for key, post in posts.items():
        for label, fallback_values in checkin_field_fallbacks.get(key, {}).items():
            # Do not overwrite fields parsed from the current keyword records.
            # This is intentionally a fallback for a missing field only.
            if post["fields"].get(label):
                continue
            post["fields"][label] = list(fallback_values)
    return _latest_collection_posts_by_contact(list(posts.values()), config)


def _group_collection_display_posts(posts: list[dict], config: dict) -> dict[str, dict[str, list[dict]]]:
    """Group collected posts by the configured primary and secondary fields."""
    primary_label = str((config or {}).get("collection_display_group_label") or "")
    secondary_label = str((config or {}).get("collection_display_subgroup_label") or "")
    grouped: dict[str, dict[str, list[dict]]] = {}
    for post in posts:
        primary = _checkin_group_value_for_label(
            post, primary_label, recover_from_keyword_index=False
        )
        secondary = _checkin_group_value_for_label(
            post, secondary_label, recover_from_keyword_index=False
        )
        grouped.setdefault(primary, {}).setdefault(secondary, []).append(post)
    return grouped


def _is_master_runtime_bot() -> bool:
    return bool(MASTER_BOT_NAME) and get_runtime_bot_name() == MASTER_BOT_NAME


def _is_master_bot_context(context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        bot_name = str(context.application.bot_data.get("name", "")).strip()
    except Exception:
        bot_name = ""
    return bool(MASTER_BOT_NAME) and bot_name == MASTER_BOT_NAME


def _collection_display_command_text(config: dict) -> str:
    return _checkin_command_text(config, "collection_display_command_text", "老师")


TEACHER_LIST_CALLBACK_PREFIX = "teacher_list"
TEACHER_LIST_BUTTONS_PER_ROW = 3
# Category buttons also need pagination: a ``#`` grouping can produce hundreds
# of distinct tags, which would otherwise exceed Telegram's reply-markup size.
TEACHER_LIST_CATEGORY_PAGE_SIZE = 30
# Keep every message comfortably below Telegram's text limit while preserving
# every teacher link through callback pagination.
TEACHER_LIST_PAGE_SIZE = 20
TEACHER_LIST_FILTER_CACHE_TTL_SEC = 600
TEACHER_LIST_FILTER_CACHE_MAX_ENTRIES = 100
_TEACHER_LIST_FILTER_CACHE: dict[str, dict] = {}


def _teacher_list_button_rows(buttons: list[InlineKeyboardButton]) -> list[list[InlineKeyboardButton]]:
    """Place roster navigation choices in rows of three buttons."""
    return [
        buttons[index : index + TEACHER_LIST_BUTTONS_PER_ROW]
        for index in range(0, len(buttons), TEACHER_LIST_BUTTONS_PER_ROW)
    ]


def _teacher_list_button_text(value: str) -> str:
    """Keep category captions safely within Telegram's inline-button limit."""
    value = str(value or "未分类").strip() or "未分类"
    return value if len(value) <= 60 else f"{value[:59]}…"


def _collection_category_pinyin_key(value: str) -> tuple:
    """Sort roster category names stably by pinyin, with uncategorized last."""
    name = str(value or "").strip() or "未分类"
    # “未分类” is a fallback rather than a real category, so it should not
    # displace configured city/area categories at the top of the list.
    is_fallback = name in {"未分类", "全部"}
    try:
        pinyin = tuple(lazy_pinyin(name))
    except Exception:
        pinyin = (name.casefold(),)
    return (1 if is_fallback else 0, pinyin, name.casefold())


def _teacher_list_catalog(config: dict) -> list[dict]:
    """Return collected posts organized as primary -> secondary categories.

    The existing collection-display settings determine both category levels, so
    the owner can change the fields (for example 城市 / 区域) without code
    changes. Empty category settings still retain the two-step navigation by
    using a single ``全部`` category at that level.
    """
    primary_label = str((config or {}).get("collection_display_group_label") or "").strip()
    secondary_label = str((config or {}).get("collection_display_subgroup_label") or "").strip()
    catalog: list[dict] = []
    primary_lookup: dict[str, dict] = {}

    for post in _load_collection_display_posts(config):
        primary = (
            _checkin_group_value_for_label(
                post, primary_label, recover_from_keyword_index=False
            )
            if primary_label
            else "全部"
        )
        secondary = (
            _checkin_group_value_for_label(
                post, secondary_label, recover_from_keyword_index=False
            )
            if secondary_label
            else "全部"
        )
        primary = primary or "未分类"
        secondary = secondary or "未分类"

        primary_item = primary_lookup.get(primary)
        if primary_item is None:
            primary_item = {"name": primary, "secondary": [], "lookup": {}}
            primary_lookup[primary] = primary_item
            catalog.append(primary_item)

        secondary_item = primary_item["lookup"].get(secondary)
        if secondary_item is None:
            secondary_item = {"name": secondary, "posts": []}
            primary_item["lookup"][secondary] = secondary_item
            primary_item["secondary"].append(secondary_item)
        secondary_item["posts"].append(post)

    # Category button indexes must not depend on whichever post happened to be
    # newest.  Otherwise deleting a record could reorder the catalog and make a
    # stale callback index point from 上海 to 北京.  Keep both levels in stable
    # pinyin order instead.
    catalog.sort(key=lambda item: _collection_category_pinyin_key(item.get("name")))
    # ``lookup`` only helps construction and must not leak into callback state.
    for primary_item in catalog:
        primary_item["secondary"].sort(
            key=lambda item: _collection_category_pinyin_key(item.get("name"))
        )
        primary_item.pop("lookup", None)
    return catalog


def _teacher_list_primary_keyboard(
    catalog: list[dict], page: int = 0, *, has_secondary_group: bool = True
) -> InlineKeyboardMarkup:
    """Render a bounded page of first-level categories."""
    page_count = max(
        1,
        (len(catalog) + TEACHER_LIST_CATEGORY_PAGE_SIZE - 1)
        // TEACHER_LIST_CATEGORY_PAGE_SIZE,
    )
    page = max(0, min(int(page), page_count - 1))
    start = page * TEACHER_LIST_CATEGORY_PAGE_SIZE
    visible_items = catalog[start : start + TEACHER_LIST_CATEGORY_PAGE_SIZE]
    buttons = [
        InlineKeyboardButton(
            _teacher_list_button_text(item["name"]),
            callback_data=(
                f"{TEACHER_LIST_CALLBACK_PREFIX}:secondary:{index}"
                if has_secondary_group
                else f"{TEACHER_LIST_CALLBACK_PREFIX}:content:{index}:0:0"
            ),
        )
        for index, item in enumerate(visible_items, start=start)
    ]
    rows = _teacher_list_button_rows(buttons)
    pager = []
    if page > 0:
        pager.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"{TEACHER_LIST_CALLBACK_PREFIX}:primary:{page - 1}"))
    if page < page_count - 1:
        pager.append(InlineKeyboardButton("➡️ 下一页", callback_data=f"{TEACHER_LIST_CALLBACK_PREFIX}:primary:{page + 1}"))
    if pager:
        rows.append(pager)
    # Do not use the generic ``start:back`` callback here.  A teacher list can
    # be requested in a group, where every member sees the same message; its
    # close action must pass through the requester authorization below too.
    rows.append([
        InlineKeyboardButton(
            "❌ 关闭",
            callback_data=f"{TEACHER_LIST_CALLBACK_PREFIX}:close",
        )
    ])
    return InlineKeyboardMarkup(rows)


def _teacher_list_secondary_keyboard(
    catalog: list[dict], primary_index: int, page: int = 0
) -> InlineKeyboardMarkup:
    """Render a bounded page of second-level categories."""
    primary = catalog[primary_index]
    secondary_items = primary["secondary"]
    page_count = max(
        1,
        (len(secondary_items) + TEACHER_LIST_CATEGORY_PAGE_SIZE - 1)
        // TEACHER_LIST_CATEGORY_PAGE_SIZE,
    )
    page = max(0, min(int(page), page_count - 1))
    start = page * TEACHER_LIST_CATEGORY_PAGE_SIZE
    visible_items = secondary_items[start : start + TEACHER_LIST_CATEGORY_PAGE_SIZE]
    buttons = [
        InlineKeyboardButton(
            _teacher_list_button_text(item["name"]),
            callback_data=f"{TEACHER_LIST_CALLBACK_PREFIX}:content:{primary_index}:{index}:0",
        )
        for index, item in enumerate(visible_items, start=start)
    ]
    rows = _teacher_list_button_rows(buttons)
    pager = []
    if page > 0:
        pager.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"{TEACHER_LIST_CALLBACK_PREFIX}:secondary:{primary_index}:{page - 1}"))
    if page < page_count - 1:
        pager.append(InlineKeyboardButton("➡️ 下一页", callback_data=f"{TEACHER_LIST_CALLBACK_PREFIX}:secondary:{primary_index}:{page + 1}"))
    if pager:
        rows.append(pager)
    rows.append([InlineKeyboardButton("⬅️ 返回一级分类", callback_data=f"{TEACHER_LIST_CALLBACK_PREFIX}:primary:0")])
    return InlineKeyboardMarkup(rows)


def _collection_display_category_labels(config: dict) -> tuple[str, str]:
    """Return the configured first- and second-level filter fields."""
    return (
        str((config or {}).get("collection_display_group_label") or "").strip(),
        str((config or {}).get("collection_display_subgroup_label") or "").strip(),
    )


def _collection_display_post_categories(post: dict, config: dict) -> tuple[str, str]:
    """Read only the configured category values for command filtering."""
    primary_label, secondary_label = _collection_display_category_labels(config)
    primary = (
        _checkin_group_value_for_label(
            post, primary_label, recover_from_keyword_index=False
        )
        if primary_label
        else "全部"
    )
    secondary = (
        _checkin_group_value_for_label(
            post, secondary_label, recover_from_keyword_index=False
        )
        if secondary_label
        else "全部"
    )
    return primary or "未分类", secondary or "未分类"


def _filter_collection_display_posts(
    posts: list[dict], config: dict, filter_text: str
) -> list[dict]:
    """Filter and keep each primary/secondary category contiguous.

    Collection records are stored in post order, so records for one secondary
    category can be interleaved with another one.  The filtered ``老师`` reply
    renders a category heading only when its value changes; return category
    buckets in first-seen order to ensure identical secondary categories share
    one heading and are displayed together.
    """
    keyword = str(filter_text or "").strip().casefold()
    # Category aliases are intentionally partial matches (e.g. “闵行” finds
    # “闵行区”), but a single character such as “上” or “海” is too broad and
    # would create accidental roster replies in ordinary group chat.
    if keyword and len(keyword) < 2:
        return []

    grouped: dict[tuple[str, str], list[dict]] = {}
    for post in posts:
        primary, secondary = _collection_display_post_categories(post, config)
        if keyword and keyword not in primary.casefold() and keyword not in secondary.casefold():
            continue
        grouped.setdefault((primary, secondary), []).append(
            {"primary": primary, "secondary": secondary, "post": post}
        )

    # Match the category browser: direct/filter replies must use the same
    # stable primary → secondary pinyin order, rather than source-post time.
    return [
        item
        for category in sorted(
            grouped,
            key=lambda value: (
                _collection_category_pinyin_key(value[0]),
                _collection_category_pinyin_key(value[1]),
            ),
        )
        for item in grouped[category]
    ]


def _purge_teacher_filter_cache() -> None:
    now = time.monotonic()
    for token, state in list(_TEACHER_LIST_FILTER_CACHE.items()):
        if float(state.get("expires_at", 0) or 0) <= now:
            _TEACHER_LIST_FILTER_CACHE.pop(token, None)


def _store_teacher_filter_result(
    config: dict, filter_text: str, items: list[dict]
) -> str:
    _purge_teacher_filter_cache()
    while len(_TEACHER_LIST_FILTER_CACHE) >= TEACHER_LIST_FILTER_CACHE_MAX_ENTRIES:
        oldest_token = min(
            _TEACHER_LIST_FILTER_CACHE,
            key=lambda key: float(_TEACHER_LIST_FILTER_CACHE[key].get("expires_at", 0) or 0),
        )
        _TEACHER_LIST_FILTER_CACHE.pop(oldest_token, None)
    token = uuid.uuid4().hex[:12]
    primary_label, secondary_label = _collection_display_category_labels(config)
    _TEACHER_LIST_FILTER_CACHE[token] = {
        "expires_at": time.monotonic() + TEACHER_LIST_FILTER_CACHE_TTL_SEC,
        "filter_text": str(filter_text or "").strip(),
        "items": items,
        "primary_label": primary_label,
        "secondary_label": secondary_label,
    }
    return token


def _get_teacher_filter_result(token: str) -> Optional[dict]:
    _purge_teacher_filter_cache()
    state = _TEACHER_LIST_FILTER_CACHE.get(str(token or ""))
    return state if isinstance(state, dict) else None


def _refresh_teacher_filter_result(config: dict, state: dict) -> dict:
    """Refresh a saved roster filter against the current keyword index.

    The inline keyboard only carries a short-lived filter token. Previously the
    token also retained a snapshot of its matching posts for up to ten minutes.
    As a result, an administrator could edit a channel post and successfully
    refresh the keyword index, while users paging through an already-open
    filtered ``老师`` list still saw the old collection fields. Rebuild the
    matching items at callback time so the next page always reflects the
    latest channel-post edit.
    """
    filter_text = str(state.get("filter_text") or "").strip()
    posts = _load_collection_display_posts(config)
    state["items"] = _filter_collection_display_posts(posts, config, filter_text)
    primary_label, secondary_label = _collection_display_category_labels(config)
    state["primary_label"] = primary_label
    state["secondary_label"] = secondary_label
    # Each successful interaction keeps the active result usable while the
    # user pages it, without preserving an obsolete snapshot.
    state["expires_at"] = time.monotonic() + TEACHER_LIST_FILTER_CACHE_TTL_SEC
    return state


def _teacher_list_filtered_payload(
    config: dict, token: str, state: dict, page: int
) -> tuple[str, list[MessageEntity], InlineKeyboardMarkup]:
    """Render one safe, paginated page of command-filtered roster links."""
    items = state.get("items") if isinstance(state.get("items"), list) else []
    page_count = max(1, (len(items) + TEACHER_LIST_PAGE_SIZE - 1) // TEACHER_LIST_PAGE_SIZE)
    page = max(0, min(int(page), page_count - 1))
    start = page * TEACHER_LIST_PAGE_SIZE
    visible_items = items[start : start + TEACHER_LIST_PAGE_SIZE]

    title = _checkin_command_text(config, "collection_display_text", "收录老师")
    _primary_label, secondary_group_label = _collection_display_category_labels(config)
    has_secondary_group = bool(secondary_group_label)
    filter_text = str(state.get("filter_text") or "").strip()
    if filter_text:
        primary_label = str(state.get("primary_label") or "一级分类")
        category_labels = [primary_label]
        if has_secondary_group:
            category_labels.append(str(state.get("secondary_label") or "二级分类"))
        subtitle = f"筛选：{filter_text}（{' / '.join(category_labels)}）"
    else:
        subtitle = "全部收录"
    lines = [f"📚 {title}", subtitle, f"共 {len(items)} 条", ""]
    link_items: list[tuple[str, int, int, str]] = []
    previous_primary = None
    previous_secondary = None
    for offset, item in enumerate(visible_items, start=start + 1):
        primary = str(item.get("primary") or "未分类")
        secondary = str(item.get("secondary") or "未分类")
        if primary != previous_primary:
            if previous_primary is not None:
                lines.append("")
            lines.append(f"【{primary}】")
            previous_primary = primary
            previous_secondary = None
        if has_secondary_group and secondary != previous_secondary:
            lines.append(f"〔{secondary}〕")
            previous_secondary = secondary

        post = item.get("post") if isinstance(item.get("post"), dict) else {}
        name_text = _collection_display_value(post, config)
        line = f"{offset}. {name_text}"
        lines.append(line)
        link = _fallback_channel_message_link(
            _as_int(post.get("channel_id")), _as_int(post.get("message_id"))
        )
        if link:
            link_items.append((line, len(str(offset)) + 2, len(name_text), link))

    if page_count > 1:
        lines.extend(["", f"第 {page + 1}/{page_count} 页"])
    result_text = "\n".join(lines)
    entities: list[MessageEntity] = []
    cursor = 0
    for line, name_offset, name_length, link in link_items:
        line_start = result_text.find(line, cursor)
        if line_start < 0:
            continue
        entities.append(
            MessageEntity(
                type=MessageEntity.TEXT_LINK,
                offset=line_start + name_offset,
                length=name_length,
                url=link,
            )
        )
        cursor = line_start + len(line)

    rows: list[list[InlineKeyboardButton]] = []
    if page_count > 1:
        pager = []
        if page > 0:
            pager.append(
                InlineKeyboardButton(
                    "◀️ 上一页",
                    callback_data=f"{TEACHER_LIST_CALLBACK_PREFIX}:filter:{token}:{page - 1}",
                )
            )
        if page < page_count - 1:
            pager.append(
                InlineKeyboardButton(
                    "下一页 ▶️",
                    callback_data=f"{TEACHER_LIST_CALLBACK_PREFIX}:filter:{token}:{page + 1}",
                )
            )
        if pager:
            rows.append(pager)
    rows.append([
        InlineKeyboardButton("📂 分类浏览", callback_data=f"{TEACHER_LIST_CALLBACK_PREFIX}:primary"),
        InlineKeyboardButton("❌ 关闭", callback_data=f"{TEACHER_LIST_CALLBACK_PREFIX}:close"),
    ])
    return (
        result_text,
        MessageEntity.adjust_message_entities_to_utf_16(result_text, entities),
        InlineKeyboardMarkup(rows),
    )


def _teacher_list_content_payload(
    config: dict,
    catalog: list[dict],
    primary_index: int,
    secondary_index: int,
    page: int,
    *,
    show_delete_button: bool = False,
) -> tuple[str, list[MessageEntity], InlineKeyboardMarkup]:
    primary = catalog[primary_index]
    secondary = primary["secondary"][secondary_index]
    posts = secondary["posts"]
    page_count = max(1, (len(posts) + TEACHER_LIST_PAGE_SIZE - 1) // TEACHER_LIST_PAGE_SIZE)
    page = max(0, min(page, page_count - 1))
    start = page * TEACHER_LIST_PAGE_SIZE
    visible_posts = posts[start : start + TEACHER_LIST_PAGE_SIZE]

    title = _checkin_command_text(config, "collection_display_text", "收录老师")
    _primary_label, secondary_group_label = _collection_display_category_labels(config)
    has_secondary_group = bool(secondary_group_label)
    lines = [f"📚 {title}", f"【{primary['name']}】"]
    if has_secondary_group:
        lines.append(f"〔{secondary['name']}〕")
    lines.append("")
    link_items: list[tuple[str, str]] = []
    for offset, post in enumerate(visible_posts, start=start + 1):
        name_text = _collection_display_value(post, config)
        line = f"{offset}. {name_text}"
        lines.append(line)
        link = _fallback_channel_message_link(
            _as_int(post.get("channel_id")), _as_int(post.get("message_id"))
        )
        if link:
            link_items.append((line, len(str(offset)) + 2, len(name_text), link))

    if page_count > 1:
        lines.extend(["", f"第 {page + 1}/{page_count} 页，共 {len(posts)} 条"])
    result_text = "\n".join(lines)
    entities: list[MessageEntity] = []
    cursor = 0
    for line, name_offset, name_length, link in link_items:
        line_start = result_text.find(line, cursor)
        if line_start < 0:
            continue
        entities.append(
            MessageEntity(
                type=MessageEntity.TEXT_LINK,
                offset=line_start + name_offset,
                length=name_length,
                url=link,
            )
        )
        cursor = line_start + len(line)

    rows: list[list[InlineKeyboardButton]] = []
    if page_count > 1:
        pager = []
        if page > 0:
            pager.append(
                InlineKeyboardButton(
                    "◀️ 上一页",
                    callback_data=f"{TEACHER_LIST_CALLBACK_PREFIX}:content:{primary_index}:{secondary_index}:{page - 1}",
                )
            )
        if page < page_count - 1:
            pager.append(
                InlineKeyboardButton(
                    "下一页 ▶️",
                    callback_data=f"{TEACHER_LIST_CALLBACK_PREFIX}:content:{primary_index}:{secondary_index}:{page + 1}",
                )
            )
        if pager:
            rows.append(pager)
    if show_delete_button:
        rows.append([
            InlineKeyboardButton(
                "🗑 删除收录记录",
                callback_data=f"{TEACHER_LIST_CALLBACK_PREFIX}:delete_menu:{primary_index}:{secondary_index}:{page}",
            )
        ])
    rows.append([
        InlineKeyboardButton(
            "⬅️ 返回二级分类" if has_secondary_group else "⬅️ 返回一级分类",
            callback_data=(
                f"{TEACHER_LIST_CALLBACK_PREFIX}:secondary:{primary_index}"
                if has_secondary_group
                else f"{TEACHER_LIST_CALLBACK_PREFIX}:primary:0"
            ),
        )
    ])
    return (
        result_text,
        MessageEntity.adjust_message_entities_to_utf_16(result_text, entities),
        InlineKeyboardMarkup(rows),
    )


def _teacher_list_delete_callback_data(
    action: str,
    channel_id: int,
    message_id: int,
    primary_index: int,
    secondary_index: int,
    page: int,
) -> Optional[str]:
    """Build a compact, safe callback for one selected collected post."""
    callback_data = (
        f"{TEACHER_LIST_CALLBACK_PREFIX}:{action}:{channel_id}:{message_id}:"
        f"{primary_index}:{secondary_index}:{page}"
    )
    return callback_data if len(callback_data.encode("utf-8")) <= 64 else None


def _teacher_list_find_post(
    catalog: list[dict], channel_id: int, message_id: int
) -> Optional[dict]:
    """Locate a selected roster item again before showing its delete prompt."""
    for primary in catalog:
        for secondary in primary["secondary"]:
            for post in secondary["posts"]:
                if (
                    _as_int(post.get("channel_id")) == channel_id
                    and _as_int(post.get("message_id")) == message_id
                ):
                    return post
    return None


def _teacher_list_delete_menu_payload(
    config: dict,
    catalog: list[dict],
    primary_index: int,
    secondary_index: int,
    page: int,
    *,
    notice: str = "",
) -> tuple[str, InlineKeyboardMarkup]:
    """Build the admin-only picker for deleting one record at a time."""
    primary = catalog[primary_index]
    secondary = primary["secondary"][secondary_index]
    posts = secondary["posts"]
    page_count = max(1, (len(posts) + TEACHER_LIST_PAGE_SIZE - 1) // TEACHER_LIST_PAGE_SIZE)
    page = max(0, min(page, page_count - 1))
    start = page * TEACHER_LIST_PAGE_SIZE
    visible_posts = posts[start : start + TEACHER_LIST_PAGE_SIZE]

    title = _checkin_command_text(config, "collection_display_text", "收录老师")
    _primary_label, secondary_group_label = _collection_display_category_labels(config)
    category_text = f"【{primary['name']}】"
    if secondary_group_label:
        category_text += f"〔{secondary['name']}〕"
    lines = [
        f"🗑 管理 {title}",
        category_text,
        "请选择要删除的收录记录。仅删除本地收录、关键词和关联记录，不会删除频道原帖。",
    ]
    if notice:
        lines.extend(["", notice])
    if page_count > 1:
        lines.extend(["", f"第 {page + 1}/{page_count} 页，共 {len(posts)} 条"])

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                f"🗑 删除当前分类全部（{len(posts)} 条）",
                callback_data=(
                    f"{TEACHER_LIST_CALLBACK_PREFIX}:delete_all_confirm:"
                    f"{primary_index}:{secondary_index}:{page}"
                ),
            )
        ]
    ]
    for offset, post in enumerate(visible_posts, start=start + 1):
        channel_id = _as_int(post.get("channel_id"))
        message_id = _as_int(post.get("message_id"))
        if channel_id is None or message_id is None or message_id <= 0:
            continue
        callback_data = _teacher_list_delete_callback_data(
            "delete_confirm",
            channel_id,
            message_id,
            primary_index,
            secondary_index,
            page,
        )
        if not callback_data:
            continue
        # One record per row makes the destructive choice unambiguous even when
        # several teachers share the same display name.
        caption = _teacher_list_button_text(
            f"🗑 {offset}. {_collection_display_value(post, config)}"
        )
        rows.append([InlineKeyboardButton(caption, callback_data=callback_data)])

    if page_count > 1:
        pager = []
        if page > 0:
            pager.append(
                InlineKeyboardButton(
                    "◀️ 上一页",
                    callback_data=f"{TEACHER_LIST_CALLBACK_PREFIX}:delete_menu:{primary_index}:{secondary_index}:{page - 1}",
                )
            )
        if page < page_count - 1:
            pager.append(
                InlineKeyboardButton(
                    "下一页 ▶️",
                    callback_data=f"{TEACHER_LIST_CALLBACK_PREFIX}:delete_menu:{primary_index}:{secondary_index}:{page + 1}",
                )
            )
        if pager:
            rows.append(pager)
    rows.append([
        InlineKeyboardButton(
            "⬅️ 返回老师列表",
            callback_data=f"{TEACHER_LIST_CALLBACK_PREFIX}:content:{primary_index}:{secondary_index}:{page}",
        )
    ])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _teacher_list_is_available(context: ContextTypes.DEFAULT_TYPE, config: dict) -> bool:
    """Whether this bot has enabled its own collected-post roster."""
    return bool((config or {}).get("collection_display_enabled", False))


async def show_teacher_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open the master bot's collected-teacher list at its first category level."""
    message = update.effective_message or update.message
    if not message:
        return
    config = load_publish_config()
    if not _teacher_list_is_available(context, config):
        await message.reply_text("老师列表暂未开启。")
        return

    catalog = _teacher_list_catalog(config)
    if not catalog:
        await message.reply_text("当前暂无收录内容。")
        return

    title = _checkin_command_text(config, "collection_display_text", "收录老师")
    await message.reply_text(
        f"📚 {title}\n请选择一级分类：",
        reply_markup=_teacher_list_primary_keyboard(
            catalog, has_secondary_group=bool(_collection_display_category_labels(config)[1])
        ),
    )


def _teacher_list_requester_id(query) -> Optional[int]:
    """Return the user that requested this roster, when the message is a reply.

    User-triggered roster messages are sent with ``message.reply_text()``, so
    Telegram retains the original command as ``reply_to_message`` even after
    this roster message is edited during pagination.  Proactively sent rosters
    have no reply target and intentionally remain available to the group.
    """
    roster_message = getattr(query, "message", None)
    request_message = getattr(roster_message, "reply_to_message", None)
    requester = getattr(request_message, "from_user", None)
    return _as_int(getattr(requester, "id", None))


async def _ensure_teacher_list_requester(query) -> bool:
    """Allow callbacks on a requested roster only from its requesting user."""
    requester_id = _teacher_list_requester_id(query)
    if requester_id is None:
        return True

    clicker_id = _as_int(getattr(getattr(query, "from_user", None), "id", None))
    if clicker_id == requester_id:
        return True

    await query.answer("仅发起“老师”命令的用户可以操作此列表。", show_alert=True)
    return False


async def handle_teacher_list_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Navigate the public master-bot teacher roster and admin record deletion."""
    query = update.callback_query
    if not query or not query.data or not query.data.startswith(f"{TEACHER_LIST_CALLBACK_PREFIX}:"):
        return
    if not await _ensure_teacher_list_requester(query):
        return

    config = load_publish_config()
    if not _teacher_list_is_available(context, config):
        await query.answer("老师列表暂未开启。", show_alert=True)
        return

    catalog = _teacher_list_catalog(config)
    if not catalog:
        await query.answer("当前暂无收录内容。", show_alert=True)
        return

    parts = query.data.split(":")
    stage = parts[1] if len(parts) > 1 else ""
    title = _checkin_command_text(config, "collection_display_text", "收录老师")
    can_delete = bool(query.from_user) and has_admin_permission(
        context, query.from_user.id, "submission_config"
    )

    if stage == "close":
        await query.answer()
        try:
            await query.message.delete()
        except Exception:
            await query.edit_message_text("老师列表已关闭。")
        return

    if stage == "filter":
        try:
            if len(parts) != 4:
                raise ValueError
            token = parts[2]
            page = int(parts[3])
            if page < 0:
                raise ValueError
        except (TypeError, ValueError):
            await query.answer("筛选分页参数无效，请重新发送老师命令。", show_alert=True)
            return
        state = _get_teacher_filter_result(token)
        if state is None:
            await query.answer("筛选结果已过期，请重新发送老师命令。", show_alert=True)
            return
        state = _refresh_teacher_filter_result(config, state)
        text, entities, keyboard = _teacher_list_filtered_payload(config, token, state, page)
        await query.answer()
        await query.edit_message_text(
            text,
            entities=entities or None,
            disable_web_page_preview=True,
            reply_markup=keyboard,
        )
        return

    if stage == "primary":
        try:
            page = int(parts[2]) if len(parts) > 2 else 0
            if page < 0:
                raise ValueError
        except (TypeError, ValueError):
            return await query.answer("分类分页参数无效，请重新打开老师列表。", show_alert=True)
        page_count = max(1, (len(catalog) + TEACHER_LIST_CATEGORY_PAGE_SIZE - 1) // TEACHER_LIST_CATEGORY_PAGE_SIZE)
        page = min(page, page_count - 1)
        await query.answer()
        await query.edit_message_text(
            f"📚 {title}\n请选择一级分类：{f'（第 {page + 1}/{page_count} 页）' if page_count > 1 else ''}",
            reply_markup=_teacher_list_primary_keyboard(
                catalog, page, has_secondary_group=bool(_collection_display_category_labels(config)[1])
            ),
        )
        return

    if stage in {"delete_all_confirm", "delete_all_execute"}:
        if not can_delete:
            await query.answer("你没有删除收录信息的权限。", show_alert=True)
            return
        try:
            if len(parts) != 5:
                raise ValueError
            primary_index = int(parts[2])
            secondary_index = int(parts[3])
            page = int(parts[4])
            if primary_index < 0 or secondary_index < 0 or page < 0:
                raise ValueError
            primary = catalog[primary_index]
            secondary = primary["secondary"][secondary_index]
        except (IndexError, TypeError, ValueError):
            await query.answer("分类已更新，请重新打开老师列表。", show_alert=True)
            return

        posts = list(secondary.get("posts", []) or [])
        post_refs = [
            (_as_int(post.get("channel_id")), _as_int(post.get("message_id")))
            for post in posts
            if isinstance(post, dict)
        ]
        post_refs = [
            (channel_id, message_id)
            for channel_id, message_id in post_refs
            if channel_id is not None and message_id is not None and message_id > 0
        ]
        if not post_refs:
            await query.answer("当前分类没有可删除的收录记录。", show_alert=True)
            return

        category_text = f"【{primary['name']}】"
        if _collection_display_category_labels(config)[1]:
            category_text += f"〔{secondary['name']}〕"

        if stage == "delete_all_confirm":
            await query.answer()
            return await query.edit_message_text(
                f"⚠️ 确定删除当前分类全部 {len(post_refs)} 条收录记录吗？\n\n"
                f"{category_text}\n\n"
                "频道原帖不会删除，但每条帖子的本地收录、关键词、签到及关联记录都会被清除。",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "✅ 确认全部删除",
                        callback_data=(
                            f"{TEACHER_LIST_CALLBACK_PREFIX}:delete_all_execute:"
                            f"{primary_index}:{secondary_index}:{page}"
                        ),
                    )],
                    [InlineKeyboardButton(
                        "⬅️ 返回删除列表",
                        callback_data=(
                            f"{TEACHER_LIST_CALLBACK_PREFIX}:delete_menu:"
                            f"{primary_index}:{secondary_index}:{page}"
                        ),
                    )],
                ]),
            )

        total_keyword_records = 0
        removed_posts = 0
        for channel_id, message_id in post_refs:
            result = _remove_post_collection(channel_id, message_id)
            total_keyword_records += int(result.get("keyword_records", 0) or 0)
            removed_posts += 1

        refreshed_catalog = _teacher_list_catalog(config)
        await query.answer(f"✅ 已删除 {removed_posts} 条收录记录。")
        remaining_primary_index = next(
            (
                index
                for index, item in enumerate(refreshed_catalog)
                if item.get("name") == primary.get("name")
            ),
            None,
        )
        has_secondary_group = bool(_collection_display_category_labels(config)[1])
        if remaining_primary_index is not None and has_secondary_group:
            text = (
                f"✅ 已删除 {category_text} 的全部 {removed_posts} 条收录记录"
                f"（关键词 {total_keyword_records} 条）。\n\n"
                f"📚 {title}\n一级分类：{primary['name']}\n请选择二级分类："
            )
            keyboard = _teacher_list_secondary_keyboard(
                refreshed_catalog, remaining_primary_index
            )
        else:
            text = (
                f"✅ 已删除 {category_text} 的全部 {removed_posts} 条收录记录"
                f"（关键词 {total_keyword_records} 条）。\n\n"
                "请选择一级分类："
            )
            keyboard = (
                _teacher_list_primary_keyboard(
                    refreshed_catalog, has_secondary_group=has_secondary_group
                )
                if refreshed_catalog
                else None
            )
        return await query.edit_message_text(text, reply_markup=keyboard)

    if stage in {"delete_confirm", "delete_execute"}:
        if not can_delete:
            await query.answer("你没有删除收录信息的权限。", show_alert=True)
            return
        try:
            if len(parts) != 7:
                raise ValueError
            channel_id = _as_int(parts[2])
            message_id = _as_int(parts[3])
            primary_index = int(parts[4])
            secondary_index = int(parts[5])
            page = int(parts[6])
            if (
                channel_id is None
                or message_id is None
                or message_id <= 0
                or primary_index < 0
                or secondary_index < 0
                or page < 0
            ):
                raise ValueError
        except (IndexError, TypeError, ValueError):
            await query.answer("删除参数无效，请重新打开老师列表。", show_alert=True)
            return

        selected_post = _teacher_list_find_post(catalog, channel_id, message_id)
        if selected_post is None:
            await query.answer("该收录记录已不存在，请刷新列表。", show_alert=True)
            return

        if stage == "delete_confirm":
            display_name = _collection_display_value(selected_post, config)
            confirm_callback = _teacher_list_delete_callback_data(
                "delete_execute",
                channel_id,
                message_id,
                primary_index,
                secondary_index,
                page,
            )
            if not confirm_callback:
                await query.answer("删除参数过长，请重新打开老师列表。", show_alert=True)
                return
            await query.answer()
            return await query.edit_message_text(
                f"⚠️ 确定删除收录记录吗？\n\n{display_name}\n消息 ID：{message_id}\n\n"
                "频道原帖不会删除，但该帖的本地收录、关键词、签到及关联记录会被清除。",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ 确认删除", callback_data=confirm_callback)],
                    [InlineKeyboardButton(
                        "⬅️ 返回删除列表",
                        callback_data=f"{TEACHER_LIST_CALLBACK_PREFIX}:delete_menu:{primary_index}:{secondary_index}:{page}",
                    )],
                ]),
            )

        removed = _remove_post_collection(channel_id, message_id)
        details = []
        if removed["keyword_records"]:
            details.append(f"关键词 {removed['keyword_records']} 条")
        if removed["checkin_post"]:
            details.append("签到记录")
        if removed["comment_mapping"]:
            details.append("评论映射")
        if removed["backup_mapping"]:
            details.append("备用帖映射")
        if removed["report_subjects"]:
            details.append("报告收录字段")
        detail_text = "、".join(details) or "没有找到仍需清理的本地记录"

        refreshed_catalog = _teacher_list_catalog(config)
        await query.answer("✅ 收录记录已删除。")
        try:
            # Keep administrators in the selection flow while the category still
            # exists, so multiple records can be removed without reopening it.
            refreshed_catalog[primary_index]["secondary"][secondary_index]
            text, keyboard = _teacher_list_delete_menu_payload(
                config,
                refreshed_catalog,
                primary_index,
                secondary_index,
                page,
                notice=f"✅ 已删除消息 ID {message_id} 的收录记录（{detail_text}）。",
            )
        except (IndexError, TypeError):
            text = (
                f"✅ 已删除消息 ID {message_id} 的收录记录（{detail_text}）。\n\n"
                "当前分类已无其他收录内容，请选择一级分类。"
            )
            keyboard = (
                _teacher_list_primary_keyboard(
                    refreshed_catalog,
                    has_secondary_group=bool(_collection_display_category_labels(config)[1]),
                )
                if refreshed_catalog
                else None
            )
        return await query.edit_message_text(text, reply_markup=keyboard)

    try:
        primary_index = int(parts[2])
        if primary_index < 0:
            raise IndexError
        primary = catalog[primary_index]
    except (IndexError, TypeError, ValueError):
        await query.answer("分类已更新，请重新打开老师列表。", show_alert=True)
        return

    if stage == "secondary":
        _primary_label, secondary_group_label = _collection_display_category_labels(config)
        if not secondary_group_label:
            # A stale button rendered before the setting changed should also
            # skip the synthetic “全部” category.
            text, entities, keyboard = _teacher_list_content_payload(
                config, catalog, primary_index, 0, 0, show_delete_button=can_delete
            )
            await query.answer()
            return await query.edit_message_text(
                text,
                entities=entities or None,
                disable_web_page_preview=True,
                reply_markup=keyboard,
            )
        try:
            page = int(parts[3]) if len(parts) > 3 else 0
            if page < 0:
                raise ValueError
        except (TypeError, ValueError):
            return await query.answer("分类分页参数无效，请重新打开老师列表。", show_alert=True)
        secondary_count = len(primary["secondary"])
        page_count = max(1, (secondary_count + TEACHER_LIST_CATEGORY_PAGE_SIZE - 1) // TEACHER_LIST_CATEGORY_PAGE_SIZE)
        page = min(page, page_count - 1)
        await query.answer()
        await query.edit_message_text(
            f"📚 {title}\n一级分类：{primary['name']}\n请选择二级分类：{f'（第 {page + 1}/{page_count} 页）' if page_count > 1 else ''}",
            reply_markup=_teacher_list_secondary_keyboard(catalog, primary_index, page),
        )
        return

    if stage in {"content", "delete_menu"}:
        try:
            secondary_index = int(parts[3])
            page = int(parts[4]) if len(parts) > 4 else 0
            if secondary_index < 0 or page < 0:
                raise IndexError
            # Validate before building the payload so stale callback data is safe.
            primary["secondary"][secondary_index]
        except (IndexError, TypeError, ValueError):
            await query.answer("分类已更新，请重新打开老师列表。", show_alert=True)
            return

        if stage == "delete_menu":
            if not can_delete:
                await query.answer("你没有删除收录信息的权限。", show_alert=True)
                return
            text, keyboard = _teacher_list_delete_menu_payload(
                config, catalog, primary_index, secondary_index, page
            )
            await query.answer()
            return await query.edit_message_text(text, reply_markup=keyboard)

        text, entities, keyboard = _teacher_list_content_payload(
            config,
            catalog,
            primary_index,
            secondary_index,
            page,
            show_delete_button=can_delete,
        )
        await query.answer()
        await query.edit_message_text(
            text,
            entities=entities or None,
            disable_web_page_preview=True,
            reply_markup=keyboard,
        )
        return

    await query.answer("无效的老师列表操作。", show_alert=True)


async def _reply_collection_display(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    config: dict,
    *,
    filter_text: str = "",
    posts: Optional[list[dict]] = None,
) -> None:
    """Reply to ``老师 [一级/二级分类]`` with a paginated linked roster."""
    if posts is None:
        posts = _load_collection_display_posts(config)
    if not posts:
        await message.reply_text("当前暂无收录内容。")
        return

    filter_text = str(filter_text or "").strip()
    primary_label, secondary_label = _collection_display_category_labels(config)
    if filter_text and not primary_label and not secondary_label:
        await message.reply_text("当前未配置一级或二级分类字段，暂不支持按参数筛选。")
        return

    items = _filter_collection_display_posts(posts, config, filter_text)
    if not items:
        labels = " / ".join(label for label in (primary_label, secondary_label) if label)
        labels = labels or "一级/二级分类"
        await message.reply_text(
            f"未找到 {labels} 中包含“{filter_text}”的收录老师。"
        )
        return

    token = _store_teacher_filter_result(config, filter_text, items)
    result_text, entities, keyboard = _teacher_list_filtered_payload(
        config, token, _TEACHER_LIST_FILTER_CACHE[token], page=0
    )
    await message.reply_text(
        result_text,
        entities=entities or None,
        disable_web_page_preview=True,
        reply_markup=keyboard,
    )



_EXPORTS = tuple(
    name
    for name, value in tuple(globals().items())
    if (
        isinstance(value, types.FunctionType)
        and getattr(value, "__module__", "") == __name__
    )
    or name in {
        "COLLECTION_DISPLAY_CHANNEL_OPTIONS",
        "TEACHER_LIST_CALLBACK_PREFIX",
        "TEACHER_LIST_BUTTONS_PER_ROW",
        "TEACHER_LIST_CATEGORY_PAGE_SIZE",
        "TEACHER_LIST_PAGE_SIZE",
        "TEACHER_LIST_FILTER_CACHE_TTL_SEC",
        "TEACHER_LIST_FILTER_CACHE_MAX_ENTRIES",
        "_TEACHER_LIST_FILTER_CACHE",
    }
)

_LOCAL_NAMES = set(globals())


def bind_runtime(runtime: dict) -> None:
    """Refresh dependencies supplied by the legacy publication facade.

    The roster shares storage/configuration helpers with ``publish_setting``.
    Binding at call time also keeps existing tests and monkey patches against
    that public facade effective during the staged refactor.
    """
    for name, value in runtime.items():
        if name not in _LOCAL_NAMES:
            globals()[name] = value
