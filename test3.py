import requests
from deep_translator import GoogleTranslator

url = "https://translate.google.com/"

try:
    r = requests.get(url, timeout=15)
    print("Google网页状态码：", r.status_code)
except Exception as e:
    print("Google网页访问失败：", repr(e))

try:
    result = GoogleTranslator(
        source="auto",
        target="en"
    ).translate("你好")
    print("翻译成功：", result)
except Exception as e:
    print("翻译失败：", type(e).__name__, repr(e))