"""معالجات أوامر ورسائل تيليجرام."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path

from telegram import (
    BotCommand,
    ReplyParameters,
    Update,
)
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import config, downloader, media, sender, watermark
from .logger import get_logger
from .storage import Job, Storage
from .utils import (
    clean_error,
    extract_urls,
    human_duration,
    human_size,
    is_probably_url,
    platform_key,
    platform_label,
)

log = get_logger(__name__)

START_TEXT = (
    "👋 أهلاً بك في بوت تحميل الفيديوهات والستوريات!\n\n"
    "أرسل رابطًا وسأحمّله وأرسله لك بأفضل جودة متاحة، "
    "وبدون علامة مائية قدر الإمكان.\n\n"
    "✅ المنصات المفعّلة حاليًا (للسرعة): تيك توك • إنستغرام (منشورات/ريلز/ستوريات) • فيسبوك.\n\n"
    f"🗑️ الملفات تُحفظ في السحابة لمدة {config.RETENTION_HOURS:g} ساعات ثم تُحذف تلقائيًا.\n"
    f"📦 الحد الأقصى للإرسال المباشر {config.MAX_UPLOAD_MB} ميجابايت، "
    "وما زاد عن ذلك يُضغط أو يُقسَّم تلقائيًا.\n\n"
    "🔗 أرسل الرابط الآن!"
)

HELP_TEXT = (
    "📖 طريقة الاستخدام\n\n"
    "1) انسخ رابط الفيديو أو الستوري من التطبيق.\n"
    "2) الصقه هنا وأرسله.\n"
    "3) انتظر قليلًا حتى يكتمل التحميل والإرسال.\n\n"
    "🎯 المنصات المفعّلة حاليًا:\n"
    "• https://www.tiktok.com/@user/video/123\n"
    "• https://www.instagram.com/reel/ABC/\n"
    "• https://www.instagram.com/stories/username/123/\n"
    "• https://www.facebook.com/watch/?v=123\n\n"
    "🔒 المحتوى الخاص (ستوريات إنستغرام، حسابات خاصة) يحتاج ملف كوكيز "
    "لحسابك، ويُضاف في إعدادات البوت عبر COOKIES_FILE أو COOKIES_B64.\n\n"
    "🧼 إزالة اسم المستخدم: يفحص البوت كامل الإطار ويحاول إزالة اسم صاحب المحتوى "
    "المطبوع تلقائيًا. إذا كان التشويه محتملًا سيسألك عن نص بديل يضعه في نفس المكان.\n"
    "🛠️ لإزالة اسم لم يتعرّف عليه تلقائيًا: أرسل /fix ثم أرسل النص كما يظهر بالضبط.\n\n"
    "🗑️ الحذف التلقائي: كل ملف يبقى في السحابة 3 ساعات فقط بعد التحميل.\n\n"
    "⚙️ أوامر: /start /help /id /status /fix"
)


def _is_admin(user_id: int | None) -> bool:
    return bool(user_id) and user_id in config.ADMIN_IDS


def _reply_params(update: Update) -> ReplyParameters | None:
    message = update.effective_message
    if message is None:
        return None
    return ReplyParameters(message_id=message.message_id, allow_sending_without_reply=True)


async def _safe_reply(update: Update, text: str) -> None:
    message = update.effective_message
    if message is None:
        return
    try:
        await message.reply_text(text, reply_parameters=_reply_params(update))
    except TelegramError as exc:
        log.warning("تعذر إرسال رد: %s", exc)


# ---------------------------------------------------------------------------
# حالة انتظار نص بديل / بحث يدوي عن الاسم
# ---------------------------------------------------------------------------
@dataclass
class PendingEdit:
    user_id: int
    chat_id: int
    job_id: str
    video: Path
    width: int
    height: int
    boxes: list[tuple[int, int, int, int]] = field(default_factory=list)
    variants: set[str] = field(default_factory=set)
    mode: str = "replace"  # replace | find
    title: str = ""
    risk_reason: str = ""
    texts: list[str] = field(default_factory=list)
    info: media.MediaInfo | None = None
    prompt_message_id: int = 0
    created_at: float = field(default_factory=time.time)


def _pending_map(context: ContextTypes.DEFAULT_TYPE) -> dict[int, PendingEdit]:
    return context.application.bot_data.setdefault("pending", {})


def _looks_like_replacement(text: str) -> bool:
    """تجنّب تفسير كلمات عابرة («شكرًا»، «تمام»…) كنص بديل."""
    value = (text or "").strip()
    if not value or len(value) > 80:
        return False
    if not any(ch.isalnum() for ch in value):
        return False
    if value.lower() in {
        "شكرا",
        "شكراً",
        "مشكور",
        "تمام",
        "اوك",
        "أوك",
        "ok",
        "okay",
        "thanks",
        "thank",
        "thx",
        "حلو",
        "رائع",
        "لا",
        "نعم",
        "ايوه",
        "أيوه",
    }:
        return False
    return True


def _allowed_platforms_text() -> str:
    labels = {
        "tiktok": "تيك توك",
        "instagram": "إنستغرام",
        "facebook": "فيسبوك",
        "youtube": "يوتيوب",
        "twitter": "إكس",
        "snapchat": "سناب شات",
        "reddit": "ريديت",
        "pinterest": "بينترست",
        "threads": "ثريدز",
        "vimeo": "فيميو",
    }
    names = [labels.get(key, key) for key in config.ALLOWED_PLATFORMS]
    return " • ".join(sorted(names)) if names else "كل المنصات"


def _pending_prompt(pending: PendingEdit) -> str:
    reason = pending.risk_reason or "الخلفية خلف النص معقّدة"
    return (
        "⚠️ وجدت اسم المستخدم المطبوع داخل الفيديو، لكن إزالته قد تُشوّه الصورة "
        f"({reason}).\n\n"
        "أرسل الآن أحد الخيارات:\n"
        "• النص البديل الذي تريده وسأضعه مكانه بنفس الحجم واللون وأقرب خط متاح — مثال: @myaccount\n"
        "• «إزالة» لأزيل الاسم فقط دون بديل\n"
        "• «تخطي» لإبقاء الفيديو كما هو"
    )


# ---------------------------------------------------------------------------
# الأوامر
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _safe_reply(update, START_TEXT)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _safe_reply(update, HELP_TEXT)


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None:
        return
    await _safe_reply(
        update,
        f"🆔 معرّفك: {user.id}\n💬 معرّف المحادثة: {chat.id}",
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    storage: Storage = context.application.bot_data["storage"]
    stats = await storage.stats()

    if not _is_admin(update.effective_user.id if update.effective_user else None):
        await _safe_reply(
            update,
            "ℹ️ حالة الخدمة: تعمل ✅\n"
            f"🗑️ مدة بقاء الملفات: {stats['retention_hours']:g} ساعات\n"
            f"📦 الحد الأقصى للملف: {config.MAX_UPLOAD_MB} ميجابايت",
        )
        return

    oldest = stats["oldest"]
    oldest_text = (
        human_duration(time.time() - oldest) + " مضت" if oldest else "لا يوجد"
    )
    text = (
        "📊 إحصائيات التخزين السحابي\n\n"
        f"• عدد المهام: {stats['jobs']} (نشطة: {stats['active']})\n"
        f"• الحجم الكلي: {human_size(stats['bytes'])}\n"
        f"• مدة البقاء: {stats['retention_hours']:g} ساعات\n"
        f"• أقدم ملف: {oldest_text}\n"
        f"• المسار: {stats['root']}"
    )
    await _safe_reply(update, text)


async def cmd_clean(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_admin(update.effective_user.id if update.effective_user else None):
        await _safe_reply(update, "⛔ هذا الأمر للمشرف فقط.")
        return

    storage: Storage = context.application.bot_data["storage"]
    args = [str(a).lower() for a in (context.args or [])]

    if args and args[0] in {"all", "الكل"}:
        removed, freed = await storage.purge_all()
        await _safe_reply(
            update, f"🧹 تم حذف كل الملفات: {removed} مهمة ({human_size(freed)})."
        )
        return

    removed, freed = await storage.purge_expired()
    await _safe_reply(
        update,
        f"🧹 تم حذف الملفات المنتهية: {removed} مهمة ({human_size(freed)}).",
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_status(update, context)


async def cmd_cookies(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    path = context.application.bot_data.get("cookies")
    if path and Path(path).is_file():
        size = Path(path).stat().st_size
        text = f"🍪 ملف الكوكيز محمّل حاليًا ({size} بايت)."
    else:
        text = "🍪 لا يوجد ملف كوكيز محمّل حاليًا."

    if _is_admin(user_id):
        text += (
            "\n\nللتحديث: أرسل ملف cookies.txt بصيغة Netscape كمستند، "
            "وسأستخدمه تلقائيًا في الطلبات القادمة."
        )
    else:
        text += "\nمحتوى إنستغرام الخاص والستوريات يحتاج كوكيز يضيفها المشرف."
    await _safe_reply(update, text)


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يستقبل ملف الكوكيز من المشرف ويحدّثه مباشرة."""
    message = update.effective_message
    user = update.effective_user
    if message is None or message.document is None or user is None:
        return
    if not _is_admin(user.id):
        return

    name = (message.document.file_name or "").lower()
    if not name.endswith(".txt"):
        await _safe_reply(update, "📄 أرسل ملف الكوكيز بصيغة .txt")
        return
    if (message.document.file_size or 0) > 2 * 1024 * 1024:
        await _safe_reply(update, "❌ حجم ملف الكوكيز كبير جدًا (الحد 2 ميجابايت).")
        return

    file = await message.document.get_file()
    config.STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = config.COOKIES_PATH.with_suffix(".tmp")
    try:
        await file.download_to_drive(custom_path=str(tmp))
        data = tmp.read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        log.warning("تعذر حفظ الكوكيز: %s", exc)
        await _safe_reply(update, "❌ تعذر حفظ الملف. حاول مرة أخرى.")
        return

    if "Netscape" not in data and "\t" not in data:
        tmp.unlink(missing_ok=True)
        await _safe_reply(
            update,
            "❌ الملف لا يبدو ملف كوكيز Netscape صالحًا.\n"
            "صدّر الكوكيز من المتصفح بصيغة cookies.txt.",
        )
        return

    tmp.replace(config.COOKIES_PATH)
    context.application.bot_data["cookies"] = config.COOKIES_PATH
    await _safe_reply(
        update, "✅ تم تحديث ملف الكوكيز بنجاح، وسيُستخدم في الطلبات القادمة."
    )


