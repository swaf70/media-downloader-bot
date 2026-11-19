"""أدوات ffmpeg/ffprobe: فحص الوسائط، الضغط لتناسب تيليجرام، والتقسيم عند الحاجة."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .logger import get_logger

log = get_logger(__name__)


class MediaToolError(RuntimeError):
    """أداة ffmpeg/ffprobe غير متوفرة أو فشلت."""


def _find_tool(name: str) -> str | None:
    exe = name + (".exe" if os.name == "nt" else "")

    env_value = os.getenv(f"{name.upper()}_BINARY")
    if env_value and Path(env_value).is_file():
        return env_value

    found = shutil.which(name)
    if found:
        return found

    if os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        if local.is_dir():
            patterns = (
                f"Microsoft/WinGet/Packages/Gyan.FFmpeg*/**/bin/{exe}",
                f"Microsoft/WinGet/Packages/*FFmpeg*/**/bin/{exe}",
            )
            for pattern in patterns:
                for candidate in sorted(local.glob(pattern)):
                    if candidate.is_file():
                        return str(candidate)
        for base in (r"C:\ffmpeg\bin", r"C:\Program Files\ffmpeg\bin"):
            candidate = Path(base) / exe
            if candidate.is_file():
                return str(candidate)

    # احتياطي: نسخة ffmpeg المرفقة مع حزمة imageio-ffmpeg
    if name == "ffmpeg":
        try:
            import imageio_ffmpeg  # type: ignore

            bundled = imageio_ffmpeg.get_ffmpeg_exe()
            if bundled and Path(bundled).is_file():
                return bundled
        except Exception:  # noqa: BLE001
            pass

    return None


FFMPEG = _find_tool("ffmpeg")
FFPROBE = _find_tool("ffprobe")


def ffmpeg_available() -> bool:
    return bool(FFMPEG)


@dataclass
class MediaInfo:
    path: Path
    duration: float = 0.0
    width: int = 0
    height: int = 0
    bitrate: int = 0
    size: int = 0
    has_video: bool = False
    has_audio: bool = False

    @property
    def is_video(self) -> bool:
        return self.has_video


async def _run(cmd: list[str], timeout: float) -> tuple[int, bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        try:
            await process.communicate()
        except Exception:  # noqa: BLE001
            pass
        raise MediaToolError("انتهت مهلة تنفيذ أداة الوسائط") from None
    return process.returncode or 0, stdout, stderr


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_BITRATE_RE = re.compile(r"bitrate:\s*(\d+)\s*kb/s")
_SIZE_RE = re.compile(r"(\d{2,5})x(\d{2,5})")


async def _probe_with_ffmpeg(path: Path, info: MediaInfo) -> MediaInfo:
    """بديل لـ ffprobe عند عدم توفره: نقرأ معلومات الوسائط من مخرجات ffmpeg."""
    if not FFMPEG:
        return info
    cmd = [FFMPEG, "-hide_banner", "-i", str(path)]
    try:
        _code, _stdout, stderr = await _run(cmd, timeout=60)
    except (MediaToolError, OSError):
        return info

    text = stderr.decode("utf-8", "replace")

    match = _DURATION_RE.search(text)
    if match:
        info.duration = (
            int(match.group(1)) * 3600
            + int(match.group(2)) * 60
            + float(match.group(3))
        )

    match = _BITRATE_RE.search(text)
    if match:
        info.bitrate = int(match.group(1)) * 1000

    info.has_audio = "Audio:" in text

    for line in text.splitlines():
        if "Video:" not in line:
            continue
        info.has_video = True
        cleaned = re.sub(r"(?<![0-9A-Za-z])0x[0-9a-fA-F]+", "", line)
        size_match = _SIZE_RE.search(cleaned)
        if size_match:
            info.width = int(size_match.group(1))
            info.height = int(size_match.group(2))
        break

    return info


_PROBE_CACHE: dict[tuple[str, int, int], MediaInfo] = {}
_PROBE_CACHE_MAX = 96


async def probe(path: Path) -> MediaInfo:
    """قراءة خصائص الملف (المدة، الأبعاد، معدل البت...) مع تخزين مؤقت.

    نفس الملف يُفحص عدة مرات أثناء المعالجة والإرسال، والتخزين المؤقت يوفّر
    ثوانيَ من تشغيل ffmpeg المتكرر.
    """
    try:
        stat = path.stat()
    except OSError:
        return MediaInfo(path=path)

    key = (str(path), stat.st_size, stat.st_mtime_ns)
    cached = _PROBE_CACHE.get(key)
    if cached is not None:
        return cached

    info = await _probe_uncached(path, stat.st_size)
    if len(_PROBE_CACHE) >= _PROBE_CACHE_MAX:
        _PROBE_CACHE.pop(next(iter(_PROBE_CACHE)), None)
    _PROBE_CACHE[key] = info
    return info


async def _probe_uncached(path: Path, size: int) -> MediaInfo:
    info = MediaInfo(path=path)
    info.size = size

    if not FFPROBE:
        return await _probe_with_ffmpeg(path, info)

    cmd = [
        FFPROBE,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        code, stdout, stderr = await _run(cmd, timeout=60)
    except (MediaToolError, OSError) as exc:
        log.debug("ffprobe فشل على %s: %s", path.name, exc)
        return info

    if code != 0:
        log.debug("ffprobe رمز خطأ %s على %s: %s", code, path.name, stderr[-300:])
        return await _probe_with_ffmpeg(path, info) if FFMPEG else info

    try:
        payload = json.loads(stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return await _probe_with_ffmpeg(path, info) if FFMPEG else info

    fmt = payload.get("format") or {}
    try:
        info.duration = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        info.duration = 0.0
    try:
        info.bitrate = int(fmt.get("bit_rate") or 0)
    except (TypeError, ValueError):
        info.bitrate = 0

    for stream in payload.get("streams") or []:
        kind = stream.get("codec_type")
        if kind == "video" and not info.has_video:
            info.has_video = True
            try:
                info.width = int(stream.get("width") or 0)
                info.height = int(stream.get("height") or 0)
            except (TypeError, ValueError):
                pass
            if not info.duration:
                try:
                    info.duration = float(stream.get("duration") or 0.0)
                except (TypeError, ValueError):
                    pass
        elif kind == "audio":
            info.has_audio = True

    if info.duration <= 0 and FFMPEG:
        info = await _probe_with_ffmpeg(path, info)

    return info


def _pick_height(info: MediaInfo, video_kbps: float, attempt: int) -> int:
    source = info.height or 720
    if attempt <= 1:
        cap = 720
    elif attempt == 2:
        cap = 480
    else:
        cap = 360
    if video_kbps < 260:
        cap = min(cap, 360)
    elif video_kbps < 600:
        cap = min(cap, 480)
    return min(source, cap)


async def shrink_to_fit(
    src: Path,
    limit_bytes: int,
    info: MediaInfo | None = None,
    attempt: int = 1,
) -> Path | None:
    """ضغط الفيديو ليصبح حجمه أقل من حد تيليجرام. يعيد None عند الفشل."""
    if not FFMPEG:
        raise MediaToolError("ffmpeg غير مثبت على النظام")

    info = info or await probe(src)
    duration = info.duration
    if duration <= 0:
        return None

    audio_kbps = 96 if info.has_audio else 0
    if attempt == 2:
        audio_kbps = 64
    elif attempt >= 3:
        audio_kbps = 48

    budget_kbps = (limit_bytes * 8 / duration) / 1000.0 * 0.90
    video_kbps = budget_kbps - audio_kbps
    minimum = 80 if attempt >= 3 else 120
    if video_kbps < minimum:
        return None
    video_kbps = min(video_kbps, 2600)

    target_height = _pick_height(info, video_kbps, attempt)
    crf = 28 if attempt <= 1 else (32 if attempt == 2 else 35)

    dst = src.with_name(f"{src.stem}.fit{attempt}.mp4")
    cmd = [
        FFMPEG,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(src),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        str(crf),
        "-maxrate",
        f"{int(video_kbps)}k",
        "-bufsize",
        f"{int(video_kbps * 2)}k",
        "-pix_fmt",
        "yuv420p",
    ]
    if info.height and info.height > target_height:
        cmd += ["-vf", f"scale=-2:{target_height}"]
    cmd += [
        "-c:a",
        "aac",
        "-b:a",
        f"{audio_kbps}k",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        str(dst),
    ]

    timeout = min(max(300.0, duration * 3.0), 1800.0)
    try:
        code, _stdout, stderr = await _run(cmd, timeout=timeout)
    except (MediaToolError, OSError) as exc:
        log.warning("فشل ضغط %s: %s", src.name, exc)
        return None

    if code != 0 or not dst.is_file():
        log.warning("ffmpeg ضغط فشل على %s: %s", src.name, stderr[-400:])
        dst.unlink(missing_ok=True)
        return None

    if dst.stat().st_size <= limit_bytes:
        return dst

    dst.unlink(missing_ok=True)
    return None


async def _copy_segments(src: Path, segment_seconds: float, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("part_*.mp4"):
        old.unlink(missing_ok=True)

    pattern = str(out_dir / "part_%03d.mp4")
    cmd = [
        FFMPEG,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(src),
        "-c",
        "copy",
        "-map",
        "0",
        "-f",
        "segment",
        "-segment_time",
        f"{max(1.0, segment_seconds):.2f}",
        "-reset_timestamps",
        "1",
        "-segment_format",
        "mp4",
        pattern,
    ]

    try:
        code, _stdout, stderr = await _run(cmd, timeout=900)
    except (MediaToolError, OSError) as exc:
        log.warning("فشل تقسيم %s: %s", src.name, exc)
        return []

    if code != 0:
        log.warning("ffmpeg تقسيم فشل على %s: %s", src.name, stderr[-400:])

    return sorted(out_dir.glob("part_*.mp4"))


async def split_video(
    src: Path,
    limit_bytes: int,
    info: MediaInfo | None = None,
    out_dir: Path | None = None,
) -> list[Path]:
    """تقسيم فيديو كبير إلى أجزاء بحجم أقل من الحد (بدون إعادة ترميز)."""
    if not FFMPEG:
        raise MediaToolError("ffmpeg غير مثبت على النظام")

    info = info or await probe(src)
    try:
        size = src.stat().st_size
    except OSError:
        return []

    duration = info.duration
    if duration <= 0 or size <= 0:
        return []

    bytes_per_second = size / duration
    # هامش أمان لأن حدود المقاطع لا تُقطع إلا عند الإطارات المفتاحية
    segment_seconds = int(limit_bytes * 0.65 / bytes_per_second)
    segment_seconds = max(10, min(segment_seconds, 1800))

    target_dir = out_dir or (src.parent / "parts")
    parts = await _copy_segments(src, segment_seconds, target_dir)
    if len(parts) <= 1:
        for part in parts:
            part.unlink(missing_ok=True)
        return []

    final: list[Path] = []
    queue = list(parts)
    guard = 0

    while queue and guard < 200:
        guard += 1
        part = queue.pop(0)
        if not part.is_file():
            continue
        if part.stat().st_size <= limit_bytes:
            final.append(part)
            continue

        shrunk = await shrink_to_fit(part, limit_bytes, attempt=2)
        if shrunk is None:
            shrunk = await shrink_to_fit(part, limit_bytes, attempt=3)
        if shrunk is not None:
            part.unlink(missing_ok=True)
            final.append(shrunk)
            continue

        part_info = await probe(part)
        if part_info.duration > 4:
            sub_dir = part.parent / f"{part.stem}_sub"
            sub_parts = await _copy_segments(
                part, max(2.0, part_info.duration / 2.0), sub_dir
            )
            if len(sub_parts) > 1:
                part.unlink(missing_ok=True)
                queue.extend(sub_parts)
                continue

        final.append(part)

    return final


async def remux_to_mp4(src: Path) -> Path | None:
    """تحويل الحاوية إلى mp4 بدون إعادة ترميز إن أمكن."""
    if src.suffix.lower() == ".mp4":
        return src
    if not FFMPEG:
        return None

    dst = src.with_suffix(".mp4")
    if dst.is_file() and dst.stat().st_size > 0:
        return dst

    cmd = [
        FFMPEG,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(src),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(dst),
    ]
    try:
        code, _stdout, stderr = await _run(cmd, timeout=300)
    except (MediaToolError, OSError):
        return None

    if code != 0 or not dst.is_file():
        log.debug("تعذر تحويل %s إلى mp4: %s", src.name, stderr[-300:])
        dst.unlink(missing_ok=True)
        return None
    return dst


async def make_thumbnail(video: Path, at_seconds: float = 1.0) -> Path | None:
    """إنشاء صورة مصغرة للفيديو (حد تيليجرام 200 كيلوبايت)."""
    if not FFMPEG:
        return None
    dst = video.with_name(video.stem + ".thumb.jpg")
    # إعادة استخدام صورة مصغرة حديثة إن وُجدت (توفير وقت الإرسال)
    try:
        if dst.is_file():
            dst_stat = dst.stat()
            if (
                dst_stat.st_size <= 200 * 1024
                and dst_stat.st_mtime >= video.stat().st_mtime
            ):
                return dst
    except OSError:
        pass
    cmd = [
        FFMPEG,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        str(max(0.0, at_seconds)),
        "-i",
        str(video),
        "-frames:v",
        "1",
        "-vf",
        "scale=320:-2",
        "-q:v",
        "6",
        str(dst),
    ]
    try:
        code, _stdout, _stderr = await _run(cmd, timeout=60)
    except (MediaToolError, OSError):
        return None
    if code != 0 or not dst.is_file():
        dst.unlink(missing_ok=True)
        return None
    if dst.stat().st_size > 200 * 1024:
        dst.unlink(missing_ok=True)
        return None
    return dst
