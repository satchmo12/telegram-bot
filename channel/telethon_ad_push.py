"""Scheduled, selected-group ad delivery through a Telethon protocol account."""
import os
import tempfile
import time
import re
import random
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationHandlerStop, CallbackQueryHandler, ContextTypes, MessageHandler, filters

from forward.message_forward import build_message_payload
from utils import bot_now, get_bot_path, load_json, save_json

PREFIX = "tad"
FILE_NAME = "data/telethon_ad_push.json"
STAGE_KEY = "telethon_ad_push_stage"
MAX_ADS = 20


def _path(context): return get_bot_path(context, FILE_NAME)
def _load(context):
    data = load_json(_path(context))
    data = data if isinstance(data, dict) else {}
    data.setdefault("sessions", {})
    return data

def _save(context, data): save_json(_path(context), data)
def _session_cfg(data, session): return data.setdefault("sessions", {}).setdefault(session, {"ads": [], "next_id": 1})
def _find(cfg, ad_id): return next((a for a in cfg.get("ads", []) if isinstance(a, dict) and int(a.get("id", 0)) == int(ad_id)), None)

def _new(ad_id):
    return {"id": ad_id, "name": "", "enabled": False, "mode": "interval", "interval_min": 120, "times": "", "payload": None, "random_enabled": False, "random_payloads": [], "targets": []}

def _parse_times(value: str) -> list[str]:
    slots=[]
    for item in str(value or "").replace("，", ",").split(","):
        item=item.strip()
        if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", item) and item not in slots:
            slots.append(item)
    return slots[:24]

async def _allowed(context, user, session):
    from channel.telethon_login import _can_access_session, _require_active_subscription
    return bool(
        user
        and _require_active_subscription(context, user)
        and _can_access_session(context, user, session)
    )

async def _groups(context, uid, session):
    from channel.telethon_login import _get_or_fetch_account_groups
    groups, _ = await _get_or_fetch_account_groups(context, uid, session)
    return groups or []

def _list_text(session, cfg):
    ads = cfg.get("ads", [])
    lines = ["📢 协议号广告推送", f"协议号：{session}", "", "广告只会发送到下方选择的指定群组。"]
    if not ads: lines.append("\n暂无广告。")
    for ad in ads:
        lines.append(f"#{ad['id']} {ad.get('name') or '未命名'} · {'✅' if ad.get('enabled') else '🚫'} · {len(ad.get('targets', []))} 个群")
    return "\n".join(lines)

def _list_keyboard(session, cfg):
    rows = [[InlineKeyboardButton(f"⚙️ #{a['id']} {str(a.get('name') or '未命名')[:28]}", callback_data=f"{PREFIX}:open:{session}|{a['id']}")] for a in cfg.get("ads", [])]
    if len(cfg.get("ads", [])) < MAX_ADS: rows.append([InlineKeyboardButton("➕ 新增广告", callback_data=f"{PREFIX}:add:{session}")])
    rows.append([InlineKeyboardButton("⬅️ 返回协议号", callback_data=f"tlogin:menu:{session}")])
    return InlineKeyboardMarkup(rows)

def _detail_text(session, ad):
    random_count = len([item for item in ad.get('random_payloads', []) if isinstance(item, dict)])
    return "\n".join(["📢 协议号广告", f"协议号：{session}", f"名称：{ad.get('name') or '未命名'}", f"状态：{'✅ 开启' if ad.get('enabled') else '🚫 关闭'}", f"推送方式：{'定时' if ad.get('mode') == 'fixed' else '间隔'}", f"间隔：每 {ad.get('interval_min', 120)} 分钟", f"定时：{ad.get('times') or '未设置'}", f"指定内容：{'已设置' if isinstance(ad.get('payload'), dict) else '未设置'}", f"随机广告：{'✅ 开启' if ad.get('random_enabled') else '🚫 关闭'}（{random_count} 条文案）", f"目标群组：{len(ad.get('targets', []))} 个"])

