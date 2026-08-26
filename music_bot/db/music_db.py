import sqlite3
import time
from pathlib import Path


class MusicDB:
    def __init__(self, db_path="data/music.db"):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.init_tables()

    def init_tables(self):
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS songs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL,
            provider_song_id TEXT NOT NULL,
            name TEXT NOT NULL,
            artist TEXT,
            album TEXT,
            cover_url TEXT,
            duration INTEGER DEFAULT 0,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            UNIQUE(provider, provider_song_id)
        );

        CREATE TABLE IF NOT EXISTS telegram_audio_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            song_id INTEGER NOT NULL UNIQUE,
            telegram_file_id TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            FOREIGN KEY(song_id) REFERENCES songs(id) ON DELETE CASCADE
        );
        """)
        self.conn.commit()

    def save_song(self, song):
        now = int(time.time())
        self.conn.execute("""
            INSERT INTO songs (
                provider, provider_song_id, name, artist, album,
                cover_url, duration, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, provider_song_id) DO UPDATE SET
                name=excluded.name,
                artist=excluded.artist,
                album=excluded.album,
                cover_url=excluded.cover_url,
                duration=excluded.duration,
                updated_at=excluded.updated_at
        """, (
            song["provider"],
            song["provider_song_id"],
            song["name"],
            song.get("artist", ""),
            song.get("album", ""),
            song.get("cover_url"),
            song.get("duration", 0),
            now,
            now,
        ))
        self.conn.commit()

    async def get_song(self, provider, provider_song_id):
        row = self.conn.execute("""
            SELECT * FROM songs
            WHERE provider=? AND provider_song_id=?
        """, (provider, provider_song_id)).fetchone()
        return  dict(row) if row else None

    def save_telegram_file_id(self, song_id, file_id):
        now = int(time.time())
        self.conn.execute("""
            INSERT INTO telegram_audio_cache
                (song_id, telegram_file_id, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(song_id) DO UPDATE SET
                telegram_file_id=excluded.telegram_file_id,
                updated_at=excluded.updated_at
        """, (song_id, file_id, now, now))
        self.conn.commit()

    def get_telegram_file_id(self, song_id):
        row = self.conn.execute("""
            SELECT telegram_file_id
            FROM telegram_audio_cache
            WHERE song_id=?
        """, (song_id,)).fetchone()
        return row["telegram_file_id"] if row else None
