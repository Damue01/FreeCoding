from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent / "target_configs"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APP2API_", env_file=".env", extra="ignore"
    )

    driver: str = "mock"
    enabled_targets: str = "meituan,lingbao,xiaohuoren"
    adb_path: Path | None = None
    adb_serial: str | None = None
    vivo_window_process: str = "vivoScreen.exe"
    vivo_window_title_re: str = ".*"
    vivo_clipboard_sync_seconds: float = Field(default=2.5, ge=0.2, le=5)
    template_root: Path = Path("assets/templates")
    artifact_dir: Path = Path("runtime/artifacts")
    recording_enabled: bool = True
    workers: int = Field(default=1, ge=1, le=8)
    job_timeout_seconds: float = Field(default=120, gt=0)
    event_history: int = Field(default=500, ge=50)
    config_dir: Path = DEFAULT_CONFIG_DIR
    openai_model_id: str = "meituan_xiaotuan"
    openai_lingbao_model_id: str = "wangzhe_lingbao"
    openai_xiaohuoren_model_id: str = "douyin_xiaohuoren"
    openai_owned_by: str = "freecoding"

    def enabled_target_values(self) -> set[str]:
        return {
            value.strip().casefold()
            for value in self.enabled_targets.split(",")
            if value.strip()
        }

    def openai_model_targets(self) -> dict[str, str]:
        enabled = self.enabled_target_values()
        pairs = {
            self.openai_model_id: "meituan",
            self.openai_lingbao_model_id: "lingbao",
            self.openai_xiaohuoren_model_id: "xiaohuoren",
        }
        return {
            model_id: target for model_id, target in pairs.items() if target in enabled
        }
