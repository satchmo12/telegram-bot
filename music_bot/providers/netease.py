
import asyncio
from io import BytesIO

import aiohttp


class NetEaseProvider:
    NAME = "netease"

    SEARCH_URL = "https://music.163.com/api/search/get"
    PLAYER_URL = "https://music.163.com/api/song/enhance/player/url"

    MAX_AUDIO_SIZE = 50 * 1024 * 1024

    @staticmethod
    def _headers():
        return {
            "Referer": "https://music.163.com/",
            "User-Agent": "Mozilla/5.0",
        }

    # =========================
    # 检查歌曲是否有播放地址
    # =========================
    async def check_available(self, song_id):
        timeout = aiohttp.ClientTimeout(total=10)

        async with aiohttp.ClientSession(
            headers=self._headers(),
            timeout=timeout,
        ) as session:

            async with session.get(
                self.PLAYER_URL,
                params={
                    "ids": f"[{song_id}]",
                    "br": 128000,
                },
            ) as response:

                response.raise_for_status()

                data = await response.json(
                    content_type=None
                )

        tracks = data.get("data") or []

        if not tracks:
            print(
                f"[NetEase] ❌ 没有 track: "
                f"song_id={song_id}"
            )
            return False

        track = tracks[0]

        audio_url = track.get("url")
        # 不判断 code，只判断有没有真实播放地址
        return bool(audio_url)

    # =========================
    # 搜索歌曲
    # =========================
    async def search(self, keyword, limit=10):

        # 多搜索一些
        # 例如需要 10 首，就先搜索 20 首
        search_limit = max(limit * 2, 20)

        params = {
            "s": keyword,
            "type": 1,
            "limit": search_limit,
        }

        async with aiohttp.ClientSession(
            headers=self._headers()
        ) as session:

            async with session.get(
                self.SEARCH_URL,
                params=params,
                timeout=10,
            ) as response:

                response.raise_for_status()

                data = await response.json(
                    content_type=None
                )

        songs = (
            data.get("result", {})
            .get("songs", [])
        )

        result = []

        for song in songs:

            artists = ", ".join(
                a.get("name", "")
                for a in song.get("artists", [])
            )

            album = song.get("album") or {}

            result.append({
                "provider": self.NAME,
                "provider_song_id": str(
                    song["id"]
                ),
                "name": song.get(
                    "name", ""
                ),
                "artist": artists,
                "album": album.get(
                    "name", ""
                ),
                "cover_url": album.get(
                    "picUrl"
                ),
                "duration": song.get(
                    "duration", 0
                ) or 0,
            })

        # =========================
        # 并发检查播放地址
        # =========================

        async def check_song(song):

            try:

                available = await self.check_available(
                    song["provider_song_id"]
                )

                if available:
                    return song

            except Exception as e:

                print(
                    f"[NetEase] check failed: "
                    f"song_id="
                    f"{song['provider_song_id']}, "
                    f"error={e}"
                )

            return None

        checked = await asyncio.gather(
            *(check_song(song) for song in result)
        )

        # =========================
        # 过滤不可播放歌曲
        # =========================

        available_songs = [
            song
            for song in checked
            if song is not None
        ]

        print(
            f"[NetEase] 搜索完成: "
            f"keyword={keyword}, "
            f"原始={len(result)}, "
            f"可下载={len(available_songs)}"
        )

        # 最多返回 limit 首
        return available_songs[:limit]

    # =========================
    # 下载歌曲
    # =========================
    async def download(self, song_id):

        timeout = aiohttp.ClientTimeout(
            total=90
        )

        async with aiohttp.ClientSession(
            headers=self._headers(),
            timeout=timeout,
        ) as session:

            # =========================
            # 1. 获取播放地址
            # =========================

            async with session.get(
                self.PLAYER_URL,
                params={
                    "ids": f"[{song_id}]",
                    "br": 128000,
                },
            ) as response:

                response.raise_for_status()

                data = await response.json(
                    content_type=None
                )

            tracks = data.get("data") or []

            audio_url = (
                tracks[0].get("url")
                if tracks
                else None
            )


            if not audio_url:

                print(
                    f"[NetEase] ❌ "
                    f"没有获取到播放地址: "
                    f"{song_id}"
                )

                return None

            # =========================
            # 2. 下载音频
            # =========================

            async with session.get(
                audio_url,
                allow_redirects=True,
            ) as response:

                response.raise_for_status()

                content_type = (
                    response.headers
                    .get("Content-Type", "")
                    .lower()
                )

                content_length = (
                    response.content_length
                )

                # 防止返回 HTML 错误页面
                if "text/html" in content_type:

                    print(
                        "[NetEase] ❌ "
                        "返回的是 HTML，不是音频"
                    )

                    return None

                # Content-Length 超过限制
                if (
                    content_length is not None
                    and content_length
                    > self.MAX_AUDIO_SIZE
                ):

                    print(
                        f"[NetEase] ❌ "
                        f"音频超过大小限制: "
                        f"{content_length}"
                    )

                    return None

                audio = BytesIO()

                # =========================
                # 流式下载
                # =========================

                async for chunk in response.content.iter_chunked(
                    64 * 1024
                ):

                    audio.write(chunk)

                    # 下载过程中检查大小
                    if (
                        audio.tell()
                        > self.MAX_AUDIO_SIZE
                    ):

                        print(
                            "[NetEase] ❌ "
                            "下载过程中超过大小限制"
                        )

                        audio.close()

                        return None

        # =========================
        # 检查是否为空文件
        # =========================

        if not audio.tell():

            print(
                f"[NetEase] ❌ "
                f"下载结果为空: {song_id}"
            )

            audio.close()

            return None

        # =========================
        # 设置文件指针
        # =========================

        audio.seek(0)

        audio.name = f"{song_id}.mp3"

        print(
            f"[NetEase] ✅ 下载成功: "
            f"{song_id}, "
            f"size={audio.getbuffer().nbytes}"
        )

        return audio
