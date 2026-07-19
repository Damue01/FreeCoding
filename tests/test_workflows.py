from __future__ import annotations

from app2api.drivers.base import DriverError
from app2api.drivers.mock import MockDriver
from app2api.workflows import WorkflowContext, _select_answer, run_workflow


class FlakyMockDriver(MockDriver):
    def __init__(self) -> None:
        super().__init__()
        self.tap_attempts = 0

    async def tap(self, x: int, y: int, label: str = "") -> None:
        self.tap_attempts += 1
        if self.tap_attempts == 1:
            raise DriverError("transient click failure")
        await super().tap(x, y, label)


class OcrFirstMockDriver(MockDriver):
    def __init__(self) -> None:
        super().__init__()
        self.control_reads = 0

    async def read_controls(self) -> list[str]:
        self.control_reads += 1
        if self.control_reads < 3:
            return []
        return [self.question, "这是完整的控件正文回答"]

    async def read_ocr(self, region=None) -> list[str]:
        return ["OCR 尾部残句"]


class ControlOnlyMockDriver(MockDriver):
    async def read_clipboard_answer(self) -> str | None:
        return None

    async def read_controls(self) -> list[str]:
        return [self.question, "控件回复已完成"]


async def test_navigation_retries_and_answer_stability():
    events = []

    async def emit(job_id, kind, message, **data):
        events.append((kind, message, data))

    driver = FlakyMockDriver()
    context = WorkflowContext(
        job_id="retry-test",
        question="状态机测试",
        driver=driver,
        emit=emit,
        config={
            "app_id": "test.app",
            "question_channel": "text",
            "navigation": [
                {
                    "action": "tap",
                    "x": 100,
                    "y": 200,
                    "label": "易失败入口",
                    "retries": 1,
                    "retry_delay": 0,
                }
            ],
            "response_initial_wait_seconds": 0,
            "response_timeout_seconds": 2,
            "response_poll_interval": 0,
            "response_stable_polls": 2,
            "extraction": ["control"],
        },
    )

    answer = await run_workflow(context)

    assert answer.extraction == "mock"
    assert "状态机测试" in answer.text
    assert driver.tap_attempts == 2
    assert any(kind == "retry" for kind, _, _ in events)
    click_labels = [data.get("label") for kind, _, data in events if kind == "click"]
    assert "问小团输入框" in click_labels
    candidates = [data for kind, _, data in events if kind == "recognition"]
    assert candidates and candidates[-1]["required"] == 2


def test_select_answer_ignores_status_bar_clock():
    config = {
        "response_min_chars": 4,
        "ignore_patterns": [r"^\d{1,2}:\d{2}$", r"^\d{1,3}%$"],
    }
    assert (
        _select_answer(
            ["15:53", "徐汇滨江很适合傍晚散步。"],
            "推荐一个公园",
            set(),
            config,
        )
        == "徐汇滨江很适合傍晚散步。"
    )


async def test_control_has_priority_over_early_ocr_candidate():
    events = []

    async def emit(job_id, kind, message, **data):
        events.append((kind, message, data))

    context = WorkflowContext(
        job_id="control-priority",
        question="连续对话测试",
        driver=OcrFirstMockDriver(),
        emit=emit,
        config={
            "app_id": "test.app",
            "question_channel": "text",
            "response_initial_wait_seconds": 0,
            "response_timeout_seconds": 2,
            "response_poll_interval": 0,
            "response_stable_polls": 2,
            "visual_fallback_delay_seconds": 1,
            "extraction": ["control", "ocr"],
        },
    )
    answer = await run_workflow(context)
    assert answer.text == "这是完整的控件正文回答"
    assert answer.extraction == "mock"


async def test_clipboard_preserves_markdown_without_stability_rewrite():
    events = []

    async def emit(job_id, kind, message, **data):
        events.append((kind, message, data))

    context = WorkflowContext(
        job_id="clipboard-markdown",
        question="保留格式",
        driver=MockDriver(),
        emit=emit,
        config={
            "app_id": "test.app",
            "question_channel": "text",
            "response_initial_wait_seconds": 0,
            "response_timeout_seconds": 2,
            "response_poll_interval": 0,
            "response_stable_polls": 2,
            "extraction": ["clipboard", "control", "ocr"],
        },
    )
    answer = await run_workflow(context)
    assert answer.text == "## 模拟应用回复\n\n已收到：保留格式"
    assert answer.extraction == "mock"
    assert any(
        kind == "click" and data.get("label") == "复制回复" for kind, _, data in events
    )


async def test_control_can_finish_after_clipboard_preference_window():
    async def emit(job_id, kind, message, **data):
        return None

    context = WorkflowContext(
        job_id="control-after-clipboard",
        question="控件提取测试",
        driver=ControlOnlyMockDriver(),
        emit=emit,
        config={
            "app_id": "test.app",
            "question_channel": "text",
            "response_initial_wait_seconds": 0,
            "response_timeout_seconds": 2,
            "response_poll_interval": 0,
            "response_stable_polls": 2,
            "response_use_baseline": False,
            "control_stable_polls": 1,
            "clipboard_preference_delay_seconds": 0,
            "extraction": ["clipboard", "control"],
        },
    )

    answer = await run_workflow(context)

    assert answer.text == "控件回复已完成"
