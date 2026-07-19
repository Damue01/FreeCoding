from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .models import Job, Target


class ChatMessageParam(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: Literal["developer", "system", "user", "assistant", "tool", "function"]
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[ChatMessageParam] = Field(min_length=1)
    stream: bool = False
    stream_options: dict[str, Any] | None = None
    n: int = 1
    user: str | None = None


class ResponsesRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    input: str | list[dict[str, Any]]
    instructions: str | list[dict[str, Any]] | None = None
    stream: bool = False
    previous_response_id: str | None = None
    tools: list[dict[str, Any]] = Field(default_factory=list)
    tool_choice: Any = "auto"
    metadata: dict[str, Any] = Field(default_factory=dict)
    user: str | None = None
    store: bool | None = None
    parallel_tool_calls: bool = True
    max_output_tokens: int | None = None
    reasoning: dict[str, Any] | None = None
    text: dict[str, Any] | None = None
    temperature: float | None = None
    top_p: float | None = None
    truncation: str | None = None


def message_text(message: ChatMessageParam) -> str:
    if isinstance(message.content, str):
        return message.content.strip()
    if not isinstance(message.content, list):
        return ""
    values: list[str] = []
    for part in message.content:
        if part.get("type") != "text":
            continue
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            values.append(text.strip())
    return "\n".join(values)


def last_user_text(messages: list[ChatMessageParam]) -> str:
    for message in reversed(messages):
        if message.role == "user":
            text = message_text(message)
            if text:
                return text
    raise ValueError("messages must contain a non-empty user text message")


def _response_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    values: list[str] = []
    for part in content:
        if not isinstance(part, dict) or part.get("type") not in {"input_text", "text"}:
            continue
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            values.append(text.strip())
    return "\n".join(values)


def last_response_user_text(input_value: str | list[dict[str, Any]]) -> str:
    if isinstance(input_value, str):
        if input_value.strip():
            return input_value.strip()
        raise ValueError("input must contain non-empty user text")

    for item in reversed(input_value):
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        text = _response_content_text(item.get("content"))
        if text:
            return text
    raise ValueError("input must contain a non-empty user text message")


def completion_id(job: Job) -> str:
    return f"chatcmpl-{job.id}"


def completion_created(job: Job) -> int:
    timestamp = job.finished_at or job.started_at or job.created_at
    return int(timestamp.timestamp())


def response_id(job: Job) -> str:
    return f"resp-{job.id}"


def response_message_id(job: Job) -> str:
    return f"msg-{job.id}"


def response_object(
    job: Job,
    payload: ResponsesRequest,
    *,
    status: str = "completed",
    include_output: bool = True,
) -> dict[str, Any]:
    if include_output and job.answer is None:
        raise ValueError("automation job has no answer")
    output: list[dict[str, Any]] = []
    if include_output and job.answer is not None:
        output.append(
            {
                "id": response_message_id(job),
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "phase": "final_answer",
                "content": [
                    {
                        "type": "output_text",
                        "text": job.answer.text,
                        "annotations": [],
                    }
                ],
            }
        )
    return {
        "id": response_id(job),
        "object": "response",
        "created_at": completion_created(job),
        "status": status,
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "max_output_tokens": payload.max_output_tokens,
        "model": payload.model,
        "output": output,
        "parallel_tool_calls": payload.parallel_tool_calls,
        "previous_response_id": payload.previous_response_id,
        "reasoning": payload.reasoning or {"effort": None, "summary": None},
        "store": True if payload.store is None else payload.store,
        "temperature": payload.temperature,
        "text": payload.text or {"format": {"type": "text"}},
        "tool_choice": payload.tool_choice,
        "tools": [],
        "top_p": payload.top_p,
        "truncation": payload.truncation or "disabled",
        "usage": None
        if status != "completed"
        else {
            "input_tokens": 0,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 0,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 0,
        },
        "user": payload.user,
        "metadata": payload.metadata,
    }


def responses_stream_events(
    job: Job,
    payload: ResponsesRequest,
    chunk_size: int = 48,
) -> list[tuple[str, dict[str, Any]]]:
    if job.answer is None:
        raise ValueError("automation job has no answer")
    item_id = response_message_id(job)
    text = job.answer.text
    in_progress = response_object(
        job, payload, status="in_progress", include_output=False
    )
    added_item = {
        "id": item_id,
        "type": "message",
        "status": "in_progress",
        "role": "assistant",
        "content": [],
    }
    completed_item = {
        "id": item_id,
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "phase": "final_answer",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }
    events: list[tuple[str, dict[str, Any]]] = [
        ("response.created", {"type": "response.created", "response": in_progress}),
        (
            "response.in_progress",
            {"type": "response.in_progress", "response": in_progress},
        ),
        (
            "response.output_item.added",
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": added_item,
            },
        ),
        (
            "response.content_part.added",
            {
                "type": "response.content_part.added",
                "item_id": item_id,
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            },
        ),
    ]
    for index in range(0, len(text), chunk_size):
        events.append(
            (
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "item_id": item_id,
                    "output_index": 0,
                    "content_index": 0,
                    "delta": text[index : index + chunk_size],
                },
            )
        )
    events.extend(
        [
            (
                "response.output_text.done",
                {
                    "type": "response.output_text.done",
                    "item_id": item_id,
                    "output_index": 0,
                    "content_index": 0,
                    "text": text,
                },
            ),
            (
                "response.content_part.done",
                {
                    "type": "response.content_part.done",
                    "item_id": item_id,
                    "output_index": 0,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": text, "annotations": []},
                },
            ),
            (
                "response.output_item.done",
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": completed_item,
                },
            ),
            (
                "response.completed",
                {
                    "type": "response.completed",
                    "response": response_object(job, payload),
                },
            ),
        ]
    )
    return events


