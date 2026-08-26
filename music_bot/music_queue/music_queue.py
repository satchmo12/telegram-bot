from dataclasses import dataclass
from collections import deque


@dataclass
class QueueSong:
    song_id: int
    name: str
    artist: str
    requested_by: int
    provider_song_id: str


class MusicQueue:
    def __init__(self):
        self.items = deque()
        self.current = None

    def add(self, song):
        self.items.append(song)

    def next(self):
        self.current = self.items.popleft() if self.items else None
        return self.current

    def clear(self):
        self.items.clear()

    def size(self):
        return len(self.items)

    def list(self):
        return list(self.items)


class MusicQueueManager:
    def __init__(self):
        self.queues = {}

    def get(self, chat_id):
        if chat_id not in self.queues:
            self.queues[chat_id] = MusicQueue()
        return self.queues[chat_id]

    def remove(self, chat_id):
        self.queues.pop(chat_id, None)
