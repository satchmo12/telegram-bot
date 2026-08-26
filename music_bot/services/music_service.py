


from music_bot.music_queue.music_queue import MusicQueueManager, QueueSong
from music_bot.player.audio_source import AudioSource


class MusicService:
    def __init__(self, db, provider, player):
        self.db = db
        self.provider = provider
        self.player = player
        self.queue_manager = MusicQueueManager()

    async def search(self, keyword, limit=10):
        songs = await self.provider.search(keyword, limit)
        for song in songs:
            self.db.save_song(song)
        return songs

    async def get_song(self, provider_song_id):
        return await self.db.get_song(
            self.provider.NAME,
            provider_song_id,
        )

    async def add_to_queue(self, chat_id, provider_song_id, user_id):
        song = self.get_song(provider_song_id)
        if not song:
            return None

        queue = self.queue_manager.get(chat_id)
        queue.add(QueueSong(
            song_id=song["id"],
            name=song["name"],
            artist=song["artist"],
            requested_by=user_id,
            provider_song_id=provider_song_id,
        ))
        return song

    def get_queue(self, chat_id):
        return self.queue_manager.get(chat_id)

    async def play_next(self, chat_id):
        queue = self.get_queue(chat_id)
        next_song = queue.next()

        if not next_song:
            return None

        await self.player.join(chat_id)

        # 这里必须替换成你有权使用的音频来源。
        # 当前只是播放器骨架，不会抓取网易云受版权保护的完整音频。
        source = AudioSource(
            title=next_song.name,
            performer=next_song.artist,
        )

        await self.player.play(chat_id, source)
        return next_song

    async def skip(self, chat_id):
        await self.player.stop(chat_id)
        return await self.play_next(chat_id)

    async def pause(self, chat_id):
        await self.player.pause(chat_id)

    async def resume(self, chat_id):
        await self.player.resume(chat_id)

    async def clear_queue(self, chat_id):
        self.get_queue(chat_id).clear()

    async def leave(self, chat_id):
        await self.player.stop(chat_id)
        await self.player.leave(chat_id)
        self.queue_manager.remove(chat_id)
        
    async def download(self, song):
        """
        调用具体的音乐服务提供商(provider)去下载歌曲或获取音频文件/链接
        """
        if not hasattr(self.provider, 'download'):
            # 如果你的 provider 里不叫 download，可以根据实际情况修改
            # 例如：return await self.provider.get_audio_url(song["provider_song_id"])
            raise AttributeError(f"当前的 provider '{self.provider.NAME}' 没有实现 download 方法")
            
        return await self.provider.download(song["provider_song_id"])
