from __future__ import annotations

import json
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .models import Job


class AnthropicMessageParam(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: Literal["user", "assistant"]
    content: str | list[dict[str, Any]]


class AnthropicMessagesRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    max_tokens: int = Field(ge=0)
    messages: list[AnthropicMessageParam] = Field(min_length=1)
    system: str | list[dict[str, Any]] | None = None
    stream: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    stop_sequences: list[str] = Field(default_factory=list)
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    tools: list[dict[str, Any]] = Field(default_factory=list)


def anthropic_request_id() -> str:
    return f"req_{uuid4().hex}"


def anthropic_message_id(job: Job) -> str:
    return f"msg_{job.id}"


def _content_text(content: str | list[dict[str, Any]]) -> str:
    if isinstance(content, str):
        return content.strip()
    values: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            values.append(text.strip())
    return "\n".join(values)


def last_anthropic_user_text(messages: list[AnthropicMessageParam]) -> str:
    if messages[-1].role != "user":
        raise ValueError("The final message must use the user role")
    text = _content_text(messages[-1].content)
    if not text:
        raise ValueError("The final user message must contain a non-empty text block")
    return text


def anthropic_message_object(
    job: Job,
    payload: AnthropicMessagesRequest,
) -> dict[str, Any]:
    if job.answer is None:
        raise ValueError("automation job has no answer")
    return {
        "id": anthropic_message_id(job),
        "type": "message",
        "role": "assistant",
        "model": payload.model,
        "content": [{"type": "text", "text": job.answer.text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
        },
    }


def anthropic_stream_events(
    job: Job,
    payload: AnthropicMessagesRequest,
    chunk_size: int = 48,
) -> list[tuple[str, dict[str, Any]]]:
    if job.answer is None:
        raise ValueError("automation job has no answer")
    message_id = anthropic_message_id(job)
    text = job.answer.text
    events: list[tuple[str, dict[str, Any]]] = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": message_id,
                    "type": "message",
                    "role": "assistant",
                    "model": payload.model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
    ]
    for index in range(0, len(text), chunk_size):
        events.append(
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {
                        "type": "text_delta",
                        "text": text[index : index + chunk_size],
                    },
                },
            )
        )
    events.extend(
        [
            (
                "content_block_stop",
                {"type": "content_block_stop", "index": 0},
            ),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {
                        "stop_reason": "end_turn",
                        "stop_sequence": None,
                    },
                    "usage": {"output_tokens": 0},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ]
    )
    return events


def anthropic_sse(event: str, value: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(value, ensure_ascii=False)}\n\n"


def anthropic_error_body(
    message: str,
    error_type: str = "invalid_request_error",
    request_id: str | None = None,
) -> dict[str, Any]:
    return {
        "type": "error",
        "error": {"type": error_type, "message": message},
        "request_id": request_id or anthropic_request_id(),
    }
