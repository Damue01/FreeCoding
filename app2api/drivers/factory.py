from __future__ import annotations

from ..config import Settings
from .base import AutomationDriver, DriverError
from .mock import MockDriver


def build_driver(settings: Settings, job_id: str | None = None) -> AutomationDriver:
    if settings.driver == "mock":
        return MockDriver()
    if settings.driver == "vivo_adb":
        from .vivo_adb import VivoAdbDriver

        artifact_dir = settings.artifact_dir / (job_id or "manual")
        return VivoAdbDriver(
            serial=settings.adb_serial,
            adb_path=settings.adb_path,
            template_root=settings.template_root,
            artifact_dir=artifact_dir,
            window_process=settings.vivo_window_process,
            window_title_re=settings.vivo_window_title_re,
            clipboard_sync_seconds=settings.vivo_clipboard_sync_seconds,
        )
    raise DriverError(f"未知驱动 {settings.driver!r}；当前可用值：mock、vivo_adb")
