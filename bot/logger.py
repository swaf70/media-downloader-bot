"""إعداد نظام السجلات (logging) لكل أجزاء البوت."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "bot.log"

_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: str = "INFO") -> None:
    """تهيئة السجلات مرة واحدة عند بدء التشغيل (طرفية + ملف دائم)."""
    numeric = getattr(logging, str(level).upper(), logging.INFO)

    # ضمان طباعة النصوص العربية على طرفية ويندوز
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    root = logging.getLogger()
    root.setLevel(numeric)

    # إزالة أي معالجات سابقة (force=True القديم) حتى لا تتكرر السطور
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler(stream=sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    # ملف دائم حتى لو انقطعت مخرجات الطرفية أو أُعيد تشغيل الجلسة
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except Exception:  # noqa: BLE001 - لا نريد إسقاط البوت بسبب تعذر الكتابة
        pass

    # تقليل ضجيج المكتبات الخارجية
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext.Application").setLevel(logging.INFO)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
