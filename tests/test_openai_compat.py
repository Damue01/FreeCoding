import json
from pathlib import Path

from fastapi.testclient import TestClient

from app2api.api import create_app
from app2api.config import Settings


def test_openai_chat_completions_non_stream_and_models(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            driver="mock",
            enabled_targets="meituan",
            config_dir=Path("app2api/target_configs"),
            artifact_dir=tmp_path / "artifacts",
            recording_enabled=False,
            job_timeout_seconds=5,
        )
    )
    with TestClient(app) as client:
        models = client.get("/v1/models")
        assert models.status_code == 200
        assert models.json()["data"][0]["id"] == "meituan_xiaotuan"
        assert models.json()["models"][0]["slug"] == "meituan_xiaotuan"

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "meituan_xiaotuan",
                "messages": [
                    {"role": "system", "content": "ignored transport context"},
                    {"role": "user", "content": "第一问"},
                    {"role": "assistant", "content": "历史回答"},
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": "最后一条用户问题"}],
                    },
                ],
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["object"] == "chat.completion"
        assert body["id"].startswith("chatcmpl-")
        assert body["model"] == "meituan_xiaotuan"
        assert body["choices"][0]["message"]["role"] == "assistant"
        assert "最后一条用户问题" in body["choices"][0]["message"]["content"]
        assert body["choices"][0]["finish_reason"] == "stop"


def test_openai_chat_completions_stream_and_errors(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            driver="mock",
            enabled_targets="meituan",
            config_dir=Path("app2api/target_configs"),
            artifact_dir=tmp_path / "artifacts",
            recording_enabled=False,
            job_timeout_seconds=5,
        )
    )
    with TestClient(app) as client:
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "meituan_xiaotuan",
                "messages": [{"role": "user", "content": "流式协议测试"}],
                "stream": True,
            },
        ) as response:
            assert response.status_code == 200
            lines = [line for line in response.iter_lines() if line]
        assert lines[-1] == "data: [DONE]"
        chunks = [json.loads(line.removeprefix("data: ")) for line in lines[:-1]]
        assert all(chunk["object"] == "chat.completion.chunk" for chunk in chunks)
        assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
        assert chunks[-1]["choices"][0]["finish_reason"] == "stop"

        invalid = client.post(
            "/v1/chat/completions",
            json={
                "model": "unknown",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
        assert invalid.status_code == 404
        assert invalid.json()["error"]["code"] == "model_not_found"


def test_openai_responses_non_stream_and_stream(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            driver="mock",
            enabled_targets="meituan",
            config_dir=Path("app2api/target_configs"),
            artifact_dir=tmp_path / "artifacts",
            recording_enabled=False,
            job_timeout_seconds=5,
        )
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/responses",
            json={
                "model": "meituan_xiaotuan",
                "instructions": "transport instructions are not sent to the phone",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "Responses 普通测试"}
                        ],
                    }
                ],
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["object"] == "response"
        assert body["status"] == "completed"
        assert body["model"] == "meituan_xiaotuan"
        assert body["output"][0]["phase"] == "final_answer"
        assert "Responses 普通测试" in body["output"][0]["content"][0]["text"]

        with client.stream(
            "POST",
            "/v1/responses",
            json={
                "model": "meituan_xiaotuan",
                "input": "Responses 流式测试",
                "stream": True,
            },
        ) as stream_response:
            assert stream_response.status_code == 200
            lines = [line for line in stream_response.iter_lines() if line]

        event_names = [
            line.removeprefix("event: ") for line in lines if line.startswith("event: ")
        ]
        data = [
            json.loads(line.removeprefix("data: "))
            for line in lines
            if line.startswith("data: ")
        ]
        assert event_names[0] == "response.created"
        assert "response.output_text.delta" in event_names
        assert event_names[-1] == "response.completed"
        assert data[-1]["response"]["status"] == "completed"
        assert data[-1]["response"]["output"][0]["phase"] == "final_answer"


def test_lingbao_is_a_separate_openai_model(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            driver="mock",
            enabled_targets="meituan,lingbao",
            config_dir=Path("app2api/target_configs"),
            artifact_dir=tmp_path / "artifacts",
            recording_enabled=False,
            job_timeout_seconds=5,
        )
    )
    with TestClient(app) as client:
        models = client.get("/v1/models").json()
        assert [item["id"] for item in models["data"]] == [
            "meituan_xiaotuan",
            "wangzhe_lingbao",
        ]
        lingbao_catalog = next(
            item for item in models["models"] if item["slug"] == "wangzhe_lingbao"
        )
        assert lingbao_catalog["display_name"] == "Wangzhe Lingbao"

        response = client.post(
            "/v1/responses",
            json={"model": "wangzhe_lingbao", "input": "赵云怎么玩？"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["model"] == "wangzhe_lingbao"
        assert "赵云怎么玩" in body["output"][0]["content"][0]["text"]


def test_xiaohuoren_is_a_separate_openai_model(tmp_path):
    app = create_app(
        Settings(
            _env_file=None,
            driver="mock",
            enabled_targets="xiaohuoren",
            config_dir=Path("app2api/target_configs"),
            artifact_dir=tmp_path / "artifacts",
            recording_enabled=False,
            job_timeout_seconds=5,
        )
    )
    with TestClient(app) as client:
        models = client.get("/v1/models").json()
        assert [item["id"] for item in models["data"]] == ["douyin_xiaohuoren"]
        catalog = next(
            item for item in models["models"] if item["slug"] == "douyin_xiaohuoren"
        )
        assert catalog["display_name"] == "Douyin Xiaohuoren"
