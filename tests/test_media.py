"""اختبار أدوات الوسائط: الفحص، الضغط، التقسيم، والصورة المصغرة."""

import asyncio
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import config, media  # noqa: E402

LIMIT = 400 * 1024  # 400 كيلوبايت لاختبار التقسيم


async def make_sample(dst: Path) -> None:
    cmd = [
        media.FFMPEG,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=640x360:rate=30",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=44100",
        "-t",
        "20",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(dst),
    ]
    code, _out, err = await media._run(cmd, timeout=120)
    assert code == 0, err[-500:]


async def main() -> None:
    if not media.ffmpeg_available():
        print("SKIP: ffmpeg غير متوفر")
        return

    print("ffmpeg:", media.FFMPEG)
    print("ffprobe:", media.FFPROBE)

    work = config.JOBS_DIR / "test-media"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    src = work / "sample.mp4"
    await make_sample(src)

    info = await media.probe(src)
    print("المدة:", round(info.duration, 2), "الأبعاد:", info.width, "x", info.height)
    assert info.duration > 15, info
    assert info.width == 640 and info.height == 360, info
    assert info.has_video and info.has_audio, info

    thumb = await media.make_thumbnail(src)
    assert thumb is not None and thumb.stat().st_size <= 200 * 1024
    print("الصورة المصغرة:", thumb.name, thumb.stat().st_size)

    shrunk = await media.shrink_to_fit(src, 900 * 1024, info, attempt=1)
    assert shrunk is not None, "فشل الضغط"
    assert shrunk.stat().st_size <= 900 * 1024
    print("الضغط:", shrunk.name, shrunk.stat().st_size)

    parts = await media.split_video(src, LIMIT, info)
    assert len(parts) >= 2, parts
    for part in parts:
        assert part.stat().st_size <= LIMIT, (part.name, part.stat().st_size)
    print("التقسيم:", len(parts), "أجزاء", [p.stat().st_size for p in parts])

    print("MEDIA_TEST_OK")
    shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
