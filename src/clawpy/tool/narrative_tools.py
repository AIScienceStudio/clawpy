"""EANAT tools for Perspective-Claw.

Each tool wraps a narrative API endpoint (localhost:5002) and returns
JSON data to the LLM for synthesis.
"""

from __future__ import annotations

from typing import Any

import httpx

from clawpy.tool.base import Permission, RunContext, ToolResult

NARRATIVE_API = "http://localhost:5002/narrative"
_TIMEOUT = 30.0


class _BaseNarrativeTool:
    """Base class for narrative API tools."""

    endpoint: str = ""
    _name: str = ""
    _description: str = ""
    _params: dict[str, Any] = {}

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    def input_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": self._params}

    def permission_for(self, input: dict[str, Any]) -> Permission:
        return Permission.READ_ONLY

    def is_read_only(self, input: dict[str, Any]) -> bool:
        return True

    async def _fetch(self, params: dict[str, Any] | None = None) -> ToolResult:
        url = f"{NARRATIVE_API}{self.endpoint}"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(_TIMEOUT)) as client:
                resp = await client.get(url, params=params or {})
                resp.raise_for_status()
                data = resp.json()
                if data.get("success"):
                    return ToolResult(content=_compact_json(data.get("data")))
                return ToolResult(content=f"API returned no data for {self.endpoint}")
        except Exception as e:
            return ToolResult(content=f"Error fetching {self.endpoint}: {e}", is_error=True)

    async def run(self, input: dict[str, Any], ctx: RunContext) -> ToolResult:
        return await self._fetch(input or None)


def _compact_json(data: Any, max_chars: int = 8000) -> str:
    """Compact JSON for LLM context — truncate if too long."""
    import json
    text = json.dumps(data, ensure_ascii=False, default=str)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n... (truncated, {len(text)} total chars)"
    return text


# ─── 10 EANAT Tools ─────────────────────────────────────────────────


class NarrativeTopicsTool(_BaseNarrativeTool):
    _name = "narrative_topics"
    _description = (
        "Get topic intelligence: which political topics are brewing (emerging), "
        "hot (active), or fading. Includes lifecycle data, momentum scores, and "
        "monthly trends. Use when asked about trends, what's happening, or emerging narratives."
    )
    endpoint = "/topics/intelligence"
    _params = {}


class NarrativeAlertsTool(_BaseNarrativeTool):
    _name = "narrative_alerts"
    _description = (
        "Get active narrative intelligence alerts: coordinated narrative pushes "
        "(multiple figures aligning), stance instability (volatile figures), and "
        "high manipulation sources. Use when asked about threats, risks, or coordination."
    )
    endpoint = "/alerts"
    _params = {"region": {"type": "string", "description": "Region: 'us', 'bd', or 'austin'", "default": "us"}}


class NarrativeCoordinatedTool(_BaseNarrativeTool):
    _name = "narrative_coordinated"
    _description = (
        "Detect coordinated narrative clusters: groups of 3+ figures who aligned "
        "on the same topic with the same stance within a time window. Reveals "
        "echo chambers and potential coordinated influence. Use when asked about "
        "coordination, alignment, or echo chambers."
    )
    endpoint = "/coordinated"
    _params = {
        "region": {"type": "string", "description": "Region: 'us', 'bd'", "default": "us"},
        "window_hours": {"type": "integer", "description": "Time window in hours", "default": 48},
        "min_figures": {"type": "integer", "description": "Min figures per cluster", "default": 3},
    }


class NarrativeCredibilityTool(_BaseNarrativeTool):
    _name = "narrative_credibility"
    _description = (
        "Get credibility scores for all analyzed media sources. Each source has: "
        "credibility score (0-1), manipulation score, disinformation indicators "
        "(with counts), and factual accuracy ratings. Use when asked about "
        "trustworthiness, bias, reliability, or 'is this source credible'."
    )
    endpoint = "/credibility"


