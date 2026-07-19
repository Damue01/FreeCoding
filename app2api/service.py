from __future__ import annotations

import asyncio
import mimetypes
from collections import OrderedDict
from contextlib import suppress
from pathlib import Path

from .config import Settings
from .drivers import build_driver
from .drivers.base import FrameCapture
from .events import EventStore
from .models import Artifact, AskRequest, Job, JobStatus, utcnow
from .workflows import WorkflowContext, load_target_config, run_workflow


class JobNotFound(KeyError):
    pass


class AutomationService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.events = EventStore(settings.event_history)
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._done: dict[str, asyncio.Event] = {}

    async def start(self) -> None:
        if self._workers:
            return
        self.settings.artifact_dir.mkdir(parents=True, exist_ok=True)
        self._workers = [
            asyncio.create_task(self._worker(index), name=f"automation-worker-{index}")
            for index in range(self.settings.workers)
        ]

    async def stop(self) -> None:
        for worker in self._workers:
            worker.cancel()
        for worker in self._workers:
            with suppress(asyncio.CancelledError):
                await worker
        self._workers.clear()

    async def submit(self, request: AskRequest) -> Job:
        job = Job(
            target=request.target,
            question=request.question,
            metadata=request.metadata,
        )
        self._jobs[job.id] = job
        self._done[job.id] = asyncio.Event()
        await self.events.emit(job.id, "status", "任务已进入队列", status=job.status)
        await self._queue.put(job.id)
        return job

    def get(self, job_id: str) -> Job:
        try:
            return self._jobs[job_id]
        except KeyError as exc:
            raise JobNotFound(job_id) from exc

    def list(self, limit: int = 50) -> list[Job]:
        return list(reversed(self._jobs.values()))[:limit]

    async def wait(self, job_id: str, timeout: float | None = None) -> Job:
        self.get(job_id)
        await asyncio.wait_for(self._done[job_id].wait(), timeout=timeout)
        return self.get(job_id)

    async def _emit(self, job_id: str, kind: str, message: str, **data) -> None:
        frame = data.pop("_frame", None)
        if isinstance(frame, FrameCapture):
            await self.events.set_frame(job_id, frame.body, frame.media_type)
        await self.events.emit(job_id, kind, message, **data)

    async def _worker(self, index: int) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                await self._execute(job_id, index)
            finally:
                self._queue.task_done()

    async def _execute(self, job_id: str, worker_index: int) -> None:
        job = self.get(job_id)
        job.status = JobStatus.RUNNING
        job.started_at = utcnow()
        await self._emit(
            job.id, "status", "任务开始执行", status=job.status, worker=worker_index
        )
        driver = None
        recording_started = False
        try:
            config = load_target_config(self.settings.config_dir, job.target)
            driver = build_driver(self.settings, job.id)
            if self.settings.recording_enabled and config.get("recording", True):
                try:
                    recording_started = await driver.start_recording(
                        self._job_dir(job.id) / "session.mp4",
                        self.settings.job_timeout_seconds + 10,
                    )
                    if recording_started:
                        await self._emit(job.id, "recording", "已开始录制运行画面")
                except Exception as exc:
                    await self._emit(
                        job.id,
                        "warning",
                        "录屏启动失败，任务将继续",
                        error=str(exc),
                    )
            context = WorkflowContext(
                job_id=job.id,
                question=job.question,
                driver=driver,
                emit=self._emit,
                config=config,
            )
            job.answer = await asyncio.wait_for(
                run_workflow(context), timeout=self.settings.job_timeout_seconds
            )
            job.status = JobStatus.SUCCEEDED
            await self._emit(
                job.id,
                "status",
                "任务执行成功",
                status=job.status,
                answer=job.answer.text,
            )
        except TimeoutError:
            job.status = JobStatus.TIMED_OUT
            job.error = f"任务超过 {self.settings.job_timeout_seconds:g} 秒"
            await self._emit(job.id, "error", job.error, status=job.status)
        except Exception as exc:
            job.status = JobStatus.FAILED
            job.error = str(exc)
            await self._emit(job.id, "error", job.error, status=job.status)
        finally:
            if driver is not None:
                if recording_started:
                    try:
                        recording = await driver.stop_recording()
                        if recording:
                            await self._emit(job.id, "recording", "运行画面录制完成")
                    except Exception as exc:
                        await self._emit(
                            job.id,
                            "warning",
                            "录屏停止失败",
                            error=str(exc),
                        )
                with suppress(Exception):
                    await driver.close()
            job.finished_at = utcnow()
            paths = driver.artifacts() if driver is not None else []
            await self._persist_artifacts(job, paths)
            self._done[job.id].set()

    def _job_dir(self, job_id: str) -> Path:
        return (self.settings.artifact_dir / job_id).resolve()

    async def _persist_artifacts(self, job: Job, driver_paths: list[Path]) -> None:
        directory = self._job_dir(job.id)
        directory.mkdir(parents=True, exist_ok=True)

        events_path = directory / "events.jsonl"
        event_text = (
            "\n".join(event.model_dump_json() for event in self.events.list(job.id))
            + "\n"
        )
        await asyncio.to_thread(events_path.write_text, event_text, encoding="utf-8")
        self._register_artifact(job, events_path, "event_log", "application/x-ndjson")

        frame = self.events.frame(job.id)
        if frame is not None:
            suffix = ".svg" if frame.media_type == "image/svg+xml" else ".png"
            frame_path = directory / f"final-frame{suffix}"
            await asyncio.to_thread(frame_path.write_bytes, frame.body)
            self._register_artifact(job, frame_path, "frame", frame.media_type)

        for path in driver_paths:
            resolved = path.resolve()
            try:
                resolved.relative_to(directory)
            except ValueError:
                await self._emit(
                    job.id,
                    "warning",
                    "忽略了任务目录之外的运行产物",
                    path=str(resolved),
                )
                continue
            suffix = resolved.suffix.casefold()
            kind = "video" if suffix in {".mp4", ".webm"} else "other"
            self._register_artifact(job, resolved, kind)

    def _register_artifact(
        self,
        job: Job,
        path: Path,
        kind: str,
        media_type: str | None = None,
    ) -> None:
        if not path.is_file() or any(item.name == path.name for item in job.artifacts):
            return
        guessed = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        job.artifacts.append(
            Artifact(
                name=path.name,
                kind=kind,
                media_type=media_type or guessed,
                size_bytes=path.stat().st_size,
                url=f"/v1/jobs/{job.id}/artifacts/{path.name}",
            )
        )

    def artifact_path(self, job_id: str, name: str) -> tuple[Path, Artifact]:
        job = self.get(job_id)
        artifact = next((item for item in job.artifacts if item.name == name), None)
        if artifact is None:
            raise JobNotFound(f"{job_id}/{name}")
        path = (self._job_dir(job_id) / name).resolve()
        try:
            path.relative_to(self._job_dir(job_id))
        except ValueError as exc:
            raise JobNotFound(f"{job_id}/{name}") from exc
        if not path.is_file():
            raise JobNotFound(f"{job_id}/{name}")
        return path, artifact
