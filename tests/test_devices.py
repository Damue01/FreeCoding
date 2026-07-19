from pathlib import Path
from subprocess import CompletedProcess

from app2api.config import Settings
from app2api.devices import AdbExecutable, discover_devices, parse_adb_devices
from app2api.models import AndroidDevice, DeviceDiscovery
from app2api.preflight import build_preflight

ADB_SAMPLE = """List of devices attached
127.0.0.1:7555 device product:MuMu12 model:V2307A device:emu64xa transport_id:4
emulator-5554 offline transport_id:7
"""


def test_parse_adb_devices_long_format():
    devices = parse_adb_devices(ADB_SAMPLE)
    assert len(devices) == 2
    assert devices[0].serial == "127.0.0.1:7555"
    assert devices[0].state == "device"
    assert devices[0].model == "V2307A"
    assert devices[0].transport_id == "4"
    assert devices[1].state == "offline"


def test_discover_devices_uses_located_adb(monkeypatch):
    executable = Path("C:/tools/adb.exe")
    monkeypatch.setattr(
        "app2api.devices.locate_adb",
        lambda configured=None: AdbExecutable(executable, "path"),
    )

    def fake_run(adb, *arguments, timeout=10):
        if arguments == ("version",):
            return CompletedProcess([], 0, "Android Debug Bridge version 1.0.41\n", "")
        assert arguments == ("devices", "-l")
        return CompletedProcess([], 0, ADB_SAMPLE, "")

    monkeypatch.setattr("app2api.devices._run_adb", fake_run)
    result = discover_devices(Settings())
    assert result.adb_available is True
    assert result.adb_source == "path"
    assert result.adb_version == "Android Debug Bridge version 1.0.41"
    assert len(result.devices) == 2


def test_preflight_checks_configured_packages(monkeypatch):
    device = AndroidDevice(serial="127.0.0.1:7555", state="device", model="MuMu")
    monkeypatch.setattr(
        "app2api.preflight.discover_devices",
        lambda settings: DeviceDiscovery(
            adb_available=True,
            adb_executable="C:/tools/adb.exe",
            adb_source="configured",
            devices=[device],
        ),
    )
    monkeypatch.setattr(
        "app2api.preflight.locate_adb",
        lambda configured=None: AdbExecutable(Path("C:/tools/adb.exe"), "configured"),
    )

    def fake_run(adb, *arguments, timeout=10):
        package = arguments[-1]
        if (
            arguments[-3:-1] == ("list", "packages")
            and package == "com.sankuai.meituan"
        ):
            return CompletedProcess([], 0, "package:com.sankuai.meituan\n", "")
        if arguments[-2] == "path" and package == "com.sankuai.meituan":
            return CompletedProcess([], 0, "package:/data/app/meituan/base.apk\n", "")
        return CompletedProcess([], 0, "", "")

    monkeypatch.setattr("app2api.preflight._run_adb", fake_run)
    result = build_preflight(
        Settings(
            _env_file=None,
            adb_serial=None,
            enabled_targets="meituan",
            config_dir=Path("app2api/target_configs"),
        )
    )
    assert result.ready is True
    assert result.device == device
    statuses = {item.target.value: item.status for item in result.apps}
    assert statuses == {"meituan": "ready"}
