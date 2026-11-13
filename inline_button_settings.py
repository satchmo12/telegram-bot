"""Shared URL inline-button storage, layout, and rendering helpers.

A button collection stays as a flat list so existing configurations keep working.
``new_row`` controls the visual layout: omitted (the legacy format) means that
that button starts a new row; ``False`` places it beside the previous button.
"""
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

MAX_INLINE_BUTTONS = 20
MAX_BUTTONS_PER_ROW = 8
VALID_BUTTON_STYLES = frozenset({"primary", "success", "danger"})
VALID_URL_PREFIXES = ("http://", "https://", "tg://")


def is_valid_url(url: str) -> bool:
    return str(url or "").strip().startswith(VALID_URL_PREFIXES)


def normalize_buttons(raw_buttons, *, limit: int = MAX_INLINE_BUTTONS) -> List[dict]:
    """Return valid URL buttons while preserving presentation and row layout.

    Legacy buttons that lack ``new_row`` deliberately remain one button per
    row. This is backwards compatible and avoids silently changing existing
    messages when an installation upgrades.
    """
    if not isinstance(raw_buttons, list):
        return []

    normalized: List[dict] = []
    buttons_in_row = 0
    for raw_button in raw_buttons[:limit]:
        if not isinstance(raw_button, dict):
            continue
        text = str(raw_button.get("text") or "").strip()
        url = str(raw_button.get("url") or "").strip()
        if not text or not url:
            continue

        starts_new_row = (
            not normalized
            or raw_button.get("new_row", True) is not False
            or buttons_in_row >= MAX_BUTTONS_PER_ROW
        )
        if starts_new_row:
            buttons_in_row = 0

        button = {"text": text[:64], "url": url}
        # Keep the field explicit only when it changes legacy behaviour.
        if not starts_new_row:
            button["new_row"] = False
        style = str(raw_button.get("style") or "").strip().lower()
        if style in VALID_BUTTON_STYLES:
            button["style"] = style
        emoji_id = str(raw_button.get("icon_custom_emoji_id") or "").strip()
        if emoji_id.isdigit():
            button["icon_custom_emoji_id"] = emoji_id
        normalized.append(button)
        buttons_in_row += 1
    return normalized


def button_at(buttons, index) -> Optional[dict]:
    """Fetch a one-based button index without raising on malformed input."""
    try:
        item_index = int(index) - 1
    except (TypeError, ValueError):
        return None
    if not isinstance(buttons, list) or not 0 <= item_index < len(buttons):
        return None
    button = buttons[item_index]
    return button if isinstance(button, dict) else None


def button_api_kwargs(button: dict) -> dict:
    """Return optional Bot API presentation fields for a URL button."""
    api_kwargs = {}
    style = str(button.get("style") or "").strip().lower()
    if style in VALID_BUTTON_STYLES:
        api_kwargs["style"] = style
    emoji_id = str(button.get("icon_custom_emoji_id") or "").strip()
    if emoji_id.isdigit():
        api_kwargs["icon_custom_emoji_id"] = emoji_id
    return api_kwargs


def button_rows(buttons, *, limit: int = MAX_INLINE_BUTTONS) -> List[List[Tuple[int, dict]]]:
    """Group a flat button list into Telegram keyboard rows.

    Each tuple contains the one-based source position and the button mapping.
    Invalid entries are skipped, and no rendered row exceeds Telegram's
    eight-button maximum.
    """
    if not isinstance(buttons, list):
        return []

    rows: List[List[Tuple[int, dict]]] = []
    current_row: List[Tuple[int, dict]] = []
    for index, button in enumerate(buttons[:limit], start=1):
        if not isinstance(button, dict):
            continue
        text = str(button.get("text") or "").strip()
        url = str(button.get("url") or "").strip()
        if not text or not url:
            continue
        starts_new_row = not current_row or button.get("new_row", True) is not False
        if starts_new_row or len(current_row) >= MAX_BUTTONS_PER_ROW:
            if current_row:
                rows.append(current_row)
            current_row = []
        current_row.append((index, button))
    if current_row:
        rows.append(current_row)
    return rows


def build_reply_markup(buttons, *, limit: int = MAX_INLINE_BUTTONS):
    """Render a stored button collection into an inline keyboard."""
    rows = []
    for row in button_rows(buttons, limit=limit):
        rows.append([
            InlineKeyboardButton(
                text=str(button.get("text") or "").strip()[:64],
                url=str(button.get("url") or "").strip(),
                api_kwargs=button_api_kwargs(button) or None,
            )
            for _index, button in row
        ])
    return InlineKeyboardMarkup(rows) if rows else None


