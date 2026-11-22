"""نقطة تجميع التطبيق: تهيئة التخزين، المهمة الدورية للحذف، ووضع التشغيل."""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from contextlib import suppress

from telegram import Update
from telegram.error import NetworkError, TelegramError, TimedOut
from telegram.ext import Application, ContextTypes

from . import bridge, config, handlers, media, meminfo, watermark
from .logger import get_logger, setup_logging
from .storage import Storage

log = get_logger(__name__)


async def _cleanup_loop(storage: Storage) -> None:
    """حذف الملفات التي تجاوزت مدة البقاء كل فترة قصيرة، مع نبضة حياة دورية."""
    while True:
        try:
            await asyncio.sleep(config.CLEANUP_INTERVAL_SECONDS)
            removed, freed = await storage.purge_expired()
            if removed:
                log.info(
                    "🗑️ حذف تلقائي: %s مهمة (%.1f MB) بعد انتهاء مدة البقاء",
                    removed,
                    freed / 1048576,
                )
            stats = await storage.stats()
            log.info(
                "💓 نبضة حياة: البوت يعمل — %s مهمة (نشطة: %s)، %.1f MB محفوظة",
                stats["jobs"],
                stats["active"],
                stats["bytes"] / 1048576,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("خطأ داخل مهمة الحذف الدورية")


async def _memory_monitor_loop() -> None:
    """سجلّ خفيف لاستهلاك الذاكرة أثناء المعالجة (لتشخيص OOM)."""
    last = -1.0
    while True:
        try:
            await asyncio.sleep(5)
            rss, total = await asyncio.to_thread(meminfo.memory_mb)
            if last < 0 or abs(total - last) >= 20 or total > 350:
                log.info(
                    "🧠 ذاكرة: العملية=%.0fMB، الحاوية=%.0fMB | %s",
                    rss,
                    total,
                    await asyncio.to_thread(meminfo.details),
                )
                last = total
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.debug("تعذرت قراءة الذاكرة")


async def _post_init(app: Application) -> None:
    config.ensure_directories()

    if not config.WEBHOOK_URL:
        # تفادي تعارض 409 إذا كان هناك Webhook قديم مسجّل من نشر سحابي
        try:
            await app.bot.delete_webhook(drop_pending_updates=False)
        except TelegramError as exc:
            log.debug("تعذر حذف Webhook قديم: %s", exc)

    storage = Storage()
    await storage.startup()

    app.bot_data["storage"] = storage
    app.bot_data["sem"] = asyncio.Semaphore(config.MAX_CONCURRENT_DOWNLOADS)
    app.bot_data["locks"] = {}
    app.bot_data["cooldowns"] = {}
    app.bot_data["cookies"] = config.resolve_cookies()
    app.bot_data["cleanup_task"] = asyncio.create_task(_cleanup_loop(storage))
    app.bot_data["memory_task"] = asyncio.create_task(_memory_monitor_loop())

    if config.WEBHOOK_URL:
        log.info("جسر n8n معطّل في وضع Webhook لتفادي تضارب المنفذ")
    else:
        app.bot_data["bridge_runner"] = await bridge.start(app)

    if app.bot_data["cookies"]:
        log.info("تم تحميل ملف الكوكيز: %s", app.bot_data["cookies"])
    else:
        log.info("لا يوجد ملف كوكيز (مطلوب لبعض محتوى إنستغرام الخاص)")

    if media.ffmpeg_available():
        log.info("ffmpeg متوفر: %s", media.FFMPEG)
    else:
        log.warning("ffmpeg غير متوفر — لن يعمل الضغط أو التقسيم أو الدمج")

    if config.REMOVE_USERNAME:
        log.info(
            "إزالة اسم المستخدم المطبوع: مفعّلة (حتى %s ثانية للفيديو، %s إطار فحص)",
            config.USERNAME_MAX_SECONDS,
            config.USERNAME_OCR_FRAMES,
        )
        # تحميل محرك OCR مسبقًا في الخلفية حتى لا يتأخر أول طلب
        asyncio.create_task(asyncio.to_thread(watermark._load_engine))
    else:
        log.info("إزالة اسم المستخدم المطبوع: معطّلة")

    me = None
    for attempt in range(3):
        try:
            me = await app.bot.get_me()
            break
        except TelegramError as exc:
            log.warning(
                "تعذر الاتصال بتيليجرام (محاولة %s/3): %s", attempt + 1, exc
            )
            await asyncio.sleep(2.0 * (attempt + 1))

    if me is not None:
        log.info("تم تشغيل البوت: @%s (id=%s)", me.username, me.id)
    else:
        log.warning("تعذر التحقق من هوية البوت مؤقتًا، سيستمر التشغيل والمحاولة تلقائيًا")

    try:
        await app.bot.set_my_commands(handlers.BOT_COMMANDS)
    except TelegramError as exc:
        log.warning("تعذر ضبط أوامر البوت: %s", exc)


async def _post_shutdown(app: Application) -> None:
    runner = app.bot_data.get("bridge_runner")
    if runner is not None:
        await bridge.stop(runner)
    for key in ("cleanup_task", "memory_task"):
        task = app.bot_data.get(key)
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    # انقطاع الشبكة مؤقت ويعالج نفسه تلقائيًا؛ لا نُغرِق السجل بآلاف الأسطر
    if isinstance(error, (NetworkError, TimedOut)):
        log.warning("انقطاع شبكة مؤقت أثناء التحديث: %s", error)
        return

    log.error("خطأ غير معالج أثناء تحديث", exc_info=error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ حدث خطأ غير متوقع. حاول مرة أخرى."
            )
        except TelegramError:
            pass


def build_application() -> Application:
    app = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .concurrent_updates(True)
        .read_timeout(60)
        .write_timeout(1800)
        .connect_timeout(30)
        .pool_timeout(60)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    handlers.register_handlers(app)
    app.add_error_handler(_on_error)
    return app


def _run(app: Application) -> None:
    if config.WEBHOOK_URL:
        path = "tg/" + hashlib.sha256(config.BOT_TOKEN.encode()).hexdigest()[:32]
        secret = hashlib.sha256(
            (config.BOT_TOKEN + ":webhook").encode()
        ).hexdigest()[:48]
        log.info("وضع Webhook: %s/%s (منفذ %s)", config.WEBHOOK_URL, path, config.PORT)
        app.run_webhook(
            listen="0.0.0.0",
            port=config.PORT,
            url_path=path,
            webhook_url=f"{config.WEBHOOK_URL}/{path}",
            secret_token=secret,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=False,
        )
    else:
        log.info("وضع Polling محلي (لا يوجد WEBHOOK_URL)")
        app.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=False,
        )


def main() -> None:
    setup_logging(config.LOG_LEVEL)
    log.info("🚀 بدء تشغيل البوت (PID=%s) — السجل: %s", os.getpid(), config.BASE_DIR / "logs" / "bot.log")
    config.validate()

    # إعادة المحاولة تلقائيًا عند انقطاع الشبكة أو أعطال تيليجرام المؤقتة
    while True:
        app = build_application()
        try:
            _run(app)
            return
        except KeyboardInterrupt:
            return
        except Exception as exc:  # noqa: BLE001
            log.error("توقف البوت (%s)، إعادة المحاولة بعد 5 ثوانٍ…", exc)
            time.sleep(5)


if __name__ == "__main__":
    main()
