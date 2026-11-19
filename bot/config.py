"""إعدادات البوت — تُقرأ من متغيرات البيئة أو من ملف .env بجانب المشروع."""

from __future__ import annotations

import base64
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _int(name: str, default: int) -> int:
    try:
        return int(float(_str(name, str(default))))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(_str(name, str(default)))
    except (TypeError, ValueError):
        return default


def _bool(name: str, default: bool) -> bool:
    raw = _str(name)
    if not raw:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y", "نعم", "تشغيل"}


def _ids(name: str) -> set[int]:
    result: set[int] = set()
    for chunk in _str(name).replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk and chunk.lstrip("-").isdigit():
            result.add(int(chunk))
    return result


def _path(name: str, default: Path) -> Path:
    raw = _str(name)
    path = Path(raw).expanduser() if raw else default
    if not path.is_absolute():
        path = (BASE_DIR / path).resolve()
    return path


# ---------------------------------------------------------------------------
# الهوية والصلاحيات
# ---------------------------------------------------------------------------
BOT_TOKEN: str = _str("BOT_TOKEN")
ADMIN_IDS: set[int] = _ids("ADMIN_IDS")

# ---------------------------------------------------------------------------
# التخزين السحابي المؤقت
# ---------------------------------------------------------------------------
STORAGE_DIR: Path = _path("STORAGE_DIR", BASE_DIR / "storage")
JOBS_DIR: Path = STORAGE_DIR / "jobs"
INDEX_FILE: Path = STORAGE_DIR / "index.json"

# مدة بقاء الملفات في السحابة قبل الحذف التلقائي (3 ساعات افتراضيًا)
RETENTION_HOURS: float = max(0.1, _float("RETENTION_HOURS", 3.0))
RETENTION_SECONDS: float = RETENTION_HOURS * 3600.0
CLEANUP_INTERVAL_SECONDS: int = max(30, _int("CLEANUP_INTERVAL_SECONDS", 300))

# ---------------------------------------------------------------------------
# حدود المعالجة والإرسال
# ---------------------------------------------------------------------------
# الحد الآمن لرفع الملفات عبر Telegram Bot API هو 50 ميجابايت
MAX_UPLOAD_MB: int = max(5, _int("MAX_UPLOAD_MB", 49))
MAX_UPLOAD_BYTES: int = MAX_UPLOAD_MB * 1024 * 1024

MAX_CONCURRENT_DOWNLOADS: int = max(1, _int("MAX_CONCURRENT_DOWNLOADS", 2))
MAX_URLS_PER_MESSAGE: int = max(1, _int("MAX_URLS_PER_MESSAGE", 5))
USER_COOLDOWN_SECONDS: int = max(0, _int("USER_COOLDOWN_SECONDS", 5))
MAX_VIDEO_DURATION_SECONDS: int = max(60, _int("MAX_VIDEO_DURATION_SECONDS", 3600))
MAX_PLAYLIST_ITEMS: int = max(1, _int("MAX_PLAYLIST_ITEMS", 25))
DOWNLOAD_TIMEOUT_SECONDS: int = max(60, _int("DOWNLOAD_TIMEOUT_SECONDS", 1800))

# ---------------------------------------------------------------------------
# المنصات المسموح بها (للسرعة) — فارغة = كل المنصات
# ---------------------------------------------------------------------------
ALLOWED_PLATFORMS: set[str] = {
    item.strip().lower()
    for item in _str("ALLOWED_PLATFORMS", "tiktok,instagram,facebook").split(",")
    if item.strip()
}

