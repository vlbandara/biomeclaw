"""Shared provider construction for CLI, SDK, and gateway."""

from __future__ import annotations

from pathlib import Path

from nanobot.config.schema import Config
from nanobot.health.storage import HealthWorkspace, health_distribution_enabled, is_health_workspace
from nanobot.providers.anonymizing import AnonymizingProvider
from nanobot.providers.base import GenerationSettings, LLMProvider, LLMResponse
from nanobot.security.anonymizer import PIIAnonymizer


def _apply_health_runtime_overrides(config: Config, workspace: Path) -> Config:
    resolved = config.model_copy(deep=True)
    overrides = HealthWorkspace(workspace).runtime_overrides()
    if not overrides:
        return resolved

    provider = overrides["provider"]
    resolved.agents.defaults.provider = provider["provider"]
    resolved.agents.defaults.model = provider["model"]
    resolved.agents.defaults.context_window_tokens = 204_800
    provider_config = getattr(resolved.providers, provider["provider"], None)
    if provider_config is not None:
        provider_config.api_key = provider["api_key"]

    for channel_name, channel_override in overrides["channels"].items():
        current = getattr(resolved.channels, channel_name, None)
        if current is None:
            current = {}
        elif hasattr(current, "model_dump"):
            current = current.model_dump(by_alias=True)
        elif not isinstance(current, dict):
            current = dict(current)
        merged = {**current, **channel_override}
        merged["allowFrom"] = merged.get("allowFrom") or merged.get("allow_from") or ["*"]
        setattr(resolved.channels, channel_name, merged)
    return resolved


def _build_configured_provider(config: Config, *, workspace: Path) -> LLMProvider:
    from nanobot.providers.registry import find_by_name

    model = config.agents.defaults.model
    provider_name = config.get_provider_name(model)
    p = config.get_provider(model)
    spec = find_by_name(provider_name) if provider_name else None
    backend = spec.backend if spec else "openai_compat"

    if backend == "azure_openai":
        if not p or not p.api_key or not p.api_base:
            raise ValueError("Azure OpenAI requires api_key and api_base in config.")
    elif backend == "openai_compat" and not model.startswith("bedrock/"):
        needs_key = not (p and p.api_key)
        exempt = spec and (spec.is_oauth or spec.is_local or spec.is_direct)
        if needs_key and not exempt:
            raise ValueError(f"No API key configured for provider '{provider_name}'.")

    if backend == "openai_codex":
        from nanobot.providers.openai_codex_provider import OpenAICodexProvider

        provider: LLMProvider = OpenAICodexProvider(default_model=model)
    elif backend == "github_copilot":
        from nanobot.providers.github_copilot_provider import GitHubCopilotProvider

        provider = GitHubCopilotProvider(default_model=model)
    elif backend == "azure_openai":
        from nanobot.providers.azure_openai_provider import AzureOpenAIProvider

        provider = AzureOpenAIProvider(
            api_key=p.api_key,
            api_base=p.api_base,
            default_model=model,
        )
    elif backend == "anthropic":
        from nanobot.providers.anthropic_provider import AnthropicProvider

        provider = AnthropicProvider(
            api_key=p.api_key if p else None,
            api_base=config.get_api_base(model),
            default_model=model,
            extra_headers=p.extra_headers if p else None,
        )
    else:
        from nanobot.providers.openai_compat_provider import OpenAICompatProvider

        provider = OpenAICompatProvider(
            api_key=p.api_key if p else None,
            api_base=config.get_api_base(model),
            default_model=model,
            extra_headers=p.extra_headers if p else None,
            spec=spec,
        )

    defaults = config.agents.defaults
    provider.generation = GenerationSettings(
        temperature=defaults.temperature,
        max_tokens=defaults.max_tokens,
        reasoning_effort=defaults.reasoning_effort,
    )

    if is_health_workspace(workspace):
        provider = AnonymizingProvider(provider, PIIAnonymizer(workspace))
    return provider


class DeferredHealthProvider(LLMProvider):
    """Wait for hosted setup activation before using the real provider."""

    def __init__(self, base_config: Config, *, workspace: Path) -> None:
        super().__init__(api_key=None, api_base=None)
        self._base_config = base_config.model_copy(deep=True)
        self._workspace = workspace
        defaults = base_config.agents.defaults
        self.generation = GenerationSettings(
            temperature=defaults.temperature,
            max_tokens=defaults.max_tokens,
            reasoning_effort=defaults.reasoning_effort,
        )

    def _resolve(self) -> LLMProvider | None:
        config = _apply_health_runtime_overrides(self._base_config, self._workspace)
        try:
            provider = _build_configured_provider(config, workspace=self._workspace)
        except ValueError:
            return None
        provider.generation = self.generation
        return provider

    async def chat(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, object] | None = None,
    ) -> LLMResponse:
        provider = self._resolve()
        if provider is None:
            return LLMResponse(
                content="Health setup is not complete yet. Finish setup in the web flow first.",
                finish_reason="error",
            )
        return await provider.chat(
            messages=messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            tool_choice=tool_choice,
        )

    def get_default_model(self) -> str:
        provider = self._resolve()
        if provider is not None:
            return provider.get_default_model()
        return self._base_config.agents.defaults.model


def create_provider(config: Config, *, workspace: Path | None = None) -> LLMProvider:
    """Create the configured provider and wrap it for health workspaces."""
    workspace_path = workspace or config.workspace_path
    resolved_config = _apply_health_runtime_overrides(config, workspace_path)

    if health_distribution_enabled(workspace_path):
        try:
            return _build_configured_provider(resolved_config, workspace=workspace_path)
        except ValueError:
            return DeferredHealthProvider(config, workspace=workspace_path)

    return _build_configured_provider(resolved_config, workspace=workspace_path)
