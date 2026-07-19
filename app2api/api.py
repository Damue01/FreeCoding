from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from .anthropic_compat import (
    AnthropicMessagesRequest,
    anthropic_error_body,
    anthropic_message_object,
    anthropic_request_id,
    anthropic_sse,
    anthropic_stream_events,
    last_anthropic_user_text,
)
from .config import Settings
from .devices import discover_devices
from .diagnostics import build_diagnostics
from .models import (
    AskRequest,
    DeviceDiscovery,
    Diagnostics,
    Health,
    Job,
    JobStatus,
    SystemPreflight,
    Target,
)
from .openai_compat import (
    ChatCompletionRequest,
    ResponsesRequest,
    chat_completion,
    chat_completion_chunks,
    codex_model_object,
    error_body,
    last_response_user_text,
    last_user_text,
    model_object,
    response_object,
    response_sse,
    responses_stream_events,
    sse_data,
)
from .preflight import build_preflight
from .service import AutomationService, JobNotFound


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    service = AutomationService(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await service.start()
        yield
        await service.stop()

    app = FastAPI(
        title="FreeCoding",
        version="0.1.0",
        description="把真实应用画面中的 AI 问答流程封装为 OpenAI 和 Anthropic 兼容 HTTP API。",
        lifespan=lifespan,
    )
    app.state.service = service
    model_targets = {
        model_id: Target(target)
        for model_id, target in settings.openai_model_targets().items()
    }

    def find(job_id: str) -> Job:
        try:
            return service.get(job_id)
        except JobNotFound as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc

    @app.get("/health", response_model=Health)
    async def health() -> Health:
        return Health(ok=True, driver=settings.driver, workers=settings.workers)

    @app.get("/v1/system/diagnostics", response_model=Diagnostics)
    async def diagnostics() -> Diagnostics:
        return await asyncio.to_thread(build_diagnostics, settings)

    @app.get("/v1/system/devices", response_model=DeviceDiscovery)
    async def devices() -> DeviceDiscovery:
        return await asyncio.to_thread(discover_devices, settings)

    @app.get("/v1/system/preflight", response_model=SystemPreflight)
    async def preflight() -> SystemPreflight:
        return await asyncio.to_thread(build_preflight, settings)

    @app.post("/v1/ask", response_model=Job, status_code=202)
    async def ask(payload: AskRequest) -> Job:
        if payload.target.value not in settings.enabled_target_values():
            raise HTTPException(status_code=404, detail="target is disabled")
        job = await service.submit(payload)
        if payload.wait:
            timeout = payload.timeout_seconds or settings.job_timeout_seconds + 5
            try:
                return await service.wait(job.id, timeout)
            except TimeoutError as exc:
                raise HTTPException(
                    status_code=504, detail="API wait timed out"
                ) from exc
        return job

    async def execute_protocol(
        target: Target,
        question: str,
        user: str | None,
        protocol: str,
    ) -> Job:
        request = AskRequest(
            target=target,
            question=question,
            metadata={"protocol": protocol, "user": user},
        )
        job = await service.submit(request)
        return await service.wait(job.id, settings.job_timeout_seconds + 5)

    def anthropic_json_error(
        message: str,
        status_code: int,
        error_type: str = "invalid_request_error",
    ) -> JSONResponse:
        request_id = anthropic_request_id()
        return JSONResponse(
            anthropic_error_body(message, error_type, request_id),
            status_code=status_code,
            headers={"request-id": request_id},
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(request: Request, exc: RequestValidationError):
        if request.url.path == "/v1/messages":
            details = "; ".join(
                f"{'.'.join(str(part) for part in error['loc'][1:])}: {error['msg']}"
                for error in exc.errors()
            )
            return anthropic_json_error(details or "Invalid request body", 400)
        return await request_validation_exception_handler(request, exc)

    @app.get("/v1/models")
    async def list_models():
        created = int(time.time())
        return {
            "object": "list",
            "data": [
                model_object(
                    model_id,
                    settings.openai_owned_by,
                    created,
                )
                for model_id in model_targets
            ],
            "models": [
                codex_model_object(model_id, target)
                for model_id, target in model_targets.items()
            ],
        }

    @app.get("/v1/models/{model_id}")
    async def retrieve_model(model_id: str):
        if model_id not in model_targets:
            return JSONResponse(
                error_body(
                    f"The model {model_id!r} does not exist",
                    "invalid_request_error",
                    "model_not_found",
                    "model",
                ),
                status_code=404,
            )
        return model_object(
            model_id,
            settings.openai_owned_by,
            int(time.time()),
        )

    @app.post("/v1/chat/completions")
    async def create_chat_completion(payload: ChatCompletionRequest):
        target = model_targets.get(payload.model)
        if target is None:
            return JSONResponse(
                error_body(
                    f"The model {payload.model!r} does not exist",
                    "invalid_request_error",
                    "model_not_found",
                    "model",
                ),
                status_code=404,
            )
        if payload.n != 1:
            return JSONResponse(
                error_body(
                    "Only n=1 is supported by a single physical device",
                    "invalid_request_error",
                    "unsupported_value",
                    "n",
                ),
                status_code=400,
            )
        try:
            question = last_user_text(payload.messages)
        except ValueError as exc:
            return JSONResponse(
                error_body(
                    str(exc),
                    "invalid_request_error",
                    "invalid_messages",
                    "messages",
                ),
                status_code=400,
            )

        job = await execute_protocol(
            target,
            question,
            payload.user,
            "openai-chat-completions",
        )
        if job.status != JobStatus.SUCCEEDED or job.answer is None:
            return JSONResponse(
                error_body(
                    job.error or "application automation failed",
                    "server_error",
                    "automation_failed",
                ),
                status_code=502,
            )

        if payload.stream:

            async def generate_chat_stream():
                for chunk in chat_completion_chunks(job, payload.model):
                    yield sse_data(chunk)
                yield "data: [DONE]\n\n"

            return StreamingResponse(
                generate_chat_stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache"},
            )

        return chat_completion(job, payload.model)

    @app.post("/v1/responses")
    async def create_response(payload: ResponsesRequest):
        target = model_targets.get(payload.model)
        if target is None:
            return JSONResponse(
                error_body(
                    f"The model {payload.model!r} does not exist",
                    "invalid_request_error",
                    "model_not_found",
                    "model",
                ),
                status_code=404,
            )
        try:
            question = last_response_user_text(payload.input)
        except ValueError as exc:
            return JSONResponse(
                error_body(
                    str(exc),
                    "invalid_request_error",
                    "invalid_input",
                    "input",
                ),
                status_code=400,
            )

        job = await execute_protocol(
            target,
            question,
            payload.user,
            "openai-responses",
        )
        if job.status != JobStatus.SUCCEEDED or job.answer is None:
            return JSONResponse(
                error_body(
                    job.error or "application automation failed",
                    "server_error",
                    "automation_failed",
                ),
                status_code=502,
            )

        if payload.stream:

            async def generate_response_stream():
                for event, value in responses_stream_events(job, payload):
                    yield response_sse(event, value)

            return StreamingResponse(
                generate_response_stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache"},
            )

        return response_object(job, payload)

    @app.post("/v1/messages")
    async def create_anthropic_message(payload: AnthropicMessagesRequest):
        target = model_targets.get(payload.model)
        if target is None:
            return anthropic_json_error(
                f"The model {payload.model!r} does not exist",
                404,
                "not_found_error",
            )
        if payload.max_tokens == 0:
            return anthropic_json_error(
                "max_tokens=0 prompt-cache requests are not supported",
                400,
            )
        if payload.tools:
            return anthropic_json_error(
                "Tools are not supported by physical application models",
                400,
            )
        try:
            question = last_anthropic_user_text(payload.messages)
        except ValueError as exc:
            return anthropic_json_error(str(exc), 400)

        user = payload.metadata.get("user_id")
        if user is not None and not isinstance(user, str):
            user = str(user)

        job = await execute_protocol(
            target,
            question,
            user,
            "anthropic-messages",
        )
        if job.status != JobStatus.SUCCEEDED or job.answer is None:
            return anthropic_json_error(
                job.error or "application automation failed",
                502,
                "api_error",
            )

        if payload.stream:
            request_id = anthropic_request_id()

            async def generate_anthropic_stream():
                for event, value in anthropic_stream_events(job, payload):
                    yield anthropic_sse(event, value)

            return StreamingResponse(
                generate_anthropic_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "request-id": request_id,
                },
            )

        request_id = anthropic_request_id()
        return JSONResponse(
            anthropic_message_object(job, payload),
            headers={"request-id": request_id},
        )

    @app.get("/v1/jobs", response_model=list[Job])
    async def list_jobs(limit: int = Query(default=50, ge=1, le=200)) -> list[Job]:
        return service.list(limit)

    @app.get("/v1/jobs/{job_id}", response_model=Job)
    async def get_job(job_id: str) -> Job:
        return find(job_id)

    @app.get("/v1/jobs/{job_id}/events")
    async def events(job_id: str, after: int = Query(default=0, ge=0)):
        find(job_id)
        return service.events.list(job_id, after)

    @app.get("/v1/jobs/{job_id}/frame")
    async def frame(job_id: str) -> Response:
        find(job_id)
        latest = service.events.frame(job_id)
        if latest is None:
            raise HTTPException(status_code=404, detail="frame not available")
        return Response(latest.body, media_type=latest.media_type)

    @app.get("/v1/jobs/{job_id}/artifacts/{name}")
    async def artifact(job_id: str, name: str):
        find(job_id)
        try:
            path, metadata = service.artifact_path(job_id, name)
        except JobNotFound as exc:
            raise HTTPException(status_code=404, detail="artifact not found") from exc
        return FileResponse(
            path, media_type=metadata.media_type, filename=metadata.name
        )

    @app.get("/v1/jobs/{job_id}/stream")
    async def stream(job_id: str, request: Request, after: int = 0):
        find(job_id)

        async def generate():
            cursor = after
            while not await request.is_disconnected():
                batch = await service.events.wait_for_events(job_id, cursor)
                if not batch:
                    yield ": keep-alive\n\n"
                    continue
                for event in batch:
                    cursor = event.sequence
                    yield f"id: {cursor}\ndata: {json.dumps(event.model_dump(mode='json'), ensure_ascii=False)}\n\n"
                if find(job_id).finished_at is not None:
                    return

        return StreamingResponse(generate(), media_type="text/event-stream")

    dashboard = Path(__file__).with_name("dashboard.html")

    @app.get("/", include_in_schema=False)
    async def root():
        return FileResponse(dashboard)

    return app


app = create_app()
