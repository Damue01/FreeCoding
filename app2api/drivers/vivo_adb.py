from __future__ import annotations

import asyncio
import ctypes
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from ctypes import wintypes
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..devices import locate_adb
from ..ocr import OcrLine, PaddleOcrEngine
from .base import AutomationDriver, DriverError, FrameCapture

_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
_QUERY_ID = "com.sankuai.meituan:id/tv_ai_small_tuan_query_text"
_THINKING_ID = "com.sankuai.meituan:id/small_tuan_thinking_v2_tv_title"
_SUGGESTION_ID = "com.sankuai.meituan:id/small_tuan_sug_tv"
_INPUT_IDS = {
    "com.sankuai.meituan:id/ai_search_input_bar",
    "com.sankuai.meituan:id/et_expanded_input",
}
_SEND_ID = "com.sankuai.meituan:id/iv_expanded_input_btn"
_COPY_ID = "com.sankuai.meituan:id/aixiaotuan_feedback_copy_button"
_MEITUAN_APP_ID = "com.sankuai.meituan"
_SMALL_TUAN_TITLE_ID = "com.sankuai.meituan:id/dka"


def _node_value(node: dict[str, str]) -> str:
    return (node.get("content-desc") or node.get("text") or "").strip()


def _matches_resource(
    node: dict[str, str], exact: set[str], suffixes: set[str]
) -> bool:
    resource_id = node.get("resource-id", "")
    return resource_id in exact or any(
        resource_id.endswith(value) for value in suffixes
    )


def find_xiaohuoren_card_action(
    nodes: list[dict[str, str]],
    question: str,
    card_ids: set[str] | None = None,
    card_suffixes: set[str] | None = None,
) -> dict[str, str] | None:
    """Find the newest Xiaohuoren detail card after the current user question."""

    normalized = " ".join(question.split())
    question_indexes = [
        index
        for index, node in enumerate(nodes)
        if normalized and normalized in " ".join(_node_value(node).split())
    ]
    if not question_indexes:
        return None
    start = question_indexes[-1]
    exact = card_ids or set()
    suffixes = card_suffixes or {":id/gen"}
    candidates = [
        node
        for node in nodes[start + 1 :]
        if node.get("clickable") == "true" and _matches_resource(node, exact, suffixes)
    ]
    return candidates[-1] if candidates else None


def extract_xiaohuoren_control_answer(
    nodes: list[dict[str, str]],
    question: str,
    message_ids: set[str] | None = None,
    message_suffixes: set[str] | None = None,
    max_left: int = 220,
) -> tuple[str, tuple[int, int, int, int] | None] | None:
    """Collect left-side Xiaohuoren bubbles belonging to the current question."""

    normalized = " ".join(question.split())
    question_indexes = [
        index
        for index, node in enumerate(nodes)
        if normalized and normalized in " ".join(_node_value(node).split())
    ]
    if not question_indexes:
        return None
    start = question_indexes[-1]
    exact = message_ids or set()
    suffixes = message_suffixes or {":id/kd8"}
    values: list[str] = []
    boxes: list[tuple[int, int, int, int]] = []
    for node in nodes[start + 1 :]:
        if not _matches_resource(node, exact, suffixes):
            continue
        bounds = parse_bounds(node.get("bounds", ""))
        if bounds is None or bounds[0] > max_left:
            continue
        value = " ".join(_node_value(node).split()).strip()
        if value and value not in values:
            values.append(value)
            boxes.append(bounds)
    if not values:
        return None
    bounds = (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )
    return "\n".join(values), bounds


def parse_bounds(value: str) -> tuple[int, int, int, int] | None:
    match = _BOUNDS_RE.fullmatch(value or "")
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def parse_ui_xml(xml: bytes | str) -> list[dict[str, str]]:
    data = xml if isinstance(xml, bytes) else xml.encode("utf-8")
    root = ET.fromstring(data)
    return [dict(node.attrib) for node in root.iter("node")]


def find_small_tuan_entry(
    nodes: list[dict[str, str]],
) -> dict[str, str] | None:
    """Find the bottom navigation entry without relying on screen understanding."""
    candidates: list[tuple[int, dict[str, str]]] = []
    for node in nodes:
        label = (node.get("content-desc") or node.get("text") or "").strip()
        bounds = parse_bounds(node.get("bounds", ""))
        if label != "小团" or bounds is None:
            continue
        _, y1, _, y2 = bounds
        candidates.append((y1 + y2, node))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def find_send_action(nodes: list[dict[str, str]]) -> dict[str, str] | None:
    """Return only a real send action; never confuse the voice capsule with send."""
    exact = next(
        (
            node
            for node in nodes
            if node.get("resource-id") == _SEND_ID and node.get("clickable") == "true"
        ),
        None,
    )
    if exact is not None:
        return exact
    return next(
        (
            node
            for node in nodes
            if node.get("clickable") == "true"
            and node.get("class") == "android.widget.ImageView"
            and "send" in node.get("resource-id", "").casefold()
        ),
        None,
    )


def is_functional_small_tuan_page(nodes: list[dict[str, str]]) -> bool:
    """Reject look-alike internal pages that have an input but no live session."""
    has_input = any(node.get("resource-id") in _INPUT_IDS for node in nodes)
    if not has_input:
        return False
    return any(
        node.get("resource-id")
        in {
            _QUERY_ID,
            _THINKING_ID,
            _SMALL_TUAN_TITLE_ID,
        }
        or "我是小团" in node.get("text", "")
        for node in nodes
    )


def find_copy_action(
    nodes: list[dict[str, str]],
    question: str | None = None,
) -> dict[str, str] | None:
    """Find the copy action belonging to the latest requested answer."""
    start = -1
    if question:
        normalized_question = " ".join(question.split())
        query_indexes = [
            index
            for index, node in enumerate(nodes)
            if node.get("resource-id") == _QUERY_ID
            and normalized_question in " ".join(node.get("text", "").split())
        ]
        if query_indexes:
            start = query_indexes[-1]
    if start < 0:
        end = len(nodes)
    else:
        end = next(
            (
                index
                for index in range(start + 1, len(nodes))
                if nodes[index].get("resource-id") == _QUERY_ID
            ),
            len(nodes),
        )
    candidates = [
        node
        for node in nodes[start + 1 : end]
        if node.get("resource-id") == _COPY_ID and node.get("clickable") == "true"
    ]
    return candidates[-1] if candidates else None


