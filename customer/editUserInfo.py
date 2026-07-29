import re

from telegram import (
    Update,
    InputStoryContentPhoto,
    InputStoryContentVideo,
    InputProfilePhoto,
    InputProfilePhotoStatic,
    TimePeriod,
)

from telegram.ext import CommandHandler, ContextTypes

from command_router import register_command
from customer.customer_qa import get_business_connection_id
from tool.utils.update_helper import get_message


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
    # 发布状态
    # ==========================
    if text.startswith("发布状态"):

        reply = msg.reply_to_message

        if not reply:
            await msg.reply_text(
                "❌ 请回复一张图片或视频，然后发送：发布状态"
            )
            return True


        caption = text.replace(
            "发布状态",
            "",
            1
        ).strip()


        try:

            # 图片
            if reply.photo:

                file_id = reply.photo[-1].file_id

                content = InputStoryContentPhoto(
                    photo=file_id
                )


            # 视频
            elif reply.video:

                content = InputStoryContentVideo(
                    video=reply.video.file_id
                )


            else:
                await msg.reply_text(
                    "❌ 只支持图片或视频"
                )
                return True


            story = await context.bot.post_story(
                business_connection_id=
                    business_connection_id,

                content=content,

                active_period=
                    TimePeriod.DAY,

                caption=caption or None
            )


            await msg.reply_text(
                f"✅ 状态发布成功 ID:{story.id}"
            )


        except Exception as e:

            await msg.reply_text(
                f"❌ 发布失败：{e}"
            )


        return True



    # ==========================
    # 修改头像
    # ==========================
    if text.startswith("修改头像"):

        reply = msg.reply_to_message

        if not reply or not reply.photo:

            await msg.reply_text(
                "❌ 请回复一张图片，然后发送：修改头像"
            )
            return True


        try:

            photo = InputProfilePhotoStatic(
                photo=reply.photo[-1].file_id
            )


            await context.bot.set_business_account_profile_photo(
                business_connection_id=
                    business_connection_id,

                photo=photo
            )


            await msg.reply_text(
                "✅ Business头像修改成功"
            )


        except Exception as e:

            await msg.reply_text(
                f"❌ 修改头像失败：{e}"
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



def register_customer_edit_handlers(app):

    app.add_handler(
        CommandHandler(
            "handle_business_profile",
            handle_business_profile
        )
    )
    
    app.add_handler(
            CommandHandler(
                "handle_business_state",
                handle_business_state
            )
        )
    
    
