"""جسر HTTP صغير يسمح لـ n8n بتمرير روابط الريلز إلى معالجات البوت.

البوت يبقى في وضع Polling؛ الجسر فقط يضخ Update إلى نفس قائمة انتظار PTB،
فلا يتغيّر سلوك التحميل/إزالة الاسم/الإرسال في المحادثة.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from aiohttp import web
from telegram import Chat, Message, Update, User

from . import config
from .logger import get_logger
from .utils import is_probably_url, platform_key

log = get_logger(__name__)


def _authorized(request: web.Request) -> bool:
    secret = config.N8N_BRIDGE_SECRET
    if not secret:
        return False
    supplied = request.headers.get("X-Bridge-Secret") or request.query.get("secret", "")
    return supplied == secret


async def _handle_reel(request: web.Request) -> web.Response:
    if not _authorized(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"ok": False, "error": "invalid json"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"ok": False, "error": "invalid payload"}, status=400)

    url = str(payload.get("url") or "").strip()
    if not url or not is_probably_url(url):
        return web.json_response({"ok": False, "error": "invalid url"}, status=400)

    key = platform_key(url)
    if config.ALLOWED_PLATFORMS and key not in config.ALLOWED_PLATFORMS:
        return web.json_response(
            {"ok": False, "error": f"platform not allowed: {key}"}, status=403
        )

    try:
        chat_id = int(payload.get("chat_id") or payload.get("chat"))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "invalid chat_id"}, status=400)
    try:
        user_id = int(payload.get("user_id") or chat_id)
    except (TypeError, ValueError):
        user_id = chat_id

    app = request.app["ptb"]
    last_update = int(app.bot_data.get("bridge_update_id", 0))
    update_id = max(int(time.time() * 1000), last_update + 1)
    app.bot_data["bridge_update_id"] = update_id
    message_id = int(app.bot_data.get("bridge_message_id", 900_000_000)) + 1
    app.bot_data["bridge_message_id"] = message_id

    user = User(
        id=user_id,
        is_bot=False,
        first_name=str(payload.get("first_name") or "n8n")[:64],
    )
    chat = Chat(id=chat_id, type="private")
    message = Message(
        message_id=message_id,
        date=datetime.now(timezone.utc),
        chat=chat,
        from_user=user,
        text=url,
    )
    await app.update_queue.put(Update(update_id=update_id, message=message))
    log.info("استقبل n8n رابط ريلز وأدخله في المعالجات: chat=%s url=%s", chat_id, url)
    return web.json_response({"ok": True, "queued": True, "platform": key})


async def start(app) -> web.AppRunner | None:
    """تشغيل الجسر إن وُجد سرّ مُضبط. يعيد runner لإيقافه عند الإغلاق."""
    if not config.N8N_BRIDGE_SECRET:
        log.info("جسر n8n معطّل (N8N_BRIDGE_SECRET غير مضبوط)")
        return None

    webapp = web.Application()
    webapp["ptb"] = app
    webapp.router.add_post(config.N8N_BRIDGE_PATH, _handle_reel)
    runner = web.AppRunner(webapp)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=config.N8N_BRIDGE_PORT)
    await site.start()
    log.info(
        "جسر n8n جاهز: POST http://0.0.0.0:%s%s",
        config.N8N_BRIDGE_PORT,
        config.N8N_BRIDGE_PATH,
    )
    return runner


async def stop(runner: web.AppRunner | None) -> None:
    if runner is not None:
        await runner.cleanup()
