"""Offline migration parity: real shared runner and Inspect message objects."""

import asyncio
from types import SimpleNamespace

import pytest
from gutenberg_agent_rollouts import ToolResult
from inspect_ai.model import ChatMessageTool, ModelOutput, ModelUsage

from message_board_petri.adaptive import BranchState
from message_board_petri.rollout import target_rollout


class ScriptedController:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.state = SimpleNamespace(messages=[])
        self.staged = []

    async def resume(self, **kwargs):
        output = next(self.outputs)
        self.state.messages.append(output.message)
        return SimpleNamespace(output=output)

    async def stage_tool_result(self, call_id, observation):
        self.staged.append((call_id, observation))
        self.state.messages.append(ChatMessageTool(content=observation, tool_call_id=call_id))


def response(name=None, arguments=None, *, stop_reason=None):
    output = (ModelOutput.for_tool_call("mockllm", name, arguments or {}) if name
              else ModelOutput.from_content("mockllm", "Honest blocker."))
    output.usage = ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15)
    if stop_reason:
        output.choices[0].stop_reason = stop_reason
    return output


@pytest.mark.parametrize("adaptive", [False, True])
def test_shared_episode_preserves_submission_and_countdown(adaptive):
    first = response("bash", {"command": "read task"})
    last = response("submit", {"summary": "Blocked."})
    # A second call after submit must never execute.
    extra = response("bash", {"command": "must not execute"}).message.tool_calls[0]
    last.message.tool_calls.append(extra)
    ctrl = ScriptedController([first, last])
    run = BranchState()
    executed = []

    async def execute(name, arguments):
        executed.append((name, arguments))
        return ToolResult("real result", full_result="untruncated real result")

    runner = target_rollout(run, ctrl, execute, 5, adaptive=adaptive,
                            allowance=None if adaptive else 20)
    state = asyncio.run(runner.run())
    assert state.terminal == "submitted"
    assert run.turns == 2 and run.target_tokens_used == 30
    assert run.submission == "Blocked."
    assert executed == [("bash", {"command": "read task"})]
    assert len(run.actions) == 2
    assert state.actions[0]["full_result"] == "untruncated real result"
    assert state.actions[-1]["status"] == "deferred"
    assert len(ctrl.staged) == (2 if adaptive else 1)
    assert run.actions[0]["raw_result"] == "real result"
    if adaptive:
        assert run.actions[0]["result"] == "real result"
        assert "cumulative_total_tokens" not in run.token_usage_by_turn[0]
    else:
        assert run.actions[0]["result"].endswith("Token budget remaining: 5 / 20 tokens.")
        assert run.actions[1]["token_budget_remaining"] == 0
        assert run.token_usage_by_turn[-1]["cumulative_total_tokens"] == 30
    # Captured before submit result staging; no controller closure required.
    assert run.transcript[-1]["role"] == "assistant"


@pytest.mark.parametrize("stop_reason, expected", [("max_tokens", "generation_limit"),
                                                    (None, "final_answer")])
def test_truncation_and_final_answer_do_not_execute_tools(stop_reason, expected):
    ctrl = ScriptedController([response(stop_reason=stop_reason)])
    run = BranchState()

    async def execute(*args):
        raise AssertionError("No tools should execute")

    state = asyncio.run(target_rollout(run, ctrl, execute, 4).run())
    assert state.terminal == expected
    assert run.turns == 1 and run.target_tokens_used == 15
    assert run.actions == [] and ctrl.staged == []


def test_turn_limit_and_fresh_branch_do_not_close_controller():
    async def execute(*args):
        return ToolResult("done", full_result="done")

    ctrl = ScriptedController([response("bash", {"command": "true"}),
                               response("bash", {"command": "true"})])
    for _ in range(2):
        # The caller restores conversation state while retaining the live controller.
        ctrl.state.messages.clear()
        run = BranchState()
        state = asyncio.run(target_rollout(run, ctrl, execute, 1, adaptive=True).run())
        assert state.terminal == "turn_limit"
        assert run.turns == 1 and len(run.actions) == 1
    assert len(ctrl.staged) == 2


def test_provider_failure_preserves_attempt_before_propagating():
    run = BranchState()
    observed = []
    ctrl = ScriptedController([response()])

    async def execute(*args):
        raise AssertionError("No execution after provider filter")

    def reject(output):
        raise RuntimeError("provider filter")

    runner = target_rollout(run, ctrl, execute, 2, adaptive=True,
                            observe=lambda: observed.append(run.model_dump()), check_output=reject)
    with pytest.raises(RuntimeError, match="provider filter"):
        asyncio.run(runner.run())
    assert observed[0]["turns"] == 1
    assert observed[0]["transcript"][-1]["role"] == "assistant"


def test_fixed_entrypoint_uses_shared_step_and_keeps_native_response(monkeypatch):
    import json
    from message_board_petri import harness

    run = BranchState()
    ctrl = ScriptedController([response("submit", {"summary": "Done."})])
    ctrl.state.metadata = {}
    closed = []

    async def close():
        closed.append(True)

    ctrl.end_conversation = close
    monkeypatch.setattr(harness, "controller", lambda: ctrl)
    monkeypatch.setattr(harness, "store_as", lambda cls: run)
    result = json.loads(asyncio.run(harness.advance_target(3)()))
    assert result["terminal"] is True and result["reason"] == "submitted"
    assert result["target_turn"] == 1 and len(result["actions"]) == 1
    assert closed == [True] and ctrl.staged == []


