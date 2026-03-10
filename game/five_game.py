import asyncio
from io import BytesIO
import sys
from PIL import Image, ImageDraw, ImageFont
from telegram import Update, InputFile
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import aiohttp

from command_router import register_command

BOARD_SIZE = 15
CELL_SIZE = 40
MARGIN = 20
IMG_SIZE = CELL_SIZE * (BOARD_SIZE - 1) + MARGIN * 2

games = {}  # chat_id -> 游戏数据

BOARD_SIZE = 15
CELL_SIZE = 40
MARGIN = 20
IMG_SIZE = CELL_SIZE * (BOARD_SIZE - 1) + MARGIN * 2

games = {}  # chat_id -> 游戏数据

# ---------- 工具函数 ----------


async def fetch_user_avatar(user_id, context):
    """异步获取用户头像并返回 PIL.Image"""
    photos = await context.bot.get_user_profile_photos(user_id)
    if photos.total_count == 0:
        return None
    file_id = photos.photos[0][0].file_id
    file = await context.bot.get_file(file_id)
    f = BytesIO()
    await file.download_to_memory(out=f)
    f.seek(0)
    return Image.open(f).convert("RGBA")


def draw_board_with_numbers(board, game, win_line=None):
    """画棋盘，黑白棋子，左侧和上方大号行列编号"""
    BOARD_SIZE = len(board)
    CELL_SIZE = 40
    MARGIN = CELL_SIZE // 2
    IMG_SIZE = CELL_SIZE * BOARD_SIZE
    offset = 50  # 左上留白
    font_size = 30

    # 创建画布
    img = Image.new("RGB", (IMG_SIZE + offset + 20, IMG_SIZE + offset + 40), "white")
    draw = ImageDraw.Draw(img)

    # ---------- 加载 TrueType 字体 ----------
    font_path = None
    if sys.platform.startswith("win"):
        font_path = r"C:\Windows\Fonts\arial.ttf"
    elif sys.platform.startswith("darwin"):
        font_path = "/System/Library/Fonts/Supplemental/Arial.ttf"
    else:  # Linux
        font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

    try:
        font = ImageFont.truetype(font_path, font_size)
    except:
        font = ImageFont.load_default()

    # ---------- 绘制行列编号 ----------
    for i in range(BOARD_SIZE):
        x = MARGIN + i * CELL_SIZE
        y = MARGIN + i * CELL_SIZE

        text = str(i)
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        draw.text(
            (x - text_width / 2 + offset / 2 + 2, offset - text_height - offset / 2),
            text,
            fill="black",
            font=font,
        )

        # 行号在左侧
        draw.text(
            (offset - text_width - 2, y - text_height / 2 + offset / 2),
            text,
            fill="black",
            font=font,
        )

    # ---------- 绘制网格 ----------
    for i in range(BOARD_SIZE):
        draw.line(
            (
                offset,
                offset + i * CELL_SIZE,
                offset + CELL_SIZE * (BOARD_SIZE - 1),
                offset + i * CELL_SIZE,
            ),
            fill="black",
            width=2,
        )
        draw.line(
            (
                offset + i * CELL_SIZE,
                offset,
                offset + i * CELL_SIZE,
                offset + CELL_SIZE * (BOARD_SIZE - 1),
            ),
            fill="black",
            width=2,
        )

    # ---------- 绘制棋子 ----------
    for i in range(BOARD_SIZE):
        for j in range(BOARD_SIZE):
            if board[i][j] == 0:
                continue
            cx = offset + j * CELL_SIZE
            cy = offset + i * CELL_SIZE
            r = CELL_SIZE // 2 - 2
            color = "black" if board[i][j] == 1 else "white"
            draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color, outline="black")

    # ---------- 胜利高亮线 ----------
    if win_line:
        coords = [(offset + y * CELL_SIZE, offset + x * CELL_SIZE) for x, y in win_line]
        draw.line(coords, fill="red", width=4)

    # ---------- 当前玩家提示 ----------
    text_y = offset + CELL_SIZE * BOARD_SIZE + 5
    if not game.get("over", False):
        current = game["current_player"]
        text = f"轮到: {'黑棋' if current == 1 else '白棋'}"
        draw.text((offset, text_y), text, fill="black", font=font)
    else:
        draw.text((offset, text_y), "游戏结束", fill="red", font=font)

    return img


