from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path

import uvicorn

from .config import Settings
from .devices import discover_devices
from .diagnostics import build_diagnostics
from .drivers import build_driver
from .models import Target
from .preflight import build_preflight
from .workflows import load_target_config


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="freecoding")
    subcommands = result.add_subparsers(dest="command")
    serve = subcommands.add_parser("serve", help="start the HTTP API and dashboard")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    subcommands.add_parser(
        "diagnostics", help="check vivo ADB, workflow and OCR readiness"
    )
    devices = subcommands.add_parser(
        "devices", help="discover Android devices through ADB"
    )
    devices.add_argument(
        "--connect",
        metavar="HOST:PORT",
        help="explicitly run 'adb connect HOST:PORT' before discovery",
    )
    subcommands.add_parser(
        "preflight",
        help="check whether configured target apps are installed on the selected device",
    )
    capture = subcommands.add_parser(
        "capture", help="capture the current app page for calibration"
    )
    capture.add_argument(
        "--target", required=True, choices=[item.value for item in Target]
    )
    capture.add_argument("--output-dir", type=Path)
    capture.add_argument(
        "--skip-start",
        action="store_true",
        help="capture the currently visible page without launching the configured package",
    )
    return result


async def capture_state(
    settings: Settings,
    target: Target,
    output_dir: Path | None,
    start_app: bool = True,
) -> dict:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    directory = (
        output_dir or Path("runtime/calibration") / target.value / timestamp
    ).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    config = load_target_config(settings.config_dir, target)
    driver = build_driver(settings, f"calibration-{target.value}")
    result = {
        "target": target.value,
        "driver": settings.driver,
        "directory": str(directory),
    }
    try:
        if start_app:
            await driver.start_app(config["app_id"])
            await asyncio.sleep(1)
        frame = await driver.capture()
        suffix = ".svg" if frame.media_type == "image/svg+xml" else ".png"
        frame_path = directory / f"screen{suffix}"
        frame_path.write_bytes(frame.body)
        result["screen"] = str(frame_path)
        try:
            controls = await driver.read_controls()
            controls_path = directory / "controls.json"
            controls_path.write_text(
                json.dumps(controls, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            result["controls"] = str(controls_path)
        except Exception as exc:
            result["controls_error"] = str(exc)
        try:
            region = config.get("ocr_region")
            ocr = await driver.read_ocr(tuple(region) if region else None)
            ocr_path = directory / "ocr.json"
            ocr_path.write_text(
                json.dumps(ocr, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            result["ocr"] = str(ocr_path)
        except Exception as exc:
            result["ocr_error"] = str(exc)
        return result
    finally:
        await driver.close()


def main() -> None:
    args = parser().parse_args()
    if args.command == "diagnostics":
        report = build_diagnostics(Settings())
        print(report.model_dump_json(indent=2))
        raise SystemExit(0 if report.ready else 1)
    if args.command == "devices":
        report = discover_devices(Settings(), connect=args.connect)
        print(report.model_dump_json(indent=2))
        raise SystemExit(0 if report.adb_available and not report.error else 1)
    if args.command == "preflight":
        report = build_preflight(Settings())
        print(report.model_dump_json(indent=2))
        raise SystemExit(0 if report.ready else 1)
    if args.command == "capture":
        result = asyncio.run(
            capture_state(
                Settings(),
                Target(args.target),
                args.output_dir,
                start_app=not args.skip_start,
            )
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    host = args.host if args.command == "serve" else "127.0.0.1"
    port = args.port if args.command == "serve" else 8000
    uvicorn.run("app2api.api:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
