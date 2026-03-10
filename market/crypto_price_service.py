from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import aiohttp
from zoneinfo import ZoneInfo


ZH_COIN_ALIAS = {
    "比特币": "bitcoin",
    "以太坊": "ethereum",
    "狗狗币": "dogecoin",
    "币安币": "binancecoin",
    "瑞波": "ripple",
    "卡尔达诺": "cardano",
    "莱特币": "litecoin",
    "波场": "tron",
    "柴犬币": "shiba-inu",
    "比特现金": "bitcoin-cash",
    "大饼": "bitcoin",
    "以太": "ethereum",
}

COIN_ALIAS = {
    "btc": "bitcoin",
    "eth": "ethereum",
    "doge": "dogecoin",
    "bnb": "binancecoin",
    "ada": "cardano",
    "sol": "solana",
    "ltc": "litecoin",
}


@dataclass
class CoinQuote:
    coin_id: str
    usd: float
    change_24h: float
    updated_at: int


def resolve_coin_id(coin_code: str) -> str:
    key = (coin_code or "").strip().lower()
    return ZH_COIN_ALIAS.get(key) or COIN_ALIAS.get(key) or key


async def fetch_quote(coin_id: str) -> Optional[CoinQuote]:
    url = "https://api.coingecko.com/api/v3/simple/price"
    params = {
        "ids": coin_id.lower(),
        "vs_currencies": "usd",
        "include_24hr_change": "true",
        "include_last_updated_at": "true",
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                return None
            payload = await resp.json()

    data = payload.get(coin_id)
    if not isinstance(data, dict):
        return None

    return CoinQuote(
        coin_id=coin_id,
        usd=float(data.get("usd", 0) or 0),
        change_24h=float(data.get("usd_24h_change", 0) or 0),
        updated_at=int(data.get("last_updated_at", 0) or 0),
    )


def format_quote_text(title: str, quote: CoinQuote) -> str:
    time_str = datetime.fromtimestamp(
        quote.updated_at, tz=ZoneInfo("Asia/Dubai")
    ).strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"{title}\n"
        f"💰 美元: ${quote.usd:,.4f}\n"
        f"📊 24h 涨跌：{quote.change_24h:+.2f}%\n"
        f"🕒 更新时间（UTC4）：{time_str}"
    )
