"""Anthropic Messages API provider.

Talks directly to the Anthropic API via httpx — no SDK dependency.
Converts between neutral types and Anthropic's native API format.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)

import httpx

from clawpy.config.config import ProviderConfig
from clawpy.provider.base import (
    Delta,
    EventType,
    Request,
    Response,
    StopReason,
    StreamEvent,
    ToolSpec,
    Usage,
)
from clawpy.provider.registry import register
from clawpy.provider.sse import parse_sse_with_event
from clawpy.types import ContentBlock, ContentType, ToolCall

_API_VERSION = "2023-06-01"
_DEFAULT_BASE_URL = "https://api.anthropic.com"
_CLAWPY_VERSION = "0.1.0"

# Beta headers required for OAuth + Claude Code features
_OAUTH_BETAS = [
    "oauth-2025-04-20",
    "claude-code-20250219",
    "interleaved-thinking-2025-05-14",
    "prompt-caching-scope-2026-01-05",
    "effort-2025-11-24",
    "web-search-2025-03-05",
]

# Server-side tool for web search (handled by Anthropic, not locally)
_WEB_SEARCH_TOOL: dict[str, Any] = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": 5,
}

_STOP_MAP: dict[str | None, StopReason] = {
    "end_turn": StopReason.END_TURN,
    "tool_use": StopReason.TOOL_USE,
    "max_tokens": StopReason.MAX_TOKENS,
    "stop_sequence": StopReason.STOP_SEQUENCE,
}

_DEFAULT_MODELS = [
    "claude-opus-4-20250514",
    "claude-sonnet-4-20250514",
    "claude-haiku-4-20250506",
]


class AnthropicProvider:
    """Direct Anthropic Messages API provider with SSE streaming.

    Supports both API key auth and OAuth (Claude subscription) auth.
    """

    def __init__(self, cfg: ProviderConfig) -> None:
        self._api_key = cfg.api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._auth_token: str = ""  # OAuth access token (Claude subscription)
        self._account_pool = None  # Multi-account pool (claude-swap integration)
        self._base_url = (cfg.base_url or _DEFAULT_BASE_URL).rstrip("/")
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(7200.0, connect=10.0))

        # If no API key, try account pool first, then single OAuth
        if not self._api_key:
            self._try_load_pool()
            if not self._auth_token:
                self._try_load_oauth()

    def _try_load_pool(self) -> None:
        """Try to load multi-account pool from claude-swap."""
        try:
            from clawpy.auth.account_pool import get_pool
            pool = get_pool()
            if pool.available_accounts:
                self._account_pool = pool
                self._auth_token = pool.get_best_token() or ""
                if self._auth_token:
                    logger.info(f"Using account pool ({len(pool.available_accounts)} accounts)")
        except Exception as e:
            logger.debug(f"Account pool not available: {e}")

    def _try_load_oauth(self) -> None:
        """Try to load OAuth tokens from stored credentials."""
        try:
            from clawpy.auth.oauth import load_tokens, refresh_tokens
            tokens = load_tokens()
            if tokens is None:
                return
            self._auth_token = tokens.access_token
        except Exception:
            pass

    async def _ensure_valid_token(self) -> None:
        """Refresh OAuth token — uses pool rotation if available."""
        if not self._auth_token:
            return
        if self._account_pool:
            # Pool handles rotation — just get the best token
            best = self._account_pool.get_best_token()
            if best:
                self._auth_token = best
            return
        # Single account — refresh if expired
        from clawpy.auth.oauth import load_tokens, refresh_tokens
        tokens = load_tokens()
        if tokens and tokens.is_expired and tokens.refresh_token:
            tokens = await refresh_tokens(tokens)
            self._auth_token = tokens.access_token

    def _handle_rate_limit(self) -> bool:
        """Handle 429 by switching to next account. Returns True if switched."""
        if not self._account_pool:
            return False
        self._account_pool.mark_rate_limited(self._auth_token, cooldown_seconds=300)
        next_token = self._account_pool.get_next_token(self._auth_token)
        if next_token:
            self._auth_token = next_token
            logger.info("Switched to next account after rate limit")
            return True
        return False

    def _handle_unauthorized(self) -> bool:
        """Handle 401 by reloading tokens from disk. Returns True if refreshed."""
        if not self._account_pool:
            return False
        self._account_pool.mark_unauthorized(self._auth_token)
        new_token = self._account_pool.get_best_token()
        if new_token and new_token != self._auth_token:
            self._auth_token = new_token
            logger.info("Reloaded tokens after 401 Unauthorized")
            return True
        return False

    @property
    def name(self) -> str:
        return "anthropic"

    def models(self) -> list[str]:
        return _DEFAULT_MODELS

    def _headers(self) -> dict[str, str]:
        import uuid as _uuid
        headers: dict[str, str] = {
            "anthropic-version": _API_VERSION,
            "content-type": "application/json",
            "x-app": "cli",
            "User-Agent": f"claude-cli/{_CLAWPY_VERSION} (external, cli)",
            "x-client-request-id": str(_uuid.uuid4()),
        }
        if self._auth_token:
            # OAuth (Claude subscription) — Bearer token + required beta headers
            headers["Authorization"] = f"Bearer {self._auth_token}"
            headers["anthropic-beta"] = ",".join(_OAUTH_BETAS)
        else:
            # API key auth
            headers["x-api-key"] = self._api_key
        return headers

    def _build_body(self, request: Request) -> dict[str, Any]:
        """Convert neutral Request to Anthropic API request body."""
        body: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "messages": self._convert_messages(request),
        }

        # System prompt — prepend billing header for OAuth
        system_parts: list[dict[str, Any]] = []
        if self._auth_token:
            billing = (
                f"x-anthropic-billing-header: "
                f"cc_version={_CLAWPY_VERSION}; cc_entrypoint=cli; cch=00000;"
            )
            system_parts.append({"type": "text", "text": billing})
        if request.system:
            system_parts.append({"type": "text", "text": request.system})
        if system_parts:
            body["system"] = system_parts

        # Tools: user-defined + server-side web search (skip web search for tool-free queries)
        tools_list: list[dict[str, Any]] = []
        if request.tools:
            tools_list.extend(self._convert_tools(request.tools))
            tools_list.append(_WEB_SEARCH_TOOL)
        if tools_list:
            body["tools"] = tools_list

        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.stop_sequences:
            body["stop_sequences"] = request.stop_sequences

        # Metadata for OAuth — includes account UUID for billing routing
        if self._auth_token:
            import json as _json
            import uuid as _uuid
            account_uuid = ""
            try:
                from clawpy.auth.oauth import load_tokens
                tokens = load_tokens()
                if tokens:
                    account_uuid = tokens.account_uuid
            except Exception:
                pass
            body["metadata"] = {
                "user_id": _json.dumps({
                    "account_uuid": account_uuid,
                    "session_id": str(_uuid.uuid4()),
                }),
            }

        return body

    def _convert_messages(self, request: Request) -> list[dict[str, Any]]:
        """Convert neutral Messages to Anthropic format."""
        from clawpy.types import Message

        result: list[dict[str, Any]] = []
        for msg in request.messages:
            assert isinstance(msg, Message)
            content_blocks: list[dict[str, Any]] = []
            for block in msg.content:
                match block.type:
                    case ContentType.TEXT:
                        content_blocks.append({"type": "text", "text": block.text})
                    case ContentType.TOOL_CALL:
                        assert block.tool_call is not None
                        content_blocks.append({
                            "type": "tool_use",
                            "id": block.tool_call.id,
                            "name": block.tool_call.name,
                            "input": block.tool_call.input,
                        })
                    case ContentType.TOOL_RESULT:
                        assert block.tool_result is not None
                        content_blocks.append({
                            "type": "tool_result",
                            "tool_use_id": block.tool_result.tool_call_id,
                            "content": block.tool_result.content,
                            **({"is_error": True} if block.tool_result.is_error else {}),
                        })
                    case ContentType.THINKING:
                        content_blocks.append({
                            "type": "thinking",
                            "thinking": block.thinking,
                        })
            result.append({"role": msg.role.value, "content": content_blocks})
        return result

    def _convert_tools(self, tools: list[ToolSpec]) -> list[dict[str, Any]]:
        """Convert ToolSpecs to Anthropic tool format."""
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in tools
        ]

    async def stream(self, request: Request) -> AsyncIterator[StreamEvent]:
        """Stream a request to the Anthropic API, yielding neutral StreamEvents."""
        await self._ensure_valid_token()
        body = self._build_body(request)
        body["stream"] = True

        # State for assembling tool calls
        active_tool_calls: dict[int, ToolCall] = {}
        tool_json_bufs: dict[int, str] = {}
        block_index = -1

        async with self._client.stream(
            "POST",
            f"{self._base_url}/v1/messages",
            json=body,
            headers=self._headers(),
        ) as resp:
            if resp.status_code == 429 and self._handle_rate_limit():
                pass  # Fall through to retry below
            elif resp.status_code != 200:
                error_body = await resp.aread()
                yield StreamEvent(
                    type=EventType.ERROR,
                    error=RuntimeError(
                        f"Anthropic API error {resp.status_code}: {error_body.decode()}"
                    ),
                )
                return
            else:
                async for event_type, data in parse_sse_with_event(resp):
                    ev = self._map_event(
                        data, active_tool_calls, tool_json_bufs, block_index
                    )
                    if ev is not None:
                        if data.get("type") == "content_block_start":
                            block_index = data.get("index", block_index + 1)
                        yield ev
                return

        # Retry with alternate account after 429
        logger.info("Retrying stream with alternate account after 429")
        async with self._client.stream(
            "POST",
            f"{self._base_url}/v1/messages",
            json=body,
            headers=self._headers(),
        ) as resp:
            if resp.status_code != 200:
                error_body = await resp.aread()
                yield StreamEvent(
                    type=EventType.ERROR,
                    error=RuntimeError(
                        f"Anthropic API error {resp.status_code}: {error_body.decode()}"
                    ),
                )
                return

            async for event_type, data in parse_sse_with_event(resp):
                ev = self._map_event(
                    data, active_tool_calls, tool_json_bufs, block_index
                )
                if ev is not None:
                    # Track block index for tool calls
                    if data.get("type") == "content_block_start":
                        block_index = data.get("index", block_index + 1)
                    yield ev

    def _map_event(
        self,
        data: dict[str, Any],
        active_tool_calls: dict[int, ToolCall],
        tool_json_bufs: dict[int, str],
        block_index: int,
    ) -> StreamEvent | None:
        """Map a single Anthropic SSE event to a neutral StreamEvent."""
        event_type = data.get("type", "")

        match event_type:
            case "message_start":
                # Extract usage from initial message
                msg = data.get("message", {})
                usage_data = msg.get("usage", {})
                return StreamEvent(
                    type=EventType.DELTA,
                    delta=Delta(),
                    usage=Usage(
                        input_tokens=usage_data.get("input_tokens", 0),
                        output_tokens=0,
                    ),
                )

            case "content_block_start":
                index = data.get("index", 0)
                block = data.get("content_block", {})
                block_type = block.get("type", "")

                if block_type == "tool_use":
                    tc = ToolCall(
                        id=block.get("id", ""),
                        name=block.get("name", ""),
                        input={},
                    )
                    active_tool_calls[index] = tc
                    tool_json_bufs[index] = ""
                    return StreamEvent(type=EventType.TOOL_START, tool_call=tc)
                elif block_type == "server_tool_use":
                    # Server-side tool (web_search) — show as a tool call
                    name = block.get("name", "web_search")
                    return StreamEvent(
                        type=EventType.DELTA,
                        delta=Delta(text=f"\n[searching the web...]\n"),
                    )
                elif block_type == "web_search_tool_result":
                    # Web search results — will be followed by text with citations
                    return None
                elif block_type == "thinking":
                    return StreamEvent(type=EventType.DELTA, delta=Delta())
                else:
                    return StreamEvent(type=EventType.DELTA, delta=Delta())

            case "content_block_delta":
                index = data.get("index", 0)
                delta = data.get("delta", {})
                delta_type = delta.get("type", "")

                if delta_type == "text_delta":
                    return StreamEvent(
                        type=EventType.DELTA,
                        delta=Delta(text=delta.get("text", "")),
                    )
                elif delta_type == "input_json_delta":
                    partial = delta.get("partial_json", "")
                    if index in tool_json_bufs:
                        tool_json_bufs[index] += partial
                    return StreamEvent(
                        type=EventType.TOOL_DELTA,
                        delta=Delta(text=partial),
                    )
                elif delta_type == "thinking_delta":
                    return StreamEvent(
                        type=EventType.DELTA,
                        delta=Delta(thinking=delta.get("thinking", "")),
                    )

            case "content_block_stop":
                index = data.get("index", 0)
                if index in active_tool_calls:
                    # Finalize tool call with parsed JSON input
                    tc = active_tool_calls.pop(index)
                    raw_json = tool_json_bufs.pop(index, "")
                    parsed_input: dict[str, Any] = {}
                    if raw_json:
                        try:
                            parsed_input = json.loads(raw_json)
                        except json.JSONDecodeError:
                            parsed_input = {"_raw": raw_json}
                    finalized = ToolCall(id=tc.id, name=tc.name, input=parsed_input)
                    return StreamEvent(type=EventType.TOOL_END, tool_call=finalized)
                return None

            case "message_delta":
                delta = data.get("delta", {})
                usage_data = data.get("usage", {})
                stop = delta.get("stop_reason")
                return StreamEvent(
                    type=EventType.MESSAGE_STOP,
                    stop_reason=_STOP_MAP.get(stop, StopReason.END_TURN),
                    usage=Usage(
                        input_tokens=0,
                        output_tokens=usage_data.get("output_tokens", 0),
                    ),
                )

            case "message_stop":
                return None  # Already handled by message_delta

            case "ping":
                return None

        return None

    async def send(self, request: Request) -> Response:
        """Non-streaming request with automatic account rotation on 429."""
        await self._ensure_valid_token()
        body = self._build_body(request)

        resp = await self._client.post(
            f"{self._base_url}/v1/messages",
            json=body,
            headers=self._headers(),
        )

        # On 429, try switching account and retry once
        if resp.status_code == 429 and self._handle_rate_limit():
            logger.info("Retrying with alternate account after 429")
            resp = await self._client.post(
                f"{self._base_url}/v1/messages",
                json=body,
                headers=self._headers(),
            )

        # On 401, reload tokens from disk and retry
        if resp.status_code == 401 and self._handle_unauthorized():
            logger.info("Retrying with refreshed token after 401")
            resp = await self._client.post(
                f"{self._base_url}/v1/messages",
                json=body,
                headers=self._headers(),
            )

        resp.raise_for_status()
        data = resp.json()

        content: list[ContentBlock] = []
        for block in data.get("content", []):
            if block["type"] == "text":
                content.append(ContentBlock(type=ContentType.TEXT, text=block["text"]))
            elif block["type"] == "tool_use":
                content.append(
                    ContentBlock(
                        type=ContentType.TOOL_CALL,
                        tool_call=ToolCall(
                            id=block["id"],
                            name=block["name"],
                            input=block.get("input", {}),
                        ),
                    )
                )

        usage_data = data.get("usage", {})
        return Response(
            id=data.get("id", ""),
            content=content,
            stop_reason=_STOP_MAP.get(data.get("stop_reason"), StopReason.END_TURN),
            usage=Usage(
                input_tokens=usage_data.get("input_tokens", 0),
                output_tokens=usage_data.get("output_tokens", 0),
            ),
        )


def _factory(cfg: ProviderConfig) -> AnthropicProvider:
    return AnthropicProvider(cfg)


register("anthropic", _factory)
