"""Health-profile tools that persist lightweight conversational preferences."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema
from nanobot.health.storage import HealthWorkspace


@tool_parameters(
    tool_parameters_schema(
        preferred_name=StringSchema(
            "The short name or alias the user wants you to call them.",
            min_length=1,
            max_length=40,
        ),
        required=["preferred_name"],
    )
)
class SetPreferredNameTool(Tool):
    """Persist the user's preferred conversational name inside the encrypted vault."""

    def __init__(self, workspace: Path):
        self._health = HealthWorkspace(workspace)

    @property
    def name(self) -> str:
        return "set_preferred_name"

    @property
    def description(self) -> str:
        return (
            "Save the user's preferred name or alias in the encrypted health vault. "
            "Use this when the user tells you what they want to be called."
        )

    async def execute(self, preferred_name: str, **kwargs: Any) -> str:
        try:
            saved = self._health.save_preferred_name(preferred_name)
        except ValueError as exc:
            return f"Error: {exc}"
        return f"Saved preferred name: {saved}"
