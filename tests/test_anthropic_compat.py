import json
from pathlib import Path

import httpx
from anthropic import AsyncAnthropic
from fastapi.testclient import TestClient

from app2api.api import create_app
from app2api.config import Settings


def _app(tmp_path):
    return create_app(
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


def _headers():
    return {
        "x-api-key": "local",
        "anthropic-version": "2023-06-01",
    }


def test_anthropic_messages_non_stream(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        response = client.post(
            "/v1/messages",
            headers=_headers(),
            json={
                "model": "meituan_xiaotuan",
                "max_tokens": 1024,
                "system": "This transport instruction is not sent to the phone.",
                "messages": [
                    {"role": "user", "content": "历史问题"},
                    {"role": "assistant", "content": "历史回答"},
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": "Anthropic 最后一问"}],
                    },
                ],
                "metadata": {"user_id": "test-user"},
            },
        )

        assert response.status_code == 200
        assert response.headers["request-id"].startswith("req_")
        body = response.json()
        assert body["id"].startswith("msg_")
        assert body["type"] == "message"
        assert body["role"] == "assistant"
        assert body["model"] == "meituan_xiaotuan"
        assert body["content"][0]["type"] == "text"
        assert "Anthropic 最后一问" in body["content"][0]["text"]
        assert body["stop_reason"] == "end_turn"
        assert body["stop_sequence"] is None
        assert body["usage"] == {"input_tokens": 0, "output_tokens": 0}


def test_anthropic_messages_stream_event_sequence(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        with client.stream(
            "POST",
            "/v1/messages",
            headers=_headers(),
            json={
                "model": "meituan_xiaotuan",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": "Anthropic 流式测试"}],
                "stream": True,
            },
        ) as response:
            assert response.status_code == 200
            assert response.headers["request-id"].startswith("req_")
            lines = [line for line in response.iter_lines() if line]

        event_names = [
            line.removeprefix("event: ") for line in lines if line.startswith("event: ")
        ]
        data = [
            json.loads(line.removeprefix("data: "))
            for line in lines
            if line.startswith("data: ")
        ]
        assert event_names[0:2] == ["message_start", "content_block_start"]
        assert "content_block_delta" in event_names
        assert event_names[-3:] == [
            "content_block_stop",
            "message_delta",
            "message_stop",
        ]
        assert len(event_names) == len(data)
        assert all(line != "data: [DONE]" for line in lines)

        text = "".join(
            value["delta"]["text"]
            for value in data
            if value["type"] == "content_block_delta"
        )
        assert "Anthropic 流式测试" in text
        message_delta = next(
            value for value in data if value["type"] == "message_delta"
        )
        assert message_delta["delta"]["stop_reason"] == "end_turn"
        assert data[-1] == {"type": "message_stop"}


def test_anthropic_messages_protocol_errors(tmp_path):
    with TestClient(_app(tmp_path)) as client:
        unknown = client.post(
            "/v1/messages",
            headers=_headers(),
            json={
                "model": "unknown",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
        assert unknown.status_code == 404
        assert unknown.json()["type"] == "error"
        assert unknown.json()["error"]["type"] == "not_found_error"
        assert unknown.json()["request_id"] == unknown.headers["request-id"]

        tools = client.post(
            "/v1/messages",
            headers=_headers(),
            json={
                "model": "meituan_xiaotuan",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "hello"}],
                "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
            },
        )
        assert tools.status_code == 400
        assert tools.json()["error"]["type"] == "invalid_request_error"

        assistant_prefill = client.post(
            "/v1/messages",
            headers=_headers(),
            json={
                "model": "meituan_xiaotuan",
                "max_tokens": 10,
                "messages": [{"role": "assistant", "content": "prefix"}],
            },
        )
        assert assistant_prefill.status_code == 400
        assert "final message" in assistant_prefill.json()["error"]["message"]

        missing_max_tokens = client.post(
            "/v1/messages",
            headers=_headers(),
            json={
                "model": "meituan_xiaotuan",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
        assert missing_max_tokens.status_code == 400
        assert missing_max_tokens.json()["type"] == "error"
        assert (
            missing_max_tokens.json()["request_id"]
            == missing_max_tokens.headers["request-id"]
        )

        image_only = client.post(
            "/v1/messages",
            headers=_headers(),
            json={
                "model": "meituan_xiaotuan",
                "max_tokens": 10,
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "image", "source": {}}],
                    }
                ],
            },
        )
        assert image_only.status_code == 400
        assert "text block" in image_only.json()["error"]["message"]


async def test_official_anthropic_sdk_can_call_messages(tmp_path):
    app = _app(tmp_path)
    transport = httpx.ASGITransport(app=app)

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://freecoding.test",
        ) as http_client,
    ):
        client = AsyncAnthropic(
            base_url="http://freecoding.test",
            api_key="local",
            http_client=http_client,
        )
        message = await client.messages.create(
            model="meituan_xiaotuan",
            max_tokens=1024,
            messages=[{"role": "user", "content": "官方 Anthropic SDK 测试"}],
        )
        async with client.messages.stream(
            model="meituan_xiaotuan",
            max_tokens=1024,
            messages=[{"role": "user", "content": "官方 SDK 流式测试"}],
        ) as stream:
            streamed_text = await stream.get_final_text()
            streamed_message = await stream.get_final_message()

    assert message.type == "message"
    assert message.role == "assistant"
    assert message.model == "meituan_xiaotuan"
    assert message.stop_reason == "end_turn"
    assert message.content[0].type == "text"
    assert "官方 Anthropic SDK 测试" in message.content[0].text
    assert "官方 SDK 流式测试" in streamed_text
    assert streamed_message.stop_reason == "end_turn"
