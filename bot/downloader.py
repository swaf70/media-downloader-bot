"""تحميل الوسائط من مواقع التواصل عبر yt-dlp (أفضل جودة وبأقل علامات مائية ممكنة)."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

import httpx
import yt_dlp
from yt_dlp.utils import DownloadError as YtDlpError

from . import config, media
from .logger import get_logger
from .utils import clean_error, clean_text, human_duration, human_size, platform_key

log = get_logger(__name__)

ProgressCallback = Callable[[str], Awaitable[None]]

VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".flv", ".ts", ".3gp", ".avi"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
AUDIO_EXTS = {".m4a", ".mp3", ".opus", ".ogg", ".wav", ".aac", ".flac"}
ALL_EXTS = VIDEO_EXTS | IMAGE_EXTS | AUDIO_EXTS

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class DownloadFailed(RuntimeError):
    """فشل التحميل مع رسالة صالحة للعرض للمستخدم."""


@dataclass
class DownloadResult:
    title: str = ""
    uploader: str = ""
    uploader_id: str = ""
    channel: str = ""
    platform: str = "generic"
    files: list[Path] = field(default_factory=list)
    videos: list[Path] = field(default_factory=list)
    photos: list[Path] = field(default_factory=list)
    audios: list[Path] = field(default_factory=list)
    thumbnail: Path | None = None

    @property
    def count(self) -> int:
        return len(self.files)


class _YdlLogger:
    """توجيه سجلات yt-dlp إلى نظام السجلات بدل الطرفية."""

    def debug(self, msg: str) -> None:
        if msg.startswith("[debug] "):
            return
        log.debug("yt-dlp: %s", msg)

    def info(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def warning(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        log.warning("yt-dlp: %s", msg)


def _build_format(platform: str) -> str:
    """اختيار أفضل صيغة مع تفضيل mp4 وتجنّب نسخ تيك توك ذات العلامة المائية."""
    if not media.ffmpeg_available():
        # بدون ffmpeg لا يمكن الدمج، لذلك نختار ملفًا واحدًا جاهزًا
        if platform == "tiktok":
            return "b[format_id!*=download]/b"
        return "b[ext=mp4]/b"

    best = "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b"
    if platform == "tiktok":
        # نسخ "download" في تيك توك تكون مدمجة بالعلامة المائية؛ نفضّلها أخيرًا
        best = (
            "bv*[format_id!*=download]+ba[format_id!*=download]/"
            "b[format_id!*=download]/" + best
        )
    return best


def _match_filter(info: dict, *, incomplete: bool = False):
    if incomplete:
        return None
    duration = info.get("duration")
    if duration and duration > config.MAX_VIDEO_DURATION_SECONDS:
        limit_minutes = max(1, config.MAX_VIDEO_DURATION_SECONDS // 60)
        return f"المدة تتجاوز الحد المسموح ({limit_minutes} دقيقة)"
    return None


def _make_progress_hook(
    loop: asyncio.AbstractEventLoop,
    callback: ProgressCallback | None,
    interval: float = 2.0,
):
    last = {"time": 0.0}

    def report(text: str) -> None:
        if callback is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(callback(text), loop)
        except RuntimeError:
            pass

    def hook(data: dict) -> None:
        status = data.get("status")
        if status == "finished":
            report("⚙️ جاري المعالجة النهائية…")
            return
        if status != "downloading":
            return

        now = time.monotonic()
        if now - last["time"] < interval:
            return
        last["time"] = now

        total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
        done = data.get("downloaded_bytes") or 0
        speed = data.get("speed") or 0
        eta = data.get("eta")

        line = "⬇️ جاري التحميل"
        if total:
            line += f"… {done * 100.0 / total:.0f}%"
        line += f" • {human_size(done)}"
        if speed:
            line += f" • {human_size(speed)}/ث"
        if eta:
            line += f" • متبقٍ {human_duration(eta)}"

        info = data.get("info_dict") or {}
        index = info.get("playlist_index")
        total_items = info.get("n_entries")
        if index and total_items:
            line += f"\n📚 المقطع {index} من {total_items}"

        report(line)

    return hook


def _build_options(
    job_dir: Path,
    cookies: Path | None,
    loop: asyncio.AbstractEventLoop,
    progress: ProgressCallback | None,
    platform: str,
) -> dict:
    options: dict = {
        "paths": {"home": str(job_dir), "temp": str(job_dir / ".tmp")},
        "outtmpl": {"default": "%(title).70s [%(id)s].%(ext)s"},
        "format": _build_format(platform),
        "noplaylist": True,
        "playlistend": config.MAX_PLAYLIST_ITEMS,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "restrictfilenames": True,
        "ignoreerrors": False,
        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 3,
        "file_access_retries": 3,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 8,
        "writethumbnail": False,
        "writeinfojson": False,
        "clean_infojson": True,
        "geo_bypass": True,
        "nocheckcertificate": True,
        "overwrites": True,
        "trim_file_name": 120,
        "logger": _YdlLogger(),
        "progress_hooks": [_make_progress_hook(loop, progress)],
        "match_filter": _match_filter,
        "http_headers": {"User-Agent": USER_AGENT},
        "cachedir": str(job_dir / ".cache"),
    }

    if media.ffmpeg_available():
        options["merge_output_format"] = "mp4"
        options["ffmpeg_location"] = str(media.FFMPEG)
        options["postprocessors"] = [
            {"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}
        ]

    if cookies is not None:
        options["cookiefile"] = str(cookies)
    if config.PROXY_URL:
        options["proxy"] = config.PROXY_URL

    return options


def _collect_files(job_dir: Path) -> DownloadResult:
    """تصنيف الملفات الناتجة إلى فيديو/صور/صوت مع تمييز الصور المصغرة."""
    result = DownloadResult()
    if not job_dir.exists():
        return result

    for path in sorted(job_dir.rglob("*")):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix not in ALL_EXTS:
            continue
        # تجاهل أيقونات أو صور مصغرة تلقائية من yt-dlp (تُعالج لاحقًا)
        if path.name.endswith(".thumb.jpg"):
            continue
        if suffix in VIDEO_EXTS:
            result.videos.append(path)
        elif suffix in AUDIO_EXTS:
            result.audios.append(path)
        elif suffix in IMAGE_EXTS:
            result.photos.append(path)

    video_stems = {path.stem for path in result.videos}
    photos: list[Path] = []
    for image in result.photos:
        if image.stem in video_stems:
            result.thumbnail = result.thumbnail or image
        else:
            photos.append(image)
    result.photos = photos

    result.files = result.videos + result.photos + result.audios
    return result


async def _normalize_containers(result: DownloadResult) -> None:
    normalized: list[Path] = []
    for video in result.videos:
        if video.suffix.lower() == ".mp4":
            normalized.append(video)
            continue
        converted = await media.remux_to_mp4(video)
        if converted is not None:
            normalized.append(converted)
        else:
            normalized.append(video)
    result.videos = normalized
    result.files = result.videos + result.photos + result.audios


async def _http_download(
    url: str,
    dst: Path,
    progress: ProgressCallback | None = None,
    label: str = "",
) -> bool:
    """تنزيل ملف عبر HTTP مع تحديث التقدم (يُستخدم كمصدر بديل لتيك توك)."""
    headers = {"User-Agent": USER_AGENT, "Referer": "https://www.tiktok.com/"}
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(90.0, connect=30.0),
            follow_redirects=True,
            headers=headers,
        ) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                total = int(response.headers.get("content-length") or 0)
                done = 0
                last = 0.0
                with dst.open("wb") as handle:
                    async for chunk in response.aiter_bytes(262144):
                        handle.write(chunk)
                        done += len(chunk)
                        now = time.monotonic()
                        if progress is not None and now - last >= 2.0:
                            last = now
                            percent = f" {done * 100 // total}%" if total else ""
                            await progress(
                                f"⬇️ جاري التحميل {label}{percent} • {human_size(done)}"
                            )
    except Exception as exc:  # noqa: BLE001
        log.warning("فشل تنزيل بديل من %s: %s", url[:120], exc)
        dst.unlink(missing_ok=True)
        return False

    return dst.is_file() and dst.stat().st_size > 0


async def _tiktok_fallback(
    url: str,
    job_dir: Path,
    progress: ProgressCallback | None = None,
) -> DownloadResult | None:
    """مصدر بديل لتيك توك عند حجب yt-dlp (يعيد نسخة بلا علامة مائية)."""
    api = "https://www.tikwm.com/api/"
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=15.0),
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            response = await client.get(api, params={"url": url, "hd": 1})
            payload = response.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("تعذر الوصول إلى المصدر البديل لتيك توك: %s", exc)
        return None

    if not isinstance(payload, dict) or payload.get("code") != 0:
        log.info("المصدر البديل لم يُرجع نتيجة: %s", payload.get("msg") if isinstance(payload, dict) else "")
        return None

    data = payload.get("data") or {}
    play_url = data.get("hdplay") or data.get("play")
    if not play_url:
        return None

    video_id = str(data.get("id") or "video")
    dst = job_dir / f"tiktok_{video_id}.mp4"
    if not await _http_download(play_url, dst, progress, "من تيك توك"):
        return None

    info = await media.probe(dst)
    if info.duration and info.duration > config.MAX_VIDEO_DURATION_SECONDS:
        dst.unlink(missing_ok=True)
        raise DownloadFailed(
            f"مدة الفيديو تتجاوز الحد المسموح ({config.MAX_VIDEO_DURATION_SECONDS // 60} دقيقة)"
        )

    author = data.get("author") or {}
    result = DownloadResult(
        title=clean_text(data.get("title") or "", limit=180),
        uploader=clean_text(author.get("nickname") or author.get("unique_id") or "", limit=80),
        uploader_id=clean_text(author.get("unique_id") or "", limit=80),
        channel=clean_text(author.get("unique_id") or "", limit=80),
        platform="tiktok",
        files=[dst],
        videos=[dst],
    )

    cover_url = data.get("origin_cover") or data.get("cover")
    if cover_url:
        cover = job_dir / f"tiktok_{video_id}.jpg"
        if await _http_download(cover_url, cover, None, ""):
            result.thumbnail = cover

    log.info("تم التحميل عبر المصدر البديل لتيك توك: %s", result.title)
    return result


async def download(
    url: str,
    job_dir: Path,
    cookies: Path | None = None,
    progress: ProgressCallback | None = None,
) -> DownloadResult:
    """تحميل رابط واحد (قد يحتوي عدة مقاطع/ستوريات) إلى مجلد المهمة."""
    job_dir.mkdir(parents=True, exist_ok=True)
    platform = platform_key(url)
    loop = asyncio.get_running_loop()
    options = _build_options(job_dir, cookies, loop, progress, platform)

    def _work() -> dict | None:
        with yt_dlp.YoutubeDL(options) as ydl:
            return ydl.extract_info(url, download=True)

    try:
        info = await asyncio.to_thread(_work)
    except Exception as exc:  # noqa: BLE001
        error_text = clean_error(str(exc)) or "خطأ غير معروف أثناء التحميل"
        if platform == "tiktok":
            if progress is not None:
                await progress("🔁 تعذر المصدر الأساسي… تجربة مصدر بديل")
            try:
                fallback = await _tiktok_fallback(url, job_dir, progress)
            except DownloadFailed:
                raise
            except Exception as fb_exc:  # noqa: BLE001
                log.warning("فشل المصدر البديل: %s", fb_exc)
                fallback = None
            if fallback is not None:
                return fallback
        raise DownloadFailed(error_text) from exc

    if not info:
        raise DownloadFailed("لم يتم العثور على محتوى في هذا الرابط")

    result = _collect_files(job_dir)
    if not result.files and platform == "tiktok":
        fallback = await _tiktok_fallback(url, job_dir, progress)
        if fallback is not None:
            return fallback
    if not result.files:
        raise DownloadFailed("لم يتم تنزيل أي ملف من هذا الرابط")

    await _normalize_containers(result)
    if not result.files:
        raise DownloadFailed("تعذر تجهيز الملفات بعد التحميل")

    result.platform = platform
    result.title = clean_text(
        info.get("title") or info.get("description") or "", limit=180
    )
    channel = clean_text(info.get("channel") or "", limit=80)
    uploader_id = clean_text(info.get("uploader_id") or info.get("channel_id") or "", limit=80)
    # إنستغرام يعيد أحيانًا uploader_id رقميًا والهاندل الحقيقي في channel
    if channel and (not uploader_id or uploader_id.isdigit()):
        uploader_id = channel
    result.channel = channel
    result.uploader = clean_text(
        info.get("uploader")
        or channel
        or info.get("channel")
        or uploader_id
        or "",
        limit=80,
    )
    result.uploader_id = uploader_id
    if not result.title:
        result.title = result.videos[0].stem if result.videos else "محتوى"

    log.info(
        "اكتمل التحميل: %s | منصة=%s | ملفات=%s | حجم=%.1fMB | صاحب=%s",
        url,
        platform,
        len(result.files),
        sum(p.stat().st_size for p in result.files if p.exists()) / 1048576,
        result.uploader_id or result.uploader or "-",
    )
    return result