def chat_completion(job: Job, model: str) -> dict[str, Any]:
    if job.answer is None:
        raise ValueError("automation job has no answer")
    return {
        "id": completion_id(job),
        "object": "chat.completion",
        "created": completion_created(job),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": job.answer.text,
                },
                "logprobs": None,
                "finish_reason": "stop",
            }
        ],
    }


def chat_completion_chunks(
    job: Job,
    model: str,
    chunk_size: int = 48,
) -> list[dict[str, Any]]:
    if job.answer is None:
        raise ValueError("automation job has no answer")
    common = {
        "id": completion_id(job),
        "object": "chat.completion.chunk",
        "created": completion_created(job),
        "model": model,
    }
    chunks: list[dict[str, Any]] = []
    text = job.answer.text
    for index in range(0, len(text), chunk_size):
        delta: dict[str, Any] = {"content": text[index : index + chunk_size]}
        if index == 0:
            delta["role"] = "assistant"
        chunks.append(
            {
                **common,
                "choices": [
                    {
                        "index": 0,
                        "delta": delta,
                        "logprobs": None,
                        "finish_reason": None,
                    }
                ],
            }
        )
    chunks.append(
        {
            **common,
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "logprobs": None,
                    "finish_reason": "stop",
                }
            ],
        }
    )
    return chunks


def sse_data(value: dict[str, Any]) -> str:
    return f"data: {json.dumps(value, ensure_ascii=False)}\n\n"


def response_sse(event: str, value: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(value, ensure_ascii=False)}\n\n"


def error_body(
    message: str,
    error_type: str,
    code: str | None = None,
    param: str | None = None,
) -> dict[str, Any]:
    return {
        "error": {
            "message": message,
            "type": error_type,
            "param": param,
            "code": code,
        }
    }


def model_object(model_id: str, owned_by: str, created: int) -> dict[str, Any]:
    return {
        "id": model_id,
        "object": "model",
        "created": created,
        "owned_by": owned_by,
    }


def codex_model_object(
    model_id: str, target: Target = Target.MEITUAN
) -> dict[str, Any]:
    if target == Target.LINGBAO:
        display_name = "Wangzhe Lingbao"
        description = "Wangzhe Rongyao Lingbao through the local FreeCoding provider."
        instructions = (
            "Return the answer from the connected Wangzhe Rongyao Lingbao application."
        )
    elif target == Target.XIAOHUOREN:
        display_name = "Douyin Xiaohuoren"
        description = "Douyin Xiaohuoren through the local FreeCoding provider."
        instructions = "Return the answer from Xiaohuoren in the connected Douyin chat."
    else:
        display_name = "Meituan Xiaotuan"
        description = "Meituan Xiaotuan through the local FreeCoding provider."
        instructions = (
            "Return the answer from the connected Meituan Xiaotuan application."
        )
    return {
        "slug": model_id,
        "display_name": display_name,
        "description": description,
        "default_reasoning_level": "medium",
        "supported_reasoning_levels": [
            {"effort": "low", "description": "Direct application response"},
            {"effort": "medium", "description": "Direct application response"},
            {"effort": "high", "description": "Direct application response"},
        ],
        "shell_type": "shell_command",
        "visibility": "list",
        "supported_in_api": True,
        "priority": 1,
        "additional_speed_tiers": [],
        "service_tiers": [],
        "availability_nux": None,
        "upgrade": None,
        "base_instructions": instructions,
        "include_skills_usage_instructions": False,
        "supports_reasoning_summaries": False,
        "default_reasoning_summary": "none",
        "support_verbosity": False,
        "default_verbosity": "low",
        "apply_patch_tool_type": "freeform",
        "web_search_tool_type": "text",
        "truncation_policy": {"mode": "bytes", "limit": 10000},
        "supports_parallel_tool_calls": False,
        "supports_image_detail_original": False,
        "context_window": 272000,
        "max_context_window": 272000,
        "effective_context_window_percent": 95,
        "experimental_supported_tools": [],
        "input_modalities": ["text"],
        "supports_search_tool": False,
        "use_responses_lite": False,
    }