# ---------------------------------------------------------------------------
# معالجة الروابط
# ---------------------------------------------------------------------------
def friendly_error(raw: str) -> str:
    text = clean_error(raw)
    low = text.lower()

    if any(
        key in low
        for key in (
            "login",
            "log in",
            "cookies",
            "private",
            "requested content is not available",
            "rate-limit",
            "authentication",
            "empty media response",
        )
    ):
        return (
            "🔒 هذا المحتوى خاص أو يتطلب تسجيل الدخول.\n"
            "الحل: أضف ملف cookies.txt لحسابك في إعدادات البوت "
            "(COOKIES_FILE أو COOKIES_B64) ثم أعد المحاولة."
        )
    if "unsupported url" in low:
        return "🚫 هذا الرابط غير مدعوم حاليًا. جرّب رابطًا آخر."
    if "video unavailable" in low or "has been removed" in low or "not available" in low:
        return "🚫 المحتوى غير متاح أو تم حذفه."
    if "timed out" in low or "timeout" in low or "temporary failure" in low:
        return "⏱️ انتهت مهلة الاتصال بالموقع. تحقق من الشبكة وحاول مجددًا."
    if "403" in low or "429" in low or "forbidden" in low or "too many requests" in low:
        return "🚧 الموقع رفض الطلب مؤقتًا (حماية أو حد للطلبات). حاول لاحقًا."
    if "no video formats" in low or "requested format" in low:
        return "🎞️ لا توجد صيغة قابلة للتحميل لهذا الرابط."
    if "duration" in low and "limit" in low:
        return f"⏳ مدة الفيديو تتجاوز الحد المسموح ({config.MAX_VIDEO_DURATION_SECONDS // 60} دقيقة)."
    return f"❌ تعذر التحميل: {text[:300]}"


