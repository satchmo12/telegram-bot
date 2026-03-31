import logging
from typing import Callable, Optional

from telegram.error import InvalidToken

ACTIVE_APPS: dict[str, object] = {}
APP_FACTORY: Optional[Callable[[dict], object]] = None
POST_INIT_HOOK: Optional[Callable[[object], object]] = None


def configure_runtime_hooks(app_factory, post_init_hook) -> None:
    global APP_FACTORY, POST_INIT_HOOK
    APP_FACTORY = app_factory
    POST_INIT_HOOK = post_init_hook


def register_running_app(app) -> None:
    name = str(app.bot_data.get("name", "")).strip()
    if name:
        ACTIVE_APPS[name] = app


def unregister_running_app(name: str) -> None:
    ACTIVE_APPS.pop(str(name or "").strip(), None)


def get_running_app(name: str):
    return ACTIVE_APPS.get(str(name or "").strip())


def is_bot_running(name: str) -> bool:
    return get_running_app(name) is not None


def update_running_bot_features(name: str, features) -> bool:
    app = get_running_app(name)
    if not app:
        return False
    app.bot_data["enabled_features"] = set(features or [])
    return True


async def start_bot(config: dict) -> tuple[bool, str]:
    if not APP_FACTORY or not POST_INIT_HOOK:
        return False, "运行时管理器尚未初始化。"

    name = str(config.get("name", "")).strip()
    if not name:
        return False, "机器人名称不能为空。"
    if is_bot_running(name):
        return False, f"{name} 已在运行。"

    app = APP_FACTORY(config)
    try:
        await app.initialize()
        await app.bot.get_me()
        await POST_INIT_HOOK(app)
        await app.start()
        await app.updater.start_polling()
        register_running_app(app)
        return True, f"✅ 已启动机器人：{name}"
    except InvalidToken:
        try:
            await app.shutdown()
        except Exception:
            pass
        return False, f"❌ {name} 的 token 无效，启动失败。"
    except Exception as e:
        logging.exception("启动机器人失败 [%s]: %s", name, e)
        try:
            if app.updater and app.updater.running:
                await app.updater.stop()
            if app.running:
                await app.stop()
            await app.shutdown()
        except Exception:
            pass
        return False, f"❌ 启动失败：{e}"


async def stop_bot(name: str) -> tuple[bool, str]:
    app = get_running_app(name)
    if not app:
        return False, f"{name} 当前未运行。"

    try:
        if app.updater and app.updater.running:
            await app.updater.stop()
        if app.running:
            await app.stop()
        await app.shutdown()
        unregister_running_app(name)
        return True, f"🛑 已停止机器人：{name}"
    except Exception as e:
        logging.exception("停止机器人失败 [%s]: %s", name, e)
        return False, f"❌ 停止失败：{e}"