def _detail_keyboard(session, ad):
    aid=ad['id']
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{'✅' if ad.get('enabled') else '🚫'} 启用广告", callback_data=f"{PREFIX}:toggle:{session}|{aid}")],
        [InlineKeyboardButton("🏷 名称", callback_data=f"{PREFIX}:field:{session}|{aid}|name"), InlineKeyboardButton("📎 指定广告内容", callback_data=f"{PREFIX}:field:{session}|{aid}|payload")],
        [InlineKeyboardButton(f"{'✅' if ad.get('random_enabled') else '🚫'} 随机广告", callback_data=f"{PREFIX}:random_toggle:{session}|{aid}"), InlineKeyboardButton("🎲 随机文案管理", callback_data=f"{PREFIX}:random_menu:{session}|{aid}")],
        [
            InlineKeyboardButton(f"{'✅' if ad.get('mode', 'interval') == 'interval' else '⬜'} 间隔", callback_data=f"{PREFIX}:mode_interval:{session}|{aid}"),
            InlineKeyboardButton(f"{'✅' if ad.get('mode') == 'fixed' else '⬜'} 定时", callback_data=f"{PREFIX}:mode_fixed:{session}|{aid}"),
        ],
        [InlineKeyboardButton("⏱ 推送间隔", callback_data=f"{PREFIX}:field:{session}|{aid}|interval"), InlineKeyboardButton("🕒 定时时间", callback_data=f"{PREFIX}:field:{session}|{aid}|times")],
        [InlineKeyboardButton("👥 选择发送群组", callback_data=f"{PREFIX}:targets:{session}|{aid}")],
        [InlineKeyboardButton("🚀 立即发送", callback_data=f"{PREFIX}:send:{session}|{aid}"), InlineKeyboardButton("🗑 删除", callback_data=f"{PREFIX}:delete:{session}|{aid}")],
        [InlineKeyboardButton("⬅️ 返回广告列表", callback_data=f"{PREFIX}:list:{session}")],
    ])

async def _send(context, session, ad):
    from channel.telethon_login import _get_api_creds
    from utils import get_session_path
    api_id, api_hash = _get_api_creds()
    if not api_id or not api_hash: raise RuntimeError("未配置 API_ID/API_HASH。")
    payload, targets = ad.get("payload"), ad.get("targets", [])
    random_payloads = [item for item in ad.get("random_payloads", []) if isinstance(item, dict)]
    if bool(ad.get("random_enabled")):
        if not random_payloads:
            raise RuntimeError("随机广告已开启，但尚未添加随机文案。")
        payload = random.choice(random_payloads)
    if not isinstance(payload, dict): raise RuntimeError("请先设置指定广告内容。")
    if not targets: raise RuntimeError("请至少选择一个发送群组。")
    from telethon import TelegramClient
    # The protocol forwarder may already keep this session open. Reuse it to
    # avoid a second SQLite connection locking the session and silently making
    # the immediate-send action fail.
    from channel.telethon_forwarder import get_active_session_client
    bot_name = str(context.application.bot_data.get("name", "") or "")
    client = get_active_session_client(bot_name, session)
    owns_client = client is None
    if owns_client:
        client = TelegramClient(get_session_path(context, session), api_id, api_hash)
    temp = None; sent=failed=0; errors=[]
    try:
        if owns_client:
            await client.connect()
        if not await client.is_user_authorized(): raise RuntimeError("协议号未登录。")
        kind=str(payload.get('type') or 'text'); text=str(payload.get('text') or payload.get('caption') or '')
        if kind != 'text':
            file = await context.bot.get_file(payload.get('file_id'))
            # Telegram Bot API downloads do not retain a useful local suffix by
            # default. Telethon then treats the temporary .bin as a document,
            # which is why forwarded photos showed a download button. Preserve
            # a presentation-compatible extension for photos/videos instead.
            suffix = {
                'photo': '.jpg',
                'video': '.mp4',
                'animation': '.gif',
                'audio': '.mp3',
                'voice': '.ogg',
            }.get(kind, '.bin')
            temp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix).name
            await file.download_to_drive(temp)
        for group_id in targets:
            try:
                # Resolve the marked dialog ID through this account's entity
                # cache first. This is more reliable than passing a raw -100…
                # value, especially for basic groups and recently joined groups.
                target = await client.get_input_entity(int(group_id))
                if kind == 'text':
                    await client.send_message(target, text)
                elif kind == 'photo':
                    await client.send_file(target, temp, caption=text, force_document=False)
                elif kind == 'video':
                    await client.send_file(
                        target, temp, caption=text, force_document=False,
                        supports_streaming=True,
                    )
                elif kind == 'animation':
                    await client.send_file(target, temp, caption=text, force_document=False)
                else:
                    # Files, voice messages and documents deliberately remain
                    # downloadable documents instead of pretending to be media.
                    await client.send_file(target, temp, caption=text, force_document=True)
                sent += 1
            except Exception as exc:
                failed += 1; errors.append(f"{group_id}: {exc}"); print(f"协议号广告发送失败 session={session} group={group_id}: {exc}")
    finally:
        if owns_client:
            try:
                await client.disconnect()
            except Exception:
                pass
        if temp:
            try: os.remove(temp)
            except OSError: pass
    return sent, failed, "；".join(errors[:2])