async def _remove_or_prepare(
    result: downloader.DownloadResult,
    job: Job,
    user_id: int,
    chat_id: int,
    url: str,
    set_status,
) -> tuple[str, PendingEdit | None]:
    """يزيل الاسم منخفض الخطورة، ويجهّز حالة انتظار عندما يكون التشويه محتملًا."""
    variants = watermark.username_variants(
        result.uploader,
        result.uploader_id,
        url,
        title=result.title,
        channel=result.channel,
    )
    engine = await asyncio.to_thread(watermark._load_engine)
    if engine is None:
        return "ℹ️ تعذّر فحص الفيديو (محرك OCR غير متوفر).", None
    if not variants:
        return "ℹ️ لم أتعرف على اسم صاحب المحتوى من الرابط، أُرسل الفيديو كما هو.", None
    log.info("أشكال اسم المستخدم المطابقة: %s", sorted(variants))

    notes: list[str] = []
    pending: PendingEdit | None = None
    videos: list[Path] = []

    for video in result.videos:
        try:
            analysis = await watermark.analyze_video(
                video, variants, engine, set_status
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("تعذر تحليل %s", video)
            videos.append(video)
            continue

        if analysis is None:
            videos.append(video)
            continue
        if analysis.reason == "too-long":
            notes.append("too-long")
            videos.append(video)
            continue
        if not analysis.boxes:
            notes.append("not-found")
            videos.append(video)
            continue

        if (
            config.USERNAME_OFFER_MANUAL
            and analysis.risk >= config.USERNAME_RISK_THRESHOLD
            and pending is None
        ):
            pending = PendingEdit(
                user_id=user_id,
                chat_id=chat_id,
                job_id=job.id,
                video=video,
                width=analysis.width,
                height=analysis.height,
                boxes=list(analysis.boxes),
                variants=variants,
                mode="replace",
                title=result.title,
                risk_reason=analysis.risk_reason,
                texts=list(analysis.texts),
                info=analysis.info,
            )
            notes.append("risk")
            videos.append(video)
            continue

        cleaned = await watermark.apply_removal(analysis, progress=set_status)
        if cleaned is not None:
            video.unlink(missing_ok=True)
            videos.append(cleaned)
            notes.append("removed")
        else:
            videos.append(video)
            notes.append("failed")

    result.videos = videos
    result.files = result.videos + result.photos + result.audios

    if "removed" in notes:
        note = "🧼 تمت إزالة اسم المستخدم من الفيديو."
    elif "risk" in notes:
        note = "⚠️ وجدت اسم المستخدم، لكن قد تُشوّه إزالته الصورة — راجع الرسالة التالية."
    elif "too-long" in notes:
        minutes = max(1, config.USERNAME_MAX_SECONDS // 60)
        note = f"ℹ️ الفيديو أطول من {minutes} دقيقة، أُرسل كما هو دون إزالة الاسم."
    elif "not-found" in notes:
        note = "ℹ️ لم أجد اسم المستخدم مطبوعًا داخل الفيديو. أرسل /fix لإزالته يدويًا."
    else:
        note = ""

    if pending is not None:
        log.info(
            "بانتظار نص بديل للمستخدم %s (خطورة عالية) للرابط %s",
            user_id,
            url,
        )
    return note, pending


async def _process_url(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if message is None or chat is None or user is None:
        return

    storage: Storage = context.application.bot_data["storage"]
    semaphore: asyncio.Semaphore = context.application.bot_data["sem"]
    cookies = context.application.bot_data.get("cookies")

    log.info("بدء معالجة رابط للمستخدم %s: %s", user.id, url)

    status_message = await message.reply_text(
        "🔍 جاري تحليل الرابط…",
        reply_parameters=_reply_params(update),
    )

    async def set_status(text: str) -> None:
        try:
            await status_message.edit_text(text)
        except BadRequest:
            pass
        except TelegramError as exc:
            log.debug("تعذر تحديث رسالة الحالة: %s", exc)

    job: Job = await storage.create_job(user.id, chat.id, url, platform_key(url))

    removal_note = ""
    pending_edit: PendingEdit | None = None

    try:
        async with semaphore:
            await set_status("⏳ في قائمة الانتظار…")
            result = await downloader.download(
                url=url,
                job_dir=job.directory,
                cookies=cookies,
                progress=set_status,
            )

            if config.REMOVE_USERNAME and result.videos:
                await set_status("🧼 جاري فحص كامل الإطارات لكشف اسم المستخدم…")
                removal_note, pending_edit = await _remove_or_prepare(
                    result=result,
                    job=job,
                    user_id=user.id,
                    chat_id=chat.id,
                    url=url,
                    set_status=set_status,
                )

            await storage.finish_job(job, result.files, result.title)

        await set_status("📤 جاري إرسال الملفات…")
        report = await sender.deliver(
            bot=context.bot,
            chat_id=chat.id,
            job=job,
            reply_to=message.message_id,
            status=set_status,
        )

        note_line = f"\n{removal_note}" if removal_note else ""

        if report.sent and not report.failed:
            await set_status(
                f"✅ تم إرسال {report.sent} ملف بنجاح.{note_line}\n"
                f"🗑️ سيُحذف تلقائيًا من السحابة بعد {config.RETENTION_HOURS:g} ساعات."
            )
        elif report.sent:
            notes = "\n".join(f"• {note[:120]}" for note in report.notes[:3])
            await set_status(
                f"⚠️ تم إرسال {report.sent} ملف، وتعذر إرسال {report.failed}.{note_line}\n{notes}"
            )
        else:
            notes = "\n".join(f"• {note[:200]}" for note in report.notes[:3])
            await set_status(f"❌ تعذر إرسال الملفات.{note_line}\n{notes}")
            await storage.delete_job(job.id)

        if pending_edit is not None and report.sent:
            _pending_map(context)[user.id] = pending_edit
            try:
                sent = await context.bot.send_message(
                    chat_id=chat.id,
                    text=_pending_prompt(pending_edit),
                    reply_parameters=_reply_params(update),
                )
                pending_edit.prompt_message_id = sent.message_id
            except TelegramError as exc:
                log.warning("تعذر إرسال رسالة النص البديل: %s", exc)

    except downloader.DownloadFailed as exc:
        await storage.fail_job(job)
        await set_status(friendly_error(str(exc)))
    except asyncio.CancelledError:
        await storage.fail_job(job)
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("خطأ غير متوقع أثناء معالجة %s", url)
        await storage.fail_job(job)
        await set_status(
            "❌ حدث خطأ غير متوقع أثناء المعالجة.\n"
            f"{clean_error(str(exc))[:200]}"
        )


async def _handle_pending_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    pending: PendingEdit,
    text: str,
) -> None:
    """تنفيذ طلب المستخدم بعد سؤاله عن نص بديل أو نص للبحث اليدوي."""
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return

    pending_map = _pending_map(context)
    raw = text.strip()
    low = raw.lower()

    if low in {"تخطي", "skip", "لا", "إلغاء", "الغاء", "cancel"}:
        pending_map.pop(user.id, None)
        await _safe_reply(update, "✅ تم الإبقاء على الفيديو كما هو.")
        return

    remove_only = low in {"إزالة", "ازالة", "remove", "احذف", "حذف"}

    status_message = await message.reply_text("🧼 جاري تعديل الفيديو…")

    async def set_status(line: str) -> None:
        try:
            await status_message.edit_text(line)
        except BadRequest:
            pass
        except TelegramError as exc:
            log.debug("تعذر تحديث رسالة الحالة: %s", exc)

    replacement: str | None = None

    if pending.mode == "find":
        target = raw
        for separator in ("=>", "->", "←", "="):
            if separator in raw:
                old, new = raw.split(separator, 1)
                target = old.strip()
                replacement = new.strip() or None
                break
        if not target:
            await set_status("❌ أرسل النص كما يظهر في الفيديو.")
            return

        engine = await asyncio.to_thread(watermark._load_engine)
        if engine is None:
            pending_map.pop(user.id, None)
            await set_status("ℹ️ محرك OCR غير متوفر، لا يمكن البحث اليدوي.")
            return

        analysis = await watermark.analyze_video(
            pending.video,
            set(),
            engine,
            set_status,
            target=target,
        )
        if analysis is None or not analysis.boxes:
            await set_status(
                "❌ لم أجد هذا النص في الفيديو.\n"
                "أرسله كما يظهر بالضبط (بدون رموز زائدة)، أو أرسل /fix للمحاولة مجددًا."
            )
            return

        pending.boxes = analysis.boxes
        pending.width = analysis.width
        pending.height = analysis.height
        pending.info = analysis.info
    else:
        if not remove_only:
            replacement = raw.strip().strip('"').strip("«»").strip() or None
        analysis = watermark.VideoAnalysis(
            video=pending.video,
            boxes=pending.boxes,
            width=pending.width,
            height=pending.height,
            info=pending.info,
        )

    cleaned = await watermark.apply_removal(
        analysis, replacement=replacement, progress=set_status
    )
    if cleaned is None:
        pending_map.pop(user.id, None)
        await set_status("❌ تعذر تعديل الفيديو. سيبقى كما هو.")
        return

    storage: Storage = context.application.bot_data["storage"]
    job = await storage.get_job(pending.job_id)
    if job is None:
        pending_map.pop(user.id, None)
        cleaned.unlink(missing_ok=True)
        await set_status("⌛ انتهت صلاحية الملف، أعد إرسال الرابط.")
        return

    remaining = [
        path
        for path in job.paths
        if path.is_file() and path != pending.video and path != cleaned
    ]
    pending.video.unlink(missing_ok=True)
    await storage.finish_job(job, remaining + [cleaned], job.title)
    pending_map.pop(user.id, None)

    await set_status("📤 جاري إرسال الفيديو المعدّل…")
    report = await sender.deliver(
        bot=context.bot,
        chat_id=pending.chat_id,
        job=job,
        reply_to=message.message_id,
        status=set_status,
    )
    if report.sent:
        await set_status("✅ تم تعديل الفيديو وإرساله بنجاح.")
    else:
        notes = "\n".join(f"• {note[:150]}" for note in report.notes[:2])
        await set_status(f"❌ تعذر إرسال الفيديو المعدّل.\n{notes}")


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return

    text = message.text or message.caption or ""
    log.info(
        "رسالة من %s (@%s) في %s: %s",
        user.id,
        user.username or "-",
        message.chat_id,
        text[:200].replace("\n", " "),
    )
    urls = [url for url in extract_urls(text) if is_probably_url(url)]

    pending = _pending_map(context).get(user.id)
    if pending is not None and time.time() - pending.created_at > 1800:
        _pending_map(context).pop(user.id, None)
        pending = None

    if pending is not None and not urls and text.strip():
        reply_to = message.reply_to_message
        is_reply = bool(
            reply_to is not None
            and pending.prompt_message_id
            and reply_to.message_id == pending.prompt_message_id
        )
        control = text.strip().lower()
        if (
            pending.mode == "find"
            or is_reply
            or _looks_like_replacement(text)
            or control
            in {
                "إزالة",
                "ازالة",
                "remove",
                "احذف",
                "حذف",
                "تخطي",
                "skip",
                "إلغاء",
                "الغاء",
                "cancel",
            }
        ):
            await _handle_pending_text(update, context, pending, text)
            return
        await _safe_reply(
            update,
            "ℹ️ لديك فيديو بانتظار تعديل.\n"
            "أرسل النص البديل، أو «إزالة» لإزالة الاسم فقط، أو «تخطي» للإبقاء عليه.",
        )
        return

    if not urls:
        hint = (
            "🔗 أرسل رابط فيديو أو ستوري، وسأتكفّل بالباقي.\n"
            "اكتب /help للمزيد من التفاصيل."
        )
        if config.USERNAME_OFFER_MANUAL:
            hint += "\n\n🛠️ لإزالة اسم لم يتعرّف عليه البوت تلقائيًا: أرسل /fix."
        await _safe_reply(update, hint)
        return

    urls = urls[: config.MAX_URLS_PER_MESSAGE]

    allowed: list[str] = []
    blocked: list[tuple[str, str]] = []
    for url in urls:
        key = platform_key(url)
        if not config.ALLOWED_PLATFORMS or key in config.ALLOWED_PLATFORMS:
            allowed.append(url)
        else:
            blocked.append((url, key))

    if blocked:
        labels = "، ".join(sorted({platform_label(url) for url, _key in blocked}))
        await _safe_reply(
            update,
            f"🚫 هذه المنصة غير مفعّلة حاليًا ({labels}).\n"
            f"✅ المدعوم: {_allowed_platforms_text()}.",
        )

    urls = allowed
    if not urls:
        return

    # المستخدم انتقل إلى رابط جديد — لا نُبقيه أسير حالة انتظار قديمة
    _pending_map(context).pop(user.id, None)

    cooldowns: dict[int, float] = context.application.bot_data["cooldowns"]
    now = time.monotonic()
    last = cooldowns.get(user.id, 0.0)
    if now - last < config.USER_COOLDOWN_SECONDS:
        remaining = int(config.USER_COOLDOWN_SECONDS - (now - last)) + 1
        await _safe_reply(update, f"⏳ انتظر {remaining} ثانية قبل إرسال رابط جديد.")
        return
    cooldowns[user.id] = now

    locks: dict[int, asyncio.Lock] = context.application.bot_data["locks"]
    lock = locks.setdefault(user.id, asyncio.Lock())

    async with lock:
        for url in urls:
            await _process_url(update, context, url)


async def cmd_fix(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """بدء إزالة/استبدال يدوي لاسم لم يتعرّف عليه البوت تلقائيًا."""
    user = update.effective_user
    if user is None:
        return

    storage: Storage = context.application.bot_data["storage"]
    jobs = await storage.jobs_of_user(user.id)
    for job in sorted(jobs, key=lambda item: item.created_at, reverse=True):
        for path in job.paths:
            if path.suffix.lower() not in sender.VIDEO_EXTS or not path.is_file():
                continue
            info = await media.probe(path)
            _pending_map(context)[user.id] = PendingEdit(
                user_id=user.id,
                chat_id=job.chat_id,
                job_id=job.id,
                video=path,
                width=info.width,
                height=info.height,
                mode="find",
                title=job.title,
                info=info,
            )
            await _safe_reply(
                update,
                "🔎 أرسل النص كما يظهر في الفيديو بالضبط لأزيله،\n"
                "أو «القديم => الجديد» لاستبداله بنص آخر في نفس المكان والخط.",
            )
            return

    await _safe_reply(
        update,
        "📭 لا يوجد فيديو حديث. أرسل رابط فيديو أولًا ثم استخدم /fix.",
    )


# ---------------------------------------------------------------------------
# التسجيل
# ---------------------------------------------------------------------------
def register_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("clean", cmd_clean))
    app.add_handler(CommandHandler("cookies", cmd_cookies))
    app.add_handler(CommandHandler("fix", cmd_fix))
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.CAPTION) & ~filters.COMMAND,
            on_message,
        )
    )
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))


BOT_COMMANDS = [
    BotCommand("start", "بدء البوت"),
    BotCommand("help", "طريقة الاستخدام"),
    BotCommand("id", "معرّف حسابك"),
    BotCommand("status", "حالة الخدمة والتخزين"),
    BotCommand("cookies", "حالة ملف الكوكيز"),
    BotCommand("fix", "إزالة/استبدال اسم يدويًا"),
]
