from registries.game_modules import register_game_handlers
from chat.my_bot import register_my_bot_handlers
from slave.action_handler import register_action_handlers
from slave.guard_system import register_guard_handlers
from slave.kidnap import register_kinnap_handlers
from slave.work_game import register_work_handlers
from feature_flags import is_feature_enabled


def register_simulation_handlers(app):
    if not is_feature_enabled(app, "simulation"):
        return

    # 聊天学习与动作/工作系统
    if is_feature_enabled(app, "my_bot"):
        register_my_bot_handlers(app)
    if is_feature_enabled(app, "work"):
        register_work_handlers(app)
    if is_feature_enabled(app, "action"):
        register_action_handlers(app)

    # 大型玩法集合（农场/牧场/花园/背包/婚姻/宠物/奴隶）
    if is_feature_enabled(app, "game_hub"):
        register_game_handlers(app)

    # 扩展玩法
    if is_feature_enabled(app, "kidnap"):
        register_kinnap_handlers(app)
    if is_feature_enabled(app, "guard"):
        register_guard_handlers(app)