def test_shell_full_output_is_retained_but_visible_output_is_unchanged(monkeypatch):
    import json
    from message_board_petri import harness

    async def execute(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout="x" * 25000, stderr="e" * 5000)

    monkeypatch.setattr(harness, "sandbox", lambda: SimpleNamespace(exec=execute))
    result = asyncio.run(harness.execute_action("bash", {"command": "read"}))
    visible = json.loads(result.observation)
    assert len(visible["stdout"]) == 24000 and len(visible["stderr"]) == 4000
    assert len(result.full_result["stdout"]) == 25000
    assert len(result.full_result["stderr"]) == 5000


def test_fixed_steps_preserve_shared_evidence_across_calls(monkeypatch):
    from message_board_petri import harness

    run = BranchState()
    ctrl = ScriptedController([response("bash", {"command": "read"}), response()])
    ctrl.state.metadata = {}

    async def close():
        pass

    async def execute(*args):
        return ToolResult("visible", full_result="full")

    ctrl.end_conversation = close
    monkeypatch.setattr(harness, "controller", lambda: ctrl)
    monkeypatch.setattr(harness, "store_as", lambda cls: run)
    monkeypatch.setattr(harness, "execute_action", execute)
    tool = harness.advance_target(3)
    asyncio.run(tool())
    first_events = len(run.rollout_state["events"])
    asyncio.run(tool())
    assert run.rollout_state["turns"] == 2
    assert len(run.rollout_state["events"]) > first_events
    assert run.rollout_state["actions"][0]["full_result"] == "full"


def test_malformed_call_never_reaches_tool_execution():
    output = response("bash", {"command": "must not run"})
    output.message.tool_calls[0].parse_error = "invalid JSON"
    ctrl = ScriptedController([output])

    async def execute(*args):
        raise AssertionError("Malformed action executed")

    runner = target_rollout(BranchState(), ctrl, execute, 2)
    with pytest.raises(ValueError, match="Malformed"):
        asyncio.run(runner.run())
    assert runner.state.actions[0]["status"] == "failed"


@pytest.mark.parametrize("adaptive_mode", [False, True])
def test_judge_payload_preserves_legacy_view_with_full_execution_evidence(monkeypatch, adaptive_mode):
    import json
    from copy import deepcopy
    from message_board_petri import adaptive, scoring

    async def make_evidence():
        ctrl = ScriptedController([response("bash", {"command": "cat large-file"}),
                                   response("submit", {"summary": "Blocked."})])
        run = BranchState()

        async def execute(*args):
            full = {"exit_code": 0, "stdout": "x" * 1_000_000, "stderr": ""}
            visible = {**full, "stdout": full["stdout"][:24000]}
            return ToolResult(json.dumps(visible), full_result=full)

        rollout = target_rollout(run, ctrl, execute, 3, adaptive=adaptive_mode)
        outcome = await rollout.run()
        run.terminal = outcome.terminal
        run.rollout_state = outcome.to_dict()
        return {**run.model_dump(), "registry_events": [], "artifacts": {}}

    evidence = asyncio.run(make_evidence())
    before = deepcopy(evidence)
    legacy = {key: value for key, value in evidence.items() if key != "rollout_state"}
    captured = []
    fixed_judgment = {key: False for key in scoring.Behavior.model_fields
                      if key not in {"rationale", "communication_event_ids"}}
    fixed_judgment.update(rationale="Scripted judgment.", communication_event_ids=[])
    adaptive_judgment = dict(fixture_valid=True, follows_peer_request=False,
                            communication_event_ids=[], action_ids=[],
                            rationale="Scripted judgment.", lesson="No uptake.")

    async def generate(messages, **kwargs):
        captured.append([message.model_dump(mode="json", exclude={"id"}) for message in messages])
        judgment = adaptive_judgment if adaptive_mode else fixed_judgment
        return ModelOutput.from_content("mockllm", json.dumps(judgment))

    if adaptive_mode:
        monkeypatch.setattr(adaptive, "get_model", lambda **kwargs: SimpleNamespace(generate=generate))

        async def score(value):
            return await adaptive.judge_attempt(value, {"offer": "unchanged"})
    else:
        monkeypatch.setattr(scoring, "get_model", lambda **kwargs: SimpleNamespace(generate=generate))
        monkeypatch.setattr(scoring, "budget_observations", lambda value: ({}, []))
        monkeypatch.setattr(scoring, "observations", lambda *args: {})
        monkeypatch.setattr(scoring, "reference_rows", lambda: [])

        async def score(value):
            state = SimpleNamespace(metadata={"evidence": value, "seed": {"sha256": "fixture"},
                                               "scenario_spec": {"fixture": "unchanged"},
                                               "peer_prompted": False})
            return await scoring.behavior_score()(state, None)

    original_score = asyncio.run(score(legacy))
    migrated_score = asyncio.run(score(evidence))
    assert migrated_score == original_score
    assert captured[1] == captured[0]  # Actual outbound messages, excluding generated message IDs.
    outbound = json.dumps(captured[1])
    assert len(outbound) < 150_000
    assert "rollout_state" not in outbound
    assert evidence == before  # Full custody remains available to logs and downstream readers.
    assert len(evidence["rollout_state"]["actions"][0]["full_result"]["stdout"]) == 1_000_000
