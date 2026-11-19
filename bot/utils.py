"""أدوات مساعدة عامة: الروابط، أسماء المنصات، تنسيق الأحجام."""

from __future__ import annotations

import re
from urllib.parse import urlparse

URL_RE = re.compile(r"https?://[^\s<>\"'\)\]]+", re.IGNORECASE)

# مفاتيح المنصات وأسماؤها بالعربية
PLATFORMS: list[tuple[tuple[str, ...], str, str]] = [
    (("tiktok.com", "vt.tiktok.com", "vm.tiktok.com"), "tiktok", "تيك توك"),
    (("instagram.com", "instagr.am", "ig.me"), "instagram", "إنستغرام"),
    (("facebook.com", "fb.watch", "fb.com", "m.facebook.com"), "facebook", "فيسبوك"),
    (("youtube.com", "youtu.be", "youtube-nocookie.com"), "youtube", "يوتيوب"),
    (("twitter.com", "x.com", "t.co"), "twitter", "إكس (تويتر)"),
    (("snapchat.com", "snap.com"), "snapchat", "سناب شات"),
    (("reddit.com", "redd.it"), "reddit", "ريديت"),
    (("pinterest.com", "pin.it"), "pinterest", "بينترست"),
    (("threads.net", "threads.com"), "threads", "ثريدز"),
    (("vimeo.com",), "vimeo", "فيميو"),
    (("likee.video", "likee.com"), "likee", "لايكي"),
    (("kwai.com", "kwai-video.com"), "kwai", "كواي"),
    (("dailymotion.com", "dai.ly"), "dailymotion", "ديلي موشن"),
    (("twitch.tv",), "twitch", "تويتش"),
    (("t.me", "telegram.me"), "telegram", "تيليجرام"),
    (("linkedin.com",), "linkedin", "لينكدإن"),
    (("tumblr.com",), "tumblr", "تمبلر"),
    (("vk.com", "vkvideo.ru"), "vk", "في كيه"),
    (("bilibili.com", "b23.tv"), "bilibili", "بيليبيلي"),
    (("soundcloud.com",), "soundcloud", "ساوند كلاود"),
    (("streamable.com",), "streamable", "ستريمبل"),
    (("imgur.com",), "imgur", "إيمجور"),
]


def extract_urls(text: str | None) -> list[str]:
    """يستخرج كل الروابط الفريدة من نص الرسالة."""
    if not text:
        return []
    found: list[str] = []
    for match in URL_RE.finditer(text):
        url = match.group(0).rstrip(".,;:!?،؛»”\"'")
        if url and url not in found:
            found.append(url)
    return found


def _host(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def platform_key(url: str) -> str:
    """مفتاح المنصة (tiktok / instagram / ...) أو generic."""
    host = _host(url)
    if not host:
        return "generic"
    for domains, key, _label in PLATFORMS:
        for domain in domains:
            if host == domain or host.endswith("." + domain):
                return key
    return "generic"


def platform_label(url: str) -> str:
    """اسم المنصة بالعربية للعرض للمستخدم."""
    host = _host(url)
    for domains, _key, label in PLATFORMS:
        for domain in domains:
            if host == domain or host.endswith("." + domain):
                return label
    return host or "رابط مباشر"


def is_probably_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname) and "." in parsed.hostname


def human_size(num_bytes: float) -> str:
    """تحويل الحجم إلى صيغة مقروءة."""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


def human_duration(seconds: float | int | None) -> str:
    """تحويل الثواني إلى mm:ss أو hh:mm:ss."""
    if not seconds or seconds <= 0:
        return "00:00"
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def clean_text(value: str | None, limit: int = 200) -> str:
    """تنظيف النصوص القادمة من المواقع (أسطر جديدة/مسافات زائدة)."""
    if not value:
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def clean_error(message: str) -> str:
    """إزالة تلوين الطرفية وبادئة ERROR من رسائل yt-dlp."""
    text = ANSI_RE.sub("", str(message or "")).strip()
    for prefix in ("ERROR:", "WARNING:"):
        if text.upper().startswith(prefix):
            text = text[len(prefix) :].strip()
    return text
