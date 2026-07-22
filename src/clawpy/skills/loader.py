"""Skill discovery and loading — SKILL.md files.

Skills are markdown instruction files that teach the agent how and when to use
tools. Each skill lives in a directory containing a SKILL.md file with YAML
frontmatter and a markdown body.

Loading order (highest precedence first):
  1. Workspace skills    <work_dir>/skills/
  2. Project skills      <work_dir>/.clawpy/skills/
  3. Personal skills     ~/.clawpy/skills/
  4. Bundled skills      <package>/skills/
  5. Extra directories   skills.load.extraDirs from config

Skill roots support grouped layouts — SKILL.md is discovered up to 6 levels
deep. The skill's name comes from the `name` frontmatter field (or directory
name when missing). Higher-precedence sources win on name collision.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clawpy.config.paths import global_config_dir

logger = logging.getLogger("clawpy.skills")

MAX_DEPTH = 6


@dataclass(slots=True)
class Skill:
    """A loaded skill definition."""

    name: str
    description: str
    instructions: str
    source_dir: str
    source_priority: int  # 1 = highest (workspace), 5 = lowest (bundled)

    # Optional frontmatter
    user_invocable: bool = True
    disable_model_invocation: bool = False
    command_dispatch: str = ""  # "tool" to bypass model
    command_tool: str = ""
    command_arg_mode: str = "raw"
    homepage: str = ""
    model: str = ""
    allowed_tools: list[str] = field(default_factory=list)

    # Gating
    requires_bins: list[str] = field(default_factory=list)
    requires_env: list[str] = field(default_factory=list)

    def base_dir(self) -> Path:
        return Path(self.source_dir)

    def resolve_path(self, text: str) -> str:
        """Replace {baseDir} placeholders with the skill's directory path."""
        return text.replace("{baseDir}", str(self.source_dir))


class SkillRegistry:
    """Discovers and manages all loaded skills."""

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def discover(
        self,
        work_dir: str,
        extra_dirs: list[str] | None = None,
        plugin_skill_dirs: list[str] | None = None,
    ) -> None:
        """Discover skills from all sources in precedence order."""
        self._skills.clear()

        sources: list[tuple[Path, int]] = []

        # Priority 1: Workspace skills
        ws = Path(work_dir) / "skills"
        if ws.is_dir():
            sources.append((ws, 1))

        # Priority 2: Project .clawpy/skills
        proj = Path(work_dir) / ".clawpy" / "skills"
        if proj.is_dir():
            sources.append((proj, 2))

        # Priority 3: Personal ~/.clawpy/skills
        personal = global_config_dir() / "skills"
        if personal.is_dir():
            sources.append((personal, 3))

        # Priority 4: Bundled skills (shipped with clawpy)
        bundled = Path(__file__).parent / "bundled"
        if bundled.is_dir():
            sources.append((bundled, 4))

        # Priority 5: Extra directories from config + plugin skills
        for extra in extra_dirs or []:
            p = Path(extra)
            if p.is_dir():
                sources.append((p, 5))
        for psd in plugin_skill_dirs or []:
            p = Path(psd)
            if p.is_dir():
                sources.append((p, 5))

        # Discover from lowest to highest priority (highest overwrites)
        for root, priority in reversed(sources):
            for skill in _find_skills_in_root(root, priority):
                if not _check_gating(skill):
                    logger.debug("Skill '%s' gated out (missing requirements)", skill.name)
                    continue
                self._skills[skill.name] = skill

        logger.info("Loaded %d skills from %d sources", len(self._skills), len(sources))

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def list_all(self) -> list[Skill]:
        return list(self._skills.values())

    def list_user_invocable(self) -> list[Skill]:
        return [s for s in self._skills.values() if s.user_invocable]

    def list_for_model(self) -> list[Skill]:
        """Skills the model should know about (for system prompt injection)."""
        return [s for s in self._skills.values() if not s.disable_model_invocation]

    def names(self) -> list[str]:
        return sorted(self._skills.keys())

    def build_system_prompt_section(self) -> str:
        """Build the skills section for the system prompt."""
        model_skills = self.list_for_model()
        if not model_skills:
            return ""

        lines = ["\n# Available Skills\n"]
        lines.append("Skills provide specialized capabilities. Invoke with the Skill tool.\n")
        for s in sorted(model_skills, key=lambda x: x.name):
            lines.append(f"- **{s.name}**: {s.description}")
        return "\n".join(lines)

    def invoke(self, name: str, args: str = "") -> str | None:
        """Get the full instruction text for a skill invocation.

        Returns the skill's instructions with {baseDir} resolved and args appended,
        or None if skill not found.
        """
        skill = self._skills.get(name)
        if not skill:
            return None

        text = skill.resolve_path(skill.instructions)
        if args:
            text = f"{text}\n\n## User Arguments\n\n{args}"
        return text


