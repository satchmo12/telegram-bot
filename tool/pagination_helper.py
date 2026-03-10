import re
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes


class Paginator:
    def __init__(self, items, page_size=20):
        self.items = list(items)
        self.page_size = page_size
        self.total_pages = (len(self.items) + page_size - 1) // page_size or 1

    def get_page(self, page):
        if page < 1 or page > self.total_pages:
            return []
        start = (page - 1) * self.page_size
        end = start + self.page_size
        return self.items[start:end]

    def build_keyboard(self, prefix, page):
        keyboard = []
        buttons = []
        if page > 1:
            buttons.append(
                InlineKeyboardButton("⬅️ 上一页", callback_data=f"{prefix}_{page-1}")
            )
        if page < self.total_pages:
            buttons.append(
                InlineKeyboardButton("➡️ 下一页", callback_data=f"{prefix}_{page+1}")
            )
        if buttons:
            keyboard.append(buttons)
        return InlineKeyboardMarkup(keyboard) if keyboard else None


async def send_paginated_list(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    items,
    page: int = 1,
    prefix: str = "page",
    title="列表",
):
    paginator = Paginator(items)
    page = max(1, min(page, paginator.total_pages))  # 限定有效页数
    page_items = paginator.get_page(page)

    text_lines = [f"📖 {title}（第 {page}/{paginator.total_pages} 页）:"]
    text_lines.extend(page_items)  # 不再添加额外序号

    markup = paginator.build_keyboard(prefix, page)

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.edit_text(
            "\n".join(text_lines), reply_markup=markup, parse_mode="HTML"
        )
    else:
        await update.message.reply_text(
            "\n".join(text_lines), reply_markup=markup, parse_mode="HTML"
        )


async def generic_pagination_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    prefix: str,
    get_items_func,
    format_item_func,
    title: str,
):
    """
    通用回调处理分页请求

    :param update: Telegram Update 对象
    :param context: ContextTypes.DEFAULT_TYPE
    :param prefix: callback_data 前缀，用于匹配回调数据
    :param get_items_func: 无参数函数，返回当前分页的全部条目列表
    :param format_item_func: 格式化单条条目 (i, item) -> str
    :param title: 列表标题
    """
    query = update.callback_query
    await query.answer()

    pattern = rf"^{re.escape(prefix)}_(\d+)$"
    match = re.match(pattern, query.data)
    if not match:
        return

    page = int(match.group(1))
    items = get_items_func()
    await send_paginated_list(
        update, context, items, page, prefix, format_item_func, title
    )
