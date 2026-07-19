from __future__ import annotations

from .config import Settings
from .devices import _run_adb, discover_devices, locate_adb
from .models import SystemPreflight, Target, TargetAppStatus
from .workflows import load_target_config


def build_preflight(settings: Settings) -> SystemPreflight:
    discovery = discover_devices(settings)
    if not discovery.adb_available or discovery.error:
        message = discovery.error or "ADB is unavailable"
        return SystemPreflight(ready=False, error=message)

    online = [device for device in discovery.devices if device.state == "device"]
    if settings.adb_serial:
        selected = next(
            (device for device in online if device.serial == settings.adb_serial),
            None,
        )
        if selected is None:
            return SystemPreflight(
                ready=False,
                error=f"Configured Android device is not online: {settings.adb_serial}",
            )
    elif len(online) == 1:
        selected = online[0]
    elif not online:
        return SystemPreflight(ready=False, error="No online Android device was found")
    else:
        return SystemPreflight(
            ready=False,
            error="Multiple Android devices are online; set APP2API_ADB_SERIAL",
        )

    installation = locate_adb(settings.adb_path)
    if installation is None:
        return SystemPreflight(ready=False, device=selected, error="ADB disappeared")

    apps: list[TargetAppStatus] = []
    for target in Target:
        if target.value not in settings.enabled_target_values():
            continue
        try:
            config = load_target_config(settings.config_dir, target)
            app_id = str(config["app_id"])
        except Exception as exc:
            apps.append(
                TargetAppStatus(
                    target=target,
                    app_id="",
                    device_serial=selected.serial,
                    status="error",
                    message=f"Target configuration is invalid: {exc}",
                )
            )
            continue

        result = _run_adb(
            installation.path,
            "-s",
            selected.serial,
            "shell",
            "pm",
            "list",
            "packages",
            app_id,
        )
        output = result.stdout.strip()
        if result.returncode != 0:
            message = (result.stderr or result.stdout).strip()
            apps.append(
                TargetAppStatus(
                    target=target,
                    app_id=app_id,
                    device_serial=selected.serial,
                    status="error",
                    message=message
                    or f"adb shell exited with code {result.returncode}",
                )
            )
        elif any(
            line.removeprefix("package:").strip() == app_id
            for line in output.splitlines()
        ):
            path_result = _run_adb(
                installation.path,
                "-s",
                selected.serial,
                "shell",
                "pm",
                "path",
                app_id,
            )
            package_path = next(
                (
                    line.removeprefix("package:").strip()
                    for line in path_result.stdout.splitlines()
                    if line.startswith("package:")
                ),
                None,
            )
            apps.append(
                TargetAppStatus(
                    target=target,
                    app_id=app_id,
                    device_serial=selected.serial,
                    installed=True,
                    package_path=package_path,
                    status="ready",
                    message="Package is installed",
                )
            )
        else:
            apps.append(
                TargetAppStatus(
                    target=target,
                    app_id=app_id,
                    device_serial=selected.serial,
                    installed=False,
                    status="missing",
                    message="Package is not installed",
                )
            )

    return SystemPreflight(
        ready=bool(apps) and all(app.status == "ready" for app in apps),
        device=selected,
        apps=apps,
    )
