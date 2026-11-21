"""إزالة أو استبدال اسم المستخدم المطبوع داخل الفيديو (OCR + delogo + نص بديل).

الفكرة:
1) نأخذ عيّنات إطارات من الفيديو (تشمل بداية ونهاية الفيديو).
2) نقرأ كل النصوص داخلها بمحرك OCR على كامل الإطار.
3) نطابق أي نص مع اسم صاحب المحتوى (الهاندل في الرابط أو اسم القناة)،
   مع تحمّل أخطاء القراءة عبر مطابقة تقريبية.
4) نجمع المربعات الثابتة عبر الإطارات (الاسم عادةً ثابت في زاوية واحدة).
5) نحسب «خطورة التشويه»: إن كانت عالية نسأل المستخدم عن نص بديل.
6) نزيل النص بـ delogo، وإن وُجد نص بديل نرسمه في نفس المكان بنفس الحجم
   واللون وبأقرب خط متاح.

كل شيء هنا «أفضل جهد»: أي فشل يعيد الفيديو الأصلي كما هو حتى لا يمنع الوصول للمستخدم.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlparse

from . import config, media
from .logger import get_logger

log = get_logger(__name__)

ProgressCallback = Callable[[str], Awaitable[None]]

# أجزاء مسارات لا تمثل اسم مستخدم
_URL_PREFIXES = {
    "p",
    "reel",
    "reels",
    "tv",
    "stories",
    "story",
    "watch",
    "video",
    "videos",
    "status",
    "shorts",
    "photo",
    "photos",
    "share",
    "explore",
    "feed",
    "i",
    "web",
    "www",
    "profile",
    "post",
    "posts",
    "clip",
    "clips",
    "highlights",
    "live",
}

# نطاقات روابط مختصرة لا تحمل اسم المستخدم في المسار
_SHORT_HOSTS = {
    "vt.tiktok.com",
    "vm.tiktok.com",
    "fb.watch",
    "ig.me",
    "instagr.am",
    "pin.it",
    "t.co",
    "youtu.be",
    "b23.tv",
    "dai.ly",
    "redd.it",
    "t.me",
    "telegram.me",
}

_NORM_RE = re.compile(r"[^0-9a-z\u0600-\u06ff]+")
_ARABIC_RE = re.compile(r"[\u0600-\u06ff]")
_MENTION_RE = re.compile(r"(?<![\w.])@([0-9A-Za-z_.]{3,40})")


# ---------------------------------------------------------------------------
# تطبيع ومطابقة اسم المستخدم
# ---------------------------------------------------------------------------
def _norm(text: str | None) -> str:
    """تطبيع النص: حروف صغيرة، وإزالة @ والمسافات والرموز."""
    return _NORM_RE.sub("", (text or "").lower())


def _looks_like_id(value: str) -> bool:
    """تجاهل المقاطع التي تبدو معرّفات رقمية أو هاش وليست أسماء."""
    low = value.lower()
    if low.isdigit():
        return True
    if len(low) >= 16 and re.fullmatch(r"[0-9a-f]+", low):
        return True
    return False


def _host(url: str) -> str:
    try:
        host = (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def username_variants(
    uploader: str = "",
    uploader_id: str = "",
    url: str = "",
    title: str = "",
    channel: str = "",
) -> set[str]:
    """كل الصيغ المحتملة لاسم صاحب المحتوى (مطبّعة)."""
    raw: set[str] = set()

    for value in (uploader, uploader_id, channel):
        value = (value or "").strip()
        if value:
            raw.add(value.lstrip("@"))

    host = _host(url)
    short_link = any(host == h or host.endswith("." + h) for h in _SHORT_HOSTS)

    try:
        parsed = urlparse(url or "")
        segments = [s for s in parsed.path.split("/") if s]
    except ValueError:
        segments = []

    if not short_link:
        for index, segment in enumerate(segments):
            clean = segment.strip()
            if clean.startswith("@"):
                raw.add(clean[1:])
                continue
            if index == 0 and clean.lower() not in _URL_PREFIXES:
                raw.add(clean)
            if clean.lower() in {"stories", "highlights"} and index + 1 < len(segments):
                nxt = segments[index + 1]
                if nxt.lower() not in _URL_PREFIXES:
                    raw.add(nxt)

    # استخراج الهاندل من الاستعلام مثل ?username= أو ?u=
    try:
        query = urlparse(url or "").query
    except ValueError:
        query = ""
    for pair in query.split("&"):
        if "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        if key.lower() in {"username", "user", "u", "handle", "screen_name"}:
            raw.add(value)

    # استخراج الهاندل من عنوان الفيديو (إنستغرام: "Video by omar_a.55")
    if title:
        for match in re.finditer(
            r"(?:video|photo|reel|post|clip)\s+by\s+([@0-9A-Za-z_.\-]+)",
            title,
            re.I,
        ):
            raw.add(match.group(1))
        for match in _MENTION_RE.finditer(title):
            raw.add(match.group(1))
        match = re.match(
            r"(.+?)\s+on\s+(?:Instagram|TikTok|Facebook|Threads)\b",
            title,
            re.I,
        )
        if match:
            raw.add(match.group(1))

    variants: set[str] = set()
    for value in raw:
        value = value.strip().lstrip("@").strip()
        if len(value) < 3 or _looks_like_id(value):
            continue
        normalized = _norm(value)
        if len(normalized) >= 3:
            variants.add(normalized)
    return variants


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (ca != cb),
                )
            )
        previous = current
    return previous[-1]


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return 1.0 - _levenshtein(a, b) / max(len(a), len(b))


def _match_strength(
    text: str,
    variants: set[str],
    target: str = "",
) -> tuple[bool, bool]:
    """يعيد (هل طابق؟، هل المطابقة قوية؟).

    القوية = تطابق تام أو احتواء مباشر. الضعيفة = مطابقة تقريبية تتحمّل أخطاء OCR.
    """
    normalized = _norm(text)
    if len(normalized) < 3:
        return False, False

    if target:
        wanted = _norm(target)
        if len(wanted) < 2:
            return False, False
        if normalized == wanted or (len(wanted) >= 3 and wanted in normalized):
            return True, True
        if len(normalized) >= 5 and len(wanted) >= 5:
            if _similarity(normalized, wanted) >= 0.82:
                return True, False
        return False, False

    matched = False
    for variant in variants:
        if len(variant) < 3:
            continue
        if normalized == variant:
            return True, True
        if len(variant) >= 4 and variant in normalized:
            return True, True
        if len(normalized) >= 5 and normalized in variant:
            return True, True
        if len(variant) >= 5 and len(normalized) >= 5:
            if abs(len(variant) - len(normalized)) <= 4:
                if _similarity(normalized, variant) >= 0.84:
                    matched = True
    return matched, False


def text_matches_username(text: str, variants: set[str]) -> bool:
    """هل النص المقروء يطابق أحد أشكال اسم المستخدم؟"""
    return _match_strength(text, variants)[0]


def _is_mention(text: str) -> bool:
    """نص يبدأ بـ @ = يوزر صريح مكتوب داخل الفيديو."""
    stripped = (text or "").strip()
    return stripped.startswith("@") and len(_norm(stripped)) >= 4


# ---------------------------------------------------------------------------
# محرك OCR
# ---------------------------------------------------------------------------
_ENGINE = None
_ENGINE_TRIED = False
# onnxruntime ليس آمنًا تمامًا عند الاستدعاء المتوازي من عدة مهام
_OCR_LOCK = asyncio.Lock()
# قفل تحميل المحرك حتى لا تتصادم مهام متعددة أثناء التحميل المسبق
_ENGINE_LOCK = threading.Lock()


def _load_engine():
    """تحميل محرك RapidOCR مرة واحدة فقط (اختياري تمامًا)."""
    global _ENGINE, _ENGINE_TRIED
    with _ENGINE_LOCK:
        if _ENGINE_TRIED:
            return _ENGINE
        _ENGINE_TRIED = True
        try:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore

            # حاوية السحابة محدودة بـ 512MB؛ الإعدادات الافتراضية لـ onnxruntime
            # تستخدم كل أنوية الجهاز المضيف وتستهلك ذاكرة تكفي لإيقاف الحاوية
            # (OOMKilled) أثناء فحص الإطارات. نقيّد الخيوط ونعطّل تصنيف الاتجاه
            # ونصغّر دفعات الاستدلال للحفاظ على الذاكرة منخفضة.
            _ENGINE = RapidOCR(
                intra_op_num_threads=1,
                inter_op_num_threads=1,
                use_cls=False,
                det_limit_side_len=640,
                rec_batch_num=1,
                cls_batch_num=1,
            )
            log.info("محرك OCR جاهز (rapidocr-onnxruntime) — وضع الذاكرة المنخفضة")
        except Exception as exc:  # noqa: BLE001
            log.warning("محرك OCR غير متوفر، سيتم تخطي إزالة اسم المستخدم: %s", exc)
            _ENGINE = None
        return _ENGINE


def ocr_available() -> bool:
    """هل يمكن استخدام OCR الآن؟ (يحمّل المحرك عند أول نداء)"""
    return _load_engine() is not None


# ---------------------------------------------------------------------------
# استخراج الإطارات وقراءتها
# ---------------------------------------------------------------------------
@dataclass
class _Detection:
    box: tuple[float, float, float, float]  # x1, y1, x2, y2 بإحداثيات الفيديو الأصلي
    text: str
    score: float
    strong: bool = False


@dataclass
class _Cluster:
    detections: list[_Detection] = field(default_factory=list)
    frames: set[int] = field(default_factory=set)

    @property
    def strong(self) -> bool:
        return any(d.strong for d in self.detections)

    @property
    def boxes(self) -> list[tuple[float, float, float, float]]:
        return [d.box for d in self.detections]


def _median_box(boxes: list[tuple[float, float, float, float]]):
    xs1 = sorted(b[0] for b in boxes)
    ys1 = sorted(b[1] for b in boxes)
    xs2 = sorted(b[2] for b in boxes)
    ys2 = sorted(b[3] for b in boxes)
    mid = len(boxes) // 2
    return (xs1[mid], ys1[mid], xs2[mid], ys2[mid])


def _timestamps(duration: float, count: int) -> list[float]:
    if duration <= 0:
        return [0.0]
    count = max(1, count)
    margin = min(1.0, duration * 0.05)
    start = margin
    end = max(start, duration - margin)
    if count == 1 or end <= start:
        base = [duration / 2.0]
    else:
        step = (end - start) / (count - 1)
        base = [start + step * i for i in range(count)]

    # إطارات قريبة جدًا من البداية والنهاية لالتقاط الاسم العابر
    base.append(min(0.35, duration * 0.02))
    base.append(max(0.0, duration - min(0.35, duration * 0.02)))

    ordered: list[float] = []
    for value in sorted(set(round(t, 3) for t in base)):
        if 0.0 <= value <= duration:
            ordered.append(value)
    return ordered


async def _grab_frame(video: Path, seconds: float, dst: Path, width: int | None) -> bool:
    if not media.FFMPEG:
        return False
    cmd = [
        media.FFMPEG,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{max(0.0, seconds):.3f}",
        "-i",
        str(video),
        "-frames:v",
        "1",
    ]
    if width:
        cmd += ["-vf", f"scale={width}:-2"]
    cmd += ["-q:v", "2", str(dst)]

    try:
        code, _out, _err = await media._run(cmd, timeout=90)  # noqa: SLF001
    except Exception:  # noqa: BLE001
        return False
    return code == 0 and dst.is_file() and dst.stat().st_size > 0


def _image_size(path: Path) -> tuple[int, int]:
    try:
        from PIL import Image  # type: ignore

        with Image.open(path) as image:
            return image.size
    except Exception:  # noqa: BLE001
        return (0, 0)


async def _ocr_frame(engine, path: Path) -> list[tuple[tuple[float, float, float, float], str, float]]:
    def work():
        try:
            result, _elapsed = engine(str(path))
        except Exception as exc:  # noqa: BLE001
            log.debug("فشل OCR على %s: %s", path.name, exc)
            return []
        return result or []

    async with _OCR_LOCK:
        raw = await asyncio.to_thread(work)

    boxes: list[tuple[tuple[float, float, float, float], str, float]] = []
    for item in raw:
        try:
            quad = item[0]
            text = str(item[1])
            score = float(item[2])
        except (IndexError, TypeError, ValueError):
            continue
        if score < config.USERNAME_OCR_MIN_SCORE or not text.strip():
            continue
        xs = [float(point[0]) for point in quad]
        ys = [float(point[1]) for point in quad]
        boxes.append(((min(xs), min(ys), max(xs), max(ys)), text, score))
    return boxes


async def _ocr_zoom_corners(
    engine,
    frame: Path,
    width: int,
    height: int,
) -> list[tuple[tuple[float, float, float, float], str, float]]:
    """فحص مكّبر لأربع مناطق متداخلة لالتقاط النصوص الصغيرة في الزوايا."""
    try:
        from PIL import Image  # type: ignore
    except Exception:  # noqa: BLE001
        return []

    regions = [
        (0, 0, int(width * 0.58), int(height * 0.58)),
        (int(width * 0.42), 0, width, int(height * 0.58)),
        (0, int(height * 0.42), int(width * 0.58), height),
        (int(width * 0.42), int(height * 0.42), width, height),
    ]

    found: list[tuple[tuple[float, float, float, float], str, float]] = []
    for index, (x1, y1, x2, y2) in enumerate(regions):
        if x2 - x1 < 32 or y2 - y1 < 32:
            continue
        tmp = frame.with_name(f"{frame.stem}.zoom{index}.png")
        try:
            with Image.open(frame) as image:
                crop = image.crop((x1, y1, x2, y2))
                crop = crop.resize(
                    ((x2 - x1) * 2, (y2 - y1) * 2), Image.LANCZOS
                )
                crop.save(tmp)
        except Exception:  # noqa: BLE001
            tmp.unlink(missing_ok=True)
            continue

        try:
            for box, text, score in await _ocr_frame(engine, tmp):
                found.append(
                    (
                        (
                            box[0] / 2.0 + x1,
                            box[1] / 2.0 + y1,
                            box[2] / 2.0 + x1,
                            box[3] / 2.0 + y1,
                        ),
                        text,
                        score,
                    )
                )
        finally:
            tmp.unlink(missing_ok=True)
    return found


# ---------------------------------------------------------------------------
# تجميع المربعات الثابتة
# ---------------------------------------------------------------------------
def _iou(a, b) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _is_edge_box(box, width: int, height: int) -> bool:
    x1, y1, x2, y2 = box
    cx = (x1 + x2) / 2.0
    cy = (y1 + y2) / 2.0
    return (
        cy <= height * 0.28
        or cy >= height * 0.72
        or cx <= width * 0.28
        or cx >= width * 0.72
    )


def _build_clusters(
    per_frame: list[list[_Detection]],
    width: int,
) -> list[_Cluster]:
    clusters: list[_Cluster] = []
    tolerance = max(6.0, width * 0.02)

    for frame_index, detections in enumerate(per_frame):
        for detection in detections:
            box = detection.box
            cx = (box[0] + box[2]) / 2.0
            cy = (box[1] + box[3]) / 2.0

            target: _Cluster | None = None
            for cluster in clusters:
                rep = _median_box(cluster.boxes)
                if _iou(rep, box) >= 0.30:
                    target = cluster
                    break
                ccx = (rep[0] + rep[2]) / 2.0
                ccy = (rep[1] + rep[3]) / 2.0
                if abs(ccx - cx) <= tolerance and abs(ccy - cy) <= tolerance:
                    target = cluster
                    break

            if target is None:
                target = _Cluster()
                clusters.append(target)

            target.detections.append(detection)
            target.frames.add(frame_index)

    return clusters


def _has_confident_cluster(
    per_frame: list[list[_Detection]],
    width: int,
    min_frames: int = 2,
) -> bool:
    """هل ظهرت منطقة ثابتة في عدد كافٍ من الإطارات؟ (لإيقاف الفحص مبكرًا)"""
    for cluster in _build_clusters(per_frame, width):
        if len(cluster.frames) >= min_frames:
            return True
    return False


def _cluster_detections(
    per_frame: list[list[_Detection]],
    frame_count: int,
    width: int,
    height: int,
) -> list[tuple[float, float, float, float]]:
    clusters = _build_clusters(per_frame, width)

    if frame_count <= 2:
        needed = 1
    else:
        needed = max(2, int(round(frame_count * 0.30)))

    result: list[tuple[float, float, float, float]] = []
    for cluster in clusters:
        median = _median_box(cluster.boxes)
        edge = _is_edge_box(median, width, height)
        frames = len(cluster.frames)

        if frames < needed:
            # مطابقة قوية في حافة الإطار تكفي من إطار واحد (الاسم عادةً ثابت)
            if not (cluster.strong and edge and frames >= 1):
                continue
        result.append(median)
    return result


def _overlaps(a, b) -> bool:
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _merge_boxes(boxes: list[tuple[float, float, float, float]]):
    merged: list[tuple[float, float, float, float]] = []
    for box in boxes:
        current = box
        others: list[tuple[float, float, float, float]] = []
        for existing in merged:
            if _iou(existing, current) > 0.05 or _overlaps(existing, current):
                current = (
                    min(existing[0], current[0]),
                    min(existing[1], current[1]),
                    max(existing[2], current[2]),
                    max(existing[3], current[3]),
                )
            else:
                others.append(existing)
        others.append(current)
        merged = others
    return merged


def _clamp_box(
    box: tuple[float, float, float, float],
    width: int,
    height: int,
) -> tuple[int, int, int, int] | None:
    """تحويل المربع إلى (x, y, w, h) صالح لفلتر delogo."""
    pad_x = max(3.0, width * 0.006)
    pad_y = max(3.0, height * 0.006)

    x1 = max(1.0, box[0] - pad_x)
    y1 = max(1.0, box[1] - pad_y)
    x2 = min(width - 1.0, box[2] + pad_x)
    y2 = min(height - 1.0, box[3] + pad_y)

    x = int(math.floor(x1))
    y = int(math.floor(y1))
    right = int(math.ceil(x2))
    bottom = int(math.ceil(y2))

    w = right - x
    h = bottom - y
    if w < 4 or h < 4:
        return None
    if w * h > 0.45 * width * height:
        # منطقة ضخمة = غالبًا ليست اسم مستخدم؛ نتجنب تشويه الفيديو
        return None

    w = min(w, width - 1 - x)
    h = min(h, height - 1 - y)
    if w < 4 or h < 4:
        return None
    return (x, y, w, h)


# ---------------------------------------------------------------------------
# تقدير خطورة التشويه
# ---------------------------------------------------------------------------
def _compute_risk(
    reference: Path,
    boxes: list[tuple[int, int, int, int]],
    width: int,
    height: int,
) -> tuple[float, str]:
    """تقدير 0..1 لاحتمال أن تبدو منطقة الإزالة مشوّهة."""
    if not boxes or width <= 0 or height <= 0:
        return 0.0, ""

    size_score = 0.0
    texture_score = 0.0
    center_score = 0.0
    reasons: list[str] = []

    try:
        from PIL import Image, ImageStat  # type: ignore

        with Image.open(reference) as source:
            gray = source.convert("L")
            total = float(width * height)
            for x, y, w, h in boxes:
                area_ratio = (w * h) / total
                size_score = max(size_score, min(1.0, area_ratio / 0.025))
                if area_ratio >= 0.025:
                    reasons.append("النص كبير مقارنة بالإطار")

                try:
                    crop = gray.crop((x, y, x + w, y + h))
                    stddev = ImageStat.Stat(crop).stddev[0]
                    texture_score = max(texture_score, min(1.0, stddev / 70.0))
                    if stddev >= 55:
                        reasons.append("الخلفية خلف النص معقّدة")
                except Exception:  # noqa: BLE001
                    pass

                cx = (x + w / 2.0) / width
                cy = (y + h / 2.0) / height
                if 0.30 < cx < 0.70 and 0.30 < cy < 0.70:
                    center_score = 1.0
                    reasons.append("النص في وسط الصورة")
    except Exception:  # noqa: BLE001
        total = float(width * height)
        for x, y, w, h in boxes:
            area_ratio = (w * h) / total
            size_score = max(size_score, min(1.0, area_ratio / 0.025))
            if area_ratio >= 0.025:
                reasons.append("النص كبير مقارنة بالإطار")
            cx = (x + w / 2.0) / width
            cy = (y + h / 2.0) / height
            if 0.30 < cx < 0.70 and 0.30 < cy < 0.70:
                center_score = 1.0
                reasons.append("النص في وسط الصورة")

    risk = 0.50 * size_score + 0.35 * texture_score + 0.15 * center_score

    widest = max((w / width for _x, _y, w, _h in boxes), default=0.0)
    tallest = max((h / height for _x, _y, _w, h in boxes), default=0.0)
    if widest >= 0.55:
        risk = max(risk, 0.90)
        reasons.append("النص عريض جدًا")
    if tallest >= 0.22:
        risk = max(risk, 0.85)
        reasons.append("النص طويل جدًا")

    reason = "، ".join(dict.fromkeys(reasons))
    return min(1.0, risk), reason


# ---------------------------------------------------------------------------
# رسم النص البديل (PIL) — نفس المكان والحجم واللون بأقرب خط
# ---------------------------------------------------------------------------
def _font_candidates(text: str, bold: bool = True) -> list[str]:
    configured = (config.USERNAME_FONT_FILE or "").strip()
    base = [configured] if configured else []
    arabic = bool(_ARABIC_RE.search(text or ""))
    if arabic:
        base += [
            "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
            "C:/Windows/Fonts/tahomabd.ttf" if bold else "C:/Windows/Fonts/tahoma.ttf",
            "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ]
    else:
        base += [
            "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
            "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
            "C:/Windows/Fonts/tahomabd.ttf" if bold else "C:/Windows/Fonts/tahoma.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ]
    return base


def _load_font(text: str, size: int, bold: bool = True):
    from PIL import ImageFont  # type: ignore

    for candidate in _font_candidates(text, bold):
        if candidate and Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, size)
            except Exception:  # noqa: BLE001
                continue
    try:
        return ImageFont.load_default(size=size)  # type: ignore[call-arg]
    except Exception:  # noqa: BLE001
        return ImageFont.load_default()


def _shape_text(text: str) -> str:
    """تشكيل الحروف العربية إن كانت المكتبات متوفرة."""
    if not _ARABIC_RE.search(text or ""):
        return text
    try:
        import arabic_reshaper  # type: ignore
        from bidi.algorithm import get_display  # type: ignore

        return get_display(arabic_reshaper.reshape(text))
    except Exception:  # noqa: BLE001
        return text


def _text_metrics(image, box: tuple[int, int, int, int]):
    """استنتاج لون النص وحدوده وارتفاعه وسماكته من منطقة المربع.

    المربع هنا بصيغة (x, y, w, h) كما يستخدمه delogo، لذا نحوّله إلى
    (left, upper, right, lower) قبل القص.
    """
    default = ((255, 255, 255), (0, 0, 0), 0, True)
    try:
        import numpy as np  # type: ignore

        x, y, w, h = box
        if w < 2 or h < 2:
            return default
        crop = image.crop((x, y, x + w, y + h))
        arr = np.asarray(crop.convert("RGB"), dtype=np.float32)
        if arr.size == 0:
            return default
        gray = arr.mean(axis=2)
        p5, p50, p95 = np.percentile(gray, [5, 50, 95])

        if p95 - p50 >= 40:
            threshold = max(float(np.percentile(gray, 90)), float(p50) + 55.0)
            mask = gray >= threshold
            stroke = (0, 0, 0)
        elif p50 - p5 >= 40:
            threshold = min(float(np.percentile(gray, 10)), float(p50) - 55.0)
            mask = gray <= threshold
            stroke = (255, 255, 255)
        else:
            return default

        if int(mask.sum()) < 4:
            return default
        color = tuple(int(v) for v in arr[mask].mean(axis=0))
        if sum(color) < 30 or sum(color) > 740:
            color = (255, 255, 255)

        row_counts = mask.sum(axis=1)
        min_pixels = max(2, int(mask.shape[1] * 0.08))
        rows = np.where(row_counts >= min_pixels)[0]
        text_height = int(rows[-1] - rows[0] + 1) if rows.size else 0

        runs: list[int] = []
        for row in mask:
            run = 0
            for value in row:
                if value:
                    run += 1
                elif run:
                    runs.append(run)
                    run = 0
            if run:
                runs.append(run)
        average_run = float(np.mean(runs)) if runs else 0.0
        bold = bool(text_height > 0 and (average_run / text_height) >= 0.16)
        return color, stroke, text_height, bold
    except Exception:  # noqa: BLE001
        return default


def _render_overlay(
    reference: Path,
    boxes: list[tuple[int, int, int, int]],
    width: int,
    height: int,
    replacement: str,
    dst: Path,
) -> Path | None:
    """إنشاء صورة شفافة تحتوي النص البديل في نفس مواضع الاسم الأصلي."""
    text = _shape_text((replacement or "").strip())
    if not text or not boxes:
        return None

    try:
        from PIL import Image, ImageDraw  # type: ignore

        with Image.open(reference) as source:
            base = source.convert("RGB")
    except Exception as exc:  # noqa: BLE001
        log.warning("تعذر فتح إطار مرجعي لرسم النص البديل: %s", exc)
        return None

    try:
        overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        margin = max(2, int(min(width, height) * 0.005))

        for box in boxes:
            x, y, w, h = box
            if w < 6 or h < 6:
                continue
            color, stroke, text_height, bold = _text_metrics(base, box)
            right_aligned = (x + w / 2.0) > width * 0.62

            available = (x + w - margin) if right_aligned else (width - x - margin)
            max_width = max(int(w * 0.85), int(available))

            if text_height > 4:
                size = max(10, int(text_height / 0.78))
            else:
                size = max(10, int(h * 0.62))

            chosen = None
            bbox = (0, 0, 0, 0)
            while size >= 9:
                font = _load_font(text, size, bold=bold)
                stroke_w = max(1, size // 22)
                bbox = draw.textbbox((0, 0), text, font=font, stroke_width=stroke_w)
                text_w = bbox[2] - bbox[0]
                if text_w <= max_width:
                    chosen = (font, stroke_w)
                    break
                size -= 2

            if chosen is None:
                font = _load_font(text, 9, bold=bold)
                stroke_w = 1
                bbox = draw.textbbox((0, 0), text, font=font, stroke_width=stroke_w)
                chosen = (font, stroke_w)

            font, stroke_w = chosen
            text_w = bbox[2] - bbox[0]
            text_h = bbox[3] - bbox[1]

            if right_aligned:
                tx = x + w - text_w - bbox[0]
            else:
                tx = x - bbox[0]
            ty = y + (h - text_h) / 2.0 - bbox[1]

            draw.text(
                (tx, ty),
                text,
                font=font,
                fill=color + (255,),
                stroke_width=stroke_w,
                stroke_fill=stroke + (255,),
            )

        overlay.save(dst)
        return dst if dst.is_file() and dst.stat().st_size > 0 else None
    except Exception as exc:  # noqa: BLE001
        log.warning("تعذر رسم النص البديل: %s", exc)
        dst.unlink(missing_ok=True)
        return None


# ---------------------------------------------------------------------------
# إعادة الترميز مع delogo (ونص بديل اختياري)
# ---------------------------------------------------------------------------
async def _apply_delogo(
    video: Path,
    boxes: list[tuple[int, int, int, int]],
    width: int,
    height: int,
    duration: float,
    source_bitrate: int = 0,
    overlay: Path | None = None,
) -> Path | None:
    if not media.FFMPEG or not boxes:
        return None

    filters = [f"delogo=x={x}:y={y}:w={w}:h={h}" for x, y, w, h in boxes]
    if width % 2 or height % 2:
        filters.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")
    chain = ",".join(filters)

    dst = video.with_name(f"{video.stem}.clean.mp4")
    base = [
        media.FFMPEG,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video),
    ]

    if overlay is not None:
        base += ["-i", str(overlay)]
        filter_complex = (
            f"[0:v]{chain}[bg];"
            f"[bg][1:v]overlay=0:0:eof_action=repeat:format=auto[v]"
        )
        base += [
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
            "-map",
            "0:a?",
        ]
    else:
        base += ["-vf", chain, "-map", "0:v:0", "-map", "0:a?"]

    base += [
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
    ]
    # الحفاظ على حجم قريب من الأصل بدل تضخيمه
    if source_bitrate > 0:
        maxrate = max(300, int(source_bitrate * 1.10 / 1000))
        base += ["-maxrate", f"{maxrate}k", "-bufsize", f"{maxrate * 2}k"]
    base += ["-movflags", "+faststart"]

    timeout = min(max(300.0, duration * 4.0), 3600.0)
    attempts = [
        base + ["-c:a", "copy", str(dst)],
        base + ["-c:a", "aac", "-b:a", "128k", str(dst)],
        base + ["-an", str(dst)],
    ]

    for cmd in attempts:
        dst.unlink(missing_ok=True)
        try:
            code, _out, err = await media._run(cmd, timeout=timeout)  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            log.warning("فشل تعديل %s: %s", video.name, exc)
            continue
        if code == 0 and dst.is_file() and dst.stat().st_size > 0:
            return dst
        log.warning(
            "ffmpeg delogo فشل على %s: %s",
            video.name,
            err[-300:].decode("utf-8", "replace"),
        )

    dst.unlink(missing_ok=True)
    return None


# ---------------------------------------------------------------------------
# التحليل (بدون تعديل الفيديو)
# ---------------------------------------------------------------------------
@dataclass
class VideoAnalysis:
    video: Path
    boxes: list[tuple[int, int, int, int]] = field(default_factory=list)
    width: int = 0
    height: int = 0
    risk: float = 0.0
    risk_reason: str = ""
    texts: list[str] = field(default_factory=list)
    info: media.MediaInfo | None = None
    reason: str = ""


async def _match_detections(
    engine,
    frames: list[tuple[Path, float, float]],
    variants: set[str],
    target: str,
    progress: ProgressCallback | None,
    *,
    early_exit: bool = False,
    width: int = 0,
) -> list[list[_Detection]]:
    per_frame: list[list[_Detection]] = []
    total = len(frames)
    for index, (path, scale_x, scale_y) in enumerate(frames):
        # تحديث الحالة ليس ضروريًا لكل إطار؛ كل إطارين يكفي ويوفّر وقت إرسال API
        if progress is not None and (index % 2 == 0 or index == total - 1):
            await progress(
                f"🧼 فحص الإطارات لكشف اسم المستخدم ({index + 1}/{total})…"
            )
        found: list[_Detection] = []
        for box, text, score in await _ocr_frame(engine, path):
            matched, strong = _match_strength(text, variants, target)
            if not matched and config.USERNAME_ANY_MENTION and _is_mention(text):
                matched, strong = True, True
            if not matched:
                continue
            found.append(
                _Detection(
                    box=(
                        box[0] * scale_x,
                        box[1] * scale_y,
                        box[2] * scale_x,
                        box[3] * scale_y,
                    ),
                    text=text,
                    score=score,
                    strong=strong,
                )
            )
        per_frame.append(found)

        # إيقاف مبكر: إن ظهر الاسم في إطارين على الأقل فلا داعي لفحص الباقي
        if early_exit and len(per_frame) >= 3 and found:
            if _has_confident_cluster(per_frame, width, min_frames=2):
                log.debug(
                    "اكتشاف مبكر لاسم المستخدم بعد %s إطار من %s", len(per_frame), total
                )
                break
    return per_frame


async def analyze_video(
    video: Path,
    variants: set[str],
    engine,
    progress: ProgressCallback | None = None,
    target: str = "",
) -> VideoAnalysis | None:
    """فحص فيديو واحد واستخراج مناطق الاسم وخطورة التشويه دون تعديله."""
    if not variants and not target:
        return None

    try:
        info = await media.probe(video)
    except Exception:  # noqa: BLE001
        return None

    if not info.has_video or info.width <= 0 or info.height <= 0:
        return None
    if info.duration <= 0:
        return None
    if info.duration > config.USERNAME_MAX_SECONDS:
        return VideoAnalysis(
            video=video,
            info=info,
            reason="too-long",
        )

    work_dir = video.parent / f".ocr-{video.stem}"
    shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        times = _timestamps(info.duration, config.USERNAME_OCR_FRAMES)

        reference = work_dir / "reference.jpg"
        ref_time = times[len(times) // 2]

        async def _grab_scaled(index: int, seconds: float):
            path = work_dir / f"frame_{index:02d}.jpg"
            if not await _grab_frame(video, seconds, path, config.USERNAME_OCR_WIDTH):
                return None
            frame_w, frame_h = _image_size(path)
            if frame_w <= 0 or frame_h <= 0:
                return None
            return (path, frame_w, frame_h)

        # نجلب الإطار المرجعي والدفعة الأولى معًا بالتوازي (بحد أقصى 3 عمليات ffmpeg)
        grab_sem = asyncio.Semaphore(3)

        async def _bounded(coro):
            async with grab_sem:
                return await coro

        async def _grab_batch(indices):
            return await asyncio.gather(
                *(_bounded(_grab_scaled(i, times[i])) for i in indices)
            )

        first_n = min(4, len(times))
        ref_ok, first_items = await asyncio.gather(
            _bounded(_grab_frame(video, ref_time, reference, None)),
            _grab_batch(range(first_n)),
        )
        if not ref_ok:
            return VideoAnalysis(video=video, info=info, reason="no-frame")
        display_w, display_h = _image_size(reference)
        if display_w <= 0 or display_h <= 0:
            return VideoAnalysis(video=video, info=info, reason="no-frame")

        def _to_frames(items):
            frames: list[tuple[Path, float, float]] = []
            for item in items:
                if item is None:
                    continue
                path, frame_w, frame_h = item
                frames.append((path, display_w / frame_w, display_h / frame_h))
            return frames

        first_frames = _to_frames(first_items)
        if not first_frames:
            return VideoAnalysis(video=video, info=info, reason="no-frame")

        per_frame = await _match_detections(
            engine,
            first_frames,
            variants,
            target,
            progress,
            early_exit=True,
            width=display_w,
        )

        # لا نجلب بقية الإطارات إلا إذا لم نطمئن بعد إلى وجود الاسم
        if first_n < len(times) and not _has_confident_cluster(
            per_frame, display_w, min_frames=2
        ):
            rest_frames = _to_frames(
                await _grab_batch(range(first_n, len(times)))
            )
            if rest_frames:
                per_frame.extend(
                    await _match_detections(
                        engine,
                        rest_frames,
                        variants,
                        target,
                        progress,
                        early_exit=True,
                        width=display_w,
                    )
                )

        clusters = _cluster_detections(
            per_frame, len(per_frame), display_w, display_h
        )

        # فحص مكّبر للزوايا إن لم نجد شيئًا في الفحص الكامل
        if not clusters:
            if progress is not None:
                await progress("🔎 فحص مكّبر لزوايا الإطار…")
            zoom = await _ocr_zoom_corners(engine, reference, display_w, display_h)
            zoom_detections: list[_Detection] = []
            for box, text, score in zoom:
                matched, strong = _match_strength(text, variants, target)
                if not matched and config.USERNAME_ANY_MENTION and _is_mention(text):
                    matched, strong = True, True
                if matched:
                    zoom_detections.append(
                        _Detection(box=box, text=text, score=score, strong=strong)
                    )
            if zoom_detections:
                per_frame.append(zoom_detections)
                clusters = _cluster_detections(
                    per_frame, len(per_frame), display_w, display_h
                )

        if not clusters:
            return VideoAnalysis(video=video, info=info, reason="not-found")

        merged = _merge_boxes(clusters)
        boxes: list[tuple[int, int, int, int]] = []
        texts: list[str] = []
        for box in merged:
            clamped = _clamp_box(box, display_w, display_h)
            if clamped is not None:
                boxes.append(clamped)
        for detections in per_frame:
            for detection in detections:
                if detection.text not in texts:
                    texts.append(detection.text)

        if not boxes:
            return VideoAnalysis(video=video, info=info, reason="not-found")

        risk, reason = _compute_risk(reference, boxes, display_w, display_h)
        log.info(
            "تحليل %s: %s منطقة، خطورة=%.2f (%s)، نصوص=%s",
            video.name,
            len(boxes),
            risk,
            reason or "-",
            texts[:4],
        )
        return VideoAnalysis(
            video=video,
            boxes=boxes,
            width=display_w,
            height=display_h,
            risk=risk,
            risk_reason=reason,
            texts=texts,
            info=info,
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


async def apply_removal(
    analysis: VideoAnalysis,
    replacement: str | None = None,
    progress: ProgressCallback | None = None,
) -> Path | None:
    """إزالة الاسم (ووضع نص بديل إن طُلب). يعيد الملف الجديد أو None."""
    if not analysis.boxes:
        return None

    info = analysis.info
    if info is None:
        info = await media.probe(analysis.video)

    overlay: Path | None = None
    reference: Path | None = None
    work: Path | None = None

    try:
        if replacement and replacement.strip():
            if progress is not None:
                await progress("✍️ تجهيز النص البديل بنفس المكان والخط…")
            work = analysis.video.parent / f".overlay-{analysis.video.stem}"
            shutil.rmtree(work, ignore_errors=True)
            work.mkdir(parents=True, exist_ok=True)
            reference = work / "reference.jpg"
            mid = (info.duration / 2.0) if info.duration > 0 else 0.0
            if not await _grab_frame(analysis.video, mid, reference, None):
                reference = None
            else:
                overlay = _render_overlay(
                    reference,
                    analysis.boxes,
                    analysis.width,
                    analysis.height,
                    replacement,
                    work / "overlay.png",
                )

        if progress is not None:
            await progress("🧼 جاري إزالة اسم المستخدم من الفيديو…")

        cleaned = await _apply_delogo(
            analysis.video,
            analysis.boxes,
            analysis.width,
            analysis.height,
            info.duration,
            info.bitrate,
            overlay=overlay,
        )
        if cleaned is None:
            return None

        cleaned_info = await media.probe(cleaned)
        if cleaned_info.duration <= 0 or abs(
            cleaned_info.duration - info.duration
        ) > max(2.0, info.duration * 0.05):
            cleaned.unlink(missing_ok=True)
            return None

        log.info(
            "تم تعديل %s (%s منطقة%s)",
            analysis.video.name,
            len(analysis.boxes),
            "، مع نص بديل" if overlay is not None else "",
        )
        return cleaned
    finally:
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# الواجهة القديمة (متوافقة مع الاختبارات)
# ---------------------------------------------------------------------------
@dataclass
class RemovalReport:
    videos: list[Path] = field(default_factory=list)
    removed: int = 0
    boxes: int = 0
    skipped_long: int = 0
    note: str = ""

    @property
    def applied(self) -> bool:
        return self.removed > 0


async def remove_usernames(
    videos: list[Path],
    uploader: str = "",
    uploader_id: str = "",
    url: str = "",
    progress: ProgressCallback | None = None,
    replacement: str | None = None,
    title: str = "",
    channel: str = "",
) -> RemovalReport:
    """إزالة اسم صاحب المحتوى من كل فيديو. لا يرفع استثناءً أبدًا."""
    report = RemovalReport(videos=list(videos))
    if not videos or not config.REMOVE_USERNAME:
        return report

    variants = username_variants(uploader, uploader_id, url, title=title, channel=channel)
    if not variants:
        report.note = "no-username"
        return report

    engine = await asyncio.to_thread(_load_engine)
    if engine is None:
        report.note = "no-ocr"
        return report

    for index, video in enumerate(list(videos)):
        try:
            analysis = await analyze_video(video, variants, engine, progress)
            if analysis is None:
                continue
            if analysis.reason == "too-long":
                report.skipped_long += 1
                log.info(
                    "تخطي إزالة الاسم من %s: المدة %.0f ثانية تتجاوز الحد",
                    video.name,
                    analysis.info.duration if analysis.info else 0,
                )
                continue
            if not analysis.boxes:
                continue
            cleaned = await apply_removal(
                analysis, replacement=replacement, progress=progress
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("تعذرت إزالة اسم المستخدم من %s", video)
            continue

        if cleaned is None:
            continue

        try:
            video.unlink(missing_ok=True)
        except OSError as exc:
            log.debug("تعذر حذف الفيديو الأصلي %s: %s", video, exc)

        report.videos[index] = cleaned
        report.removed += 1
        report.boxes += len(analysis.boxes)

    if not report.removed and report.skipped_long:
        report.note = "too-long"

    return report
