"""Experiment projections around the shared target-episode runner.

Petri owns conversation history; experiment code owns token-countdown treatment,
legacy evidence shape, and controller lifetime.
"""

from gutenberg_agent_rollouts import Limits, Rollout, RolloutState, ToolResult
from gutenberg_agent_rollouts.adapters.petri import PetriSession

from .interventions import budget_notice, consumed_tokens, turn_notice


def target_rollout(run, ctrl, execute_action, max_turns, *, allowance=None,
                   adaptive=False, observe=None, check_output=None, turn_budget_notice=False):
    async def on_response(response):
        run.turns += 1
        usage = response.output.usage
        run.target_tokens_used += consumed_tokens(usage, required=allowance is not None)
        entry = {"turn": run.turns, "usage": usage.model_dump(mode="json") if usage else None}
        if not adaptive:
            entry["cumulative_total_tokens"] = run.target_tokens_used
        run.token_usage_by_turn.append(entry)
        run.transcript = [m.model_dump(mode="json") for m in ctrl.state.messages]
        if observe:
            observe()
        if check_output:
            check_output(response.output)

    async def execute(call):
        if call.parse_error or not isinstance(call.arguments, dict):
            raise ValueError("Malformed target tool arguments; action was not executed")
        action = {"turn": run.turns, "function": call.name, "arguments": call.arguments}
        submitted = call.name == "submit"
        if submitted:
            run.submission = call.arguments["summary"]
            raw = "Submission accepted. Branch ended." if adaptive else "Submission accepted. Run ended."
            full = raw
        else:
            execution = await execute_action(call.name, call.arguments)
            raw, full = execution.observation, execution.full_result
        result = budget_notice(raw, allowance=allowance, used=run.target_tokens_used)
        if turn_budget_notice and not submitted:
            result = turn_notice(result, used=run.turns, maximum=max_turns)
        action.update(raw_result=raw, result=result)
        if allowance is not None:
            action["token_budget_remaining"] = max(0, allowance - run.target_tokens_used)
        run.actions.append(action)
        if submitted:
            run.terminal = "submitted"
        if observe:
            observe()
        return ToolResult(observation=result, full_result=full,
                          signal="stop" if submitted else "continue",
                          reason="submitted" if submitted else None,
                          deliver=adaptive or not submitted)

    return Rollout(PetriSession(ctrl, on_response=on_response), execute,
                   state=(RolloutState.from_dict(run.rollout_state) if run.rollout_state
                          else RolloutState(turns=run.turns, terminal=run.terminal)),
                   limits=Limits(max_turns=max_turns))
