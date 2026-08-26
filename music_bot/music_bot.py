from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler

from music_bot.db.music_db import MusicDB
from music_bot.player.mock_player import MockVoiceChatPlayer
from music_bot.providers.netease import NetEaseProvider
from music_bot.handlers.music import create_music_handlers
from music_bot.services.music_service import MusicService

def register_music_bot_handlers(app):
    
    db = MusicDB("music_bot/data/music.db")
    provider = NetEaseProvider()
    player = MockVoiceChatPlayer()
    music_service = MusicService(db, provider, player)
    
    for handler in create_music_handlers(music_service):
        app.add_handler(handler)


