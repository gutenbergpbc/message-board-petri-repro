import asyncio
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from inspect_ai.model import ChatMessageSystem, ChatMessageUser, ModelOutput
from inspect_petri.target import ExitSignal

from message_board_petri import prefill


def test_normalization_is_narrow():
    def observation(text):
        return json.dumps({"exit_code": 0, "stdout": text, "stderr": ""})
    first = observation("-rw-r--r-- 1 root root 32 Oct  5 21:33 report.md\n172.18.0.3 packages.internal\n")
    second = observation("-rw-r--r-- 1 root root 32 Oct  5 22:44 report.md\n172.29.0.2     packages.internal\n")
    assert prefill.normalized_observation(first) == prefill.normalized_observation(second)
    assert prefill.normalized_observation(first) != prefill.normalized_observation(second.replace("32 Oct", "33 Oct"))
    assert prefill.normalized_observation(observation("Budget 100")) != prefill.normalized_observation(observation("Budget 101"))
    assert prefill.normalized_observation(observation("Created Oct  5 21:33")) != prefill.normalized_observation(observation("Created Oct  5 22:44"))


def test_prefix_forwarded_verbatim_without_new_user_nudge(monkeypatch):
    prefix = [
        {"id": "sys", "role": "system", "content": "unchanged system"},
        {"id": "usr", "role": "user", "content": "unchanged assignment"},
        {"id": "asst", "role": "assistant", "content": "", "tool_calls": [
            {"id": "call1", "function": "bash", "arguments": {"command": "curl internal"}, "type": "function"}
        ]},
        {"id": "result", "role": "tool", "tool_call_id": "call1", "content": "404"},
    ]
    original = copy.deepcopy(prefix)
    captured = []

    class Model:
        async def generate(self, **kwargs):
            captured.append(([m.model_dump(exclude_none=True) for m in kwargs["input"]], kwargs["cache"]))
            return ModelOutput.from_content("mock", "done")

    class Context:
        def __init__(self):
            self.metadata = {"continuation_prefix": {"messages": prefix}}
            self.tools = []

        def replayable(self, function, **kwargs):
            return function

        async def wait_for_resume(self):
            pass

        async def system_message(self):
            return ChatMessageSystem(content="unchanged system")

        async def user_message(self):
            return ChatMessageUser(content="unchanged assignment")

        def expect(self, value):
            pass

        async def send_output(self, value):
            raise ExitSignal()

    monkeypatch.setattr(prefill, "get_model", lambda **kwargs: Model())
    state = SimpleNamespace(messages=[])
    asyncio.run(prefill.prefilled_target()(state, Context()))
    assert [m["role"] for m in captured[0][0]] == [m["role"] for m in prefix]
    assert [m["content"] for m in captured[0][0]] == [m["content"] for m in prefix]
    assert captured[0][0][2]["tool_calls"] == prefix[2]["tool_calls"]
    assert captured[0][1] is False
    assert prefix == original


def test_cut_keeps_whole_tool_batch_and_excludes_future():
    path = Path(__file__).parents[1] / "scripts/build_retrieval_prefixes.py"
    spec = importlib.util.spec_from_file_location("build_prefixes", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    actions = [
        {"turn": 1, "function": "bash", "arguments": {"command": command},
         "raw_result": result, "result": result}
        for command, result in [("fetch", "requested URL returned error: 404"), ("ls", "files")]
    ]
    transcript = [{"role": "system", "content": "system"}, {"role": "user", "content": "task"},
                  {"role": "assistant", "content": "", "tool_calls": [
                      {"id": str(i), "function": "bash", "arguments": a["arguments"]}
                      for i, a in enumerate(actions)
                  ]},
                  *[{"role": "tool", "tool_call_id": str(i), "content": a["result"]}
                    for i, a in enumerate(actions)],
                  {"role": "assistant", "content": "future must not leak"}]
    evidence = {"actions": actions, "transcript": transcript,
                "token_usage_by_turn": [{"turn": 1, "usage": {"total_tokens": 10}},
                                        {"turn": 2, "usage": {"total_tokens": 30}}]}
    result = module.cut_prefix(evidence)
    assert result["turns"] == 1
    assert result["target_tokens_used"] == 10
    assert result["messages"] == transcript[:-1]
    assert result["actions"] == actions
    evidence["transcript"].pop(4)
    with pytest.raises(AssertionError):
        module.cut_prefix(evidence)
