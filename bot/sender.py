"""إرسال الملفات إلى المستخدم مع ضمان الوصول: ضغط، تقسيم، وإعادة محاولة."""

from __future__ import annotations

import asyncio
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Iterator

from telegram import Bot, InputFile, ReplyParameters
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError, TimedOut

from . import config, media
from .logger import get_logger
from .storage import Job
from .utils import human_size, platform_label

log = get_logger(__name__)

StatusCallback = Callable[[str], Awaitable[None]]

CAPTION_LIMIT = 1000
PHOTO_LIMIT_BYTES = 10 * 1024 * 1024

VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".flv", ".ts", ".3gp", ".avi"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
AUDIO_EXTS = {".m4a", ".mp3", ".opus", ".ogg", ".wav", ".aac", ".flac"}


class FileTooBig(RuntimeError):
    """الملف أكبر من الحد المسموح حتى بعد المحاولات."""


@dataclass
class DeliveryReport:
    sent: int = 0
    failed: int = 0
    notes: list[str] = field(default_factory=list)


async def _noop(_text: str) -> None:
    return None


@contextmanager
def _local_file(path: Path) -> Iterator[InputFile]:
    """فتح الملف للرفع دون تحميله كاملًا في الذاكرة."""
    handle = path.open("rb")
    try:
        yield InputFile(handle, filename=path.name, read_file_handle=False)
    finally:
        handle.close()


def build_caption(job: Job, suffix: str = "") -> str:
    title = job.title or "تم التحميل"
    lines = [f"🎬 {title}"]
    label = platform_label(job.url)
    if label:
        lines.append(f"🌐 {label}")
    lines.append(f"🔗 {job.url}")
    lines.append(
        f"🗑️ سيُحذف الملف من السحابة تلقائيًا بعد {config.RETENTION_HOURS:g} ساعات"
    )
    if suffix:
        lines.append(suffix)
    caption = "\n".join(lines)
    return caption[:CAPTION_LIMIT]


async def _call_with_retry(factory: Callable[[], Awaitable], attempts: int = 3):
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return await factory()
        except RetryAfter as exc:
            wait = float(exc.retry_after) + 1.0
            log.warning("تيليجرام طلب الانتظار %.0f ثانية", wait)
            await asyncio.sleep(wait)
            last_error = exc
        except (TimedOut, NetworkError) as exc:
            last_error = exc
            await asyncio.sleep(2.0 * (attempt + 1))
        except BadRequest as exc:
            text = str(exc).lower()
            if "too big" in text or "file is too large" in text:
                raise FileTooBig(str(exc)) from exc
            raise
    if last_error is not None:
        raise last_error
    raise TelegramError("فشل غير معروف في الإرسال")


def _reply_parameters(reply_to: int | None) -> ReplyParameters | None:
    if not reply_to:
        return None
    return ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)


async def _upload_video(
    bot: Bot,
    chat_id: int,
    path: Path,
    caption: str,
    reply_to: int | None,
) -> None:
    info, thumbnail = await asyncio.gather(
        media.probe(path), media.make_thumbnail(path)
    )

    async def factory():
        with ExitStack() as stack:
            video_file = stack.enter_context(_local_file(path))
            kwargs = {
                "chat_id": chat_id,
                "video": video_file,
                "caption": caption[:CAPTION_LIMIT],
                "supports_streaming": True,
                "reply_parameters": _reply_parameters(reply_to),
            }
            if info.duration > 0:
                kwargs["duration"] = int(info.duration)
            if info.width and info.height:
                kwargs["width"] = info.width
                kwargs["height"] = info.height
            if thumbnail is not None:
                kwargs["thumbnail"] = stack.enter_context(_local_file(thumbnail))
            return await bot.send_video(**kwargs)

    await _call_with_retry(factory)


async def _upload_photo(
    bot: Bot,
    chat_id: int,
    path: Path,
    caption: str,
    reply_to: int | None,
) -> None:
    if path.stat().st_size <= PHOTO_LIMIT_BYTES:
        async def factory():
            with _local_file(path) as photo_file:
                return await bot.send_photo(
                    chat_id=chat_id,
                    photo=photo_file,
                    caption=caption[:CAPTION_LIMIT],
                    reply_parameters=_reply_parameters(reply_to),
                )

        try:
            await _call_with_retry(factory)
            return
        except BadRequest as exc:
            log.warning("تعذر إرسال الصورة كصورة، ستُرسل كملف: %s", exc)

    await _upload_document(bot, chat_id, path, caption, reply_to)


