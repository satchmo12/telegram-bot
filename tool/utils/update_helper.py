from telegram import Update


def get_message(update: Update):
    """
    兼容:
    - 普通消息
    - Business 客服消息
    - 编辑消息
    """
    return (
        update.message
        or getattr(update, "business_message", None)
        or update.edited_message
        or getattr(update, "edited_business_message", None)
    )


def get_message_text(update: Update):
    msg = get_message(update)

    if not msg:
        return None

    return msg.text