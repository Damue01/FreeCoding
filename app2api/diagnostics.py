from __future__ import annotations

import importlib.util
from pathlib import Path

from .config import Settings
from .devices import discover_devices
from .models import DiagnosticCheck, Diagnostics, Target
from .workflows import load_target_config


def _check(name: str, status: str, message: str, **data) -> DiagnosticCheck:
    return DiagnosticCheck(name=name, status=status, message=message, data=data)


def _package(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def build_diagnostics(settings: Settings) -> Diagnostics:
    checks: list[DiagnosticCheck] = [
        _check(
            "config_dir",
            "ok" if settings.config_dir.is_dir() else "error",
            "目标流程配置目录可用"
            if settings.config_dir.is_dir()
            else "目标流程配置目录不存在",
            path=str(settings.config_dir.resolve()),
        ),
        _check(
            "artifact_dir",
            "ok" if settings.artifact_dir.is_dir() else "warning",
            "运行产物目录可用"
            if settings.artifact_dir.is_dir()
            else "运行时将创建产物目录",
            path=str(settings.artifact_dir.resolve()),
        ),
    ]

    configs: dict[Target, dict] = {}
    for target in Target:
        if target.value not in settings.enabled_target_values():
            continue
        try:
            configs[target] = load_target_config(settings.config_dir, target)
            checks.append(
                _check(
                    f"workflow_{target.value}",
                    "ok",
                    f"{configs[target].get('name', target.value)}流程配置有效",
                )
            )
        except Exception as exc:
            checks.append(_check(f"workflow_{target.value}", "error", str(exc)))

    if settings.driver not in {"mock", "vivo_adb"}:
        checks.append(_check("driver", "error", f"未知驱动：{settings.driver}"))
    else:
        checks.append(_check("driver", "ok", f"当前驱动：{settings.driver}"))

    if settings.driver != "mock" and any(
        "ocr" in config.get("extraction", []) for config in configs.values()
    ):
        installed = _package("paddleocr")
        checks.append(
            _check(
                "package_paddleocr",
                "ok" if installed else "error",
                "PaddleOCR 兜底可用" if installed else "缺少 PaddleOCR 运行时",
            )
        )

    if settings.driver == "vivo_adb":
        discovery = discover_devices(settings)
        online = [device for device in discovery.devices if device.state == "device"]
        selected = next(
            (device for device in online if device.serial == settings.adb_serial),
            None,
        )
        ready = discovery.adb_available and not discovery.error and bool(selected)
        checks.append(
            _check(
                "adb_device",
                "ok" if ready else "error",
                f"设备 {settings.adb_serial} 已连接"
                if ready
                else discovery.error or f"设备 {settings.adb_serial} 不在线",
                executable=discovery.adb_executable,
                source=discovery.adb_source,
            )
        )
        try:
            from .drivers.vivo_adb import find_projection_window

            window = find_projection_window(
                settings.vivo_window_process,
                settings.vivo_window_title_re,
            )
        except Exception as exc:
            window = None
            window_error = str(exc)
        else:
            window_error = None
        checks.append(
            _check(
                "vivo_projection",
                "ok" if window else "error",
                f"已找到 vivo 投屏窗口：{window[1]}"
                if window
                else "未找到 vivo 投屏窗口",
                error=window_error,
            )
        )

    missing_templates: list[str] = []
    for config in configs.values():
        steps = [*config.get("navigation", [])]
        if config.get("submit"):
            steps.append(config["submit"])
        for step in steps:
            if step.get("action") != "tap_template":
                continue
            path = Path(step["path"])
            if not path.is_absolute():
                path = settings.template_root / path
            if not path.is_file():
                missing_templates.append(str(path))
    if missing_templates:
        checks.append(
            _check(
                "templates", "error", "存在缺失的模板文件", missing=missing_templates
            )
        )

    return Diagnostics(
        ready=not any(check.status == "error" for check in checks),
        driver=settings.driver,
        checks=checks,
    )