async def _upload_audio(
    bot: Bot,
    chat_id: int,
    path: Path,
    caption: str,
    reply_to: int | None,
) -> None:
    info = await media.probe(path)

    async def factory():
        with _local_file(path) as audio_file:
            kwargs = {
                "chat_id": chat_id,
                "audio": audio_file,
                "caption": caption[:CAPTION_LIMIT],
                "reply_parameters": _reply_parameters(reply_to),
            }
            if info.duration > 0:
                kwargs["duration"] = int(info.duration)
            return await bot.send_audio(**kwargs)

    await _call_with_retry(factory)


async def _upload_animation(
    bot: Bot,
    chat_id: int,
    path: Path,
    caption: str,
    reply_to: int | None,
) -> None:
    async def factory():
        with _local_file(path) as animation_file:
            return await bot.send_animation(
                chat_id=chat_id,
                animation=animation_file,
                caption=caption[:CAPTION_LIMIT],
                reply_parameters=_reply_parameters(reply_to),
            )

    await _call_with_retry(factory)


async def _upload_document(
    bot: Bot,
    chat_id: int,
    path: Path,
    caption: str,
    reply_to: int | None,
) -> None:
    async def factory():
        with _local_file(path) as document_file:
            return await bot.send_document(
                chat_id=chat_id,
                document=document_file,
                caption=caption[:CAPTION_LIMIT],
                reply_parameters=_reply_parameters(reply_to),
            )

    await _call_with_retry(factory)


async def _send_video_file(
    bot: Bot,
    chat_id: int,
    path: Path,
    job: Job,
    reply_to: int | None,
    status: StatusCallback,
) -> None:
    limit = config.MAX_UPLOAD_BYTES
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise FileTooBig(f"تعذر قراءة الملف: {exc}") from exc

    if size <= limit:
        await _upload_video(bot, chat_id, path, build_caption(job), reply_to)
        return

    info = await media.probe(path)

    # المحاولة الأولى: ضغط ذكي
    for attempt in (1, 2):
        await status(
            f"🗜️ حجم الفيديو {human_size(size)} أكبر من حد تيليجرام… "
            f"جاري الضغط (محاولة {attempt}/2)"
        )
        shrunk = await media.shrink_to_fit(path, limit, info, attempt=attempt)
        if shrunk is not None:
            await _upload_video(
                bot,
                chat_id,
                shrunk,
                build_caption(job, "📉 تم ضغط الفيديو ليتناسب مع تيليجرام"),
                reply_to,
            )
            return

    # المحاولة الثانية: تقسيم إلى أجزاء
    await status("✂️ تعذر ضغط الفيديو بدرجة كافية… جاري تقسيمه إلى أجزاء")
    parts = await media.split_video(path, limit, info)
    if not parts:
        raise FileTooBig(
            f"حجم الفيديو {human_size(size)} أكبر من الحد المسموح ({config.MAX_UPLOAD_MB}MB)"
        )

    total = len(parts)
    for index, part in enumerate(parts, start=1):
        await status(f"📤 إرسال الجزء {index} من {total}…")
        suffix = f"🧩 الجزء {index} من {total}"
        await _upload_video(bot, chat_id, part, build_caption(job, suffix), reply_to)


async def deliver(
    bot: Bot,
    chat_id: int,
    job: Job,
    reply_to: int | None = None,
    status: StatusCallback | None = None,
) -> DeliveryReport:
    """إرسال كل ملفات المهمة إلى المستخدم وإرجاع تقرير بالنتيجة."""
    status = status or _noop
    report = DeliveryReport()
    files = [path for path in job.paths if path.is_file()]

    if not files:
        report.notes.append("لا توجد ملفات للإرسال")
        return report

    total = len(files)
    for index, path in enumerate(files, start=1):
        await status(f"📤 جاري إرسال الملف {index} من {total}…")
        suffix = path.suffix.lower()
        try:
            if suffix in VIDEO_EXTS:
                await _send_video_file(bot, chat_id, path, job, reply_to, status)
            elif suffix == ".gif":
                await _upload_animation(bot, chat_id, path, build_caption(job), reply_to)
            elif suffix in IMAGE_EXTS:
                await _upload_photo(bot, chat_id, path, build_caption(job), reply_to)
            elif suffix in AUDIO_EXTS:
                await _upload_audio(bot, chat_id, path, build_caption(job), reply_to)
            else:
                await _upload_document(bot, chat_id, path, build_caption(job), reply_to)
            report.sent += 1
        except FileTooBig as exc:
            report.failed += 1
            report.notes.append(str(exc))
            log.warning("ملف كبير جدًا %s: %s", path.name, exc)
        except TelegramError as exc:
            report.failed += 1
            report.notes.append(str(exc))
            log.warning("فشل إرسال %s: %s", path.name, exc)
        except Exception as exc:  # noqa: BLE001
            report.failed += 1
            report.notes.append(str(exc))
            log.exception("خطأ غير متوقع أثناء إرسال %s", path.name)

    return report
