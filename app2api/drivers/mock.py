from __future__ import annotations

import asyncio
import html

from .base import AutomationDriver, FrameCapture


class MockDriver(AutomationDriver):
    """Deterministic visual driver for API, queue and dashboard development."""

    is_mock = True

    def __init__(self) -> None:
        self.app_id = ""
        self.question = ""
        self.answer = ""
        self.last_action = "等待任务"
        self.recognition = "reply"

    async def start_app(self, app_id: str) -> None:
        self.app_id = app_id
        self.last_action = f"启动 {app_id}"
        await asyncio.sleep(0.03)

    async def tap(self, x: int, y: int, label: str = "") -> None:
        self.last_action = f"点击 {label or f'({x}, {y})'}"
        self._last_action_metadata = {
            "x": x,
            "y": y,
            "label": label,
            "width": 540,
            "height": 960,
        }
        await asyncio.sleep(0.02)

    async def tap_text(self, text: str, timeout: float = 10) -> None:
        self.last_action = f"点击文字：{text}"
        self._last_action_metadata = {
            "x": 440,
            "y": 860,
            "label": text,
            "width": 540,
            "height": 960,
        }
        await asyncio.sleep(0.02)

    async def tap_template(
        self, path: str, threshold: float = 0.82, timeout: float = 10
    ) -> None:
        self.last_action = f"匹配并点击模板：{path}"
        self._last_action_metadata = {
            "x": 270,
            "y": 600,
            "label": path,
            "width": 540,
            "height": 960,
        }
        await asyncio.sleep(0.02)

    async def input_text(self, text: str) -> None:
        self.question = text
        self.last_action = "输入问题"
        self._last_action_metadata = {
            "x": 270,
            "y": 820,
            "label": "问小团输入框",
            "width": 540,
            "height": 960,
        }
        await asyncio.sleep(0.03)

    async def keyevent(self, key: str) -> None:
        self.last_action = f"按键：{key}"
        await asyncio.sleep(0.02)

    async def read_controls(self) -> list[str]:
        self.answer = f"模拟应用已收到问题：{self.question}"
        self.last_action = "读取回复控件"
        self.recognition = "control"
        return [self.question, self.answer]

    async def read_clipboard_answer(self) -> str | None:
        self.answer = f"## 模拟应用回复\n\n已收到：{self.question}"
        self.last_action = "点击复制并读取回复"
        self.recognition = "clipboard"
        self._last_action_metadata = {
            "x": 80,
            "y": 690,
            "label": "复制回复",
            "width": 540,
            "height": 960,
        }
        return self.answer

    async def read_ocr(self, region=None) -> list[str]:
        self.answer = f"模拟 OCR 回复：{self.question}"
        self.last_action = "OCR 识别回复"
        self.recognition = "OCR"
        return [self.answer]

    async def capture(self) -> FrameCapture:
        app = html.escape(self.app_id or "未启动")
        question = html.escape(self.question or "等待输入")
        answer = html.escape(self.answer or "等待回答")
        action = html.escape(self.last_action)
        recognition = html.escape(self.recognition)
        svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="540" height="960">
<rect width="540" height="960" fill="#17191f"/><rect x="28" y="30" width="484" height="900" rx="32" fill="#f7f7f5"/>
<rect x="28" y="30" width="484" height="82" rx="32" fill="#22252b"/><text x="60" y="82" fill="white" font-size="24">{app}</text>
<rect x="58" y="180" width="420" height="110" rx="18" fill="#e7e9ef"/><text x="78" y="220" fill="#333" font-size="19">用户提问</text>
<foreignObject x="78" y="235" width="375" height="45"><div xmlns="http://www.w3.org/1999/xhtml" style="font:17px sans-serif;color:#111;overflow:hidden">{question}</div></foreignObject>
<rect x="58" y="330" width="420" height="150" rx="18" fill="#dcefe3" stroke="#38a866" stroke-width="3"/><rect x="392" y="318" width="76" height="24" rx="6" fill="#38a866"/><text x="404" y="336" fill="white" font-size="13">{recognition}</text><text x="78" y="370" fill="#245b38" font-size="19">应用回复</text>
<foreignObject x="78" y="390" width="375" height="70"><div xmlns="http://www.w3.org/1999/xhtml" style="font:17px sans-serif;color:#111;overflow:hidden">{answer}</div></foreignObject>
<circle cx="270" cy="600" r="31" fill="none" stroke="#ff6b35" stroke-width="6"/><circle cx="270" cy="600" r="8" fill="#ff6b35"/>
<text x="58" y="850" fill="#555" font-size="17">当前操作：{action}</text></svg>"""
        return FrameCapture(svg.encode("utf-8"), "image/svg+xml", 540, 960)

    def recognition_metadata(self, text: str) -> dict[str, object]:
        return {
            "box": [58, 330, 478, 480],
            "width": 540,
            "height": 960,
            "confidence": 1.0,
        }
