"""اختبار منطق الإرسال باستخدام بوت وهمي (بدون اتصال بتيليجرام)."""

import asyncio
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import config, media, sender  # noqa: E402
from bot.storage import Job  # noqa: E402


class FakeBot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def _read(self, upload) -> int:
        handle = upload.input_file_content
        data = handle.read(4096)
        return len(data)

    async def send_video(self, **kwargs):
        self.calls.append(("video", self._read(kwargs["video"])))
        return None

    async def send_photo(self, **kwargs):
        self.calls.append(("photo", self._read(kwargs["photo"])))
        return None

    async def send_document(self, **kwargs):
        self.calls.append(("document", self._read(kwargs["document"])))
        return None

    async def send_audio(self, **kwargs):
        self.calls.append(("audio", self._read(kwargs["audio"])))
        return None

    async def send_animation(self, **kwargs):
        self.calls.append(("animation", self._read(kwargs["animation"])))
        return None


async def make_sample(dst: Path) -> None:
    cmd = [
        media.FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
        "-t", "20", "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(dst),
    ]
    code, _out, err = await media._run(cmd, timeout=120)
    assert code == 0, err[-500:]


async def main() -> None:
    if not media.ffmpeg_available():
        print("SKIP: ffmpeg غير متوفر")
        return

    work = config.JOBS_DIR / "test-sender"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    sample = work / "sample.mp4"
    await make_sample(sample)

    job = Job(
        id="test-sender",
        user_id=1,
        chat_id=1,
        url="https://example.com/video",
        platform="generic",
        title="فيديو اختبار",
        files=[sample.name],
    )

    bot = FakeBot()
    report = await sender.deliver(bot, 1, job)
    assert report.sent == 1 and report.failed == 0, report
    assert bot.calls and bot.calls[0][0] == "video", bot.calls
    print("إرسال عادي:", bot.calls)

    # اختبار تجاوز الحد => ضغط أو تقسيم
    original = config.MAX_UPLOAD_BYTES
    config.MAX_UPLOAD_BYTES = 300 * 1024
    try:
        bot2 = FakeBot()
        report2 = await sender.deliver(bot2, 1, job)
        assert report2.sent >= 1 and report2.failed == 0, report2
        print("إرسال بعد التقسيم/الضغط:", len(bot2.calls), "ملف", bot2.calls[:3])
    finally:
        config.MAX_UPLOAD_BYTES = original

    assert "سيُحذف" in sender.build_caption(job)
    print("SENDER_TEST_OK")
    shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
