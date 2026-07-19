from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class DriverError(RuntimeError):
    pass


@dataclass(slots=True)
class FrameCapture:
    body: bytes
    media_type: str = "image/png"
    width: int | None = None
    height: int | None = None


class AutomationDriver(ABC):
    """The small hardware boundary shared by Android and Windows workflows."""

    is_mock = False

    def configure(self, config: dict[str, Any]) -> None:
        return None

    def clear_action_metadata(self) -> None:
        self._last_action_metadata: dict[str, Any] = {}

    def action_metadata(self) -> dict[str, Any]:
        return dict(getattr(self, "_last_action_metadata", {}))

    def recognition_metadata(self, text: str) -> dict[str, Any]:
        return {}

    def extraction_method(self, requested: str) -> str:
        return requested

    @abstractmethod
    async def start_app(self, app_id: str) -> None: ...

    @abstractmethod
    async def tap(self, x: int, y: int, label: str = "") -> None: ...

    @abstractmethod
    async def tap_text(self, text: str, timeout: float = 10) -> None: ...

    @abstractmethod
    async def tap_template(
        self, path: str, threshold: float = 0.82, timeout: float = 10
    ) -> None: ...

    @abstractmethod
    async def input_text(self, text: str) -> None: ...

    async def keyevent(self, key: str) -> None:
        raise DriverError(f"当前驱动不支持按键事件：{key}")

    @abstractmethod
    async def read_controls(self) -> list[str]: ...

    async def read_clipboard_answer(self) -> str | None:
        return None

    @abstractmethod
    async def read_ocr(
        self, region: tuple[int, int, int, int] | None = None
    ) -> list[str]: ...

    @abstractmethod
    async def capture(self) -> FrameCapture: ...

    async def start_recording(self, output: Path, max_time: float) -> bool:
        return False

    async def stop_recording(self) -> Path | None:
        return None

    def artifacts(self) -> list[Path]:
        return []

    async def close(self) -> None:
        return None