def check_win_line(board, x, y):
    """检查胜利，返回胜利五子坐标"""
    player = board[x][y]

    def line(dx, dy):
        positions = [(x, y)]
        for d in [1, -1]:
            nx, ny = x + d * dx, y + d * dy
            while (
                0 <= nx < BOARD_SIZE
                and 0 <= ny < BOARD_SIZE
                and board[nx][ny] == player
            ):
                positions.append((nx, ny))
                nx += d * dx
                ny += d * dy
        if len(positions) >= 5:
            return positions
        return None

    directions = [(0, 1), (1, 0), (1, 1), (1, -1)]
    for dx, dy in directions:
        result = line(dx, dy)
        if result:
            return result
    return None


# ---------- Bot 命令 ----------
@register_command("五子棋")
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if chat_id in games:
        await update.message.reply_text(
            "游戏已存在，请另一名玩家 /join  或发送 加入五子棋 加入游戏"
        )
        return

    board = [[0] * BOARD_SIZE for _ in range(BOARD_SIZE)]
    avatar_img = await fetch_user_avatar(user_id, context)
    games[chat_id] = {
        "board": board,
        "player1": {"id": user_id, "avatar_img": avatar_img},
        "player2": None,
        "current_player": 1,
        "over": False,
    }
    await update.message.reply_text(
        "🎮 五子棋创建成功！黑棋先行，请另一名玩家 /join 或发送 加入五子棋 加入游戏"
    )


@register_command("加入五子棋")
async def join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if chat_id not in games:
        await update.message.reply_text("请先 /start 或 五子棋 创建游戏")
        return

    game = games[chat_id]
    if game["player2"]:
        await update.message.reply_text("游戏已满，两名玩家正在进行")
        return
    if user_id == game["player1"]["id"]:
        await update.message.reply_text("你已经是黑棋玩家")
        return

    avatar_img = await fetch_user_avatar(user_id, context)
    game["player2"] = {"id": user_id, "avatar_img": avatar_img}

    img = draw_board_with_numbers(game["board"], game)
    bio = BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    await update.message.reply_photo(
        photo=InputFile(bio), caption="白棋加入，游戏开始！"
    )


@register_command("下棋", "放", "0")
async def put(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if chat_id not in games:
        return
        await update.message.reply_text("请先 /start 或发送 加入五子棋 创建游戏")
        return

    game = games[chat_id]
    if not game["player2"]:
        await update.message.reply_text(
            "请等待另一名玩家 发生 /join 或 加入五子棋 加入"
        )
        return

    if len(context.args) != 2:
        await update.message.reply_text("格式错误: 下棋 x y")
        return

    try:
        x, y = map(int, context.args)
    except:
        await update.message.reply_text("坐标必须是整数")
        return

    if user_id != (
        game["player1"]["id"] if game["current_player"] == 1 else game["player2"]["id"]
    ):
        await update.message.reply_text("轮到另一名玩家")
        return

    if not (0 <= x < BOARD_SIZE and 0 <= y < BOARD_SIZE):
        await update.message.reply_text("坐标越界")
        return

    board = game["board"]
    if board[x][y] != 0:
        await update.message.reply_text("这里已经有棋子")
        return

    board[x][y] = game["current_player"]

    win_line = check_win_line(board, x, y)
    if win_line:
        game["over"] = True
        img = draw_board_with_numbers(board, game, win_line)
        bio = BytesIO()
        img.save(bio, format="PNG")
        bio.seek(0)
        await update.message.reply_photo(
            photo=InputFile(bio),
            caption=f"{'黑棋' if game['current_player']==1 else '白棋'}获胜！游戏结束",
        )
        del games[chat_id]
        return

    # 切换玩家
    game["current_player"] = 2 if game["current_player"] == 1 else 1
    img = draw_board_with_numbers(board, game)
    bio = BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    await update.message.reply_photo(photo=InputFile(bio))

# ---------- 结束游戏 ----------
@register_command("结束五子棋", "end_five")
async def end_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if chat_id not in games:
        await update.message.reply_text("当前没有进行中的游戏")
        return

    del games[chat_id]
    await update.message.reply_text("⚠️ 游戏已结束，棋盘已清理")

# ---------- 主程序 ----------
def register_five_handlers(app):
    app.add_handler(CommandHandler("start_five", start))
    app.add_handler(CommandHandler("join", join))
    app.add_handler(CommandHandler("put", put))
    app.add_handler(CommandHandler("end_five", end_game))  # 新增结束游戏
