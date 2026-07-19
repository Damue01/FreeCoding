from pathlib import Path

from fastapi.testclient import TestClient

from app2api.api import create_app
from app2api.config import Settings


def test_ask_wait_and_read_events(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            driver="mock",
            enabled_targets="meituan",
            config_dir=Path("app2api/target_configs"),
            artifact_dir=tmp_path / "artifacts",
            job_timeout_seconds=5,
        )
    )
    with TestClient(app) as client:
        diagnostics = client.get("/v1/system/diagnostics")
        assert diagnostics.status_code == 200
        assert diagnostics.json()["ready"] is True
        devices = client.get("/v1/system/devices")
        assert devices.status_code == 200
        assert isinstance(devices.json()["adb_available"], bool)
        preflight = client.get("/v1/system/preflight")
        assert preflight.status_code == 200
        assert isinstance(preflight.json()["ready"], bool)
        response = client.post(
            "/v1/ask",
            json={"target": "meituan", "question": "API 测试", "wait": True},
        )
        assert response.status_code == 202
        job = response.json()
        assert job["status"] == "succeeded"
        assert "API 测试" in job["answer"]["text"]
        assert client.get(f"/v1/jobs/{job['id']}/events").json()
        assert client.get(f"/v1/jobs/{job['id']}/frame").status_code == 200
        kinds = {artifact["kind"] for artifact in job["artifacts"]}
        assert {"event_log", "frame"} <= kinds
        for artifact in job["artifacts"]:
            assert client.get(artifact["url"]).status_code == 200
