from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .drivers.base import DriverError


@dataclass(slots=True)
class OcrLine:
    text: str
    confidence: float
    box: tuple[int, int, int, int] | None = None


def _classic_lines(value: Any) -> Iterable[OcrLine]:
    """Flatten PaddleOCR 2.x results while ignoring unrelated list nodes."""
    if isinstance(value, (list, tuple)):
        if (
            len(value) == 2
            and isinstance(value[0], (list, tuple))
            and isinstance(value[1], (list, tuple))
            and len(value[1]) >= 2
            and isinstance(value[1][0], str)
        ):
            points = value[0]
            xs = [int(point[0]) for point in points]
            ys = [int(point[1]) for point in points]
            yield OcrLine(
                text=value[1][0].strip(),
                confidence=float(value[1][1]),
                box=(min(xs), min(ys), max(xs), max(ys)),
            )
            return
        for child in value:
            yield from _classic_lines(child)


class PaddleOcrEngine:
    def __init__(self, language: str = "ch") -> None:
        self.language = language
        self._engine: Any = None

    def _get_engine(self):
        if self._engine is not None:
            return self._engine
        try:
            from paddleocr import PaddleOCR
        except ImportError as exc:
            raise DriverError("PaddleOCR 未安装，请执行 pip install -e .[ocr]") from exc
        try:
            self._engine = PaddleOCR(
                use_angle_cls=True, lang=self.language, show_log=False
            )
        except TypeError:
            self._engine = PaddleOCR(lang=self.language)
        return self._engine

    def recognize(self, image: Any) -> list[OcrLine]:
        engine = self._get_engine()
        if hasattr(engine, "ocr"):
            try:
                result = engine.ocr(image, cls=True)
            except TypeError:
                result = engine.ocr(image)
            lines = [line for line in _classic_lines(result) if line.text]
            if lines:
                return lines
        raise DriverError("PaddleOCR 返回了无法解析的结果格式")
