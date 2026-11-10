"""Trusted protocol-account senders for automatic channel submissions.

A Telethon account must explicitly opt in through a forwarding rule before the
bot accepts its ``/protocol_submit`` command.  The forwarder refreshes this
small, per-bot registry from the currently active rules; it is not a general
allow-list for ordinary users.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional, Union

from utils import load_json, save_json

PROTOCOL_SUBMISSION_SENDERS_FILE = "data/protocol_submission_senders.json"
HISTORY_REQUESTS_FILE = "data/history_forward_requests.json"
HISTORY_REQUEST_AUTHORIZATIONS_FILE = "data/history_forward_authorizations.json"
HISTORY_REQUEST_AUTH_TTL_SECONDS = 60 * 60


def set_protocol_submission_senders(senders: Dict[Union[int, str], Dict[str, Any]]) -> None:
    """Replace the active protocol-account sender registry for this bot."""
    normalized: Dict[str, Dict[str, Any]] = {}
    for sender_id, details in (senders or {}).items():
        try:
            key = str(int(sender_id))
        except (TypeError, ValueError):
            continue
        if not isinstance(details, dict):
            details = {}
        sessions = details.get("sessions", [])
        if not isinstance(sessions, list):
            sessions = []
        normalized[key] = {
            "sessions": sorted({str(item).strip() for item in sessions if str(item).strip()}),
            "updated_at": int(time.time()),
        }
    save_json(PROTOCOL_SUBMISSION_SENDERS_FILE, {"senders": normalized})


def is_protocol_submission_sender(user_id: Optional[Union[int, str]]) -> bool:
    """Return whether *user_id* belongs to an active opted-in protocol account."""
    try:
        key = str(int(user_id))
    except (TypeError, ValueError):
        return False
    data = load_json(PROTOCOL_SUBMISSION_SENDERS_FILE)
    senders = data.get("senders", {}) if isinstance(data, dict) else {}
    return isinstance(senders, dict) and key in senders


def _load_history_authorizations() -> dict:
    data = load_json(HISTORY_REQUEST_AUTHORIZATIONS_FILE)
    return data if isinstance(data, dict) else {}


def _save_history_authorizations(data: dict) -> None:
    save_json(HISTORY_REQUEST_AUTHORIZATIONS_FILE, data)


def _authorization_record(value) -> dict:
    """Normalize older integer expiry values to the current record shape."""
    if isinstance(value, dict):
        sender_ids = value.get("sender_ids", [])
        if not isinstance(sender_ids, list):
            sender_ids = []
        return {
            "expires_at": value.get("expires_at", 0),
            "sender_ids": [str(item) for item in sender_ids if str(item).isdigit()],
        }
    return {"expires_at": value, "sender_ids": []}


def _prune_history_authorizations(data: dict, now: Optional[int] = None) -> bool:
    now = int(now if now is not None else time.time())
    changed = False
    for request_id, raw_record in list(data.items()):
        record = _authorization_record(raw_record)
        try:
            expired = int(record["expires_at"]) <= now
        except (TypeError, ValueError):
            expired = True
        if expired:
            data.pop(request_id, None)
            changed = True
        elif raw_record != record:
            data[request_id] = record
            changed = True
    return changed


def authorize_protocol_history_request(request_id: str) -> None:
    """Keep a just-started history task valid long enough for bot updates to drain."""
    request_id = str(request_id or "").strip()
    if not request_id:
        return
    data = _load_history_authorizations()
    _prune_history_authorizations(data)
    data[request_id] = {
        "expires_at": int(time.time()) + HISTORY_REQUEST_AUTH_TTL_SECONDS,
        "sender_ids": [],
    }
    _save_history_authorizations(data)


def bind_protocol_history_request_sender(request_id: str, sender_id: Union[int, str]) -> bool:
    """Bind the actual Telethon account ID after its history task starts."""
    request_id = str(request_id or "").strip()
    try:
        sender_key = str(int(sender_id))
    except (TypeError, ValueError):
        return False
    if not request_id:
        return False
    data = _load_history_authorizations()
    changed = _prune_history_authorizations(data)
    if request_id not in data:
        if changed:
            _save_history_authorizations(data)
        return False
    record = _authorization_record(data[request_id])
    if sender_key not in record["sender_ids"]:
        record["sender_ids"].append(sender_key)
        changed = True
    data[request_id] = record
    if changed:
        _save_history_authorizations(data)
    return True


def revoke_protocol_history_request(request_id: str) -> None:
    """Immediately invalidate queued Bot API messages from a stopped history task."""
    request_id = str(request_id or "").strip()
    if not request_id:
        return
    data = _load_history_authorizations()
    changed = _prune_history_authorizations(data)
    if request_id in data:
        data.pop(request_id, None)
        changed = True
    if changed:
        _save_history_authorizations(data)


def is_protocol_history_request_active(request_id: str) -> bool:
    """Return whether a history-transfer request is still allowed to submit."""
    request_id = str(request_id or "").strip()
    if not request_id:
        return False
    data = _load_history_authorizations()
    changed = _prune_history_authorizations(data)
    active = request_id in data
    if changed:
        _save_history_authorizations(data)
    return active


def is_protocol_history_request_sender(request_id: str, sender_id: Union[int, str]) -> bool:
    """Check that a command originates from the Telethon account for this task."""
    request_id = str(request_id or "").strip()
    try:
        sender_key = str(int(sender_id))
    except (TypeError, ValueError):
        return False
    data = _load_history_authorizations()
    changed = _prune_history_authorizations(data)
    record = _authorization_record(data.get(request_id)) if request_id in data else None
    if changed:
        _save_history_authorizations(data)
    return bool(record and sender_key in record["sender_ids"])