async def open_protocol_ads(query, context, session):
    if not await _allowed(context, query.from_user, session): return await query.answer("无权管理该协议号。", show_alert=True)
    cfg=_session_cfg(_load(context), session); await query.answer()
    return await query.edit_message_text(_list_text(session,cfg), reply_markup=_list_keyboard(session,cfg))

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    if not q or not q.data: return
    parts=q.data.split(':',2); action=parts[1] if len(parts)>1 else ''; raw=parts[2] if len(parts)>2 else ''
    values=raw.split('|'); session=values[0] if values else ''
    if not session or not await _allowed(context,q.from_user,session): return await q.answer("无权管理该协议号。",show_alert=True)
    data=_load(context); cfg=_session_cfg(data,session)
    if action=='list': await q.answer(); return await q.edit_message_text(_list_text(session,cfg),reply_markup=_list_keyboard(session,cfg))
    if action=='add':
        aid=int(cfg.get('next_id',1)); cfg.setdefault('ads',[]).append(_new(aid)); cfg['next_id']=aid+1; _save(context,data); ad=_find(cfg,aid); await q.answer('已新增广告'); return await q.edit_message_text(_detail_text(session,ad),reply_markup=_detail_keyboard(session,ad))
    try: ad=_find(cfg,int(values[1]))
    except Exception: ad=None
    if not ad: return await q.answer('广告不存在。',show_alert=True)
    if action=='open': await q.answer(); return await q.edit_message_text(_detail_text(session,ad),reply_markup=_detail_keyboard(session,ad))
    if action=='toggle': ad['enabled']=not ad.get('enabled',False); _save(context,data); await q.answer('已更新'); return await q.edit_message_text(_detail_text(session,ad),reply_markup=_detail_keyboard(session,ad))
    if action=='random_toggle':
        ad['random_enabled']=not bool(ad.get('random_enabled', False)); _save(context,data)
        await q.answer('已开启随机广告' if ad['random_enabled'] else '已关闭随机广告')
        return await q.edit_message_text(_detail_text(session,ad),reply_markup=_detail_keyboard(session,ad))
    if action=='random_menu':
        payloads=[item for item in ad.get('random_payloads',[]) if isinstance(item,dict)]
        rows=[[InlineKeyboardButton(f"🗑 删除随机文案 #{index}",callback_data=f"{PREFIX}:random_delete:{session}|{ad['id']}|{index}")] for index,_item in enumerate(payloads, start=1)]
        if len(payloads)<20: rows.append([InlineKeyboardButton('➕ 添加随机文案',callback_data=f"{PREFIX}:random_add:{session}|{ad['id']}")])
        rows.append([InlineKeyboardButton('⬅️ 返回广告',callback_data=f"{PREFIX}:open:{session}|{ad['id']}")])
        await q.answer()
        return await q.edit_message_text(f"🎲 随机文案管理\n当前 {len(payloads)} / 20 条。开启随机广告后，每次推送会从这些文案中随机选择一条。",reply_markup=InlineKeyboardMarkup(rows))
    if action=='random_add':
        context.user_data[STAGE_KEY]={'session':session,'ad_id':ad['id'],'field':'random_payload'}
        await q.answer()
        return await q.edit_message_text('请发送一条随机广告文案（支持文字、图片、视频、文件等）。',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⬅️ 返回随机文案',callback_data=f"{PREFIX}:random_menu:{session}|{ad['id']}")]]))
    if action=='random_delete':
        index=int(values[2])-1; payloads=ad.get('random_payloads',[])
        if isinstance(payloads,list) and 0 <= index < len(payloads): payloads.pop(index); _save(context,data)
        await q.answer('已删除')
        # Re-open through the same compact menu renderer.
        payloads=[item for item in ad.get('random_payloads',[]) if isinstance(item,dict)]
        rows=[[InlineKeyboardButton(f"🗑 删除随机文案 #{n}",callback_data=f"{PREFIX}:random_delete:{session}|{ad['id']}|{n}")] for n,_item in enumerate(payloads, start=1)]
        if len(payloads)<20: rows.append([InlineKeyboardButton('➕ 添加随机文案',callback_data=f"{PREFIX}:random_add:{session}|{ad['id']}")])
        rows.append([InlineKeyboardButton('⬅️ 返回广告',callback_data=f"{PREFIX}:open:{session}|{ad['id']}")])
        return await q.edit_message_text(f"🎲 随机文案管理\n当前 {len(payloads)} / 20 条。",reply_markup=InlineKeyboardMarkup(rows))
    if action in {'mode_interval', 'mode_fixed'}:
        ad['mode'] = 'fixed' if action.endswith('fixed') else 'interval'
        _save(context,data); await q.answer('已切换推送方式')
        return await q.edit_message_text(_detail_text(session,ad),reply_markup=_detail_keyboard(session,ad))
    if action=='delete': cfg['ads']=[x for x in cfg['ads'] if x.get('id')!=ad['id']]; _save(context,data); await q.answer('已删除'); return await q.edit_message_text(_list_text(session,cfg),reply_markup=_list_keyboard(session,cfg))
    if action=='send':
        # A manual send counts as the latest interval delivery. Without this,
        # the scheduler sees a brand-new ad (last=0) and immediately sends a
        # second copy on its next one-minute tick.
        interval_key = f"{session}:{ad.get('id')}"
        last_sent = context.application.bot_data.setdefault('telethon_ad_last', {})
        previous_last = last_sent.get(interval_key)
        is_interval = str(ad.get('mode') or 'interval') != 'fixed'
        if is_interval:
            last_sent[interval_key] = int(time.time())
        try:
            sent,failed,errors=await _send(context,session,ad)
            if is_interval and not sent:
                # Do not hide a failed manual delivery behind a full interval.
                if previous_last is None:
                    last_sent.pop(interval_key, None)
                else:
                    last_sent[interval_key] = previous_last
            message=f'已发送 {sent} 个群，失败 {failed} 个。'
            if errors: message += f'\n失败原因：{errors[:160]}'
            return await q.answer(message,show_alert=True)
        except Exception as exc:
            if is_interval:
                if previous_last is None:
                    last_sent.pop(interval_key, None)
                else:
                    last_sent[interval_key] = previous_last
            return await q.answer(f'发送失败：{exc}',show_alert=True)
    if action=='targets':
        groups=await _groups(context,str(q.from_user.id),session); selected={int(x) for x in ad.get('targets',[])}; rows=[]
        for g in groups[:80]:
            gid=int(g['id']); rows.append([InlineKeyboardButton(f"{'✅' if gid in selected else '⬜'} {str(g.get('title') or gid)[:40]}",callback_data=f"{PREFIX}:target:{session}|{ad['id']}|{gid}")])
        rows.append([InlineKeyboardButton('⬅️ 返回广告',callback_data=f"{PREFIX}:open:{session}|{ad['id']}")]); await q.answer(); return await q.edit_message_text('👥 点击群组选择/取消广告发送目标：',reply_markup=InlineKeyboardMarkup(rows))
    if action=='target':
        gid=int(values[2]); targets=[int(x) for x in ad.get('targets',[])]; ad['targets']=([x for x in targets if x!=gid] if gid in targets else targets+[gid]); _save(context,data)
        groups=await _groups(context,str(q.from_user.id),session); selected={int(x) for x in ad.get('targets',[])}; rows=[]
        for g in groups[:80]:
            current_id=int(g['id']); rows.append([InlineKeyboardButton(f"{'✅' if current_id in selected else '⬜'} {str(g.get('title') or current_id)[:40]}",callback_data=f"{PREFIX}:target:{session}|{ad['id']}|{current_id}")])
        rows.append([InlineKeyboardButton('⬅️ 返回广告',callback_data=f"{PREFIX}:open:{session}|{ad['id']}")])
        await q.answer('已更新')
        return await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(rows))
    if action=='field':
        field=values[2]; context.user_data[STAGE_KEY]={'session':session,'ad_id':ad['id'],'field':field}; prompts={'name':'请输入广告名称。','payload':'请发送广告内容（支持文字、图片、视频、文件等）。','interval':'请输入广告推送间隔（分钟，5-1440）。','times':'请输入定时时间，例如：09:00,12:30,21:00'}; await q.answer(); return await q.edit_message_text(prompts.get(field,'请输入内容。'),reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('⬅️ 返回广告',callback_data=f"{PREFIX}:open:{session}|{ad['id']}")]]))

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state=context.user_data.get(STAGE_KEY)
    if not isinstance(state,dict) or not update.message or not update.effective_user: return
    session=state.get('session','')
    if not await _allowed(context,update.effective_user,session):
        context.user_data.pop(STAGE_KEY,None)
        raise ApplicationHandlerStop
    data=_load(context); cfg=_session_cfg(data,session); ad=_find(cfg,state.get('ad_id'))
    if not ad:
        context.user_data.pop(STAGE_KEY,None)
        raise ApplicationHandlerStop
    field=state.get('field'); text=(update.message.text or '').strip()
    try:
        if field=='name':
            if not text: raise ValueError('名称不能为空。')
            ad['name']=text[:64]
        elif field=='interval':
            if not text.isdigit() or not 5<=int(text)<=1440: raise ValueError('请输入 5-1440 的分钟数。')
            ad['interval_min']=int(text); ad['mode']='interval'
        elif field=='times':
            slots=_parse_times(text)
            if not slots: raise ValueError('时间格式示例：09:00,12:30,21:00')
            ad['times']=','.join(slots); ad['mode']='fixed'
        elif field=='payload': ad['payload']=build_message_payload(update.message)
        elif field=='random_payload':
            payloads=ad.setdefault('random_payloads', [])
            if len(payloads) >= 20: raise ValueError('随机文案最多 20 条。')
            payloads.append(build_message_payload(update.message))
        else: raise ValueError('设置步骤无效。')
    except Exception as exc:
        await update.message.reply_text(f'❗ {exc}')
        raise ApplicationHandlerStop
    _save(context,data); context.user_data.pop(STAGE_KEY,None)
    await update.message.reply_text(_detail_text(session,ad),reply_markup=_detail_keyboard(session,ad))
    raise ApplicationHandlerStop

