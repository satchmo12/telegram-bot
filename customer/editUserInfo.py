from io import BytesIO
import re

from telegram import (
    Update,
    InputStoryContentPhoto,
    InputStoryContentVideo,
    InputProfilePhoto,
    InputProfilePhotoStatic,
    
    # TimePeriod,
)

from telegram.ext import CommandHandler, ContextTypes, MessageHandler,filters

from command_router import register_command
from customer.customer_qa import get_business_connection_id
from tool.utils.update_helper import get_message


WAITING_AVATAR = "waiting_avatar"
WAITING_STORY = "waiting_story"


@register_command("发布状态", "修改头像")
async def handle_business_state(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    msg = get_message(update)

    if not msg or not msg.text:
        return False

    text = msg.text.strip()

    business_connection_id = get_business_connection_id( msg.chat.id)
    
    if not business_connection_id:
        await msg.reply_text("❌ 当前不是 Business 会话。")
        return True

    # ==========================
    # 修改头像
    if text.startswith("修改头像"):

        context.user_data["action"] = WAITING_AVATAR

        await msg.reply_text(
            "📷 请发送一张图片作为新的 Business 头像。"
        )

        return True


    # 发布状态
    if text.startswith("发布状态"):

        caption = text.replace("发布状态", "", 1).strip()

        context.user_data["action"] = WAITING_STORY
        context.user_data["caption"] = caption

        await msg.reply_text(
            "📷 请发送一张图片或一个视频。"
        )

        return True

    return False


@register_command("修改名称", "修改简介")
async def handle_business_profile(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    msg = get_message(update)

    if not msg or not msg.text:
        return False

    text = msg.text.strip()
    
    business_connection_id = get_business_connection_id( msg.chat.id)
    
    if not business_connection_id:
        await msg.reply_text("❌ 当前不是 Business 会话。")
        return True

    # ----------------------------
    # 修改名称
    # ----------------------------
    m = re.match(r"^修改名称\s+(.+)$", text)
    if m:
        new_name = m.group(1).strip()
       
        try:
            await context.bot.set_business_account_name(
                business_connection_id=business_connection_id,
                first_name=new_name,
            )

            await msg.reply_text(
                f"✅ 名称已修改为：{new_name}"
            )

        except Exception as e:
            await msg.reply_text(
                f"❌ 修改失败：{e}"
            )

        return True
    
    m = re.match(r"^修改简介\s+(.+)$", text)
    if m:
        new_bio = m.group(1).strip()

        try:
            await context.bot.set_business_account_bio(
                business_connection_id=business_connection_id,
                bio=new_bio,
            )

            await msg.reply_text(
                f"✅ 简介修改为：{new_bio}"
            )

        except Exception as e:
            await msg.reply_text(
                f"❌ 修改失败：{e}"
            )

        return True




    return False

async def handle_media(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    msg = update.effective_message

    action = context.user_data.get("action")

    if not action:
        return

    business_connection_id = get_business_connection_id(
        msg.chat.id
    )

    if not business_connection_id:
        await msg.reply_text(
            "❌ 当前不是 Business 会话"
        )
        context.user_data.clear()
        return


    try:

        # ==========================
        # 修改头像
        # ==========================
        if action == WAITING_AVATAR:

            if not msg.photo:
                await msg.reply_text(
                    "❌ 请发送一张图片"
                )
                return


            # 获取原文件
            tg_file = await context.bot.get_file(
                msg.photo[-1].file_id
            )


            # 下载到内存
            photo_file = BytesIO()

            await tg_file.download_to_memory(
                photo_file
            )

            photo_file.seek(0)

            # 文件名必须有
            photo_file.name = "avatar.jpg"


            await context.bot.set_business_account_profile_photo(
                business_connection_id=
                    business_connection_id,

                photo=InputProfilePhotoStatic(
                    photo=photo_file
                )
            )


            await msg.reply_text(
                "✅ Business头像修改成功"
            )


        # ==========================
        # 发布状态
        # ==========================
        elif action == WAITING_STORY:


            # 优先使用上传图片/视频自带的文字
            caption = msg.caption or context.user_data.get(
                "caption",
                ""
            )

            # 图片
            if msg.photo:

                tg_file = await context.bot.get_file(
                    msg.photo[-1].file_id
                )


                photo_file = BytesIO()

                await tg_file.download_to_memory(
                    photo_file
                )

                photo_file.seek(0)
                photo_file.name = "story.jpg"


                content = InputStoryContentPhoto(
                    photo=photo_file
                )


            # 视频
            elif msg.video:

                tg_file = await context.bot.get_file(
                    msg.video.file_id
                )


                video_file = BytesIO()

                await tg_file.download_to_memory(
                    video_file
                )

                video_file.seek(0)
                video_file.name = "story.mp4"


                content = InputStoryContentVideo(
                    video=video_file
                )


            else:

                await msg.reply_text(
                    "❌ 请发送图片或者视频"
                )
                return



            story = await context.bot.post_story(
                business_connection_id=
                    business_connection_id,

                content=content,

                active_period=86400,

                caption=caption or None,
            )


            await msg.reply_text(
                f"✅ 状态发布成功 ID:{story.id}"
            )


        context.user_data.clear()


    except Exception as e:

        context.user_data.clear()

        await msg.reply_text(
            f"❌ 操作失败：{e}"
        )

def register_customer_edit_handlers(app):

    app.add_handler(
        CommandHandler(
            "handle_business_profile",
            handle_business_profile
        )
    )
    
    app.add_handler( CommandHandler(
                "handle_business_state",
                handle_business_state
            )
        )
    app.add_handler(
    MessageHandler(
        filters.PHOTO | filters.VIDEO,
        handle_media,
    )
)
    
    
