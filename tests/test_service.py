from pathlib import Path

import pytest

from app2api.config import Settings
from app2api.models import AskRequest, JobStatus, Target
from app2api.service import AutomationService


@pytest.mark.asyncio
async def test_mock_workflow_completes(tmp_path):
    settings = Settings(
        _env_file=None,
        driver="mock",
        config_dir=Path("app2api/target_configs"),
        artifact_dir=tmp_path / "artifacts",
        job_timeout_seconds=5,
    )
    service = AutomationService(settings)
    await service.start()
    try:
        job = await service.submit(
            AskRequest(target=Target.MEITUAN, question="测试问题")
        )
        result = await service.wait(job.id, timeout=5)
        assert result.status == JobStatus.SUCCEEDED
        assert result.answer is not None
        assert "测试问题" in result.answer.text
        assert service.events.frame(job.id) is not None
        assert len(service.events.list(job.id)) >= 5
    finally:
        await service.stop()
