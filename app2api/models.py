from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


class Target(StrEnum):
    MEITUAN = "meituan"
    LINGBAO = "lingbao"
    XIAOHUOREN = "xiaohuoren"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


class AskRequest(BaseModel):
    target: Target
    question: str = Field(min_length=1, max_length=4000)
    wait: bool = False
    timeout_seconds: float | None = Field(default=None, gt=0, le=600)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Answer(BaseModel):
    text: str
    extraction: Literal["clipboard", "control", "ocr", "mock"]
    confidence: float | None = Field(default=None, ge=0, le=1)


class Artifact(BaseModel):
    name: str
    kind: Literal["event_log", "frame", "video", "other"]
    media_type: str
    size_bytes: int = Field(ge=0)
    url: str


class Job(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    target: Target
    question: str
    status: JobStatus = JobStatus.QUEUED
    answer: Answer | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[Artifact] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class Event(BaseModel):
    sequence: int
    job_id: str
    kind: str
    message: str
    at: datetime = Field(default_factory=utcnow)
    data: dict[str, Any] = Field(default_factory=dict)


class Health(BaseModel):
    ok: bool
    driver: str
    workers: int


class DiagnosticCheck(BaseModel):
    name: str
    status: Literal["ok", "warning", "error"]
    message: str
    data: dict[str, Any] = Field(default_factory=dict)


class Diagnostics(BaseModel):
    ready: bool
    driver: str
    checks: list[DiagnosticCheck]


class AndroidDevice(BaseModel):
    serial: str
    state: str
    product: str | None = None
    model: str | None = None
    device: str | None = None
    transport_id: str | None = None
    properties: dict[str, str] = Field(default_factory=dict)


class DeviceDiscovery(BaseModel):
    adb_available: bool
    adb_executable: str | None = None
    adb_source: Literal["configured", "path", "unavailable"] = "unavailable"
    adb_version: str | None = None
    devices: list[AndroidDevice] = Field(default_factory=list)
    connect_result: str | None = None
    error: str | None = None


class TargetAppStatus(BaseModel):
    target: Target
    app_id: str
    device_serial: str | None = None
    installed: bool | None = None
    package_path: str | None = None
    status: Literal["ready", "missing", "unavailable", "error"]
    message: str


class SystemPreflight(BaseModel):
    ready: bool
    device: AndroidDevice | None = None
    apps: list[TargetAppStatus] = Field(default_factory=list)
    error: str | None = None
