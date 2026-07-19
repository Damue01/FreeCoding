from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .drivers import AutomationDriver, DriverError
from .models import Answer, Target

Emit = Callable[..., Awaitable[Any]]
EXTRACTION_METHODS = {"clipboard", "control", "ocr"}
ALLOWED_ACTIONS = {
    "tap",
    "tap_text",
    "tap_template",
    "input_text",
    "keyevent",
    "sleep",
    "assert_text",
}


@dataclass(slots=True)
class WorkflowContext:
    job_id: str
    question: str
    driver: AutomationDriver
    emit: Emit
    config: dict[str, Any]

    async def frame(self) -> None:
        capture = await self.driver.capture()
        await self.emit(
            self.job_id,
            "frame",
            "已更新运行画面",
            width=capture.width,
            height=capture.height,
            _frame=capture,
        )


def load_target_config(config_dir: Path, target: Target) -> dict[str, Any]:
    path = config_dir / f"{target.value}.json"
    if not path.exists():
        raise DriverError(f"缺少目标流程配置：{path}")
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DriverError(f"无法读取目标流程配置 {path}：{exc}") from exc
    _validate_config(config, path)
    return config


def _validate_config(config: dict[str, Any], path: Path) -> None:
    if not isinstance(config.get("app_id"), str) or not config["app_id"]:
        raise DriverError(f"{path} 缺少非空 app_id")
    navigation = config.get("navigation", [])
    if not isinstance(navigation, list):
        raise DriverError(f"{path} 的 navigation 必须是数组")
    for index, step in enumerate(navigation, start=1):
        action = step.get("action") if isinstance(step, dict) else None
        if action not in ALLOWED_ACTIONS:
            raise DriverError(f"{path} 第 {index} 步使用了未知动作：{action!r}")
        retries = step.get("retries", 0)
        if not isinstance(retries, int) or retries < 0:
            raise DriverError(f"{path} 第 {index} 步 retries 必须是非负整数")
    methods = config.get("extraction", ["clipboard", "control", "ocr"])
    if not methods or any(method not in EXTRACTION_METHODS for method in methods):
        raise DriverError(f"{path} 的 extraction 包含未知提取方式")


async def _read_method(ctx: WorkflowContext, method: str) -> list[str]:
    if method == "clipboard":
        text = await ctx.driver.read_clipboard_answer()
        return [text] if text else []
    if method == "control":
        return await ctx.driver.read_controls()
    if method == "ocr":
        region = ctx.config.get("ocr_region")
        return await ctx.driver.read_ocr(tuple(region) if region else None)
    raise DriverError(f"未知提取方式：{method}")


async def _assert_text(ctx: WorkflowContext, step: dict[str, Any]) -> None:
    expected = str(step["text"])
    timeout = float(step.get("timeout", 10))
    methods = step.get("methods", ["control", "ocr"])
    deadline = time.monotonic() + timeout
    last_lines: list[str] = []
    while time.monotonic() < deadline:
        for method in methods:
            try:
                lines = await _read_method(ctx, method)
            except Exception:
                continue
            last_lines = lines
            if any(expected in line for line in lines):
                return
        await asyncio.sleep(float(step.get("interval", 0.4)))
    raise DriverError(f"等待文字 {expected!r} 超时；最后识别到：{last_lines}")


async def _perform_step(ctx: WorkflowContext, step: dict[str, Any]) -> None:
    action = step["action"]
    if action == "tap":
        await ctx.driver.tap(step["x"], step["y"], step.get("label", ""))
    elif action == "tap_text":
        await ctx.driver.tap_text(step["text"], step.get("timeout", 10))
    elif action == "tap_template":
        await ctx.driver.tap_template(
            step["path"], step.get("threshold", 0.82), step.get("timeout", 10)
        )
    elif action == "input_text":
        await ctx.driver.input_text(step["text"])
    elif action == "keyevent":
        await ctx.driver.keyevent(step["key"])
    elif action == "sleep":
        await asyncio.sleep(float(step.get("seconds", 1)))
    elif action == "assert_text":
        await _assert_text(ctx, step)
    else:
        raise DriverError(f"不支持的流程动作：{action}")


