"""Disk-backed jobs with a small pool of conversion workers.

Uploads and converted images live on disk.  Only fixed-size buffers are held
in memory (upload chunks, copy chunks, GPT areas), so peak memory does not
depend on the disk length.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .converter import (
    SourceAnalysis,
    analyze_source,
    convert,
    plan_layout,
    verify_output,
)
from .errors import ConversionError, VerificationError
from .export import (
    ExportPlan,
    ExportSource,
    plan_partition_export,
)

JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
META_NAME = "job.json"
SOURCE_NAME = "source.img"
OUTPUT_NAME = "output-4k.img"


def _export_filename(source_name: str | None, partition_index: int) -> str:
    stem = (source_name or "disk.img").replace("\\", "/").rsplit("/", 1)[-1].strip()
    stem = "".join(ch for ch in stem if ch.isprintable() and ch not in '"')
    if stem.lower().endswith(".img"):
        stem = stem[:-4]
    stem = stem[:100] or "disk"
    return f"{stem}-part{partition_index}.4k.img"


@dataclass
class Job:
    id: str
    dir: Path
    created_at: float
    updated_at: float
    status: str = "queued"  # queued|converting|verifying|done|failed
    copied_bytes: int = 0
    total_bytes: int = 0
    analysis: SourceAnalysis | None = None
    result_checks: list[str] = field(default_factory=list)
    error: dict[str, Any] | None = None
    source_name: str | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    discarded: bool = False

    @property
    def source_path(self) -> Path:
        return self.dir / SOURCE_NAME

    @property
    def output_path(self) -> Path:
        return self.dir / OUTPUT_NAME

    def export_info(self, partition_index: int) -> dict[str, Any] | None:
        """Plan metadata for one partition export, or ``None`` if unavailable."""
        if self.analysis is None:
            return None
        try:
            plan = plan_partition_export(self.id, self.analysis, partition_index)
        except ConversionError:
            return None
        return plan.metadata()

    def summary(self) -> dict[str, Any] | None:
        a = self.analysis
        if a is None:
            return None
        return {
            "disk_size": a.disk_size,
            "source_sector_size": a.sector_size,
            "target_sector_size": 4096,
            "disk_guid": a.disk_guid,
            "used_gpt_copy": a.used_copy,
            "copies": [
                {
                    "which": c.which,
                    "valid": c.valid,
                    "used": c.used,
                    "problems": c.problems,
                    "detail": c.detail,
                }
                for c in a.copies
            ],
            "partitions": [
                {
                    "index": p.index,
                    "type_guid": p.type_guid_text,
                    "unique_guid": p.unique_guid_text,
                    "attributes": f"0x{p.attributes:016x}",
                    "name": p.name,
                    "start_lba_4k": p.start_lba // 8,
                    "end_lba_4k": (p.end_lba + 1) // 8 - 1,
                    "byte_start": p.start_lba * a.sector_size,
                    "byte_end_exclusive": (p.end_lba + 1) * a.sector_size,
                    "export_url": f"/api/jobs/{self.id}/partitions/{p.index}/export",
                    "export": self.export_info(p.index),
                }
                for p in a.partitions
            ],
            "warnings": a.warnings,
        }

    def public(self) -> dict[str, Any]:
        done = self.status == "done"
        return {
            "id": self.id,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "copied_bytes": self.copied_bytes,
            "total_bytes": self.total_bytes,
            "source_name": self.source_name,
            "summary": self.summary(),
            "checks": self.result_checks,
            "error": self.error,
            "download_url": f"/api/jobs/{self.id}/download" if done else None,
            "output_size": self.total_bytes if done else None,
        }


class JobManager:
    def __init__(
        self,
        work_dir: Path,
        *,
        max_concurrent_jobs: int = 1,
        chunk_size: int = 4 * 1024 * 1024,
        job_ttl_seconds: int = 3600,
        max_upload_bytes: int = 8 * 1024**3,
    ) -> None:
        self.work_dir = Path(work_dir)
        self.chunk_size = chunk_size
        self.job_ttl_seconds = job_ttl_seconds
        self.max_upload_bytes = max_upload_bytes
        self._jobs: dict[str, Job] = {}
        self._lock = threading.RLock()
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._stop = threading.Event()
        # Number of exports currently streaming per job.  A job directory is
        # kept on disk while an export is in flight, even after the job has
        # been deleted from the in-memory table or expired.
        self._active_exports: dict[str, int] = {}
        self._pending_cleanup: dict[str, Job] = {}
        self._workers = [
            threading.Thread(
                target=self._worker, name=f"migrate-worker-{i}", daemon=True
            )
            for i in range(max(1, max_concurrent_jobs))
        ]
        self._reaper: threading.Thread | None = None
        self._started = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._started:
            return
        self.work_dir.mkdir(parents=True, exist_ok=True)
        # Job directories and the sample cache share this volume; the cache is
        # not named like a job, so orphan purging ignores it automatically.
        self._purge_orphans()
        for worker in self._workers:
            worker.start()
        self._reaper = threading.Thread(
            target=self._reap_loop, name="migrate-reaper", daemon=True
        )
        self._reaper.start()
        self._started = True

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            job.cancel.set()
        for _ in self._workers:
            self._queue.put(None)
        for worker in self._workers:
            worker.join(timeout=5)
        if self._reaper is not None:
            self._reaper.join(timeout=5)
        self._started = False

    def _purge_orphans(self) -> None:
        """Drop directories left behind by a previous process."""
        if not self.work_dir.is_dir():
            return
        with self._lock:
            active = set(self._jobs)
            active.update(self._active_exports)
            active.update(self._pending_cleanup)
        cutoff = time.time() - self.job_ttl_seconds
        for child in self.work_dir.iterdir():
            if not child.is_dir() or not JOB_ID_RE.match(child.name):
                continue
            if child.name in active:
                continue
            try:
                old = child.stat().st_mtime < cutoff
            except OSError:
                continue
            if old:
                shutil.rmtree(child, ignore_errors=True)

    def _reap_loop(self) -> None:
        interval = min(60.0, max(5.0, self.job_ttl_seconds / 4))
        while not self._stop.wait(interval):
            cutoff = time.time() - self.job_ttl_seconds
            with self._lock:
                expired = [
                    job.id
                    for job in self._jobs.values()
                    if job.updated_at < cutoff and job.status in ("done", "failed")
                ]
            for job_id in expired:
                self.discard(job_id)
            if self._stop.is_set():
                return
            self._purge_orphans()

    # -- job creation ------------------------------------------------------

    def create_job(self, source_name: str | None) -> Job:
        job_id = uuid.uuid4().hex
        job_dir = self.work_dir / job_id
        job_dir.mkdir()
        now = time.time()
        job = Job(
            id=job_id,
            dir=job_dir,
            created_at=now,
            updated_at=now,
            source_name=source_name,
        )
        with self._lock:
            self._jobs[job_id] = job
        return job

    def analyze(self, job: Job) -> SourceAnalysis:
        try:
            size = job.source_path.stat().st_size
        except OSError as exc:
            raise ConversionError(
                "SOURCE_MISSING", "源文件已不存在，任务无法继续。", status_code=410
            ) from exc
        job.total_bytes = size
        fd = os.open(job.source_path, os.O_RDONLY)
        try:
            analysis = analyze_source(fd, size)
        finally:
            os.close(fd)
        job.analysis = analysis
        return analysis

    def enqueue(self, job: Job) -> None:
        self._queue.put(job.id)

    def discard(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.pop(job_id, None)
            if job is not None:
                # Stop the worker from (re)creating files in this directory.
                job.discarded = True
            active = self._active_exports.get(job_id, 0)
            if job is not None and active > 0:
                # An export is still streaming from this directory.  Keep it
                # until that transfer finishes; the open file descriptor would
                # survive an unlink, but deferring also keeps the on-disk
                # cleanup atomic and portable.
                self._pending_cleanup[job_id] = job
                job = None
        if job is not None:
            job.cancel.set()
            self._remove_job_dir(job)

    @staticmethod
    def _remove_job_dir(job: Job) -> None:
        for _ in range(5):
            try:
                shutil.rmtree(job.dir)
                return
            except FileNotFoundError:
                return
            except OSError:
                # A concurrent meta write may still be settling; retry briefly.
                time.sleep(0.02)
        shutil.rmtree(job.dir, ignore_errors=True)

    # -- partition export --------------------------------------------------

    def open_export(
        self, job_id: str, partition_index: int, *, chunk_size: int | None = None
    ) -> ExportSource:
        """Pin the produced image and prepare a streaming partition export.

        The returned source owns a file descriptor; callers must close it (the
        HTTP response does so when the transfer ends).  Raises
        :class:`ConversionError` with the job-level error codes used by the
        download endpoint.
        """
        job = self.get(job_id)
        if job is None:
            raise ConversionError(
                "JOB_NOT_FOUND",
                "找不到该任务，可能已过期或被删除。",
                status_code=404,
            )
        if job.status != "done" or job.error is not None:
            raise ConversionError(
                "NOT_READY",
                "任务尚未完成或未通过复验，暂不可导出分区。",
                status_code=409,
            )
        if job.analysis is None:
            raise ConversionError(
                "EXPORT_UNAVAILABLE",
                "任务缺少源盘分析结果，无法导出分区。",
                status_code=409,
            )
        try:
            fd = os.open(job.output_path, os.O_RDONLY)
        except FileNotFoundError:
            raise ConversionError(
                "OUTPUT_MISSING", "成品文件已不存在（可能已过期）。", status_code=410
            ) from None
        try:
            plan: ExportPlan = plan_partition_export(
                job.id, job.analysis, partition_index
            )
            expected = job.analysis.disk_size
            actual = os.fstat(fd).st_size
            if actual != expected:
                raise ConversionError(
                    "OUTPUT_MISSING",
                    "成品文件长度异常，无法导出分区。",
                    status_code=410,
                )
            # Record the in-flight export before releasing the lock, but only
            # while the job is still registered: discard() may run between the
            # pop and this block, in which case the directory is already gone
            # and a new export must not be admitted.
            name = _export_filename(job.source_name, partition_index)
            with self._lock:
                if self._jobs.get(job.id) is not job:
                    raise ConversionError(
                        "JOB_NOT_FOUND",
                        "找不到该任务，可能已过期或被删除。",
                        status_code=404,
                    )
                self._active_exports[job.id] = self._active_exports.get(job.id, 0) + 1
            return ExportSource(
                plan,
                fd,
                filename=name,
                chunk_size=chunk_size or self.chunk_size,
                on_close=lambda: self.release_export(job.id),
            )
        except Exception:
            os.close(fd)
            raise

    def release_export(self, job_id: str) -> None:
        """Drop one in-flight export and finish any deferred directory cleanup."""
        with self._lock:
            count = self._active_exports.get(job_id, 0)
            if count <= 1:
                self._active_exports.pop(job_id, None)
                pending = self._pending_cleanup.pop(job_id, None)
            else:
                self._active_exports[job_id] = count - 1
                pending = None
        if pending is not None:
            self._remove_job_dir(pending)

    # -- reads -------------------------------------------------------------

    def get(self, job_id: str) -> Job | None:
        if not JOB_ID_RE.match(job_id):
            return None
        with self._lock:
            return self._jobs.get(job_id)

    def snapshot(self, job_id: str) -> dict[str, Any] | None:
        job = self.get(job_id)
        if job is None:
            return None
        with self._lock:
            return job.public()

    def cancel(self, job_id: str) -> dict[str, Any] | None:
        job = self.get(job_id)
        if job is None:
            return None
        job.cancel.set()
        with self._lock:
            if job.status in ("queued", "converting", "verifying"):
                job.status = "failed"
                job.error = {
                    "code": "CANCELLED",
                    "message": "任务已取消。",
                    "details": {},
                }
                job.output_path.unlink(missing_ok=True)
                self._write_meta(job)
            return job.public()

    # -- worker ------------------------------------------------------------

    def _worker(self) -> None:
        while True:
            job_id = self._queue.get()
            if job_id is None:
                return
            job = self.get(job_id)
            if job is None:
                continue
            if job.cancel.is_set():
                # Cancelled while queued: the API already set the failure state.
                self._write_meta(job)
                continue
            try:
                self._run_job(job)
            except ConversionError as exc:
                if job.status != "done":
                    self._fail(job, exc.code, exc.message, exc.details)
            except VerificationError as exc:
                self._fail(
                    job,
                    "OUTPUT_VERIFICATION_FAILED",
                    "成品复验未通过，已删除输出文件，拒绝提供下载。",
                    {"reasons": exc.reasons},
                )
            except Exception as exc:  # pragma: no cover - defensive
                self._fail(
                    job,
                    "INTERNAL_ERROR",
                    "转换过程中发生内部错误，输出已丢弃。",
                    {"type": type(exc).__name__},
                )
            finally:
                if job.status != "done" and not job.discarded:
                    job.output_path.unlink(missing_ok=True)
                self._write_meta(job)

    def _run_job(self, job: Job) -> None:
        analysis = job.analysis
        if analysis is None:
            analysis = self.analyze(job)
        plan_layout(analysis)  # fail before creating any output
        self._set_status(job, "converting")

        last_write = time.monotonic()

        def progress(copied: int) -> None:
            nonlocal last_write
            if job.cancel.is_set():
                raise ConversionError(
                    "CANCELLED", "任务已取消。", status_code=409
                )
            job.copied_bytes = copied
            now = time.monotonic()
            if now - last_write >= 1.0:
                last_write = now
                job.updated_at = time.time()
                self._write_meta(job)

        src_fd = os.open(job.source_path, os.O_RDONLY)
        try:
            dst_fd = os.open(
                job.output_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600
            )
            try:
                result = convert(
                    src_fd,
                    dst_fd,
                    analysis,
                    chunk_size=self.chunk_size,
                    progress=progress,
                )
            finally:
                os.close(dst_fd)
        finally:
            os.close(src_fd)

        if job.cancel.is_set() or job.status == "failed":
            # Cancelled during the copy; the API already explained it.
            raise ConversionError("CANCELLED", "任务已取消。", status_code=409)

        self._set_status(job, "verifying")
        job.copied_bytes = analysis.disk_size
        self._write_meta(job)

        checks = verify_output(str(job.output_path), result)
        job.result_checks = checks
        job.source_path.unlink(missing_ok=True)
        self._set_status(job, "done")
        job.updated_at = time.time()
        self._write_meta(job)

    def _set_status(self, job: Job, status: str) -> None:
        job.status = status
        job.updated_at = time.time()
        self._write_meta(job)

    def _fail(
        self,
        job: Job,
        code: str,
        message: str,
        details: dict[str, Any] | None,
    ) -> None:
        job.error = {
            "code": code,
            "message": message,
            "details": details or {},
        }
        job.status = "failed"
        job.updated_at = time.time()
        job.output_path.unlink(missing_ok=True)
        self._write_meta(job)

    def _write_meta(self, job: Job) -> None:
        if job.discarded:
            return
        meta = job.public()
        meta["version"] = 1
        path = job.dir / META_NAME
        tmp = job.dir / (META_NAME + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(meta, handle, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except FileNotFoundError:
            pass  # job was deleted concurrently


def make_job_manager(settings: Any) -> JobManager:
    return JobManager(
        settings.work_dir,
        max_concurrent_jobs=settings.max_concurrent_jobs,
        chunk_size=settings.copy_chunk_bytes,
        job_ttl_seconds=settings.job_ttl_seconds,
        max_upload_bytes=settings.max_upload_bytes,
    )