def _find_skills_in_root(root: Path, priority: int) -> list[Skill]:
    """Walk a skill root up to MAX_DEPTH levels looking for SKILL.md files."""
    skills: list[Skill] = []

    def _walk(directory: Path, depth: int) -> None:
        if depth > MAX_DEPTH:
            return
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            return

        for entry in entries:
            if not entry.is_dir():
                continue
            skill_md = entry / "SKILL.md"
            if skill_md.is_file():
                skill = _load_skill_file(skill_md, priority)
                if skill:
                    skills.append(skill)
            else:
                _walk(entry, depth + 1)

    _walk(root, 0)
    return skills


def _load_skill_file(path: Path, priority: int) -> Skill | None:
    """Load a skill from a SKILL.md file."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    meta, body = _parse_frontmatter(text)
    if not body.strip():
        return None

    name = meta.get("name", path.parent.name)

    # Parse tools list
    allowed_tools = meta.get("allowed_tools", meta.get("allowedTools", []))
    if isinstance(allowed_tools, str):
        allowed_tools = [t.strip() for t in allowed_tools.split(",")]

    # Parse requires
    requires_bins = meta.get("requires.bins", meta.get("requires_bins", []))
    if isinstance(requires_bins, str):
        requires_bins = [b.strip() for b in requires_bins.split(",")]
    requires_env = meta.get("requires.env", meta.get("requires_env", []))
    if isinstance(requires_env, str):
        requires_env = [e.strip() for e in requires_env.split(",")]

    return Skill(
        name=name,
        description=meta.get("description", f"Skill: {name}"),
        instructions=body.strip(),
        source_dir=str(path.parent),
        source_priority=priority,
        user_invocable=meta.get("user-invocable", meta.get("user_invocable", True)),
        disable_model_invocation=meta.get("disable-model-invocation", meta.get("disable_model_invocation", False)),
        command_dispatch=meta.get("command-dispatch", meta.get("command_dispatch", "")),
        command_tool=meta.get("command-tool", meta.get("command_tool", "")),
        command_arg_mode=meta.get("command-arg-mode", meta.get("command_arg_mode", "raw")),
        homepage=meta.get("homepage", ""),
        model=meta.get("model", ""),
        allowed_tools=allowed_tools,
        requires_bins=requires_bins,
        requires_env=requires_env,
    )


def _check_gating(skill: Skill) -> bool:
    """Check if skill requirements are met (binaries, env vars)."""
    import os
    import shutil

    for binary in skill.requires_bins:
        if not shutil.which(binary):
            return False
    for env_var in skill.requires_env:
        if not os.environ.get(env_var):
            return False
    return True


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse YAML-like frontmatter from SKILL.md."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta: dict[str, Any] = {}
    for line in parts[1].strip().splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            if val.startswith("[") and val.endswith("]"):
                meta[key] = [v.strip().strip("'\"") for v in val[1:-1].split(",")]
            elif val.lower() in ("true", "false"):
                meta[key] = val.lower() == "true"
            else:
                meta[key] = val.strip("'\"")
    return meta, parts[2].strip()
