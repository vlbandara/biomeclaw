"""In-chat health onboarding: persist structured profile via LLM tool call."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema
from nanobot.health.bootstrap import persist_health_onboarding
from nanobot.health.storage import get_health_vault_secret


@tool_parameters(
    tool_parameters_schema(
        submission_json=StringSchema(
            "JSON string with top-level keys phase1 and phase2. "
            "phase1: full_name, email, phone, timezone, language, preferred_channel, "
            "age_range, sex, gender, height_cm, weight_kg, known_conditions, medications, "
            "allergies, wake_time, sleep_time, consents (array). "
            "phase2: mood_interest, mood_down (0-3), activity_level, nutrition_quality, "
            "sleep_quality, stress_level, goals (array), current_concerns, "
            "reminder_preferences, medication_reminder_windows, morning_check_in, weekly_summary.",
            min_length=20,
        ),
        required=["submission_json"],
    )
)
class CompleteOnboardingTool(Tool):
    """Finalize health onboarding from structured JSON collected in conversation."""

    def __init__(
        self,
        workspace: Path,
        *,
        channel: str,
        chat_id: str,
        user_token: str | None = None,
    ):
        self._workspace = workspace
        self._channel = channel
        self._chat_id = chat_id
        self._user_token = user_token

    @property
    def name(self) -> str:
        return "complete_onboarding"

    @property
    def description(self) -> str:
        return (
            "Call once you have gathered all required health onboarding information from the user. "
            "Pass submission_json: a JSON object {\"phase1\": {...}, \"phase2\": {...}} matching the "
            "schema in your system instructions. This writes the encrypted vault and workspace files."
        )

    async def execute(self, submission_json: str, **kwargs: Any) -> str:
        from nanobot.health.api import OnboardingSubmission

        try:
            raw = json.loads(submission_json)
        except json.JSONDecodeError as e:
            return f"Invalid JSON: {e}"

        try:
            submission = OnboardingSubmission.model_validate(raw)
        except Exception as e:
            return f"Validation error (fix fields and retry): {e}"

        try:
            secret = get_health_vault_secret()
        except Exception as e:
            return f"Server configuration error (vault key): {e}"

        data = submission.model_dump()
        phase1 = data.get("phase1") or {}
        phase1.setdefault("preferred_channel", self._channel)

        persist_health_onboarding(
            self._workspace,
            data,
            invite={"channel": self._channel, "chat_id": self._chat_id},
            secret=secret,
            user_token=self._user_token,
        )
        return (
            "Onboarding saved successfully. The user is now fully set up. "
            "Confirm briefly and offer to help with their health goals."
        )
