"""Generation settings, including conversation-prefix caching for OpenRouter Claude."""

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from inspect_ai.model import GenerateConfig, get_model


def generation_config(model: str, config: Mapping[str, Any]) -> GenerateConfig:
    """Use automatic prompt caching without altering messages or caching responses.

    Inspect 0.3.268's explicit Claude breakpoints miss string-valued conversation
    messages. OpenRouter's request-level cache_control advances a breakpoint over
    the growing conversation, including tool results, regardless of that encoding.
    It coexists with Inspect's (at most three) explicit breakpoints, leaving room
    for the automatic fourth. Default ephemeral TTL is five minutes.

    https://openrouter.ai/docs/guides/best-practices/prompt-caching
    """
    ttl = config.get("prompt_cache_ttl", "5m")
    if ttl not in ("5m", "1h"):
        raise ValueError("prompt_cache_ttl must be 5m or 1h")
    marker = {"type": "ephemeral", **({"ttl": "1h"} if ttl == "1h" else {})}
    cache_options = (
        {"cache_prompt": True, "extra_body": {"cache_control": marker}}
        if model.startswith("openrouter/anthropic/")
        else {}
    )
    return GenerateConfig(
        max_tokens=config["max_tokens"],
        reasoning_effort=config["reasoning_effort"],
        max_connections=config.get("max_connections", 2),
        max_retries=2,
        timeout=180,
        **cache_options,
    )


def install_cache_ttl(api, ttl: str) -> None:
    """Instance-local adapter for pinned Inspect 0.3.268; no library/global patch.

    Inspect's static markers hard-code five minutes. Anthropic forbids putting a
    one-hour automatic breakpoint AFTER a five-minute explicit breakpoint, so all
    markers must use the requested duration. Keep message/tool/reasoning bytes intact.
    """
    from inspect_ai.model._providers.openrouter import OpenRouterAPI, _add_anthropic_cache_markers

    if ttl != "1h" or not isinstance(api, OpenRouterAPI):
        raise ValueError("Long cache adapter requires OpenRouter and ttl=1h")
    original = api._generate_completion

    async def generate_completion(request, config):
        request = deepcopy(request)
        if api._cache_prompt_enabled(config):
            _add_anthropic_cache_markers(request)

        def update(value):
            if isinstance(value, dict):
                if isinstance(value.get("cache_control"), dict):
                    value["cache_control"]["ttl"] = ttl
                for child in value.values():
                    update(child)
            elif isinstance(value, list):
                for child in value:
                    update(child)

        update(request)
        # Markers already placed; prevent the provider overwriting them with 5m.
        return await original(request, config.model_copy(update={"cache_prompt": False}))

    api._generate_completion = generate_completion


def configured_model(model: str, config: Mapping[str, Any]):
    """Build a role model, including the optional long-session transport adapter."""
    instance = get_model(model, config=generation_config(model, config))
    if config.get("prompt_cache_ttl") == "1h" and model.startswith("openrouter/anthropic/"):
        install_cache_ttl(instance.api, "1h")
    return instance