# ---------------------------------------------------------------------------
# إزالة اسم المستخدم المطبوع داخل الفيديو (OCR + delogo)
# ---------------------------------------------------------------------------
REMOVE_USERNAME: bool = _bool("REMOVE_USERNAME", True)
# عدد الإطارات التي تُقرأ بـ OCR (أكثر = دقة أعلى ووقت أطول)
USERNAME_OCR_FRAMES: int = min(18, max(2, _int("USERNAME_OCR_FRAMES", 10)))
# أقصى مدة فيديو تُعالج (إعادة الترميز تستهلك وقتًا)
USERNAME_MAX_SECONDS: int = max(30, _int("USERNAME_MAX_SECONDS", 600))
# عرض الإطار المستخدم في OCR (أكبر = نصوص أصغر تُقرأ أفضل)
USERNAME_OCR_WIDTH: int = min(1920, max(480, _int("USERNAME_OCR_WIDTH", 1280)))
# أقل ثقة مقبولة من محرك OCR
USERNAME_OCR_MIN_SCORE: float = min(0.95, max(0.1, _float("USERNAME_OCR_MIN_SCORE", 0.25)))
# إزالة أي نص يبدأ بـ @ حتى لو لم يكن اسم صاحب المحتوى (اختياري)
USERNAME_ANY_MENTION: bool = _bool("USERNAME_ANY_MENTION", False)
# عند تجاوز خطورة التشويه لهذه العتبة نسأل المستخدم عن نص بديل بدل الإزالة العمياء
USERNAME_RISK_THRESHOLD: float = min(0.95, max(0.2, _float("USERNAME_RISK_THRESHOLD", 0.45)))
# خط بديل (اختياري) لمطابقة خط الفيديو قدر الإمكان
USERNAME_FONT_FILE: str = _str("USERNAME_FONT_FILE")
# اقتراح الوضع اليدوي عندما لا يُعثر على الاسم تلقائيًا
USERNAME_OFFER_MANUAL: bool = _bool("USERNAME_OFFER_MANUAL", True)

# ---------------------------------------------------------------------------
# الشبكة والكوكيز (مطلوبة لبعض محتوى إنستغرام/فيسبوك الخاص)
# ---------------------------------------------------------------------------
PROXY_URL: str = _str("PROXY_URL")
COOKIES_FILE: Path | None = (
    _path("COOKIES_FILE", BASE_DIR / "cookies.txt") if _str("COOKIES_FILE") else None
)
COOKIES_B64: str = _str("COOKIES_B64")
COOKIES_PATH: Path = STORAGE_DIR / "cookies.txt"

# ---------------------------------------------------------------------------
# التشغيل (Polling محليًا / Webhook على السحابة)
# ---------------------------------------------------------------------------
PORT: int = _int("PORT", 8080)
WEBHOOK_URL: str = (
    _str("WEBHOOK_URL") or _str("RENDER_EXTERNAL_URL")
).rstrip("/")
LOG_LEVEL: str = _str("LOG_LEVEL", "INFO")

# ---------------------------------------------------------------------------
# أدوات مساعدة
# ---------------------------------------------------------------------------


def ensure_directories() -> None:
    """إنشاء مجلدات التخزين إن لم تكن موجودة."""
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)


def resolve_cookies() -> Path | None:
    """يعيد مسار ملف الكوكيز الجاهز إن وُجد.

    يدعم ثلاث طرق:
    1) COOKIES_FILE  : مسار ملف cookies.txt
    2) COOKIES_B64   : محتوى الملف مشفّرًا بـ Base64 (مناسب للسحابة)
    3) storage/cookies.txt المحفوظ مسبقًا
    """
    if COOKIES_FILE and COOKIES_FILE.is_file():
        return COOKIES_FILE

    if COOKIES_B64:
        try:
            data = base64.b64decode(COOKIES_B64)
            COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
            COOKIES_PATH.write_bytes(data)
            return COOKIES_PATH
        except Exception:  # noqa: BLE001 - لا نريد إسقاط البوت بسبب كوكيز تالفة
            return None

    if COOKIES_PATH.is_file() and COOKIES_PATH.stat().st_size > 0:
        return COOKIES_PATH

    return None


def validate() -> None:
    """التحقق من الإعدادات الأساسية قبل التشغيل."""
    if not BOT_TOKEN or ":" not in BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN غير موجود أو غير صالح. ضعه في ملف .env أو في متغيرات البيئة."
        )
