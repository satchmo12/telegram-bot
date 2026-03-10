from chat.ai_chat import register_ai_chat_handlers
from media.beauty import get_beauty_handler
from game.answer_book import register_answer_book_handlers
from game.chengyu_game import register_chengyu_handlers
from game.dice_game import register_dice_handlers
from game.dress_game import register_dress_handlers
from game.five_game import register_five_handlers
from game.lottery_game import register_lottery_handlers
from game.qa_game import register_qa_handlers
from game.ssc import get_ssc_handler
from game.truth_game import register_truth_handlers
from game.voice_reply import register_voice_handlers
from feature_flags import is_feature_enabled


def register_entertainment_handlers(app):
    if not is_feature_enabled(app, "entertainment"):
        return

    if is_feature_enabled(app, "dress"):
        register_dress_handlers(app)
    if is_feature_enabled(app, "chengyu"):
        register_chengyu_handlers(app)
    if is_feature_enabled(app, "five"):
        register_five_handlers(app)
    if is_feature_enabled(app, "qa"):
        register_qa_handlers(app)
    if is_feature_enabled(app, "truth"):
        register_truth_handlers(app)
    if is_feature_enabled(app, "dice"):
        register_dice_handlers(app)
    if is_feature_enabled(app, "lottery_game"):
        register_lottery_handlers(app)
    if is_feature_enabled(app, "voice_reply"):
        register_voice_handlers(app)
    if is_feature_enabled(app, "answer_book"):
        register_answer_book_handlers(app)

    if is_feature_enabled(app, "ai_chat"):
        register_ai_chat_handlers(app)
    if is_feature_enabled(app, "beauty"):
        get_beauty_handler(app)
    if is_feature_enabled(app, "ssc"):
        get_ssc_handler(app)
