from PIL import Image
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes
from command_router import register_command
from utils import group_allowed

from PIL import Image
from telegram import Update
from telegram.ext import ContextTypes
from typing import Optional
from command_router import register_command
from utils import group_allowed
import os

def render_dress(
    base_path: str,
    hair_path: Optional[str] = None,
    top_path: Optional[str] = None,
    bottom_path: Optional[str] = None,
    output_path: str = "/tmp/dress.png"
):
    """合成角色装扮"""
    base = Image.open(base_path).convert("RGBA")

    for layer in [hair_path, top_path, bottom_path]:
        if layer and os.path.exists(layer):
            img = Image.open(layer).convert("RGBA")
            base.alpha_composite(img)

    base.save(output_path)
    return output_path


@group_allowed
@register_command("我的形象")
async def show_dress(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """演示角色装扮"""
    chat_id = str(update.effective_chat.id)
    user_id = str(update.effective_user.id)

    # Demo：写死装扮素材（后面可以替换成数据库读取）
    base = "assets/dress/base/girl_base_1.jpg"
    hair = "assets/dress/hair/hair_twintail.png"
    top = "assets/dress/top/top_pajamas.png"
    bottom = "assets/dress/bottom/bottom_skirt.png"

    out = f"/tmp/dress_{chat_id}_{user_id}.png"

    # 渲染角色
    render_dress(base, hair, top, bottom, out)

    # 发送给用户
    if os.path.exists(out):
        with open(out, "rb") as f:
            await update.message.reply_photo(photo=f, caption="👗 这是你当前的形象")
    else:
        await update.message.reply_text("❌ 渲染失败，请检查素材路径")
    
def register_dress_handlers(app):
    app.add_handler(CommandHandler("show_dress", show_dress))