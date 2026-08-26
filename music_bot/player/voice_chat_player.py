from abc import ABC, abstractmethod


class VoiceChatPlayer(ABC):
    @abstractmethod
    async def join(self, chat_id: int):
        raise NotImplementedError

    @abstractmethod
    async def play(self, chat_id: int, source):
        raise NotImplementedError

    @abstractmethod
    async def pause(self, chat_id: int):
        raise NotImplementedError

    @abstractmethod
    async def resume(self, chat_id: int):
        raise NotImplementedError

    @abstractmethod
    async def stop(self, chat_id: int):
        raise NotImplementedError

    @abstractmethod
    async def leave(self, chat_id: int):
        raise NotImplementedError

    @abstractmethod
    def is_playing(self, chat_id: int) -> bool:
        raise NotImplementedError
