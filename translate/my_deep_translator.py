import re
from deep_translator import GoogleTranslator
import string

from command_router import register_command

LANG_MAP = {
    # 英文
    "en": ("英文", "en"),
    "英文": ("英文", "en"),
    "english": ("英文", "en"),

    # 中文
    "zh": ("中文", "zh-CN"),
    "中文": ("中文", "zh-CN"),
    "zh-cn": ("中文", "zh-CN"),
    "chinese": ("中文", "zh-CN"),

    # 日文
    "ja": ("日文", "ja"),
    "jp": ("日文", "ja"),
    "日文": ("日文", "ja"),
    "japanese": ("日文", "ja"),

    # 韩文
    "ko": ("韩文", "ko"),
    "kr": ("韩文", "ko"),
    "韩文": ("韩文", "ko"),
    "korean": ("韩文", "ko"),

    # 法文
    "fr": ("法文", "fr"),
    "法文": ("法文", "fr"),
    "french": ("法文", "fr"),
}

async def to_english(text: str) -> str:
    return GoogleTranslator(source="auto", target="en").translate(text)


# async def translate_text(text: str, target: str = 'en') -> str:
#     return GoogleTranslator(source='auto', target=target).translate(text)


async def translate_text(text: str, target: str = "zh") -> str:
    return GoogleTranslator(source="auto", target=target).translate(text)


# def is_pure_ascii(text: str) -> bool:
#     return all(ord(c) < 128 for c in text)


def is_pure_ascii(text: str) -> bool:
    """
    判断是否为“可视为英文的文本”
    - 允许英文、数字、空格、标点
    - 允许常见英文弯引号 / 破折号
    - 只要包含中文就返回 False
    """
    # 只要包含中文，直接判 False
    if re.search(r"[\u4e00-\u9fff]", text):
        return False

    # 剩下的都认为是“英文可翻译文本”
    return True


def is_pure_number_or_punctuation(text: str) -> bool:
    return all(c.isdigit() or c in string.punctuation or c.isspace() for c in text)


async def auto_translate(text: str) -> str:
    try:
        # 如果是纯数字、标点或空格，不翻译
        if is_pure_number_or_punctuation(text):
            return

        if is_pure_ascii(text):
            return GoogleTranslator(source="auto", target="zh-CN").translate(text)
        else:
            return
            # return GoogleTranslator(source='auto', target='en').translate(text)
    except Exception as e:
        return f"❌ 翻译失败：{e}"


async def translate(text: str, target: str) -> str:
    return GoogleTranslator(source="auto", target=target).translate(text)


@register_command("翻译")
async def reply_auto_translate(update, context):
    msg = update.message

    if not msg.reply_to_message:
        return

    origin_text = msg.reply_to_message.text
    if not origin_text:
        return

    parts = msg.text.strip().split(maxsplit=1)

    lang_key = "en"
    if len(parts) == 2:
        lang_key = parts[1].lower()

    if lang_key not in LANG_MAP:
        await msg.reply_text("❓不支持的语言代码")
        return

    lang_name, target_code = LANG_MAP[lang_key]

    try:
        result = await translate(origin_text, target_code)
        await msg.reply_text(f"🌐 {lang_name}：\n{result}")
    except Exception as e:
        await msg.reply_text(f"❌ 翻译失败：{e}")
