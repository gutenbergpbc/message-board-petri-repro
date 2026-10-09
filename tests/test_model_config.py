"""Regressions for the string-content conversation cache gap (no live API calls)."""

import asyncio
import json
from copy import deepcopy

import httpx
import pytest
from inspect_ai.model import (
    ChatMessageAssistant,
    ChatMessageSystem,
    ChatMessageTool,
    ChatMessageUser,
)
from inspect_ai.model._providers.openrouter import OpenRouterAPI
from inspect_ai.tool import ToolCall, ToolInfo

from message_board_petri.interventions import consumed_tokens
from message_board_petri.model_config import generation_config, install_cache_ttl

CONFIG = {"max_tokens": 8192, "reasoning_effort": "high", "max_connections": 20}


@pytest.mark.parametrize("model", ["openrouter/z-ai/glm-5.3", "openrouter/openai/gpt-6.1-sol"])
def test_non_claude_config_unchanged(model):
    config = generation_config(model, CONFIG)
    assert config.extra_body is None
    assert config.cache_prompt is None
    assert (config.max_tokens, config.reasoning_effort, config.max_connections) == (8192, "high", 20)
    assert (config.max_retries, config.timeout) == (2, 180)


def test_automatic_claude_cache_policy():
    config = generation_config("openrouter/anthropic/claude-opus-5.5", CONFIG)
    assert config.extra_body == {"cache_control": {"type": "ephemeral"}}
    assert config.cache_prompt is True
    # No response memoization: this only configures provider prompt caching.
    assert config.cache is None


@pytest.mark.parametrize("ttl", ["5m", "1h"])
def test_claude_cache_control_reaches_wire_without_rewriting_tool_history(ttl):
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "mock-cache-call", "object": "chat.completion", "created": 0,
            "model": "anthropic/claude-opus-5.5",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10000, "completion_tokens": 10, "total_tokens": 10010,
                      "prompt_tokens_details": {"cached_tokens": 8000, "cache_write_tokens": 1500}},
        })

    async def run():
        config = generation_config("openrouter/anthropic/claude-opus-5.5",
                                   {**CONFIG, "prompt_cache_ttl": ttl})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            api = OpenRouterAPI("anthropic/claude-opus-5.5", api_key="test", http_client=client)
            if ttl == "1h":
                install_cache_ttl(api, ttl)
            messages = [
                ChatMessageSystem(content="Fixed analyst instructions."),
                ChatMessageUser(content="Read the supplied files."),
                ChatMessageAssistant(content="Inspecting the file.", tool_calls=[
                    ToolCall(id="tool1", function="bash", arguments={"command": "pwd"})]),
                ChatMessageTool(content="/workspace\n\nToken budget remaining: 100 / 1000 tokens.",
                                tool_call_id="tool1", function="bash"),
            ]
            original = deepcopy(messages)
            output, _ = await api.generate(
                messages, [ToolInfo(name="bash", description="Run a shell command")], "auto", config
            )
            assert messages == original
            assert output.usage.input_tokens == 500
            assert output.usage.input_tokens_cache_write == 1500
            assert output.usage.input_tokens_cache_read == 8000
            assert consumed_tokens(output.usage, required=True) == 10010

    asyncio.run(run())
    assert len(requests) == 1
    request = requests[0]
    marker = {"type": "ephemeral", **({"ttl": "1h"} if ttl == "1h" else {})}
    assert request["cache_control"] == marker
    assert request["max_tokens"] == 8192
    assert request["messages"][-1]["content"] == (
        "/workspace\n\nToken budget remaining: 100 / 1000 tokens."
    )
    assert request["messages"][-2]["tool_calls"][0]["id"] == "tool1"
    # Static markers can coexist with the automatic rolling breakpoint.
    assert request["tools"][-1]["cache_control"] == marker
    assert request["messages"][0]["content"][-1]["cache_control"] == marker