def layout_description(buttons) -> List[str]:
    """Create compact, user-facing rows describing the current layout."""
    rows = button_rows(buttons)
    if not rows:
        return ["未设置按钮"]
    return [
        " · ".join(
            f"#{index} {str(button.get('text') or '未命名')[:20]}"
            for index, button in row
        )
        for row in rows
    ]


def apply_per_row_layout(buttons: List[dict], per_row: int) -> bool:
    """Lay buttons out in fixed-width rows. Returns false for invalid input."""
    try:
        per_row = int(per_row)
    except (TypeError, ValueError):
        return False
    if not isinstance(buttons, list) or not 1 <= per_row <= MAX_BUTTONS_PER_ROW:
        return False
    for index, button in enumerate(buttons):
        if not isinstance(button, dict):
            continue
        if index % per_row == 0:
            button.pop("new_row", None)
        else:
            button["new_row"] = False
    return True


def set_button_row_mode(buttons: List[dict], index: int, *, same_row: bool) -> bool:
    """Put a button alongside its predecessor or make it begin a new row."""
    button = button_at(buttons, index)
    if not button:
        return False
    if int(index) <= 1:
        button.pop("new_row", None)
        return not same_row
    if same_row:
        # When this button currently begins a row, adding it to the preceding
        # row must still respect Telegram's eight-button row ceiling.
        rows = button_rows(buttons)
        previous_row_size = 0
        for row in rows:
            positions = [position for position, _item in row]
            if int(index) in positions:
                position_in_row = positions.index(int(index))
                if position_in_row == 0:
                    row_position = rows.index(row)
                    previous_row_size = len(rows[row_position - 1]) if row_position else 0
                break
        if previous_row_size >= MAX_BUTTONS_PER_ROW:
            return False
        button["new_row"] = False
    else:
        button.pop("new_row", None)
    return True


def move_button(buttons: List[dict], index: int, direction: str) -> Optional[int]:
    """Move a button one position and return its new one-based position."""
    if not isinstance(buttons, list):
        return None
    try:
        item_index = int(index) - 1
    except (TypeError, ValueError):
        return None
    if not 0 <= item_index < len(buttons):
        return None
    delta = -1 if direction == "previous" else 1 if direction == "next" else 0
    target = item_index + delta
    if not 0 <= target < len(buttons):
        return None
    row_starts = _row_start_flags(buttons)
    buttons[item_index], buttons[target] = buttons[target], buttons[item_index]
    _restore_row_starts(buttons, row_starts)
    return target + 1


def swap_buttons(buttons: List[dict], first: int, second: int) -> bool:
    """Swap two one-based button positions while retaining the row boundaries."""
    if not isinstance(buttons, list):
        return False
    try:
        first_index, second_index = int(first) - 1, int(second) - 1
    except (TypeError, ValueError):
        return False
    if first_index == second_index or not (
        0 <= first_index < len(buttons) and 0 <= second_index < len(buttons)
    ):
        return False
    row_starts = _row_start_flags(buttons)
    buttons[first_index], buttons[second_index] = buttons[second_index], buttons[first_index]
    _restore_row_starts(buttons, row_starts)
    return True


def _row_start_flags(buttons: List[dict]) -> List[bool]:
    """Capture row boundaries by position before an ordering operation."""
    return [
        index == 0 or not isinstance(button, dict) or button.get("new_row", True) is not False
        for index, button in enumerate(buttons)
    ]


def _restore_row_starts(buttons: List[dict], row_starts: List[bool]) -> None:
    """Restore row boundaries to their positions after content is reordered."""
    for index, button in enumerate(buttons):
        if not isinstance(button, dict):
            continue
        if index == 0 or (index < len(row_starts) and row_starts[index]):
            button.pop("new_row", None)
        else:
            button["new_row"] = False


