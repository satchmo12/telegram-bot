import os
import random
from typing import Optional

import aiohttp


PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "").strip()
PEXELS_PER_PAGE = 15

KEYWORD_MAP = {
    "韩系": "korean girl",
    "日系": "japanese girl",
    "制服": "asian school uniform",
    "自拍": "asian selfie",
    "可爱": "cute asian girl",
    "运动": "asian fitness girl",
    "户外": "asian outdoor girl",
    "美女": "asian girl",
    "美腿": "beautiful legs",
    "丝袜": "stockings",
    "帅哥": "Chinese handsome man",
}


# def has_pexels_key() -> bool:
#     return bool(PEXELS_API_KEY)

PEXELS_API_KEY = (
    "kGwIqYrGUQVF4Y8n7hpjBQ6qYi951A27cAkjJsmYwc4fHQenTwInyhgM"  # 替换为你自己的 key
)

async def fetch_random_photo_url(query: str) -> Optional[str]:
    if not PEXELS_API_KEY:
        return None

    url = f"https://api.pexels.com/v1/search?query={query}&per_page={PEXELS_PER_PAGE}"
    headers = {"Authorization": PEXELS_API_KEY}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as response:
            if response.status != 200:
                return None
            data = await response.json()
            photos = data.get("photos", [])
            if not photos:
                return None
            photo = random.choice(photos)
            return photo.get("src", {}).get("large")