def extract_answer_for_copy(
    nodes: list[dict[str, str]],
    copy_action: dict[str, str],
) -> tuple[str, tuple[int, int, int, int] | None] | None:
    """Read the answer immediately preceding a known copy action.

    Long replies can scroll the question and completion marker out of Android's
    visible accessibility tree. The copy action itself is therefore the most
    reliable completion boundary.
    """
    copy_indexes = [index for index, node in enumerate(nodes) if node is copy_action]
    if not copy_indexes:
        return None
    copy_index = copy_indexes[-1]
    boundaries = [
        index
        for index, node in enumerate(nodes[:copy_index])
        if node.get("resource-id") in {_QUERY_ID, _SUGGESTION_ID, _COPY_ID}
    ]
    start = boundaries[-1] if boundaries else -1
    ignored_fragments = (
        "内容由AI生成",
        "服务须知",
        "重答",
        "分享",
        "发消息或按住说话",
    )
    candidates: list[tuple[str, tuple[int, int, int, int] | None]] = []
    for node in nodes[start + 1 : copy_index]:
        if node.get("resource-id") in {_QUERY_ID, _THINKING_ID, _SUGGESTION_ID}:
            continue
        text = (
            node.get("text", "")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .replace("\u00a0", " ")
            .strip()
        )
        if not text or any(fragment in text for fragment in ignored_fragments):
            continue
        candidates.append((text, parse_bounds(node.get("bounds", ""))))
    if not candidates:
        return None
    return max(candidates, key=lambda item: len(item[0]))


def extract_completed_answer(
    nodes: list[dict[str, str]],
    question: str | None = None,
    preserve_formatting: bool = False,
) -> tuple[str, tuple[int, int, int, int] | None] | None:
    start = -1
    if question:
        normalized_question = " ".join(question.split())
        query_indexes = [
            index
            for index, node in enumerate(nodes)
            if node.get("resource-id") == _QUERY_ID
            and normalized_question in " ".join(node.get("text", "").split())
        ]
        if not query_indexes:
            return None
        start = query_indexes[-1]
    markers = [
        index
        for index, node in enumerate(nodes)
        if index > start
        and node.get("resource-id") == _THINKING_ID
        and "已完成" in node.get("text", "")
    ]
    if not markers:
        return None
    marker = markers[0] if question else markers[-1]
    end = next(
        (
            index
            for index in range(marker + 1, len(nodes))
            if nodes[index].get("resource-id") in {_SUGGESTION_ID, _QUERY_ID}
        ),
        len(nodes),
    )
    ignored_fragments = (
        "内容由AI生成",
        "服务须知",
        "重答",
        "分享",
        "发消息或按住说话",
    )
    candidates: list[tuple[str, tuple[int, int, int, int] | None]] = []
    for node in nodes[marker + 1 : end]:
        raw_text = (
            node.get("text", "")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .replace("\u00a0", " ")
        )
        text = raw_text.strip() if preserve_formatting else " ".join(raw_text.split())
        if not text or any(fragment in text for fragment in ignored_fragments):
            continue
        if node.get("resource-id") in {_QUERY_ID, _THINKING_ID}:
            continue
        candidates.append((text, parse_bounds(node.get("bounds", ""))))
    if not candidates:
        return None
    return max(candidates, key=lambda item: len(item[0]))


def _window_process_path(pid: int) -> str:
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def find_projection_window(
    process_name: str = "vivoScreen.exe",
    title_pattern: str = ".*",
) -> tuple[int, str] | None:
    if not hasattr(ctypes, "windll"):
        return None
    user32 = ctypes.windll.user32
    matches: list[tuple[int, str, int]] = []
    pattern = re.compile(title_pattern, re.IGNORECASE)

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        path = _window_process_path(pid.value)
        if Path(path).name.casefold() != process_name.casefold():
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value
        if not pattern.search(title):
            return True
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
        matches.append((int(hwnd), title, area))
        return True

    user32.EnumWindows(callback, 0)
    if not matches:
        return None
    hwnd, title, _ = max(matches, key=lambda item: item[2])
    return hwnd, title


def _read_clipboard_text() -> str | None:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    for _ in range(10):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        raise DriverError("无法读取 Windows 剪贴板")
    try:
        if not user32.IsClipboardFormatAvailable(13):
            return None
        handle = user32.GetClipboardData(13)
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _write_clipboard_text(text: str) -> None:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    for _ in range(10):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        raise DriverError("无法写入 Windows 剪贴板")
    handle = None
    try:
        if not user32.EmptyClipboard():
            raise DriverError("无法清空 Windows 剪贴板")
        payload = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(payload)
        handle = kernel32.GlobalAlloc(0x0002, size)
        if not handle:
            raise DriverError("无法为剪贴板分配内存")
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            raise DriverError("无法锁定剪贴板内存")
        try:
            ctypes.memmove(pointer, ctypes.addressof(payload), size)
        finally:
            kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(13, handle):
            raise DriverError("Windows 拒绝写入 Unicode 剪贴板")
        handle = None
    finally:
        user32.CloseClipboard()
        if handle:
            kernel32.GlobalFree(handle)


