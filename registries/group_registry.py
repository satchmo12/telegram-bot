from channel.channel_force import register_handle_force_handlers
from channel.publish_setting import register_publish_setting_handlers
from channel.reply_to_channel import register_reply_to_channel_handlers
from channel.channel_config import register_channel_config_handlers
from game.checkin import register_checkin_handlers
from group.admin import register_admin_handlers
from group.auto_scan import register_auto_scan_handlers
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
from channel.telethon_login import register_telethon_login_handlers


def register_group_handlers(app):
    # 群配置入口常驻注册，是否可用由运行时功能开关判断。
    # 这样在运行中开启 group 后，无需重启即可立即使用群配置。
    register_group_setting_handlers(app)

    # 成员状态更新用于维护 groups.json，不能依赖群功能开关；否则关闭后再
    # 被踢出群时，机器人不会收到处理该更新的 handler，面板会保留过期群组。
    register_group_logger_handlers(app)

    if not is_feature_enabled(app, "group"):
        return

    register_auto_scan_handlers(app)

    # 导航与群核心功能
    register_admin_handlers(app)
    register_invite_handlers(app)
    register_verification_handlers(app)
    register_checkin_handlers(app)

    # 群互动能力
    register_group_care_handlers(app)
    register_group_media_tools_handlers(app)
    register_save_photos_handlers(app)
    register_talk_handlers(app)
    register_reply_to_channel_handlers(app)
    if is_feature_enabled(app, "channel"):
        register_channel_config_handlers(app)
        register_telethon_login_handlers(app)
        
    
    register_publish_setting_handlers(app)
    # 需要较后注册的群路由
    register_user_tracker_handlers(app)
    register_handle_force_handlers(app)
    
    # 有吞噬会掉的方法  app.add_handler(CallbackQueryHandler(menu_button_handler))
    register_menu_handlers(app)
