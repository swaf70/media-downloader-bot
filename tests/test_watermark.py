"""اختبار إزالة اسم المستخدم المطبوع داخل الفيديو.

يولّد فيديو بصورة تحتوي اسم مستخدم، ثم يتأكد من اكتشافه وإزالته.
"""

import asyncio
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import config, media, watermark  # noqa: E402

HANDLE = "testuser123"


def test_variants() -> None:
    variants = watermark.username_variants(
        uploader="Test User",
        uploader_id="testuser123",
        url="https://www.tiktok.com/@testuser123/video/7106594312292453675",
    )
    assert "testuser123" in variants, variants
    assert watermark.text_matches_username("@testuser123", variants)
    assert watermark.text_matches_username("test user 123", variants)
    assert not watermark.text_matches_username("subscribe now", variants)
    # تحمّل خطأ قراءة OCR (1 بدل l)
    assert watermark.text_matches_username("@testuserl23", variants)

    ig = watermark.username_variants(
        url="https://www.instagram.com/stories/my.page/1234567890/"
    )
    assert "mypage" in ig, ig
    assert "my" not in ig, ig

    reel = watermark.username_variants(url="https://www.instagram.com/reel/ABCdef123/")
    assert "abcdef123" not in reel, reel

    assert watermark.username_variants() == set()
    assert watermark._is_mention("@someone_else")
    assert not watermark._is_mention("hello there")
    print("variants: OK")


async def _make_sample(dst: Path, handle: str) -> None:
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (720, 404), (25, 32, 48))
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 40)
        small = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 24)
    except Exception:  # noqa: BLE001
        font = ImageFont.load_default()
        small = ImageFont.load_default()

    draw.text((26, 24), f"@{handle}", font=font, fill=(255, 255, 255))
    draw.text((26, 90), "subscribe for more", font=small, fill=(200, 210, 230))

    still = dst.with_suffix(".png")
    image.save(still)

    cmd = [
        media.FFMPEG,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-loop",
        "1",
        "-i",
        str(still),
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=44100:cl=mono",
        "-t",
        "8",
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
    still.unlink(missing_ok=True)
    assert code == 0, err[-600:]


async def _read_texts(video: Path, work: Path) -> list[str]:
    frame = work / "check.jpg"
    ok = await watermark._grab_frame(video, 3.0, frame, config.USERNAME_OCR_WIDTH)
    assert ok, "تعذر استخراج إطار للتحقق"
    engine = watermark._load_engine()
    assert engine is not None
    boxes = await watermark._ocr_frame(engine, frame)
    return [text for _box, text, _score in boxes]


async def main() -> None:
    test_variants()

    if not media.ffmpeg_available():
        print("SKIP: ffmpeg غير متوفر")
        return
    if watermark._load_engine() is None:
        print("SKIP: محرك OCR غير متوفر")
        return

    work = config.JOBS_DIR / "test-watermark"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    video = work / "sample.mp4"
    await _make_sample(video, HANDLE)

    before = await _read_texts(video, work)
    print("قبل الإزالة:", before)
    assert any(HANDLE in text.lower().replace("@", "") for text in before), before

    report = await watermark.remove_usernames(
        videos=[video],
        uploader=HANDLE,
        url=f"https://www.tiktok.com/@{HANDLE}/video/1",
    )
    assert report.removed == 1, report
    assert report.boxes >= 1, report
    assert report.videos and report.videos[0].is_file(), report.videos
    assert not video.exists(), "يجب حذف الفيديو الأصلي بعد نجاح الإزالة"

    cleaned = report.videos[0]
    info = await media.probe(cleaned)
    assert info.duration > 5, info
    print("بعد الإزالة:", cleaned.name, cleaned.stat().st_size, "بايت")

    after = await _read_texts(cleaned, work)
    print("نصوص الإطار بعد الإزالة:", after)
    assert not any(watermark.text_matches_username(text, {HANDLE}) for text in after), after

    # فيديو لا يحتوي على الاسم يجب أن يبقى كما هو
    other = work / "other.mp4"
    await _make_sample(other, "someoneelse")
    report2 = await watermark.remove_usernames(
        videos=[other], uploader="notinthevideo"
    )
    assert report2.removed == 0, report2
    assert report2.videos[0] == other and other.exists()

    # استبدال الاسم بنص بديل في نفس المكان
    replace_video = work / "replace.mp4"
    await _make_sample(replace_video, HANDLE)
    engine = watermark._load_engine()
    variants = watermark.username_variants(
        uploader=HANDLE,
        url=f"https://www.tiktok.com/@{HANDLE}/video/1",
    )
    analysis = await watermark.analyze_video(replace_video, variants, engine)
    assert analysis is not None and analysis.boxes, analysis
    replaced = await watermark.apply_removal(analysis, replacement="@myaccount")
    assert replaced is not None and replaced.is_file()

    after_replace = await _read_texts(replaced, work)
    joined = " ".join(after_replace).lower().replace("@", "")
    assert "myaccount" in joined, after_replace
    assert HANDLE not in joined, after_replace
    print("بعد الاستبدال:", after_replace)

    print("WATERMARK_TEST_OK")
    shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
