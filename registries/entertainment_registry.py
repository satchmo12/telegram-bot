from media.beauty import get_beauty_handler
from game.answer_book import register_answer_book_handlers
from game.chengyu_game import register_chengyu_handlers
from game.five_game import register_five_handlers
from game.lottery_game import register_lottery_handlers
from game.qa_game import register_qa_handlers
from game.voice_reply import register_voice_handlers
from game_niuniu import register_niuniu_handlers
from feature_flags import is_feature_enabled


def register_entertainment_handlers(app):
    if not is_feature_enabled(app, "entertainment"):
        return

    register_chengyu_handlers(app)
    register_five_handlers(app)
    register_qa_handlers(app)
    register_lottery_handlers(app)
    register_voice_handlers(app)
    register_answer_book_handlers(app)
    get_beauty_handler(app)
    register_niuniu_handlers(app)