class VivoAdbDriver(AutomationDriver):
    """Use ADB for Android state and vivo Screen for Unicode keyboard input."""

    def __init__(
        self,
        serial: str | None,
        adb_path: Path | None,
        template_root: Path,
        artifact_dir: Path,
        window_process: str = "vivoScreen.exe",
        window_title_re: str = ".*",
        clipboard_sync_seconds: float = 2.5,
    ) -> None:
        installation = locate_adb(adb_path)
        if installation is None:
            raise DriverError("未找到 ADB 可执行文件")
        if not serial:
            raise DriverError("vivo_adb 驱动必须配置 APP2API_ADB_SERIAL")
        self.adb = installation.path.resolve()
        self.serial = serial
        self.template_root = template_root.resolve()
        self.artifact_dir = artifact_dir.resolve()
        self.window_process = window_process
        self.window_title_re = window_title_re
        self.clipboard_sync_seconds = clipboard_sync_seconds
        self._ocr = PaddleOcrEngine("ch")
        self._last_question: str | None = None
        self._last_recognition: dict[str, Any] = {}
        self._last_extraction_method = "control"
        self._copied_question: str | None = None
        self._copied_answer: str | None = None
        self._control_question: str | None = None
        self._control_answer: tuple[str, tuple[int, int, int, int] | None] | None = None
        self._recording_process: subprocess.Popen | None = None
        self._recording_remote: str | None = None
        self._recording_output: Path | None = None
        self._artifacts: list[Path] = []
        self._workflow_config: dict[str, Any] = {}

    def configure(self, config: dict[str, Any]) -> None:
        self._workflow_config = dict(config)

    def _run(
        self,
        *arguments: str,
        timeout: float = 20,
        binary: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        command = [str(self.adb), "-s", self.serial, *arguments]
        kwargs: dict[str, Any] = {
            "capture_output": True,
            "timeout": timeout,
            "text": not binary,
            "check": False,
        }
        if not binary:
            kwargs.update(encoding="utf-8", errors="replace")
        result = subprocess.run(command, **kwargs)
        if check and result.returncode != 0:
            stderr = result.stderr if isinstance(result.stderr, str) else ""
            stdout = result.stdout if isinstance(result.stdout, str) else ""
            raise DriverError(
                f"ADB 命令失败 ({result.returncode})：{stderr.strip() or stdout.strip()}"
            )
        return result

    def _dump_nodes_sync(self) -> list[dict[str, str]]:
        remote = "/sdcard/app2api-window.xml"
        self._run("shell", "uiautomator", "dump", "--compressed", remote, timeout=15)
        xml = self._run("exec-out", "cat", remote, binary=True, timeout=10).stdout
        return parse_ui_xml(xml)

    async def _dump_nodes(self) -> list[dict[str, str]]:
        return await asyncio.to_thread(self._dump_nodes_sync)

    @staticmethod
    def _node_center(node: dict[str, str]) -> tuple[int, int]:
        bounds = parse_bounds(node.get("bounds", ""))
        if bounds is None:
            raise DriverError("控件缺少可点击坐标")
        x1, y1, x2, y2 = bounds
        return (x1 + x2) // 2, (y1 + y2) // 2

    async def start_app(self, app_id: str) -> None:
        if self._workflow_config.get("response_mode") == "xiaohuoren":
            await self._start_xiaohuoren_chat(app_id)
            return
        if app_id == _MEITUAN_APP_ID:
            try:
                current_nodes = await self._dump_nodes()
            except (DriverError, ET.ParseError):
                current_nodes = []
            if is_functional_small_tuan_page(current_nodes):
                return
            # The internal AiSmallTuanActivity needs extras prepared by Meituan's
            # own navigation. Starting it directly renders the input UI but can
            # leave the conversation session uninitialized. Only recovery starts
            # from the launcher; healthy pages retain their conversation history.
            await asyncio.to_thread(
                self._run,
                "shell",
                "am",
                "force-stop",
                app_id,
            )
            await asyncio.to_thread(
                self._run,
                "shell",
                "monkey",
                "-p",
                app_id,
                "-c",
                "android.intent.category.LAUNCHER",
                "1",
                timeout=30,
            )
            await asyncio.sleep(3)
            deadline = time.monotonic() + 15
            entry_clicked = False
            while time.monotonic() < deadline:
                nodes = await self._dump_nodes()
                if is_functional_small_tuan_page(nodes):
                    return
                entry = find_small_tuan_entry(nodes)
                if entry is not None and not entry_clicked:
                    x, y = self._node_center(entry)
                    await self.tap(x, y, "小团")
                    entry_clicked = True
                    if await self._wait_for_functional_small_tuan(8):
                        return
                await asyncio.sleep(0.5)
            raise DriverError("美团已启动，但代码未能通过底部入口进入问小团对话页")

        focus = await asyncio.to_thread(
            self._run, "shell", "dumpsys", "window", "windows"
        )
        if app_id in focus.stdout:
            return
        await asyncio.to_thread(
            self._run,
            "shell",
            "monkey",
            "-p",
            app_id,
            "-c",
            "android.intent.category.LAUNCHER",
            "1",
            timeout=30,
        )
        await asyncio.sleep(2)

    async def _xiaohuoren_chat_visible(self) -> bool:
        frame = await self.capture()
        region = self._workflow_config.get(
            "chat_input_ocr_region", [0, 2800, frame.width, frame.height]
        )
        lines = await self._ocr_frame(frame, tuple(int(value) for value in region))
        visible = " ".join(line.text for line in lines)
        markers = [
            str(value)
            for value in self._workflow_config.get(
                "chat_input_markers", ["发消息或按住说话"]
            )
        ]
        return any(marker in visible for marker in markers)

    async def _start_xiaohuoren_chat(self, app_id: str) -> None:
        """Require the user-selected chat to be visibly open.

        Never launch the app, press Back, or guess a chat row here. Douyin keeps
        hidden accessibility windows alive, and any recovery navigation can
        destroy the exact conversation selected by the user.
        """
        if await self._xiaohuoren_chat_visible():
            return
        raise DriverError("当前真实画面不是小火人聊天页，请先打开目标聊天")

    async def _wait_for_functional_small_tuan(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                nodes = await self._dump_nodes()
            except (DriverError, ET.ParseError):
                await asyncio.sleep(0.35)
                continue
            if is_functional_small_tuan_page(nodes):
                return True
            await asyncio.sleep(0.35)
        return False

    async def tap(self, x: int, y: int, label: str = "") -> None:
        await asyncio.to_thread(self._run, "shell", "input", "tap", str(x), str(y))
        frame = await self.capture()
        self._last_action_metadata = {
            "x": x,
            "y": y,
            "label": label,
            "width": frame.width,
            "height": frame.height,
        }

    async def tap_text(self, text: str, timeout: float = 10) -> None:
        deadline = time.monotonic() + timeout
        last_seen: list[str] = []
        while time.monotonic() < deadline:
            nodes = await self._dump_nodes()
            last_seen = [_node_value(node) for node in nodes if _node_value(node)]
            matches = [node for node in nodes if _node_value(node) == text]
            if not matches:
                matches = [node for node in nodes if text in _node_value(node)]
            if text == "发送" and not matches and self._last_question:
                input_nodes = [
                    node
                    for node in nodes
                    if node.get("class") == "android.widget.EditText"
                    and self._last_question in node.get("text", "")
                ]
                if input_nodes:
                    action_node = find_send_action(nodes)
                    if action_node is not None:
                        matches = [action_node]
            if matches:
                x, y = self._node_center(matches[0])
                await self.tap(x, y, text)
                return
            await asyncio.sleep(0.35)
        raise DriverError(f"控件树未找到文字/动作 {text!r}；最后看到：{last_seen}")

    async def tap_template(
        self, path: str, threshold: float = 0.82, timeout: float = 10
    ) -> None:
        try:
            import cv2
            import numpy as np
        except ImportError as exc:
            raise DriverError("模板匹配需要 OpenCV 与 NumPy") from exc
        template_path = Path(path)
        if not template_path.is_absolute():
            template_path = self.template_root / template_path
        template = cv2.imread(str(template_path), cv2.IMREAD_COLOR)
        if template is None:
            raise DriverError(f"无法读取模板：{template_path}")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = await self.capture()
            screen = cv2.imdecode(np.frombuffer(frame.body, np.uint8), cv2.IMREAD_COLOR)
            result = cv2.matchTemplate(screen, template, cv2.TM_CCOEFF_NORMED)
            _, confidence, _, location = cv2.minMaxLoc(result)
            if confidence >= threshold:
                height, width = template.shape[:2]
                await self.tap(
                    location[0] + width // 2,
                    location[1] + height // 2,
                    template_path.name,
                )
                return
            await asyncio.sleep(0.35)
        raise DriverError(f"模板匹配超时：{template_path.name}")

    async def input_text(self, text: str) -> None:
        self._copied_question = None
        self._copied_answer = None
        self._control_question = None
        self._control_answer = None
        if self._workflow_config.get("input_mode") == "xiaohuoren_mention":
            await self._input_xiaohuoren_mention(text)
            return
        wire_text = (
            f"{self._workflow_config.get('question_prefix', '')}"
            f"{text}"
            f"{self._workflow_config.get('question_suffix', '')}"
        )
        if self._workflow_config.get("input_mode") == "unity_focused_paste":
            try:
                await self._input_unity_focused_paste(wire_text)
            except DriverError as primary_error:
                if self._workflow_config.get("input_fallback") != "unity_context_paste":
                    raise
                try:
                    await self._input_unity_context_paste(wire_text)
                except DriverError as fallback_error:
                    raise DriverError(
                        "灵宝控件粘贴与长按粘贴均失败："
                        f"控件粘贴={primary_error}；长按粘贴={fallback_error}"
                    ) from fallback_error
            return
        if self._workflow_config.get("input_mode") == "unity_context_paste":
            await self._input_unity_context_paste(wire_text)
            return
        if (
            self._workflow_config.get("response_mode") == "xiaohuoren"
            and not await self._xiaohuoren_chat_visible()
        ):
            raise DriverError("当前真实画面不是小火人聊天页，拒绝点击隐藏输入控件")
        nodes = await self._dump_nodes()
        input_ids = {
            str(value)
            for value in self._workflow_config.get("input_resource_ids", _INPUT_IDS)
        }
        input_suffixes = {
            str(value)
            for value in self._workflow_config.get("input_resource_id_suffixes", [])
        }
        input_nodes = [
            node for node in nodes if _matches_resource(node, input_ids, input_suffixes)
        ]
        if not input_nodes:
            raise DriverError("当前不是目标对话页：未找到输入框")
        target = next(
            (
                node
                for node in input_nodes
                if node.get("class") == "android.widget.EditText"
            ),
            input_nodes[0],
        )
        x, y = self._node_center(target)
        await self.tap(x, y, "对话输入框")
        await asyncio.sleep(0.4)
        found = await asyncio.to_thread(
            find_projection_window,
            self.window_process,
            self.window_title_re,
        )
        if found is None:
            raise DriverError(f"未找到 vivo 投屏窗口（进程 {self.window_process!r}）")
        previous_clipboard = await asyncio.to_thread(_read_clipboard_text)
        await asyncio.to_thread(
            self._run,
            "shell",
            "input",
            "keycombination",
            "KEYCODE_CTRL_LEFT",
            "KEYCODE_A",
        )
        await asyncio.to_thread(self._run, "shell", "input", "keyevent", "KEYCODE_DEL")
        try:
            await asyncio.to_thread(_write_clipboard_text, wire_text)
            await asyncio.sleep(self.clipboard_sync_seconds)
            await asyncio.to_thread(
                self._run, "shell", "input", "keyevent", "KEYCODE_PASTE"
            )
            await asyncio.sleep(0.6)
        finally:
            await asyncio.to_thread(
                _write_clipboard_text,
                previous_clipboard if previous_clipboard is not None else "",
            )
        verified = await self._dump_nodes()
        if not any(
            node.get("class") == "android.widget.EditText"
            and wire_text in node.get("text", "")
            for node in verified
        ):
            raise DriverError("中文已注入投屏，但手机输入框未出现完整文本")
        self._last_question = wire_text

    async def _input_xiaohuoren_mention(self, text: str) -> None:
        """Create Douyin's real Xiaohuoren mention span, then append the question.

        Sending visually identical plain text does not wake the bot. Typing ``@``
        and selecting the Xiaohuoren candidate creates hidden mention metadata.
        """

        if not await self._xiaohuoren_chat_visible():
            raise DriverError("当前真实画面不是小火人聊天页")
        nodes = await self._dump_nodes()
        input_suffixes = {
            str(value)
            for value in self._workflow_config.get(
                "input_resource_id_suffixes", [":id/msg_et"]
            )
        }
        inputs = [
            node
            for node in nodes
            if node.get("class") == "android.widget.EditText"
            and _matches_resource(node, set(), input_suffixes)
        ]
        if not inputs:
            raise DriverError("当前小火人聊天页没有输入框")
        input_node = inputs[-1]
        input_x, input_y = self._node_center(input_node)
        await self.tap(input_x, input_y, "小火人输入框")
        await asyncio.sleep(0.35)

        current_text = input_node.get("text", "")
        if current_text:
            await asyncio.to_thread(
                self._run,
                "shell",
                "input",
                "keycombination",
                "KEYCODE_CTRL_LEFT",
                "KEYCODE_A",
            )
            await asyncio.to_thread(
                self._run, "shell", "input", "keyevent", "KEYCODE_DEL"
            )
            await asyncio.sleep(0.2)

        await asyncio.to_thread(self._run, "shell", "input", "text", "@")
        await asyncio.sleep(
            float(self._workflow_config.get("mention_candidate_wait_seconds", 0.8))
        )
        nodes = await self._dump_nodes()
        candidate_text = str(
            self._workflow_config.get("mention_candidate_text", "小火人")
        )
        candidate_suffixes = {
            str(value)
            for value in self._workflow_config.get(
                "mention_candidate_resource_id_suffixes", [":id/jmn"]
            )
        }
        minimum_y = int(self._workflow_config.get("mention_candidate_min_y", 2300))
        candidates = []
        for node in nodes:
            bounds = parse_bounds(node.get("bounds", ""))
            if (
                bounds is not None
                and bounds[1] >= minimum_y
                and _node_value(node) == candidate_text
                and _matches_resource(node, set(), candidate_suffixes)
            ):
                candidates.append(node)
        if not candidates:
            raise DriverError("输入 @ 后未出现小火人提及候选")
        await self.tap(*self._node_center(candidates[0]), "选择小火人提及")
        await asyncio.sleep(0.4)

        selected = await self._dump_nodes()
        selected_edits = [
            node
            for node in selected
            if node.get("class") == "android.widget.EditText"
            and _matches_resource(node, set(), input_suffixes)
        ]
        mention_text = selected_edits[-1].get("text", "") if selected_edits else ""
        if candidate_text not in mention_text:
            raise DriverError("已点击小火人候选，但输入框没有形成提及标记")

        previous_clipboard = await asyncio.to_thread(_read_clipboard_text)
        try:
            await asyncio.to_thread(_write_clipboard_text, text)
            await asyncio.sleep(self.clipboard_sync_seconds)
            await asyncio.to_thread(
                self._run, "shell", "input", "keyevent", "KEYCODE_PASTE"
            )
            await asyncio.sleep(0.45)
        finally:
            await asyncio.to_thread(
                _write_clipboard_text,
                previous_clipboard if previous_clipboard is not None else "",
            )

        verified = await self._dump_nodes()
        verified_edits = [
            node
            for node in verified
            if node.get("class") == "android.widget.EditText"
            and _matches_resource(node, set(), input_suffixes)
        ]
        final_text = verified_edits[-1].get("text", "") if verified_edits else ""
        if candidate_text not in final_text or text not in final_text:
            raise DriverError("小火人提及已创建，但问题正文未完整进入输入框")
        self._last_question = final_text

    async def _input_unity_focused_paste(self, text: str) -> None:
        """Paste into the transient Android EditText created by the Unity page.

        The Lingbao page normally exposes only a Unity surface. After the input
        area receives focus, however, the game creates a real EditText overlay.
        Waiting for that control before KEYCODE_PASTE avoids keyboard injection,
        long-press menus, and OCR-based menu selection.
        """

        expected_size = self._workflow_config.get("screen_size", [3200, 1440])
        frame = await self.capture()
        if [frame.width, frame.height] != list(expected_size):
            raise DriverError(
                "灵宝固定坐标要求屏幕分辨率为 "
                f"{expected_size[0]}x{expected_size[1]}，当前为 "
                f"{frame.width}x{frame.height}"
            )

        guard_texts = [
            str(value) for value in self._workflow_config.get("page_guard_texts", [])
        ]
        if guard_texts:
            guard_lines = await self._ocr_frame(frame)
            visible = " ".join(line.text for line in guard_lines)
            missing = [value for value in guard_texts if value not in visible]
            if missing:
                raise DriverError(f"当前不是灵宝对话页，OCR 未看到：{missing}")

        point = self._workflow_config.get("input_point", [1500, 1330])
        edit = await self._focus_unity_edit_text(
            int(point[0]),
            int(point[1]),
            float(self._workflow_config.get("edit_text_focus_timeout_seconds", 5)),
        )
        placeholders = {
            str(value).strip()
            for value in self._workflow_config.get("input_placeholder_texts", ["灵宝~"])
        }
        current_text = edit.get("text", "").strip()
        if current_text == text:
            self._last_question = text
            return
        if current_text and current_text not in placeholders:
            delete_count = min(max(len(current_text) * 2 + 8, 16), 512)
            await asyncio.to_thread(
                self._run,
                "shell",
                "input",
                "keyevent",
                *(["KEYCODE_DEL"] * delete_count),
            )
            await asyncio.sleep(0.2)

        previous_clipboard = await asyncio.to_thread(_read_clipboard_text)
        try:
            await asyncio.to_thread(_write_clipboard_text, text)
            await asyncio.sleep(self.clipboard_sync_seconds)
            await asyncio.to_thread(
                self._run, "shell", "input", "keyevent", "KEYCODE_PASTE"
            )
            await asyncio.sleep(0.35)
        finally:
            await asyncio.to_thread(
                _write_clipboard_text,
                previous_clipboard if previous_clipboard is not None else "",
            )

        deadline = time.monotonic() + float(
            self._workflow_config.get("input_verify_timeout_seconds", 4)
        )
        last_text = ""
        while time.monotonic() < deadline:
            nodes = await self._dump_nodes()
            edits = [
                node for node in nodes if node.get("class") == "android.widget.EditText"
            ]
            if edits:
                last_text = edits[0].get("text", "")
                if text in last_text:
                    self._last_question = text
                    return
            await asyncio.sleep(0.25)
        raise DriverError(
            f"系统粘贴已执行，但灵宝 EditText 未出现完整文本：{last_text!r}"
        )

    async def _focus_unity_edit_text(
        self, x: int, y: int, timeout: float
    ) -> dict[str, str]:
        deadline = time.monotonic() + timeout
        tapped_default = False
        while time.monotonic() < deadline:
            nodes = await self._dump_nodes()
            edits = [
                node for node in nodes if node.get("class") == "android.widget.EditText"
            ]
            focused = next(
                (node for node in edits if node.get("focused") == "true"), None
            )
            if focused is not None:
                return focused
            if edits:
                edit_x, edit_y = self._node_center(edits[0])
                await self.tap(edit_x, edit_y, "灵宝输入框")
            elif not tapped_default:
                await self.tap(x, y, "灵宝输入框")
                tapped_default = True
            await asyncio.sleep(0.25)
        raise DriverError("点击灵宝输入框后未出现可聚焦的 Android EditText")

    async def _input_unity_context_paste(self, text: str) -> None:
        expected_size = self._workflow_config.get("screen_size", [3200, 1440])
        frame = await self.capture()
        if [frame.width, frame.height] != list(expected_size):
            raise DriverError(
                "灵宝固定坐标要求屏幕分辨率为 "
                f"{expected_size[0]}x{expected_size[1]}，当前为 "
                f"{frame.width}x{frame.height}"
            )

        guard_texts = [
            str(value) for value in self._workflow_config.get("page_guard_texts", [])
        ]
        if guard_texts:
            guard_lines = await self._ocr_frame(frame)
            visible = " ".join(line.text for line in guard_lines)
            missing = [value for value in guard_texts if value not in visible]
            if missing:
                raise DriverError(f"当前不是灵宝对话页，OCR 未看到：{missing}")

        point = self._workflow_config.get("input_point", [1500, 1330])
        x, y = int(point[0]), int(point[1])
        await self._hide_lingbao_ime()
        await self.tap(x, y, "灵宝输入框")
        await asyncio.sleep(
            float(self._workflow_config.get("ime_open_wait_seconds", 1.2))
        )
        # Unity opens the system IME and resizes the game surface, moving the
        # input field away from its fixed coordinate. On the vivo IME, Escape
        # hides the keyboard while preserving focus; Back is consumed by Unity.
        await self._hide_lingbao_ime()

        previous_clipboard = await asyncio.to_thread(_read_clipboard_text)
        try:
            await asyncio.to_thread(_write_clipboard_text, text)
            await asyncio.sleep(self.clipboard_sync_seconds)
            duration = int(self._workflow_config.get("long_press_milliseconds", 700))
            menu_region = tuple(
                self._workflow_config.get("paste_menu_region", [0, 900, 1400, 1300])
            )
            await self._long_press(x, y, duration)
            menu_lines = await self._ocr_current(menu_region)
            select_all = self._find_ocr_action(menu_lines, {"select all", "全选"})
            if select_all is not None:
                await self._tap_ocr_line(select_all, "Select all")
                await asyncio.to_thread(
                    self._run, "shell", "input", "keyevent", "KEYCODE_DEL"
                )
                await asyncio.sleep(0.2)
                await self._long_press(x, y, duration)
                menu_lines = await self._ocr_current(menu_region)
            paste_line = self._find_ocr_action(menu_lines, {"paste", "粘贴"})
            if paste_line is None:
                raise DriverError("灵宝输入框长按后未找到 Paste/粘贴")
            await self._tap_ocr_line(paste_line, "Paste")
            await asyncio.sleep(0.5)
        finally:
            await asyncio.to_thread(
                _write_clipboard_text,
                previous_clipboard if previous_clipboard is not None else "",
            )

        verify_region = self._workflow_config.get(
            "input_verify_region", [100, 1220, 2760, 1410]
        )
        verify_lines = await self._ocr_current(tuple(verify_region))
        visible_input = "".join(line.text.replace(" ", "") for line in verify_lines)
        if text.replace(" ", "") not in visible_input:
            raise DriverError(
                f"文字已粘贴到灵宝输入框，但 OCR 未确认完整内容：{visible_input!r}"
            )
        self._last_question = text

    async def _hide_lingbao_ime(self) -> None:
        for _ in range(4):
            if not await self._lingbao_ime_is_open():
                return
            await asyncio.to_thread(
                self._run, "shell", "input", "keyevent", "KEYCODE_ESCAPE"
            )
            await asyncio.sleep(0.5)
        if not await self._lingbao_ime_is_open():
            return
        raise DriverError("无法收起灵宝系统键盘，固定坐标尚未恢复")

    async def _lingbao_ime_is_open(self) -> bool:
        frame = await self.capture()
        try:
            import cv2
            import numpy as np
        except ImportError as exc:
            raise DriverError("键盘状态检测需要 OpenCV 与 NumPy") from exc
        image = cv2.imdecode(np.frombuffer(frame.body, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise DriverError("无法解析灵宝键盘状态截图")
        if float(image[850:1400].mean()) > 150:
            return True
        lines = await self._ocr_frame(frame)
        if any(
            line.text == "123" and line.box is not None and line.box[1] > 900
            for line in lines
        ):
            return True
        keyboard_letters = {
            "Q",
            "W",
            "E",
            "R",
            "T",
            "Y",
            "U",
            "I",
            "O",
            "P",
            "A",
            "S",
            "D",
            "F",
            "G",
            "H",
            "J",
            "K",
            "L",
            "Z",
            "X",
            "C",
            "V",
            "B",
            "N",
            "M",
        }
        if (
            sum(
                1
                for line in lines
                if line.text in keyboard_letters
                and line.box is not None
                and line.box[1] > 800
            )
            >= 5
        ):
            return True
        send_boxes = [
            line.box for line in lines if line.text == "发送" and line.box is not None
        ]
        if not send_boxes:
            raise DriverError("OCR 未找到灵宝发送按钮，无法判断键盘状态")
        return min(box[1] for box in send_boxes) < 1100

    async def _long_press(self, x: int, y: int, duration_ms: int) -> None:
        await asyncio.to_thread(
            self._run,
            "shell",
            "input",
            "motionevent",
            "DOWN",
            str(x),
            str(y),
        )
        try:
            await asyncio.sleep(duration_ms / 1000)
        finally:
            await asyncio.to_thread(
                self._run,
                "shell",
                "input",
                "motionevent",
                "UP",
                str(x),
                str(y),
            )
        await asyncio.sleep(0.25)

    @staticmethod
    def _find_ocr_action(lines: list[OcrLine], labels: set[str]) -> OcrLine | None:
        normalized = {label.casefold() for label in labels}
        return next(
            (
                line
                for line in lines
                if line.box is not None and line.text.strip().casefold() in normalized
            ),
            None,
        )

    async def _tap_ocr_line(self, line: OcrLine, label: str) -> None:
        if line.box is None:
            raise DriverError(f"OCR 动作 {label!r} 缺少坐标")
        x1, y1, x2, y2 = line.box
        await self.tap((x1 + x2) // 2, (y1 + y2) // 2, label)

    async def keyevent(self, key: str) -> None:
        await asyncio.to_thread(self._run, "shell", "input", "keyevent", key)

    async def read_controls(self) -> list[str]:
        try:
            nodes = await self._dump_nodes()
        except Exception:
            if (
                self._workflow_config.get("response_mode") == "xiaohuoren"
                and self._control_question == self._last_question
                and self._control_answer is not None
            ):
                return [self._control_answer[0]]
            raise
        if (
            self._workflow_config.get("response_mode") == "xiaohuoren"
            and self._last_question
        ):
            card = find_xiaohuoren_card_action(
                nodes,
                self._last_question,
                card_ids={
                    str(value)
                    for value in self._workflow_config.get("card_resource_ids", [])
                },
                card_suffixes={
                    str(value)
                    for value in self._workflow_config.get(
                        "card_resource_id_suffixes", [":id/gen"]
                    )
                },
            )
            if card is not None:
                return []
            answer = extract_xiaohuoren_control_answer(
                nodes,
                self._last_question,
                message_ids={
                    str(value)
                    for value in self._workflow_config.get(
                        "assistant_message_resource_ids", []
                    )
                },
                message_suffixes={
                    str(value)
                    for value in self._workflow_config.get(
                        "assistant_message_resource_id_suffixes", [":id/kd8"]
                    )
                },
                max_left=int(
                    self._workflow_config.get("assistant_message_max_left", 220)
                ),
            )
            previous_answer = (
                self._control_answer
                if self._control_question == self._last_question
                else None
            )
            if answer is not None:
                if answer == previous_answer:
                    return [answer[0]]
                self._control_question = self._last_question
                self._control_answer = answer
            elif previous_answer is not None:
                return [previous_answer[0]]
            if answer is None:
                return []
            text, bounds = answer
            frame = await self.capture()
            self._last_recognition = {
                "box": list(bounds) if bounds else None,
                "width": frame.width,
                "height": frame.height,
                "confidence": 1.0,
            }
            return [text]
        if self._last_question:
            answer = extract_completed_answer(nodes, self._last_question)
            if answer is None:
                return []
            text, bounds = answer
            if bounds:
                frame = await self.capture()
                self._last_recognition = {
                    "box": list(bounds),
                    "width": frame.width,
                    "height": frame.height,
                    "confidence": 1.0,
                }
            return [text]
        values: list[str] = []
        for node in nodes:
            for key in ("text", "content-desc"):
                value = " ".join(node.get(key, "").split()).strip()
                if value and value not in values:
                    values.append(value)
        return values

    async def read_clipboard_answer(self) -> str | None:
        question = self._last_question
        if not question:
            return None
        if question == self._copied_question and self._copied_answer:
            return self._copied_answer

        if self._workflow_config.get("response_mode") == "xiaohuoren":
            return await self._read_xiaohuoren_card_clipboard(question)

        nodes = await self._dump_nodes()
        copy_action = find_copy_action(nodes, question)
        answer = (
            extract_answer_for_copy(nodes, copy_action)
            if copy_action is not None
            else None
        )
        if copy_action is None or answer is None:
            return None

        control_text, answer_bounds = answer
        previous_clipboard = await asyncio.to_thread(_read_clipboard_text)
        x, y = self._node_center(copy_action)
        await self.tap(x, y, "复制回复")

        copied_text: str | None = None
        deadline = time.monotonic() + self.clipboard_sync_seconds
        while time.monotonic() < deadline:
            current = await asyncio.to_thread(_read_clipboard_text)
            if current and current != previous_clipboard:
                copied_text = current.replace("\r\n", "\n").replace("\r", "\n").strip()
                break
            await asyncio.sleep(0.1)

        if copied_text is not None:
            await asyncio.to_thread(
                _write_clipboard_text,
                previous_clipboard if previous_clipboard is not None else "",
            )
            text = copied_text
            self._last_extraction_method = "clipboard"
            source = "clipboard"
        else:
            text = control_text
            self._last_extraction_method = "control"
            source = "control_after_copy"

        self._copied_question = question
        self._copied_answer = text
        frame = await self.capture()
        self._last_recognition = {
            "box": list(answer_bounds) if answer_bounds else None,
            "width": frame.width,
            "height": frame.height,
            "confidence": 1.0,
            "source": source,
        }
        return text

    async def _read_xiaohuoren_card_clipboard(self, question: str) -> str | None:
        nodes = await self._dump_nodes()
        card = find_xiaohuoren_card_action(
            nodes,
            question,
            card_ids={
                str(value)
                for value in self._workflow_config.get("card_resource_ids", [])
            },
            card_suffixes={
                str(value)
                for value in self._workflow_config.get(
                    "card_resource_id_suffixes", [":id/gen"]
                )
            },
        )
        if card is None:
            return None

        x, y = self._node_center(card)
        await self.tap(x, y, "小火人完整卡片")
        detail_activity = str(
            self._workflow_config.get("card_detail_activity", "AnnieXHostActivity")
        )
        open_deadline = time.monotonic() + float(
            self._workflow_config.get("card_open_timeout_seconds", 5)
        )
        opened = False
        while time.monotonic() < open_deadline:
            focus = await asyncio.to_thread(
                self._run, "shell", "dumpsys", "window", "windows"
            )
            if detail_activity in focus.stdout:
                opened = True
                break
            await asyncio.sleep(0.2)
        if not opened:
            raise DriverError("小火人卡片没有真正打开；已停止，未执行返回")
        await asyncio.sleep(
            float(self._workflow_config.get("card_open_wait_seconds", 1.2))
        )
        previous_clipboard = await asyncio.to_thread(_read_clipboard_text)
        copied_text: str | None = None
        copy_box: tuple[int, int, int, int] | None = None
        clipboard_changed = False
        try:
            max_scrolls = int(self._workflow_config.get("card_max_scrolls", 10))
            for attempt in range(max_scrolls + 1):
                frame = await self.capture()
                lines = await self._ocr_frame(frame)
                copy_line = self._find_ocr_action(lines, {"复制"})
                if copy_line is not None:
                    marker = f"__FREECODING_COPY_{uuid4().hex}__"
                    await asyncio.to_thread(_write_clipboard_text, marker)
                    clipboard_changed = True
                    await asyncio.sleep(self.clipboard_sync_seconds)
                    if copy_line.box is None:
                        raise DriverError("OCR 找到复制文字，但缺少点击坐标")
                    copy_x = (copy_line.box[0] + copy_line.box[2]) // 2
                    copy_y = (copy_line.box[1] + copy_line.box[3]) // 2 + int(
                        self._workflow_config.get("card_copy_tap_y_offset", -120)
                    )
                    await self.tap(copy_x, copy_y, "复制完整卡片")
                    copy_box = copy_line.box
                    deadline = time.monotonic() + float(
                        self._workflow_config.get(
                            "card_clipboard_timeout_seconds",
                            self.clipboard_sync_seconds + 3,
                        )
                    )
                    while time.monotonic() < deadline:
                        current = await asyncio.to_thread(_read_clipboard_text)
                        if current and current != marker:
                            copied_text = (
                                current.replace("\r\n", "\n")
                                .replace("\r", "\n")
                                .strip()
                            )
                            break
                        await asyncio.sleep(0.1)
                    break
                if attempt >= max_scrolls:
                    break
                await asyncio.to_thread(
                    self._run,
                    "shell",
                    "input",
                    "swipe",
                    "720",
                    "2700",
                    "720",
                    "550",
                    "500",
                )
                await asyncio.sleep(
                    float(self._workflow_config.get("card_scroll_wait_seconds", 0.45))
                )
        finally:
            if clipboard_changed:
                await asyncio.to_thread(
                    _write_clipboard_text,
                    previous_clipboard if previous_clipboard is not None else "",
                )

        if not copied_text or len(copied_text) < int(
            self._workflow_config.get("card_copy_min_chars", 20)
        ):
            raise DriverError("卡片已打开，但未确认复制到完整内容；未执行返回")

        # Back is deliberately last: open card -> scroll -> copy -> confirm -> back.
        await asyncio.to_thread(self._run, "shell", "input", "keyevent", "KEYCODE_BACK")
        await asyncio.sleep(0.8)
        focus = await asyncio.to_thread(
            self._run, "shell", "dumpsys", "window", "windows"
        )
        if detail_activity in focus.stdout:
            raise DriverError("卡片内容已复制，但没有成功返回聊天页")
        self._copied_question = question
        self._copied_answer = copied_text
        self._last_extraction_method = "clipboard"
        frame = await self.capture()
        self._last_recognition = {
            "box": list(copy_box) if copy_box else None,
            "width": frame.width,
            "height": frame.height,
            "confidence": 1.0,
            "source": "xiaohuoren_card_clipboard",
        }
        return copied_text

    def extraction_method(self, requested: str) -> str:
        if requested == "clipboard":
            return self._last_extraction_method
        return requested

    def recognition_metadata(self, text: str) -> dict[str, Any]:
        return dict(self._last_recognition)

    def _screenshot_sync(self) -> bytes:
        return self._run("exec-out", "screencap", "-p", binary=True, timeout=20).stdout

    async def capture(self) -> FrameCapture:
        try:
            import cv2
            import numpy as np
        except ImportError as exc:
            raise DriverError("截图解析需要 OpenCV 与 NumPy") from exc
        body = await asyncio.to_thread(self._screenshot_sync)
        image = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise DriverError("ADB 截图不是有效 PNG")
        height, width = image.shape[:2]
        return FrameCapture(
            body=body, media_type="image/png", width=width, height=height
        )

    async def read_ocr(
        self, region: tuple[int, int, int, int] | None = None
    ) -> list[str]:
        frame = await self.capture()
        tile_width = int(self._workflow_config.get("ocr_tile_width", 0))
        if region is not None and tile_width > 0:
            lines = await self._ocr_tiled_frame(
                frame,
                region,
                tile_width=tile_width,
                overlap=int(self._workflow_config.get("ocr_tile_overlap", 0)),
                scale=float(self._workflow_config.get("ocr_tile_scale", 1)),
            )
            lines = self._merge_ocr_rows(
                lines,
                int(self._workflow_config.get("ocr_line_y_tolerance", 45)),
            )
            if self._workflow_config.get("ocr_normalize_punctuation", False):
                lines = [
                    OcrLine(
                        text=re.sub(r"[·.\-:：]{2,}", "……", line.text),
                        confidence=line.confidence,
                        box=line.box,
                    )
                    for line in lines
                ]
        else:
            lines = await self._ocr_frame(frame, region)
        max_left = self._workflow_config.get("ocr_line_max_left")
        if max_left is not None:
            lines = [
                line
                for line in lines
                if line.box is not None and line.box[0] <= int(max_left)
            ]
        boxes = [line.box for line in lines if line.box is not None]
        if boxes:
            self._last_recognition = {
                "box": [
                    min(box[0] for box in boxes),
                    min(box[1] for box in boxes),
                    max(box[2] for box in boxes),
                    max(box[3] for box in boxes),
                ],
                "width": frame.width,
                "height": frame.height,
                "confidence": min(line.confidence for line in lines),
            }
        return [line.text for line in lines]

    async def _ocr_tiled_frame(
        self,
        frame: FrameCapture,
        region: tuple[int, int, int, int],
        *,
        tile_width: int,
        overlap: int,
        scale: float,
    ) -> list[OcrLine]:
        try:
            import cv2
            import numpy as np
        except ImportError as exc:
            raise DriverError("OCR 分片识别需要 OpenCV 与 NumPy") from exc
        image = cv2.imdecode(np.frombuffer(frame.body, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise DriverError("无法解析 OCR 分片截图")
        x1, y1, x2, y2 = region
        tile_width = max(200, min(tile_width, x2 - x1))
        overlap = max(0, min(overlap, tile_width - 1))
        step = tile_width - overlap
        scale = max(1.0, scale)
        result: list[OcrLine] = []
        start = x1
        while start < x2:
            end = min(x2, start + tile_width)
            tile = image[y1:y2, start:end]
            if scale != 1:
                tile = cv2.resize(
                    tile,
                    None,
                    fx=scale,
                    fy=scale,
                    interpolation=cv2.INTER_CUBIC,
                )
            try:
                lines = await asyncio.to_thread(self._ocr.recognize, tile)
            except DriverError:
                lines = []
            for line in lines:
                box = line.box
                result.append(
                    OcrLine(
                        text=line.text,
                        confidence=line.confidence,
                        box=(
                            int(round(start + box[0] / scale)),
                            int(round(y1 + box[1] / scale)),
                            int(round(start + box[2] / scale)),
                            int(round(y1 + box[3] / scale)),
                        )
                        if box is not None
                        else None,
                    )
                )
            if end >= x2:
                break
            start += step
        return result

    @staticmethod
    def _merge_ocr_rows(lines: list[OcrLine], y_tolerance: int) -> list[OcrLine]:
        boxed = [line for line in lines if line.box is not None and line.text]
        boxed.sort(key=lambda line: (((line.box[1] + line.box[3]) // 2), line.box[0]))
        rows: list[list[OcrLine]] = []
        for line in boxed:
            center_y = (line.box[1] + line.box[3]) // 2
            row = next(
                (
                    values
                    for values in rows
                    if abs(
                        center_y
                        - sum((value.box[1] + value.box[3]) // 2 for value in values)
                        // len(values)
                    )
                    <= y_tolerance
                ),
                None,
            )
            if row is None:
                rows.append([line])
            else:
                row.append(line)

        merged: list[OcrLine] = []
        terminal = "。！？!?；;：:~～"
        for row in rows:
            row.sort(key=lambda line: line.box[0])
            text = row[0].text
            accepted = [row[0]]
            for line in row[1:]:
                fragment = line.text
                if fragment in text:
                    if fragment[-1:] in terminal and text[-1:] not in terminal:
                        text += fragment[-1]
                    continue
                if text in fragment:
                    text = fragment
                    accepted.append(line)
                    continue
                overlap = 0
                for size in range(min(len(text), len(fragment)), 1, -1):
                    if text[-size:] == fragment[:size]:
                        overlap = size
                        break
                if overlap:
                    text += fragment[overlap:]
                    accepted.append(line)
                    continue
                fuzzy_overlap = 0
                for size in range(min(len(text), len(fragment)), 3, -1):
                    left = text[-size:]
                    right = fragment[:size]
                    matches = sum(a == b for a, b in zip(left, right, strict=True))
                    threshold = 0.65 if size >= 8 else 0.8
                    if matches / size >= threshold:
                        fuzzy_overlap = size
                        break
                if fuzzy_overlap:
                    if line.confidence > accepted[-1].confidence:
                        text = text[:-fuzzy_overlap] + fragment
                    else:
                        text += fragment[fuzzy_overlap:]
                    accepted.append(line)
                elif line.box[0] <= max(value.box[2] for value in accepted):
                    if fragment[-1:] in terminal and text[-1:] not in terminal:
                        text += fragment[-1]
                else:
                    text += fragment
                    accepted.append(line)
            boxes = [line.box for line in accepted]
            merged.append(
                OcrLine(
                    text=text,
                    confidence=min(line.confidence for line in accepted),
                    box=(
                        min(box[0] for box in boxes),
                        min(box[1] for box in boxes),
                        max(box[2] for box in boxes),
                        max(box[3] for box in boxes),
                    ),
                )
            )
        merged.sort(key=lambda line: (line.box[1], line.box[0]))
        return merged

    async def _ocr_current(
        self, region: tuple[int, int, int, int] | None = None
    ) -> list[OcrLine]:
        frame = await self.capture()
        return await self._ocr_frame(frame, region)

    async def _ocr_frame(
        self,
        frame: FrameCapture,
        region: tuple[int, int, int, int] | None = None,
    ) -> list[OcrLine]:
        try:
            import cv2
            import numpy as np
        except ImportError as exc:
            raise DriverError("OCR 需要 OpenCV 与 NumPy") from exc
        image = cv2.imdecode(np.frombuffer(frame.body, np.uint8), cv2.IMREAD_COLOR)
        offset_x = 0
        offset_y = 0
        if region:
            x1, y1, x2, y2 = region
            image = image[y1:y2, x1:x2]
            offset_x, offset_y = x1, y1
        lines = await asyncio.to_thread(self._ocr.recognize, image)
        if not region:
            return lines
        return [
            OcrLine(
                text=line.text,
                confidence=line.confidence,
                box=(
                    line.box[0] + offset_x,
                    line.box[1] + offset_y,
                    line.box[2] + offset_x,
                    line.box[3] + offset_y,
                )
                if line.box
                else None,
            )
            for line in lines
        ]

    async def start_recording(self, output: Path, max_time: float) -> bool:
        if self._recording_process is not None:
            return True
        output.parent.mkdir(parents=True, exist_ok=True)
        remote = f"/sdcard/app2api-{int(time.time())}.mp4"
        command = [
            str(self.adb),
            "-s",
            self.serial,
            "shell",
            "screenrecord",
            "--time-limit",
            str(max(1, min(180, int(max_time)))),
            remote,
        ]
        self._recording_process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._recording_remote = remote
        self._recording_output = output
        await asyncio.sleep(0.4)
        return self._recording_process.poll() is None

    async def stop_recording(self) -> Path | None:
        if self._recording_process is None:
            return None
        process = self._recording_process
        remote = self._recording_remote
        output = self._recording_output
        self._recording_process = None
        self._recording_remote = None
        self._recording_output = None
        await asyncio.to_thread(
            self._run,
            "shell",
            "pkill",
            "-2",
            "screenrecord",
            check=False,
        )
        try:
            await asyncio.to_thread(process.wait, 5)
        except subprocess.TimeoutExpired:
            process.terminate()
        if remote and output:
            await asyncio.to_thread(self._run, "pull", remote, str(output), timeout=60)
            await asyncio.to_thread(self._run, "shell", "rm", remote, check=False)
            if output.is_file():
                self._artifacts.append(output)
                return output
        return None

    def artifacts(self) -> list[Path]:
        return list(self._artifacts)

    async def close(self) -> None:
        if self._recording_process is not None:
            await self.stop_recording()