class NarrativeElectionTool(_BaseNarrativeTool):
    _name = "narrative_election"
    _description = (
        "Get candidate/figure narrative momentum scores for election intelligence. "
        "Ranks all figures by a composite score (reach + volume + consistency + "
        "coordination). Use when asked about who's winning, momentum, or election predictions."
    )
    endpoint = "/election/candidates"
    _params = {
        "region": {"type": "string", "description": "Region: 'us', 'bd'", "default": "us"},
        "limit": {"type": "integer", "description": "Max results", "default": 30},
    }


class NarrativeAmplificationTool(_BaseNarrativeTool):
    _name = "narrative_amplification"
    _description = (
        "Find amplification pairs: figure pairs who frequently discuss the same "
        "topics. Reveals influence networks and persistent echo chamber allies. "
        "Use when asked about who reinforces whom, influence networks, or alliances."
    )
    endpoint = "/amplification"
    _params = {
        "region": {"type": "string", "description": "Region", "default": "us"},
        "min_overlap": {"type": "integer", "description": "Min shared topics", "default": 5},
    }


class NarrativeTrajectoryTool(_BaseNarrativeTool):
    _name = "narrative_trajectory"
    _description = (
        "Predict stance trajectory for a specific figure on a specific topic. "
        "Returns: current stance, predicted stance (3 time-steps ahead), trend "
        "label (stable, escalating, softening), and confidence. Use when asked "
        "about predictions, forecasts, or 'where is this heading'."
    )
    endpoint = "/trajectory"
    _params = {
        "region": {"type": "string", "description": "Region", "default": "us"},
        "figure": {"type": "string", "description": "Figure name"},
        "topic": {"type": "string", "description": "Canonical topic name (e.g. 'governance')"},
    }

    async def run(self, input: dict[str, Any], ctx: RunContext) -> ToolResult:
        figure = input.get("figure", "")
        topic = input.get("topic", "governance")
        region = input.get("region", "us")
        if not figure:
            return ToolResult(content="Error: figure parameter is required", is_error=True)
        return await self._fetch({"region": region, "figure": figure, "topic": topic})


class NarrativeReachTool(_BaseNarrativeTool):
    _name = "narrative_reach"
    _description = (
        "Get estimated audience reach (cumulative view counts) per figure. "
        "Shows total influence scale. Use when asked about audience size, "
        "views, reach, or 'how many people see this'."
    )
    endpoint = "/reach"
    _params = {"region": {"type": "string", "description": "Region", "default": "us"}}


class NarrativeCrossRegionTool(_BaseNarrativeTool):
    _name = "narrative_cross_region"
    _description = (
        "Find topic+stance patterns that appear in BOTH Bangladesh and US media. "
        "Detects cross-region narrative convergence or foreign influence patterns. "
        "Use when asked about global patterns, foreign influence, or cross-country comparisons."
    )
    endpoint = "/cross-region"
    _params = {"topic": {"type": "string", "description": "Optional: filter to specific topic"}}


class NarrativeNewsTool(_BaseNarrativeTool):
    _name = "narrative_news"
    _description = (
        "Get latest political news articles from Texas and BD sources via Google News RSS. "
        "Use when asked about current events, breaking news, or recent coverage."
    )
    endpoint = "/news/texas"
    _params = {"limit": {"type": "integer", "description": "Max articles", "default": 15}}


# ─── Registry helper ────────────────────────────────────────────────

def register_all_narrative_tools(registry) -> None:
    """Register all 10 EANAT tools on a ToolRegistry."""
    for tool_class in [
        NarrativeTopicsTool,
        NarrativeAlertsTool,
        NarrativeCoordinatedTool,
        NarrativeCredibilityTool,
        NarrativeElectionTool,
        NarrativeAmplificationTool,
        NarrativeTrajectoryTool,
        NarrativeReachTool,
        NarrativeCrossRegionTool,
        NarrativeNewsTool,
    ]:
        registry.register(tool_class())
