"""Skill tool — lets the agent invoke loaded skills by name.

The model calls this tool with a skill name and optional args.
The tool returns the skill's instructions as context for the model.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from clawpy.tool.base import Permission, RunContext, ToolResult

if TYPE_CHECKING:
    from clawpy.skills.loader import SkillRegistry


class SkillTool:
    """Tool that invokes a loaded skill by name."""

    def __init__(self, registry: SkillRegistry | None = None) -> None:
        self._registry = registry

    @property
    def name(self) -> str:
        return "Skill"

    @property
    def description(self) -> str:
        return (
            "Invoke a skill by name. Skills provide specialized capabilities "
            "and domain knowledge. Pass the skill name and optional arguments."
        )

    def input_schema(self) -> dict[str, Any]:
        available = []
        if self._registry:
            available = self._registry.names()

        return {
            "type": "object",
            "properties": {
                "skill": {
                    "type": "string",
                    "description": f"The skill name to invoke. Available: {', '.join(available)}" if available else "The skill name to invoke.",
                },
                "args": {
                    "type": "string",
                    "description": "Optional arguments for the skill.",
                    "default": "",
                },
            },
            "required": ["skill"],
        }

    def permission_for(self, input: dict[str, Any]) -> Permission:
        return Permission.READ_ONLY

    def is_read_only(self, input: dict[str, Any]) -> bool:
        return True

    def set_registry(self, registry: SkillRegistry) -> None:
        self._registry = registry

    async def run(self, ctx: RunContext, **kwargs: Any) -> ToolResult:
        skill_name = kwargs.get("skill", "")
        args = kwargs.get("args", "")

        if not self._registry:
            return ToolResult(content="No skill registry available.", is_error=True)

        if not skill_name:
            names = self._registry.names()
            return ToolResult(
                content=f"No skill name provided. Available skills: {', '.join(names)}"
            )

        result = self._registry.invoke(skill_name, args)
        if result is None:
            names = self._registry.names()
            return ToolResult(
                content=f"Skill '{skill_name}' not found. Available: {', '.join(names)}",
                is_error=True,
            )

        header = f"<skill-invoked name=\"{skill_name}\">\n"
        footer = "\n</skill-invoked>"

        return ToolResult(content=f"{header}{result}{footer}")
