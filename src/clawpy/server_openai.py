"""OpenAI-compatible HTTP surface for ClawPy — /v1/chat/completions + /v1/models.

WHY THIS EXISTS
───────────────
ClawPy is our central AI harness (multi-provider Claude/Anthropic-first with
OAuth against a Pro/Max subscription pool, MCP client, agentic engine, tools).
Every other service in the stack (Hermes, LangGraph pipeline, ai_native_api,
future agents) needs to talk to Claude via ClawPy so we get one auth surface,
one usage/cost accounting, one place to add caching / hooks / rate limits.

But most of those clients only speak the OpenAI Chat Completions dialect:
`POST /v1/chat/completions` with tools + vision + streaming, response has
`choices[0].message.tool_calls`. Hermes explicitly requires this format.

This module gives ClawPy that dialect on top of the existing neutral engine
and provider layer — a thin bidirectional translation, NO extra dependency
beyond the existing FastAPI/pydantic that server.py already uses.

DESIGN
──────
This is PASSTHROUGH mode — the endpoint proxies straight to the configured
provider:
    OpenAI request → neutral Request → provider.stream()/send() → neutral →
    OpenAI response.

The caller (Hermes, our own agent code) owns the tool loop: it gets the
tool_calls back and executes them client-side. ClawPy does NOT run its own
engine here; that would double-up on tool execution and confuse the caller.

For the "ClawPy runs the agent for you" use case, use the existing
/orchestrator/* endpoints instead.

SUPPORTS
────────
- ✅ Streaming (SSE, OpenAI chunk shape, `data: [DONE]` terminator)
- ✅ Non-streaming
- ✅ Tool calls (bidirectional — request tools, response tool_calls)
- ✅ Vision (image_url data URIs + external URLs — fetched to base64)
- ✅ System messages (extracted to Anthropic's system field)
- ✅ Multi-turn history with tool_call_id linkage
- ✅ /v1/models — enumerates the provider's model list

DOES NOT SUPPORT (yet — add when needed)
────────────────────────────────────────
- ❌ n > 1 completions per request (rarely used, Claude doesn't natively)
- ❌ logprobs (Claude doesn't expose)
- ❌ function_call (deprecated; caller should use `tools` not `functions`)
- ❌ response_format (JSON mode) — use ClawPy's json_mode via /orchestrator instead
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from typing import Any, AsyncIterator, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request as FastAPIRequest
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from clawpy.config.config import Config
from clawpy.provider.base import (
    EventType,
    Request as NeutralRequest,
    StopReason,
    ToolSpec,
)
from clawpy.types import (
    ContentBlock,
    ContentType,
    ImageData,
    Message,
    Role,
    ToolCall,
    ToolResult,
)

logger = logging.getLogger("clawpy.server_openai")

router = APIRouter(prefix="/v1", tags=["openai-compat"])


# ─────────────────────────────────────────────────── Request/Response models ──

class ChatMessage(BaseModel):
    role: str
    content: Any = None                 # str OR list of content parts OR null
    name: Optional[str] = None
    tool_calls: Optional[list[dict[str, Any]]] = None
    tool_call_id: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    model: str = ""
    messages: list[ChatMessage]
    tools: Optional[list[dict[str, Any]]] = None
    tool_choice: Any = None            # ignored — Claude auto-decides
    stream: bool = False
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    stop: Optional[Any] = None
    user: Optional[str] = None

    # Non-standard passthrough (accepted, ignored for now):
    top_p: Optional[float] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None
    n: Optional[int] = None
    logprobs: Optional[bool] = None
    response_format: Optional[dict[str, Any]] = None


# ─────────────────────────────────────────────────── OpenAI → neutral ──

async def _fetch_image_to_bytes(url: str) -> tuple[str, bytes]:
    """Return (media_type, bytes) for a data URI or http(s) URL."""
    if url.startswith("data:"):
        # data:image/jpeg;base64,AAAA...
        try:
            header, b64 = url.split(",", 1)
            media_type = header.split(";", 1)[0].removeprefix("data:") or "image/jpeg"
            return media_type, base64.b64decode(b64)
        except Exception as e:
            raise HTTPException(400, f"bad data URI: {e}")
    # External URL — fetch it.
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.get(url)
        if r.status_code != 200:
            raise HTTPException(400, f"image fetch {url[:80]} → {r.status_code}")
        media_type = r.headers.get("content-type", "image/jpeg").split(";", 1)[0]
        return media_type, r.content


async def _openai_content_parts_to_blocks(content: Any) -> list[ContentBlock]:
    """Convert an OpenAI message.content field to neutral ContentBlocks.

    Handles: str (single text block), list of parts (text + image_url), None.
    """
    if content is None:
        return []
    if isinstance(content, str):
        return [ContentBlock(type=ContentType.TEXT, text=content)]
    if not isinstance(content, list):
        raise HTTPException(400, f"unsupported content type: {type(content).__name__}")

    blocks: list[ContentBlock] = []
    for part in content:
        if not isinstance(part, dict):
            raise HTTPException(400, "content parts must be objects")
        ptype = part.get("type")
        if ptype == "text":
            blocks.append(ContentBlock(type=ContentType.TEXT, text=part.get("text") or ""))
        elif ptype == "image_url":
            image_url = part.get("image_url") or {}
            url = image_url.get("url") if isinstance(image_url, dict) else image_url
            if not url:
                raise HTTPException(400, "image_url part missing url")
            media_type, data = await _fetch_image_to_bytes(url)
            blocks.append(ContentBlock(
                type=ContentType.IMAGE,
                image=ImageData(media_type=media_type, data=data),
            ))
        else:
            # Unknown parts get ignored with a warning rather than 400, so future
            # OpenAI parts don't break existing clients.
            logger.warning("ignoring unknown content part type=%s", ptype)
    return blocks


def _openai_tools_to_toolspecs(tools: Optional[list[dict[str, Any]]]) -> list[ToolSpec]:
    if not tools:
        return []
    specs: list[ToolSpec] = []
    for t in tools:
        if t.get("type") != "function":
            # We only understand OpenAI function-style tools.
            logger.warning("ignoring tool with type=%s (only 'function' supported)", t.get("type"))
            continue
        fn = t.get("function") or {}
        name = fn.get("name")
        if not name:
            raise HTTPException(400, "tool.function.name is required")
        specs.append(ToolSpec(
            name=name,
            description=fn.get("description") or "",
            input_schema=fn.get("parameters") or {"type": "object", "properties": {}},
        ))
    return specs


async def _openai_to_neutral_request(req: ChatCompletionRequest, default_model: str) -> NeutralRequest:
    """Full request conversion. `default_model` is used when req.model is empty
    or matches an alias — real model IDs still go through as-is."""
    system_parts: list[str] = []
    messages: list[Message] = []

    for m in req.messages:
        role_str = (m.role or "").lower()

        # SYSTEM → collect into system_prompt
        if role_str == "system":
            if isinstance(m.content, str):
                system_parts.append(m.content)
            elif isinstance(m.content, list):
                # Rare, but a system message can be parts too
                for part in m.content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        system_parts.append(part.get("text") or "")
            continue

        # TOOL role → convert to a user message carrying a ToolResult
        if role_str == "tool":
            if not m.tool_call_id:
                raise HTTPException(400, "tool message missing tool_call_id")
            result_text = m.content if isinstance(m.content, str) else json.dumps(m.content)
            messages.append(Message(
                role=Role.USER,
                content=[ContentBlock(
                    type=ContentType.TOOL_RESULT,
                    tool_result=ToolResult(
                        tool_call_id=m.tool_call_id,
                        content=result_text,
                        is_error=False,
                    ),
                )],
            ))
            continue

        # USER / ASSISTANT
        try:
            role = Role(role_str)
        except ValueError:
            raise HTTPException(400, f"unknown role: {m.role}")

        blocks = await _openai_content_parts_to_blocks(m.content)

        # Assistant tool_calls (Claude terminology: tool_use)
        if m.tool_calls:
            for tc in m.tool_calls:
                if tc.get("type") != "function":
                    continue
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                blocks.append(ContentBlock(
                    type=ContentType.TOOL_CALL,
                    tool_call=ToolCall(
                        id=tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                        name=fn.get("name") or "",
                        input=args,
                    ),
                ))

        if blocks:
            messages.append(Message(role=role, content=blocks))

    return NeutralRequest(
        model=req.model or default_model,
        system="\n\n".join(p for p in system_parts if p),
        messages=messages,
        tools=_openai_tools_to_toolspecs(req.tools),
        max_tokens=req.max_tokens or 8192,
        temperature=req.temperature,
        stop_sequences=[req.stop] if isinstance(req.stop, str) else (req.stop or []),
    )


# ─────────────────────────────────────────────────── neutral → OpenAI ──

_STOP_TO_FINISH = {
    StopReason.END_TURN: "stop",
    StopReason.TOOL_USE: "tool_calls",
    StopReason.MAX_TOKENS: "length",
    StopReason.STOP_SEQUENCE: "stop",
}


def _blocks_to_openai_message(blocks: list[ContentBlock]) -> dict[str, Any]:
    """Serialize neutral assistant content back to an OpenAI message object."""
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for b in blocks:
        if b.type == ContentType.TEXT and b.text:
            text_parts.append(b.text)
        elif b.type == ContentType.TOOL_CALL and b.tool_call:
            tool_calls.append({
                "id": b.tool_call.id,
                "type": "function",
                "function": {
                    "name": b.tool_call.name,
                    "arguments": json.dumps(b.tool_call.input, ensure_ascii=False),
                },
            })
        # THINKING blocks are dropped — OpenAI has no equivalent field.
    out: dict[str, Any] = {"role": "assistant"}
    if text_parts:
        out["content"] = "".join(text_parts)
    else:
        out["content"] = None
    if tool_calls:
        out["tool_calls"] = tool_calls
    return out


def _completion_response(model: str, blocks: list[ContentBlock],
                         stop_reason: StopReason, usage_in: int, usage_out: int) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:16]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": _blocks_to_openai_message(blocks),
            "finish_reason": _STOP_TO_FINISH.get(stop_reason, "stop"),
        }],
        "usage": {
            "prompt_tokens": usage_in,
            "completion_tokens": usage_out,
            "total_tokens": usage_in + usage_out,
        },
    }


# ─────────────────────────────────────────────────── streaming translation ──

async def _stream_openai_chunks(model: str, provider, request: NeutralRequest) -> AsyncIterator[str]:
    """Consume the provider's neutral StreamEvents, emit OpenAI SSE chunks."""
    cmpl_id = f"chatcmpl-{uuid.uuid4().hex[:16]}"
    created = int(time.time())

    def _chunk(delta: dict[str, Any], finish: Optional[str] = None) -> str:
        obj = {
            "id": cmpl_id, "object": "chat.completion.chunk",
            "created": created, "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return f"data: {json.dumps(obj)}\n\n"

    # Initial role chunk (OpenAI clients rely on this to open the assistant slot).
    yield _chunk({"role": "assistant"})

    # Tool-call accumulator: OpenAI streams tool_calls as indexed deltas.
    tool_index_by_id: dict[str, int] = {}

    try:
        async for ev in provider.stream(request):
            if ev.type == EventType.DELTA:
                if ev.delta and ev.delta.text:
                    yield _chunk({"content": ev.delta.text})
            elif ev.type == EventType.TOOL_START:
                tc = ev.tool_call
                if not tc:
                    continue
                idx = len(tool_index_by_id)
                tool_index_by_id[tc.id] = idx
                yield _chunk({
                    "tool_calls": [{
                        "index": idx, "id": tc.id, "type": "function",
                        "function": {"name": tc.name, "arguments": ""},
                    }],
                })
            elif ev.type == EventType.TOOL_DELTA:
                tc = ev.tool_call
                if not tc:
                    continue
                idx = tool_index_by_id.get(tc.id, 0)
                # Provider gave us the incremental JSON fragment via input dict → serialize
                yield _chunk({
                    "tool_calls": [{
                        "index": idx,
                        "function": {"arguments": json.dumps(tc.input, ensure_ascii=False)},
                    }],
                })
            elif ev.type == EventType.TOOL_END:
                # Nothing to emit — TOOL_END is a marker; args were streamed in DELTA.
                pass
            elif ev.type == EventType.MESSAGE_STOP:
                finish = _STOP_TO_FINISH.get(ev.stop_reason or StopReason.END_TURN, "stop")
                yield _chunk({}, finish=finish)
            elif ev.type == EventType.ERROR:
                err_msg = str(ev.error) if ev.error else "unknown provider error"
                # Emit as OpenAI-style error chunk (some clients expect this).
                yield f"data: {json.dumps({'error': {'message': err_msg, 'type': 'provider_error'}})}\n\n"
                break
    except Exception as e:
        logger.exception("provider stream failed")
        yield f"data: {json.dumps({'error': {'message': str(e), 'type': 'server_error'}})}\n\n"

    yield "data: [DONE]\n\n"


# ─────────────────────────────────────────────────── routes ──

def _get_provider_and_config(app_state) -> tuple[Any, Config]:
    """Reuse the provider/config already initialized by server.py."""
    # server.py stores these on app.state during startup.
    provider = getattr(app_state, "openai_provider", None) or getattr(app_state, "provider", None)
    config = getattr(app_state, "config", None)
    if provider is None or config is None:
        # Fallback: build one on demand.
        import clawpy.provider.anthropic  # noqa: F401
        import clawpy.provider.openai  # noqa: F401
        from clawpy.provider.registry import create as create_provider
        config = Config.load()
        provider = create_provider(config.provider, config.provider_config())
    return provider, config


@router.get("/models")
async def list_models(request: FastAPIRequest) -> JSONResponse:
    """OpenAI-compat: enumerate the models the underlying provider offers."""
    provider, config = _get_provider_and_config(request.app.state)
    try:
        model_ids = provider.models() or [config.model or "claude"]
    except Exception:
        model_ids = [config.model or "claude"]
    return JSONResponse({
        "object": "list",
        "data": [
            {"id": mid, "object": "model", "created": int(time.time()), "owned_by": provider.name}
            for mid in model_ids
        ],
    })


@router.post("/chat/completions")
async def chat_completions(request: FastAPIRequest) -> Any:
    """OpenAI-compat: POST /v1/chat/completions.

    Passthrough proxy — no ClawPy engine, no ClawPy tools. The caller owns
    the tool loop. Returns tool_calls in the response if the model asked
    for them; caller executes and sends back a follow-up request.
    """
    body = await request.json()
    try:
        req = ChatCompletionRequest.model_validate(body)
    except Exception as e:
        raise HTTPException(400, f"bad request body: {e}")

    provider, config = _get_provider_and_config(request.app.state)
    neutral_req = await _openai_to_neutral_request(req, default_model=config.model or "claude")

    logger.info(
        "chat/completions model=%s stream=%s msgs=%d tools=%d",
        neutral_req.model, req.stream, len(neutral_req.messages), len(neutral_req.tools),
    )

    if req.stream:
        return StreamingResponse(
            _stream_openai_chunks(neutral_req.model, provider, neutral_req),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # Non-streaming: use provider.send()
    try:
        resp = await provider.send(neutral_req)
    except Exception as e:
        logger.exception("provider.send failed")
        raise HTTPException(502, f"provider error: {e}")

    return JSONResponse(_completion_response(
        model=neutral_req.model,
        blocks=list(resp.content),
        stop_reason=resp.stop_reason,
        usage_in=resp.usage.input_tokens,
        usage_out=resp.usage.output_tokens,
    ))
