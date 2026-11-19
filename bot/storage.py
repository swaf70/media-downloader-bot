"""تخزين مؤقت للملفات على السحابة مع حذف تلقائي بعد مدة محددة (3 ساعات افتراضيًا)."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .logger import get_logger

log = get_logger(__name__)


@dataclass
class Job:
    """مهمة تحميل واحدة (رابط واحد) وما نتج عنها من ملفات."""

    id: str
    user_id: int
    chat_id: int
    url: str
    platform: str
    title: str = ""
    created_at: float = field(default_factory=time.time)
    files: list[str] = field(default_factory=list)
    active: bool = False  # لا يُحفظ في الفهرس؛ يمنع الحذف أثناء المعالجة

    @property
    def directory(self) -> Path:
        return config.JOBS_DIR / self.id

    @property
    def paths(self) -> list[Path]:
        return [self.directory / name for name in self.files]

    @property
    def size(self) -> int:
        total = 0
        for path in self.paths:
            try:
                total += path.stat().st_size
            except OSError:
                continue
        return total

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "chat_id": self.chat_id,
            "url": self.url,
            "platform": self.platform,
            "title": self.title,
            "created_at": self.created_at,
            "files": list(self.files),
        }

    @classmethod
    def from_dict(cls, job_id: str, data: dict) -> "Job":
        return cls(
            id=job_id,
            user_id=int(data.get("user_id", 0)),
            chat_id=int(data.get("chat_id", 0)),
            url=str(data.get("url", "")),
            platform=str(data.get("platform", "generic")),
            title=str(data.get("title", "")),
            created_at=float(data.get("created_at", time.time())),
            files=[str(name) for name in data.get("files", [])],
        )


def dir_size(path: Path) -> int:
    """حجم مجلد كامل بالبايت."""
    total = 0
    if not path.exists():
        return 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


class Storage:
    """مدير التخزين المؤقت؛ آمن للاستخدام من حلقة الأحداث فقط."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # دورة الحياة
    # ------------------------------------------------------------------
    async def startup(self) -> None:
        async with self._lock:
            config.ensure_directories()
            self._load()
            self._reconcile()
            removed, freed = self._purge_locked()
            self._save()
        if removed:
            log.info("تم حذف %s مهمة منتهية عند بدء التشغيل (%.1f MB)", removed, freed / 1048576)
        log.info("التخزين المؤقت جاهز: %s", config.STORAGE_DIR)

    def _load(self) -> None:
        if not config.INDEX_FILE.is_file():
            return
        try:
            data = json.loads(config.INDEX_FILE.read_text(encoding="utf-8"))
            jobs = data.get("jobs", {})
            for job_id, payload in jobs.items():
                self._jobs[job_id] = Job.from_dict(job_id, payload)
        except Exception as exc:  # noqa: BLE001
            log.warning("تعذر قراءة فهرس التخزين، سيُعاد بناؤه: %s", exc)
            self._jobs.clear()

    def _reconcile(self) -> None:
        """مطابقة الفهرس مع المجلدات الفعلية على القرص."""
        if not config.JOBS_DIR.exists():
            return
        on_disk = {p.name for p in config.JOBS_DIR.iterdir() if p.is_dir()}

        for job_id in list(self._jobs):
            if job_id not in on_disk:
                self._jobs.pop(job_id, None)

        for job_id in on_disk - set(self._jobs):
            directory = config.JOBS_DIR / job_id
            try:
                created = directory.stat().st_mtime
            except OSError:
                created = time.time()
            files = [
                str(p.relative_to(directory))
                for p in directory.rglob("*")
                if p.is_file()
            ]
            self._jobs[job_id] = Job(
                id=job_id,
                user_id=0,
                chat_id=0,
                url="",
                platform="unknown",
                created_at=created,
                files=files,
            )

    def _save(self) -> None:
        try:
            config.ensure_directories()
            payload = {
                "version": 1,
                "saved_at": time.time(),
                "jobs": {job_id: job.to_dict() for job_id, job in self._jobs.items()},
            }
            tmp = config.INDEX_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, config.INDEX_FILE)
        except Exception as exc:  # noqa: BLE001
            log.warning("تعذر حفظ فهرس التخزين: %s", exc)

    # ------------------------------------------------------------------
    # العمليات
    # ------------------------------------------------------------------
    async def create_job(self, user_id: int, chat_id: int, url: str, platform: str) -> Job:
        job = Job(
            id=uuid.uuid4().hex[:16],
            user_id=user_id,
            chat_id=chat_id,
            url=url,
            platform=platform,
            active=True,
        )
        async with self._lock:
            job.directory.mkdir(parents=True, exist_ok=True)
            self._jobs[job.id] = job
            self._save()
        return job

    async def finish_job(self, job: Job, files: list[Path], title: str = "") -> None:
        async with self._lock:
            relative: list[str] = []
            for path in files:
                if not path.is_file():
                    continue
                try:
                    relative.append(str(path.relative_to(job.directory)))
                except ValueError:
                    relative.append(path.name)
            job.files = relative
            job.title = title or job.title
            job.active = False
            self._save()

    async def fail_job(self, job: Job) -> None:
        """حذف ملفات مهمة فاشلة فورًا لتوفير المساحة."""
        async with self._lock:
            job.active = False
            self._jobs.pop(job.id, None)
            self._save()
        shutil.rmtree(job.directory, ignore_errors=True)

    async def delete_job(self, job_id: str) -> bool:
        async with self._lock:
            job = self._jobs.pop(job_id, None)
            self._save()
        if job is None:
            return False
        shutil.rmtree(job.directory, ignore_errors=True)
        return True

    async def get_job(self, job_id: str) -> Job | None:
        async with self._lock:
            return self._jobs.get(job_id)

    async def purge_expired(self) -> tuple[int, int]:
        async with self._lock:
            removed, freed = self._purge_locked()
            if removed:
                self._save()
        return removed, freed

    async def purge_all(self) -> tuple[int, int]:
        """حذف كل الملفات فورًا (للمشرف)."""
        async with self._lock:
            removed = 0
            freed = 0
            for job_id, job in list(self._jobs.items()):
                if job.active:
                    continue
                freed += dir_size(job.directory)
                shutil.rmtree(job.directory, ignore_errors=True)
                self._jobs.pop(job_id, None)
                removed += 1
            self._save()
        return removed, freed

    def _purge_locked(self) -> tuple[int, int]:
        now = time.time()
        removed = 0
        freed = 0

        for job_id, job in list(self._jobs.items()):
            if job.active:
                continue
            if now - job.created_at < config.RETENTION_SECONDS:
                continue
            freed += dir_size(job.directory)
            shutil.rmtree(job.directory, ignore_errors=True)
            self._jobs.pop(job_id, None)
            removed += 1

        # مجلدات يتيمة (مثلًا بعد انهيار سابق)
        if config.JOBS_DIR.exists():
            for path in config.JOBS_DIR.iterdir():
                if not path.is_dir() or path.name in self._jobs:
                    continue
                try:
                    modified = path.stat().st_mtime
                except OSError:
                    continue
                if now - modified >= config.RETENTION_SECONDS:
                    freed += dir_size(path)
                    shutil.rmtree(path, ignore_errors=True)
                    removed += 1

        return removed, freed

    async def stats(self) -> dict:
        async with self._lock:
            jobs = list(self._jobs.values())
            total = sum(job.size for job in jobs)
            oldest = min((job.created_at for job in jobs), default=0.0)
            active = sum(1 for job in jobs if job.active)
            return {
                "jobs": len(jobs),
                "active": active,
                "bytes": total,
                "oldest": oldest,
                "retention_hours": config.RETENTION_HOURS,
                "root": str(config.STORAGE_DIR),
            }

    async def jobs_of_user(self, user_id: int) -> list[Job]:
        async with self._lock:
            return [job for job in self._jobs.values() if job.user_id == user_id]
