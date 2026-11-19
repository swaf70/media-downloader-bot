"""اختبارات سريعة لا تحتاج شبكة: الإعدادات، التخزين، الأدوات المساعدة."""

import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import config, storage, utils  # noqa: E402


def test_utils() -> None:
    urls = utils.extract_urls(
        "شاهد https://www.tiktok.com/@a/video/1 و https://instagram.com/reel/X/ ثم https://x.com/a/status/2."
    )
    assert len(urls) == 3, urls
    assert utils.platform_key("https://vm.tiktok.com/abc") == "tiktok"
    assert utils.platform_key("https://www.instagram.com/stories/a/1/") == "instagram"
    assert utils.platform_key("https://youtu.be/abc") == "youtube"
    assert utils.platform_key("https://example.com/x") == "generic"
    assert utils.human_size(1048576) == "1.0 MB"
    assert utils.human_duration(3661) == "01:01:01"
    print("utils: OK")


async def test_storage() -> None:
    # نستخدم مجلدًا مؤقتًا حتى لا يمسّ الاختبار بيانات التشغيل الحقيقية
    original = (config.STORAGE_DIR, config.JOBS_DIR, config.INDEX_FILE)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "storage"
        config.STORAGE_DIR = root
        config.JOBS_DIR = root / "jobs"
        config.INDEX_FILE = root / "index.json"
        try:
            store = storage.Storage()
            await store.startup()

            job = await store.create_job(1, 1, "https://example.com/v", "generic")
            (job.directory / "video.mp4").write_bytes(b"x" * 1024)
            await store.finish_job(job, [job.directory / "video.mp4"], "اختبار")

            stats = await store.stats()
            assert stats["jobs"] >= 1, stats
            assert stats["bytes"] >= 1024, stats

            # اختبار الحذف بعد انتهاء المدة
            old = config.RETENTION_SECONDS
            config.RETENTION_SECONDS = 0.0
            removed, freed = await store.purge_expired()
            config.RETENTION_SECONDS = old
            assert removed >= 1, removed
            assert not job.directory.exists()
            print(f"storage: OK (حُذف {removed}، {freed} بايت)")
        finally:
            config.STORAGE_DIR, config.JOBS_DIR, config.INDEX_FILE = original


async def main() -> None:
    config.validate()
    test_utils()
    await test_storage()
    print("ALL_TESTS_OK")


if __name__ == "__main__":
    asyncio.run(main())
