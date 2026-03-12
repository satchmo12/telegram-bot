from channel.channel_force import register_handle_force_handlers
from channel.reply_to_channel import register_reply_to_channel_handlers
from channel.channel_config import register_channel_config_handlers
from game.checkin import register_checkin_handlers
from group.admin import register_admin_handlers
from group.group_care import register_group_care_handlers
from group.group_logger import register_group_logger_handlers
from group.group_media_tools import register_group_media_tools_handlers
from group.group_setting import register_group_setting_handlers
from group.grouplist import register_user_tracker_handlers
from group.invite_stats import register_invite_handlers
from group.save_photos import register_save_photos_handlers
from group.talk_stats import register_talk_handlers
from group.verify import register_verification_handlers
from menu import register_menu_handlers
from feature_flags import is_feature_enabled


def register_group_handlers(app):
    if not is_feature_enabled(app, "group"):
        return

    # 机器人在群内状态跟踪（被踢/退出/重新加入）
    register_group_logger_handlers(app)

    # 导航与群核心功能

    if is_feature_enabled(app, "group_setting"):
        register_group_setting_handlers(app)
    if is_feature_enabled(app, "admin"):
        register_admin_handlers(app)
    if is_feature_enabled(app, "invite_stats"):
        register_invite_handlers(app)
    if is_feature_enabled(app, "verification"):
        register_verification_handlers(app)
    if is_feature_enabled(app, "checkin"):
        register_checkin_handlers(app)

    # 群互动能力
    if is_feature_enabled(app, "group_care"):
        register_group_care_handlers(app)
    if is_feature_enabled(app, "group_media_tools"):
        register_group_media_tools_handlers(app)
    if is_feature_enabled(app, "save_photos"):
        register_save_photos_handlers(app)
    if is_feature_enabled(app, "talk_stats"):
        register_talk_handlers(app)
    register_reply_to_channel_handlers(app)
    register_channel_config_handlers(app)

    # 需要较后注册的群路由
    if is_feature_enabled(app, "user_tracker"):
        register_user_tracker_handlers(app)
    # register_handle_force_handlers(app)
    
    # 有吞噬会掉的方法  app.add_handler(CallbackQueryHandler(menu_button_handler))
    if is_feature_enabled(app, "menu"):
        register_menu_handlers(app)
