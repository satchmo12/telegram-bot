# -*- coding: utf-8 -*-
"""Resolve a channel post's linked discussion message through Telethon."""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from typing import Optional, Tuple

from telegram.ext import ContextTypes

from utils import SHARED_SESSION_NAME, get_session_path


def _api_credentials() -> Tuple[Optional[int], Optional[str]]:
    raw_id = os.getenv("TELETHON_API_ID") or os.getenv("API_ID")
    api_hash = os.getenv("TELETHON_API_HASH") or os.getenv("API_HASH")
    try:
        api_id = int(raw_id) if raw_id else None
    except (TypeError, ValueError):
        api_id = None
    return api_id, str(api_hash).strip() if api_hash else None


async def resolve_discussion_message(
    context: ContextTypes.DEFAULT_TYPE, channel_id: int, message_id: int
) -> Optional[Tuple[int, int]]:
    """Return ``(discussion_chat_id, discussion_message_id)`` when available.

    Bot API updates from linked discussion groups may be absent due to privacy or
    other handlers. Telethon's GetDiscussionMessage request does not depend on
    receiving that update.
    """
    api_id, api_hash = _api_credentials()
    if not api_id or not api_hash:
        print("[讨论映射] 未配置 API_ID/API_HASH")
        return None

    try:
        from telethon import TelegramClient, utils as telethon_utils
        from telethon.tl.functions.messages import GetDiscussionMessageRequest
    except Exception as exc:
        print("[讨论映射] Telethon 不可用:", exc)
        return None

    # The main protocol account is already in use by the running application.
    # Opening the same SQLite session directly causes ``database is locked``.
    # Use a temporary read-copy of the authorized session for this one lookup.
    source_base = get_session_path(context, SHARED_SESSION_NAME)
    source_session = f"{source_base}.session"
    if not os.path.exists(source_session):
        print(f"[讨论映射] 协议号 session 不存在: {source_session}")
        return None

    temp_dir = tempfile.mkdtemp(prefix="tg_discussion_map_")
    temp_session = os.path.join(temp_dir, "main.session")
    client = None
    try:
        shutil.copy2(source_session, temp_session)
        client = TelegramClient(temp_session, api_id, api_hash)
        await client.connect()
        if not await client.is_user_authorized():
            print("[讨论映射] sessions/main 未登录")
            return None
        peer = await client.get_input_entity(int(channel_id))
        # Query immediately, then retry while Telegram creates the discussion copy.
        for delay in (0, 1, 2):
            if delay:
                await asyncio.sleep(delay)
            try:
                result = await client(
                    GetDiscussionMessageRequest(peer=peer, msg_id=int(message_id))
                )
            except Exception as exc:
                print(f"[讨论映射] 查询失败 channel={channel_id} message={message_id}: {exc}")
                continue
            for item in getattr(result, "messages", []) or []:
                try:
                    peer_id = int(telethon_utils.get_peer_id(item.peer_id))
                except Exception:
                    continue
                if peer_id != int(channel_id) and getattr(item, "id", None):
                    return peer_id, int(item.id)
        print(f"[讨论映射] 未找到讨论消息 channel={channel_id} message={message_id}")
        return None
    except Exception as exc:
        print(f"[讨论映射] 初始化失败 channel={channel_id} message={message_id}: {exc}")
        return None
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass
        shutil.rmtree(temp_dir, ignore_errors=True)
