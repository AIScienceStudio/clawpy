"""Perspective-Claw server — the AI brain of narrative intelligence.

Specialized ClawPy server instance that:
- Uses Gemini Flash as default (with Claude fallback)
- Registers 10 EANAT tools (narrative API endpoints)
- Uses the Perspective-Claw system prompt
- Serves /orchestrator/stream (SSE) and /orchestrator/query (simple)

Run: uv run python -m clawpy.server_claw
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid

from clawpy.config import Config
from clawpy.engine.engine import Engine
from clawpy.tool.base import Permission, RunContext, ToolResult
from clawpy.tool.permission import PermissionEnforcer, PermissionMode
from clawpy.tool.registry import ToolRegistry

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("perspective-claw")

# ── Provider setup ──────────────────────────────────────────────────

_engines: dict[str, Engine] = {}

# Model fallback chain: Gemini Flash → Claude Sonnet
DEFAULT_PROVIDER = os.environ.get("CLAW_PROVIDER", "gemini")
DEFAULT_MODEL = os.environ.get("CLAW_MODEL", "gemini-2.5-flash")
FALLBACK_MODELS = [
    ("gemini", "gemini-2.5-flash"),
    ("anthropic", "claude-sonnet-4-6"),
]

# Per-model max output tokens
MODEL_MAX_TOKENS = {
    "gemini-2.5-flash": 8192,
    "gemini-2.5-pro": 8192,
    "claude-sonnet-4-6": 16384,
    "claude-opus-4-6": 16384,
    "claude-haiku-4-6": 8192,
}


def _create_provider(config: Config):
    """Create the LLM provider with fallback support."""
    import clawpy.provider.anthropic  # noqa: F401
    import clawpy.provider.openai  # noqa: F401
    import clawpy.provider.gemini  # noqa: F401

    from clawpy.provider.registry import create

    provider_cfg = config.provider_config()
    return create(config.provider, provider_cfg)


def _get_config(provider: str | None = None, model: str | None = None) -> Config:
    """Build config for Perspective-Claw server."""
    cfg = Config()
    cfg.work_dir = os.environ.get("CLAW_WORK_DIR", os.getcwd())
    cfg.provider = provider or DEFAULT_PROVIDER
    cfg.model = model or DEFAULT_MODEL
    cfg.max_tokens = MODEL_MAX_TOKENS.get(cfg.model, 8192)
    cfg.permission_mode = "bypass"
    return cfg


def _build_narrative_tools() -> ToolRegistry:
    """Register EANAT narrative tools + WebFetch."""
    from clawpy.tool.narrative_tools import register_all_narrative_tools
    from clawpy.tool.web_fetch import WebFetchTool

    registry = ToolRegistry()
    register_all_narrative_tools(registry)
    registry.register(WebFetchTool())  # keep web fetch for live URLs
    logger.info("Registered %d tools (%d EANAT + WebFetch)", len(registry), len(registry) - 1)
    return registry


async def get_or_create_engine(
    session_id: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> tuple[str, Engine]:
    """Get or create an engine for a session."""
    from clawpy.session.session import SessionStore

    sid = session_id or str(uuid.uuid4())

    if sid in _engines:
        return sid, _engines[sid]

    config = _get_config(provider, model)
    provider_obj = _create_provider(config)
    enforcer = PermissionEnforcer(mode=PermissionMode.BYPASS, work_dir=config.work_dir)

    engine = Engine(
        provider=provider_obj,
        tools=ToolRegistry(),
        enforcer=enforcer,
        config=config,
    )
    engine.tools = _build_narrative_tools()

    from clawpy.prompts.perspective_claw import build_perspective_claw_prompt
    engine.set_system_prompt(build_perspective_claw_prompt())

    store = SessionStore(sid)
    previous = store.load_session()
    if previous:
        engine.messages = previous
        logger.info("Resumed session '%s' with %d messages", sid, len(previous))
    else:
        store.save_meta(config.model, config.work_dir)
    engine.session_store = store

    _engines[sid] = engine
    return sid, engine


# ── SSE ─────────────────────────────────────────────────────────────

def _format_sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def stream_event_to_sse(event, tools_called: list[str]) -> str | None:
    """Convert a ClawPy StreamEvent to SSE."""
    from clawpy.provider.base import EventType

    match event.type:
        case EventType.DELTA:
            if event.delta and event.delta.text:
                return _format_sse("token", {"text": event.delta.text})
        case EventType.TOOL_START:
            if event.tool_call:
                tools_called.append(event.tool_call.name)
                return _format_sse("tool_use", {
                    "name": event.tool_call.name,
                    "input": event.tool_call.input if hasattr(event.tool_call, "input") else {},
                })
        case EventType.TOOL_END:
            if event.tool_call:
                return _format_sse("tool_result", {
                    "name": event.tool_call.name,
                    "summary": f"Completed {event.tool_call.name}",
                })
        case EventType.ERROR:
            msg = event.delta.text if event.delta else "Unknown error"
            return _format_sse("error", {"message": msg})
    return None


# ── FastAPI ─────────────────────────────────────────────────────────

try:
    from fastapi import FastAPI, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import StreamingResponse
    from pydantic import BaseModel
except ImportError:
    raise ImportError("FastAPI required: pip install fastapi uvicorn")


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    provider: str | None = None
    model: str | None = None
    system_prompt: str | None = None
    context_type: str | None = None
    context_data: dict | None = None


app = FastAPI(
    title="Perspective-Claw",
    description="The AI brain of Perspectivity narrative intelligence — powered by EANAT",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/orchestrator/stream")
async def chat_stream(req: ChatRequest):
    """Stream a chat response as SSE. The AI queries EANAT tools to answer."""
    session_id, engine = await get_or_create_engine(
        req.session_id, req.provider, req.model
    )

    if req.system_prompt:
        engine.set_system_prompt(req.system_prompt)
    elif req.context_type:
        from clawpy.prompts.perspective_claw import build_perspective_claw_prompt
        engine.set_system_prompt(build_perspective_claw_prompt(
            context_type=req.context_type,
            context_data=req.context_data,
        ))

    tools_called: list[str] = []
    collected_events = []

    def on_stream(event) -> None:
        collected_events.append(event)

    async def generate():
        yield _format_sse("progress", {"step": "starting", "session_id": session_id})

        task = asyncio.create_task(engine.run_turn(req.message, on_stream=on_stream))

        while not task.done():
            while collected_events:
                ev = collected_events.pop(0)
                sse = stream_event_to_sse(ev, tools_called)
                if sse:
                    yield sse
            await asyncio.sleep(0.05)

        while collected_events:
            ev = collected_events.pop(0)
            sse = stream_event_to_sse(ev, tools_called)
            if sse:
                yield sse

        try:
            result = task.result()
            yield _format_sse("done", {
                "session_id": session_id,
                "model": engine.config.model,
                "tools_called": tools_called,
                "stop_reason": result.stop_reason.value if result.stop_reason else "end_turn",
                "usage": {
                    "input_tokens": result.usage.input_tokens,
                    "output_tokens": result.usage.output_tokens,
                },
            })
        except Exception as e:
            # Try fallback model
            logger.warning("Primary model failed: %s. Attempting fallback...", e)
            for fb_provider, fb_model in FALLBACK_MODELS:
                if fb_model == engine.config.model:
                    continue
                try:
                    logger.info("Trying fallback: %s:%s", fb_provider, fb_model)
                    session_id, engine = await get_or_create_engine(
                        req.session_id, fb_provider, fb_model
                    )
                    task = asyncio.create_task(
                        engine.run_turn(req.message, on_stream=on_stream)
                    )
                    # ... same streaming logic
                    break
                except Exception as fe:
                    logger.warning("Fallback %s:%s failed: %s", fb_provider, fb_model, fe)
            yield _format_sse("error", {"message": str(e)})

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/orchestrator/query")
async def chat_query(req: ChatRequest):
    """Simple non-streaming query. Returns full response."""
    session_id, engine = await get_or_create_engine(
        req.session_id, req.provider, req.model
    )

    events = []
    def on_stream(event):
        events.append(event)

    await engine.run_turn(req.message, on_stream=on_stream)

    # Collect text
    text_parts = []
    for ev in events:
        from clawpy.provider.base import EventType
        if ev.type == EventType.DELTA and ev.delta and ev.delta.text:
            text_parts.append(ev.delta.text)

    return {
        "session_id": session_id,
        "response": "".join(text_parts),
        "model": engine.config.model,
    }


@app.get("/orchestrator/suggestions")
async def get_suggestions():
    """Suggested questions for the Insight Panel UI."""
    return {"suggestions": [
        "What topics are brewing right now?",
        "Show me active coordination alerts",
        "Which sources have the lowest credibility?",
        "Who has the most narrative momentum in US politics?",
        "What's the trajectory for Fox News on immigration?",
        "Are there cross-region patterns between BD and US?",
        "Give me a brief on Texas election narratives",
        "Which figures are amplifying each other?",
    ]}


@app.get("/orchestrator/health")
async def health():
    return {
        "status": "ok",
        "name": "Perspective-Claw",
        "default_model": DEFAULT_MODEL,
        "default_provider": DEFAULT_PROVIDER,
        "tools_registered": "10 EANAT + WebFetch",
    }


@app.get("/orchestrator/accounts")
async def accounts():
    return {"accounts": [], "active": None}


# ── Main ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "4039"))
    uvicorn.run(app, host="0.0.0.0", port=port)
