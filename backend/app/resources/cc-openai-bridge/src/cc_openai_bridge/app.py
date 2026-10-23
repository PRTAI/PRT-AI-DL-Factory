"""Standalone extraction of the deployed Claude Code protocol bridge.

The conversion itself is performed by pinned LiteLLM, exactly as in production.
No file access tools, shell execution, model downgrade, or judge selection lives here.
"""

import asyncio
import hmac
import json
import os
import re
from importlib.metadata import version

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .settings import Settings, valid_model

FORWARDED_FIELDS = {
    "messages",
    "max_tokens",
    "system",
    "stream",
    "tools",
    "tool_choice",
    "thinking",
    "temperature",
    "top_p",
    "top_k",
    "stop_sequences",
    "metadata",
    "output_config",
    "context_management",
}


def patch_tool_arguments():
    """Emit each tool_use input once, from the finished Responses item.

    LiteLLM routes argument deltas by ``item_id``. Some OpenAI-compatible upstreams
    send parallel calls whose deltas carry a different/missing ``item_id``, so every
    delta lands on the last block and Claude Code receives ``{}`` for each tool.
    The finished item (or ``arguments.done``) is authoritative per call.
    """
    from litellm.llms.anthropic.pass_through.responses_adapters import streaming_iterator

    wrapper = streaming_iterator.AnthropicResponsesStreamWrapper
    if getattr(wrapper, "_prt_tool_arguments", False):
        return
    original = wrapper._process_event

    def process(self, event):
        field = self._field
        kind = field(event, "type")
        state = self.__dict__.setdefault("_prt_tools", {"blocks": {}, "args": {}})
        if kind == "response.output_item.added" and field(field(event, "item"), "type") == "function_call":
            original(self, event)
            state["blocks"][field(event, "output_index")] = self._current_block_index
            return
        # output_index identifies the call even when item_id differs between events.
        if kind == "response.function_call_arguments.delta":
            key = field(event, "output_index")
            if key is None:
                key = field(event, "item_id")
            state["args"][key] = state["args"].get(key, "") + (field(event, "delta") or "")
            return
        if kind == "response.function_call_arguments.done":
            key = field(event, "output_index")
            if key is None:
                key = field(event, "item_id")
            state["args"][key] = field(event, "arguments") or ""
            return
        item = field(event, "item")
        if kind == "response.output_item.done" and field(item, "type") == "function_call":
            index, item_id = field(event, "output_index"), field(item, "id")
            block = self._item_id_to_block_index.get(item_id) if item_id else None
            if block is None:
                block = state["blocks"].get(index)
            if block is None:
                original(self, {"type": "response.output_item.added", "item": item})
                block = self._current_block_index
            arguments = (field(item, "arguments") or state["args"].get(index)
                         or state["args"].get(item_id) or "")
            if arguments:
                self._chunk_queue.append({
                    "type": "content_block_delta", "index": block,
                    "delta": {"type": "input_json_delta", "partial_json": arguments},
                })
            self._chunk_queue.append({"type": "content_block_stop", "index": block})
            return
        original(self, event)

    wrapper._process_event = process
    wrapper._prt_tool_arguments = True


def create_app(settings: Settings, completion=None, token_counter=None):
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    import litellm

    patch_tool_arguments()

    invoke = completion or litellm.anthropic.messages.acreate
    count = token_counter or litellm.token_counter
    capacity = asyncio.Semaphore(settings.max_concurrency)
    app = FastAPI(
        title="Claude Code OpenAI Bridge", docs_url=None, redoc_url=None, openapi_url=None
    )

    def error_message(exc):
        text = str(exc)
        for secret in (settings.upstream_api_key, settings.bridge_api_key):
            text = text.replace(secret, "***")
        return re.sub(r"sk-[A-Za-z0-9_-]{10,}", "sk-***", text)[:1000]

    def error_body(exc):
        return {"type": "error", "error": {"type": "api_error", "message": error_message(exc)}}

    async def payload(request):
        supplied = request.headers.get("x-api-key", "")
        if not supplied:
            supplied = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not hmac.compare_digest(supplied.encode(), settings.bridge_api_key.encode()):
            raise HTTPException(401, "Invalid bridge credential")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > settings.max_request_bytes:
                raise HTTPException(413, "Request exceeds bridge size limit")
        try:
            body = json.loads(raw)
        except ValueError as exc:
            raise HTTPException(400, "Invalid JSON") from exc
        if not isinstance(body, dict):
            raise HTTPException(400, "Request must be a JSON object")
        model = body.get("model")
        if not valid_model(model):
            raise HTTPException(400, "A valid model ID is required")
        if model not in settings.allowed_models:
            raise HTTPException(400, "Model is not enabled in BRIDGE_ALLOWED_MODELS")
        return model, {k: v for k, v in body.items() if k in FORWARDED_FIELDS}

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "adapter": "litellm",
            "adapter_version": version("litellm"),
            "models": settings.allowed_models,
        }

    @app.post("/v1/messages/count_tokens")
    async def count_tokens(request: Request):
        model, body = await payload(request)
        # Compatibility estimate, not authoritative provider billing or context accounting.
        value = await asyncio.to_thread(
            count, model=model, text=json.dumps(body, ensure_ascii=False)
        )
        return {"input_tokens": value}

    @app.post("/v1/messages")
    async def messages(request: Request):
        model, body = await payload(request)
        await capacity.acquire()
        try:
            result = await invoke(
                model="openai/" + model,
                api_base=settings.upstream_base_url,
                api_key=settings.upstream_api_key,
                timeout=settings.upstream_timeout_seconds,
                **body,
            )
        except Exception as exc:
            capacity.release()
            status = getattr(exc, "status_code", 502)
            return JSONResponse(
                error_body(exc),
                status_code=(status if isinstance(status, int) and 400 <= status <= 599 else 502),
            )
        except BaseException:
            capacity.release()
            raise
        if not body.get("stream"):
            capacity.release()
            return result

        async def events():
            try:
                async for event in result:
                    if isinstance(event, dict):
                        yield f"event: {event.get('type', 'message')}\ndata: {json.dumps(event)}\n\n"
                    else:
                        yield event
            except Exception as exc:
                # Headers may already be sent. Send an explicit, sanitized SSE
                # error rather than silently finishing or exposing a credential.
                yield f"event: error\ndata: {json.dumps(error_body(exc))}\n\n"
            finally:
                try:
                    if hasattr(result, "aclose"):
                        await result.aclose()
                finally:
                    capacity.release()

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app
