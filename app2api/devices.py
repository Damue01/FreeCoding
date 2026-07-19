from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .models import AndroidDevice, DeviceDiscovery


@dataclass(frozen=True)
class AdbExecutable:
    path: Path
    source: str


def locate_adb(configured: Path | None = None) -> AdbExecutable | None:
    """Locate ADB without modifying PATH or copying third-party binaries."""
    if configured is not None:
        candidate = configured.expanduser().resolve()
        if candidate.is_file():
            return AdbExecutable(candidate, "configured")

    on_path = shutil.which("adb")
    if on_path:
        return AdbExecutable(Path(on_path).resolve(), "path")

    return None


def parse_adb_devices(output: str) -> list[AndroidDevice]:
    devices: list[AndroidDevice] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if (
            not line
            or line.startswith("List of devices attached")
            or line.startswith("*")
        ):
            continue
        parts = re.split(r"\s+", line)
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        properties: dict[str, str] = {}
        for token in parts[2:]:
            key, separator, value = token.partition(":")
            if separator:
                properties[key] = value
        devices.append(
            AndroidDevice(
                serial=serial,
                state=state,
                product=properties.get("product"),
                model=properties.get("model"),
                device=properties.get("device"),
                transport_id=properties.get("transport_id"),
                properties=properties,
            )
        )
    return devices


def _run_adb(
    adb: Path, *arguments: str, timeout: float = 10
) -> subprocess.CompletedProcess[str]:
    startupinfo = None
    creationflags = 0
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        creationflags = subprocess.CREATE_NO_WINDOW
    return subprocess.run(
        [str(adb), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        startupinfo=startupinfo,
        creationflags=creationflags,
    )


def discover_devices(settings: Settings, connect: str | None = None) -> DeviceDiscovery:
    installation = locate_adb(settings.adb_path)
    if installation is None:
        configured = str(settings.adb_path) if settings.adb_path else None
        error = (
            f"Configured ADB does not exist: {configured}"
            if configured
            else "ADB was not found in APP2API_ADB_PATH or on PATH"
        )
        return DeviceDiscovery(adb_available=False, error=error)

    connect_result = None
    try:
        if connect:
            result = _run_adb(installation.path, "connect", connect)
            connect_result = (result.stdout or result.stderr).strip()
            if result.returncode != 0:
                return DeviceDiscovery(
                    adb_available=True,
                    adb_executable=str(installation.path),
                    adb_source=installation.source,
                    connect_result=connect_result,
                    error=f"adb connect exited with code {result.returncode}",
                )

        version_result = _run_adb(installation.path, "version")
        version = next(
            (
                line.strip()
                for line in version_result.stdout.splitlines()
                if line.strip()
            ),
            None,
        )
        devices_result = _run_adb(installation.path, "devices", "-l")
        if devices_result.returncode != 0:
            message = (devices_result.stderr or devices_result.stdout).strip()
            return DeviceDiscovery(
                adb_available=True,
                adb_executable=str(installation.path),
                adb_source=installation.source,
                adb_version=version,
                connect_result=connect_result,
                error=message
                or f"adb devices exited with code {devices_result.returncode}",
            )
        return DeviceDiscovery(
            adb_available=True,
            adb_executable=str(installation.path),
            adb_source=installation.source,
            adb_version=version,
            devices=parse_adb_devices(devices_result.stdout),
            connect_result=connect_result,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return DeviceDiscovery(
            adb_available=True,
            adb_executable=str(installation.path),
            adb_source=installation.source,
            connect_result=connect_result,
            error=str(exc),
        )
