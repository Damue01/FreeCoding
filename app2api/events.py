from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

from .models import Event


@dataclass(slots=True)
class Frame:
    body: bytes
    media_type: str


class EventStore:
    """In-memory event/frame store used by the API and the demo dashboard."""

    def __init__(self, history: int = 500) -> None:
        self._history = history
        self._events: dict[str, deque[Event]] = defaultdict(
            lambda: deque(maxlen=self._history)
        )
        self._frames: dict[str, Frame] = {}
        self._sequence = 0
        self._condition = asyncio.Condition()

    async def emit(self, job_id: str, kind: str, message: str, **data: Any) -> Event:
        async with self._condition:
            self._sequence += 1
            event = Event(
                sequence=self._sequence,
                job_id=job_id,
                kind=kind,
                message=message,
                data=data,
            )
            self._events[job_id].append(event)
            self._condition.notify_all()
            return event

    async def set_frame(
        self, job_id: str, body: bytes, media_type: str = "image/png"
    ) -> None:
        async with self._condition:
            self._frames[job_id] = Frame(body=body, media_type=media_type)
            self._condition.notify_all()

    def list(self, job_id: str, after: int = 0) -> list[Event]:
        return [event for event in self._events[job_id] if event.sequence > after]

    def frame(self, job_id: str) -> Frame | None:
        return self._frames.get(job_id)

    async def wait_for_events(
        self, job_id: str, after: int, timeout: float = 15
    ) -> list[Event]:
        events = self.list(job_id, after)
        if events:
            return events
        try:
            async with self._condition:
                await asyncio.wait_for(self._condition.wait(), timeout=timeout)
        except TimeoutError:
            return []
        return self.list(job_id, after)