async def _run_steps(ctx: WorkflowContext) -> None:
    for index, step in enumerate(ctx.config.get("navigation", []), start=1):
        retries = int(step.get("retries", 0))
        delay = float(step.get("retry_delay", 0.8))
        label = step.get("label", step["action"])
        for attempt in range(1, retries + 2):
            await ctx.emit(
                ctx.job_id,
                "action",
                label,
                action=step["action"],
                step=index,
                attempt=attempt,
                max_attempts=retries + 1,
            )
            try:
                ctx.driver.clear_action_metadata()
                await _perform_step(ctx, step)
                action_data = ctx.driver.action_metadata()
                if action_data.get("x") is not None:
                    await ctx.emit(
                        ctx.job_id,
                        "click",
                        f"点击：{label}",
                        **action_data,
                    )
                await ctx.frame()
                break
            except Exception as exc:
                with suppress(Exception):
                    await ctx.frame()
                if attempt <= retries:
                    await ctx.emit(
                        ctx.job_id,
                        "retry",
                        f"步骤失败，准备重试：{label}",
                        step=index,
                        attempt=attempt,
                        error=str(exc),
                        delay=delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                if step.get("on_failure", "fail") == "continue":
                    await ctx.emit(
                        ctx.job_id,
                        "warning",
                        f"步骤失败，按配置继续：{label}",
                        step=index,
                        error=str(exc),
                    )
                    break
                raise DriverError(f"导航第 {index} 步 {label!r} 失败：{exc}") from exc


def _clean_lines(lines: list[str]) -> list[str]:
    cleaned: list[str] = []
    for value in lines:
        text = " ".join(str(value).split()).strip()
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned


def _select_answer(
    lines: list[str],
    question: str,
    baseline: set[str],
    config: dict[str, Any],
) -> str:
    active_baseline = baseline if config.get("response_use_baseline", True) else set()
    ignored = {
        question.strip(),
        "",
        "发送",
        "返回",
        *[str(value).strip() for value in config.get("ignore_texts", [])],
    }
    minimum = int(config.get("response_min_chars", 1))
    ignore_patterns = [
        re.compile(str(pattern)) for pattern in config.get("ignore_patterns", [])
    ]
    candidates = [
        line
        for line in _clean_lines(lines)
        if line not in ignored
        and line not in active_baseline
        and len(line) >= minimum
        and not any(pattern.fullmatch(line) for pattern in ignore_patterns)
    ]
    if not candidates:
        raise DriverError("没有从应用画面中提取到新回复")
    if config.get("join_response_lines", False):
        separator = str(config.get("response_separator", ""))
        return separator.join(candidates)
    return candidates[-1]


async def _baseline(ctx: WorkflowContext, methods: list[str]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for method in methods:
        if method not in EXTRACTION_METHODS or method == "clipboard":
            continue
        try:
            result[method] = set(_clean_lines(await _read_method(ctx, method)))
        except Exception as exc:
            result[method] = set()
            await ctx.emit(
                ctx.job_id,
                "warning",
                f"无法建立 {method} 回复基线，将继续执行",
                error=str(exc),
            )
    return result


async def _extract_visual(
    ctx: WorkflowContext,
    methods: list[str],
    baselines: dict[str, set[str]],
) -> tuple[str, str]:
    timeout = float(ctx.config.get("response_timeout_seconds", 30))
    interval = float(ctx.config.get("response_poll_interval", 0.6))
    stable_required = max(1, int(ctx.config.get("response_stable_polls", 2)))
    fallback_delay = max(0.0, float(ctx.config.get("visual_fallback_delay_seconds", 0)))
    started = time.monotonic()
    deadline = time.monotonic() + timeout
    states: dict[str, tuple[str, int]] = {}
    last_errors: dict[str, str] = {}
    announced: set[str] = set()

    while time.monotonic() < deadline:
        for method in methods:
            if method not in EXTRACTION_METHODS:
                continue
            try:
                lines = await _read_method(ctx, method)
                if method == "clipboard":
                    if not lines or not lines[0].strip():
                        raise DriverError("复制按钮尚未出现或剪贴板回复为空")
                    text = lines[0].strip()
                    action_data = ctx.driver.action_metadata()
                    if action_data.get("x") is not None:
                        await ctx.emit(
                            ctx.job_id,
                            "click",
                            "点击：复制回复",
                            **action_data,
                        )
                        ctx.driver.clear_action_metadata()
                    actual_method = ctx.driver.extraction_method(method)
                    await ctx.emit(
                        ctx.job_id,
                        "recognition",
                        f"{actual_method} 已提取完整回复",
                        method=actual_method,
                        text=text,
                        stable=1,
                        required=1,
                        **ctx.driver.recognition_metadata(text),
                    )
                    return text, actual_method
                text = _select_answer(
                    lines,
                    ctx.question,
                    baselines.get(method, set()),
                    ctx.config,
                )
                method_stable_required = max(
                    1,
                    int(ctx.config.get(f"{method}_stable_polls", stable_required)),
                )
                previous, count = states.get(method, ("", 0))
                count = count + 1 if text == previous else 1
                states[method] = (text, count)
                if text != previous:
                    await ctx.emit(
                        ctx.job_id,
                        "recognition",
                        f"{method} 识别到候选回复",
                        method=method,
                        text=text,
                        stable=1,
                        required=method_stable_required,
                        **ctx.driver.recognition_metadata(text),
                    )
                if count >= method_stable_required:
                    if (
                        method != "clipboard"
                        and "clipboard" in methods
                        and time.monotonic() - started
                        < float(ctx.config.get("clipboard_preference_delay_seconds", 0))
                    ):
                        continue
                    if (
                        method == "ocr"
                        and "control" in methods
                        and time.monotonic() - started < fallback_delay
                    ):
                        continue
                    return text, method
            except Exception as exc:
                last_errors[method] = str(exc)
                if method not in announced:
                    announced.add(method)
                    await ctx.emit(
                        ctx.job_id,
                        "extract",
                        f"等待 {method} 出现新回复",
                        method=method,
                    )
        await asyncio.sleep(interval)
    details = "；".join(f"{key}: {value}" for key, value in last_errors.items())
    raise DriverError(
        f"回复在 {timeout:g} 秒内未稳定" + (f"；{details}" if details else "")
    )


async def run_workflow(ctx: WorkflowContext) -> Answer:
    app_id = ctx.config["app_id"]
    ctx.driver.configure(ctx.config)
    await ctx.emit(ctx.job_id, "action", "启动目标应用", app_id=app_id)
    await ctx.driver.start_app(app_id)
    await ctx.frame()
    await _run_steps(ctx)

    methods = list(ctx.config.get("extraction", ["clipboard", "control", "ocr"]))
    baselines = await _baseline(ctx, methods)
    channel = ctx.config.get("question_channel", "text")
    if channel != "text":
        raise DriverError(f"未知提问通道：{channel}")
    await ctx.emit(ctx.job_id, "action", "输入问题", action="input_text")
    await ctx.driver.input_text(ctx.question)
    input_action_data = ctx.driver.action_metadata()
    if input_action_data.get("x") is not None:
        await ctx.emit(
            ctx.job_id,
            "click",
            "点击：问小团输入框",
            **input_action_data,
        )
    submit = ctx.config.get("submit")
    if submit:
        ctx.driver.clear_action_metadata()
        if submit["action"] == "tap_text":
            await ctx.driver.tap_text(submit["text"], submit.get("timeout", 10))
        elif submit["action"] == "tap":
            await ctx.driver.tap(submit["x"], submit["y"], "发送")
        elif submit["action"] == "tap_template":
            await ctx.driver.tap_template(
                submit["path"],
                submit.get("threshold", 0.82),
                submit.get("timeout", 10),
            )
        else:
            raise DriverError(f"未知发送动作：{submit['action']}")
        action_data = ctx.driver.action_metadata()
        if action_data.get("x") is not None:
            await ctx.emit(
                ctx.job_id,
                "click",
                "点击：发送",
                **action_data,
            )
    await ctx.frame()

    initial_wait = float(
        ctx.config.get(
            "response_initial_wait_seconds",
            ctx.config.get("response_wait_seconds", 1),
        )
    )
    if initial_wait > 0:
        await ctx.emit(ctx.job_id, "wait", "等待应用开始回复", seconds=initial_wait)
        await asyncio.sleep(initial_wait)

    visual_error: Exception | None = None
    visual_methods = [method for method in methods if method in EXTRACTION_METHODS]
    if visual_methods:
        try:
            text, method = await _extract_visual(ctx, visual_methods, baselines)
            await ctx.frame()
            extraction = "mock" if ctx.driver.is_mock else method
            return Answer(text=text, extraction=extraction, confidence=None)
        except Exception as exc:
            visual_error = exc
            await ctx.emit(
                ctx.job_id,
                "warning",
                "回复提取失败",
                error=str(exc),
            )

    if visual_error:
        raise DriverError(str(visual_error)) from visual_error
    raise DriverError("没有配置剪贴板、控件或 OCR 回复提取方式")
