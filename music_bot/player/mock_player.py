


from music_bot.player.voice_chat_player import VoiceChatPlayer


class MockVoiceChatPlayer(VoiceChatPlayer):
    """开发阶段播放器。真正语音播放时替换成 PyTgCalls 等实现。"""

    def __init__(self):
        self.playing = {}

    async def join(self, chat_id):
        print(f"[Player] join voice chat: {chat_id}")

    async def play(self, chat_id, source):
        self.playing[chat_id] = True
        print(
            f"[Player] PLAY: {source.title} - "
            f"{source.performer}"
        )

    async def pause(self, chat_id):
        print(f"[Player] PAUSE: {chat_id}")

    async def resume(self, chat_id):
        print(f"[Player] RESUME: {chat_id}")

    async def stop(self, chat_id):
        self.playing[chat_id] = False
        print(f"[Player] STOP: {chat_id}")

    async def leave(self, chat_id):
        self.playing.pop(chat_id, None)
        print(f"[Player] LEAVE: {chat_id}")

    def is_playing(self, chat_id):
        return self.playing.get(chat_id, False)