async def tick(context: ContextTypes.DEFAULT_TYPE):
    data=_load(context); now=int(time.time()); current_hm=bot_now(context).strftime("%H:%M")
    runtime=context.application.bot_data
    last=runtime.setdefault('telethon_ad_last',{})
    slots_seen=runtime.setdefault('telethon_ad_slots',{})
    for session,cfg in data.get('sessions',{}).items():
        for ad in cfg.get('ads',[]) if isinstance(cfg,dict) else []:
            if not ad.get('enabled') or not isinstance(ad.get('payload'),dict) or not ad.get('targets'): continue
            key=f'{session}:{ad.get("id")}'; mode=str(ad.get('mode') or 'interval')
            if mode == 'fixed':
                if current_hm not in _parse_times(ad.get('times', '')) or slots_seen.get(key) == current_hm: continue
                try:
                    sent, failed, _errors = await _send(context,session,ad)
                    if sent: slots_seen[key]=current_hm
                except Exception as exc: print(f'协议号广告定时发送失败 session={session}: {exc}')
                continue
            interval=max(5,min(1440,int(ad.get('interval_min',120) or 120)))
            if now-last.get(key,0)<interval*60: continue
            try:
                sent, failed, _errors = await _send(context,session,ad)
                if sent: last[key]=now
            except Exception as exc: print(f'协议号广告定时发送失败 session={session}: {exc}')

def register_protocol_ad_handlers(app):
    app.add_handler(CallbackQueryHandler(callback,pattern=rf'^{PREFIX}:'))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (~filters.COMMAND),text_handler),group=-2)
    app.job_queue.run_repeating(tick,interval=60,first=60,name='telethon_ad_push_tick')
