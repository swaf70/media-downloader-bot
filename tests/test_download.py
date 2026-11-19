"""اختبار التحميل من رابط مباشر (يتطلب إنترنت)."""

import asyncio
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import config, downloader, media  # noqa: E402

TEST_URL = sys.argv[1] if len(sys.argv) > 1 else "https://download.samplelib.com/mp4/sample-5s.mp4"


async def main() -> None:
    config.ensure_directories()
    job_dir = config.JOBS_DIR / "test-download"
    shutil.rmtree(job_dir, ignore_errors=True)

    messages: list[str] = []

    async def progress(text: str) -> None:
        messages.append(text)

    result = await downloader.download(TEST_URL, job_dir, progress=progress)
    print("العنوان:", result.title)
    print("المنصة:", result.platform)
    print("عدد الملفات:", result.count)
    for path in result.files:
        print(" -", path.name, path.stat().st_size, "بايت")
        info = await media.probe(path)
        print("   مدة:", info.duration, "أبعاد:", info.width, "x", info.height)

    assert result.count >= 1, "لم يتم تنزيل أي ملف"
    assert all(p.stat().st_size > 0 for p in result.files)
    print("تحديثات التقدم:", len(messages))
    print("DOWNLOAD_TEST_OK")
    shutil.rmtree(job_dir, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