def layout_overview_keyboard(
    buttons,
    *,
    select_callback: Callable[[int], str],
    preset_callback: Callable[[int], str],
    back_callback: str,
) -> InlineKeyboardMarkup:
    """Build a reusable layout/order panel for any stored URL buttons."""
    rows = []
    for row in button_rows(buttons):
        rows.append([
            InlineKeyboardButton(
                f"#{index} {str(button.get('text') or '未命名')[:18]}",
                callback_data=select_callback(index),
            )
            for index, button in row
        ])
    rows.extend([
        [
            InlineKeyboardButton("每行 1 个", callback_data=preset_callback(1)),
            InlineKeyboardButton("每行 2 个", callback_data=preset_callback(2)),
            InlineKeyboardButton("每行 3 个", callback_data=preset_callback(3)),
        ],
        [
            InlineKeyboardButton("每行 4 个", callback_data=preset_callback(4)),
            InlineKeyboardButton("每行 5 个", callback_data=preset_callback(5)),
            InlineKeyboardButton("每行 6 个", callback_data=preset_callback(6)),
        ],
        [InlineKeyboardButton("⬅️ 返回按钮列表", callback_data=back_callback)],
    ])
    return InlineKeyboardMarkup(rows)


def layout_detail_keyboard(
    buttons,
    index: int,
    *,
    move_callback: Callable[[str], str],
    row_callback: Callable[[str], str],
    swap_callback: str,
    back_callback: str,
) -> InlineKeyboardMarkup:
    """Build common controls for one button's ordering and row placement."""
    can_move_previous = int(index) > 1
    can_move_next = isinstance(buttons, list) and int(index) < len(buttons)
    button = button_at(buttons, index) or {}
    begins_row = int(index) == 1 or button.get("new_row", True) is not False
    row_action = "same" if begins_row else "new"
    row_label = "⬅️ 与前一个同一行" if begins_row else "↩️ 从新一行开始"
    row_callback_data = row_callback(row_action) if int(index) > 1 else back_callback
    if int(index) == 1:
        row_label = "· 第一按钮固定为新行 ·"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⬅️ 前移" if can_move_previous else "· 已在最前 ·",
                callback_data=move_callback("previous") if can_move_previous else back_callback,
            ),
            InlineKeyboardButton(
                "后移 ➡️" if can_move_next else "· 已在最后 ·",
                callback_data=move_callback("next") if can_move_next else back_callback,
            ),
        ],
        [InlineKeyboardButton("🔀 与其他按钮交换", callback_data=swap_callback)],
        [InlineKeyboardButton(row_label, callback_data=row_callback_data)],
        [InlineKeyboardButton("⬅️ 返回排版", callback_data=back_callback)],
    ])


def layout_swap_keyboard(
    buttons,
    index: int,
    *,
    swap_callback: Callable[[int], str],
    back_callback: str,
) -> InlineKeyboardMarkup:
    """Build a compact selector for swapping the chosen button position."""
    options = []
    for other_index, button in enumerate(buttons if isinstance(buttons, list) else [], start=1):
        if other_index == int(index) or not isinstance(button, dict):
            continue
        options.append(InlineKeyboardButton(
            f"#{other_index} {str(button.get('text') or '未命名')[:18]}",
            callback_data=swap_callback(other_index),
        ))
    rows = [options[position:position + 2] for position in range(0, len(options), 2)]
    rows.append([InlineKeyboardButton("⬅️ 返回此按钮", callback_data=back_callback)])
    return InlineKeyboardMarkup(rows)


def parse_button_lines(raw_text: str, *, limit: int = MAX_INLINE_BUTTONS) -> List[dict]:
    """Parse bulk-editor lines into a shared button collection.

    Accepted formats are ``文字 | URL`` (new row) and
    ``文字 | URL | 同行`` (place beside the preceding button).
    """
    raw_buttons = []
    for raw_line in str(raw_text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split("|", 2)]
        if len(parts) not in {2, 3}:
            raise ValueError("每行应为：按钮文字 | https://链接 | 同行（“| 同行”可省略）")
        text, url = parts[:2]
        if not text or not url:
            raise ValueError("按钮文字和链接不能为空。")
        if not is_valid_url(url):
            raise ValueError("链接必须以 http://、https:// 或 tg:// 开头。")
        button = {"text": text[:64], "url": url}
        if len(parts) == 3:
            layout = parts[2].replace(" ", "").lower()
            if layout in {"同行", "同一行", "same", "same_row"}:
                button["new_row"] = False
            elif layout not in {"", "新行", "换行", "new", "new_row"}:
                raise ValueError("第三列仅支持“同行”或“新行”。")
        raw_buttons.append(button)
    if len(raw_buttons) > limit:
        raise ValueError(f"最多只能设置 {limit} 个按钮。")
    return normalize_buttons(raw_buttons, limit=limit)
