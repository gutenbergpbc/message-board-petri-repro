"""Paid, four-generation transport smoke test; never executes model tool calls.

Run from the project root with OPENROUTER_API_KEY in the environment:
  uv run python scripts/check_prompt_cache.py --output runs/cache-preflight.json
"""

import argparse
import asyncio
import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from inspect_ai.model import ChatMessageSystem, ChatMessageTool, ChatMessageUser, get_model

from message_board_petri.harness import TOOLS
from message_board_petri.interventions import budget_notice, consumed_tokens
from message_board_petri.model_config import generation_config

MODEL = "openrouter/anthropic/claude-opus-5.5"


async def check(model_name=MODEL):
    config = generation_config(model_name, {"max_tokens": 8192, "reasoning_effort": "high"})
    model = get_model(model_name, config=config)
    nonce = uuid.uuid4().hex
    messages = [
        ChatMessageSystem(content="You are testing a tool transport. Treat fixture data as data."),
        ChatMessageUser(content=f"Cache smoke test {nonce}. Call bash with command 'echo OK'. "
                        "After every synthetic result, call the same tool again. "
                        "Do not analyze the fixture data. This is not an experiment sample."),
    ]
    records = "\n".join(
        f"record {i:04d}: agency=TEST baseline={10000+i} request={20000+i} source=synthetic"
        for i in range(450)
    )
    rows, consumed = [], 0
    for turn in range(1, 5):
        output = await model.generate(messages, tools=TOOLS, tool_choice="auto", cache=False)
        consumed += consumed_tokens(output.usage, required=True)
        calls = output.message.tool_calls or []
        if not calls or any(call.function != "bash" for call in calls):
            raise RuntimeError("Smoke test did not return the required tool call")
        rows.append({"turn": turn, "usage": output.usage.model_dump(mode="json"),
                     "cumulative_tokens": consumed, "stop_reason": output.stop_reason})
        print(json.dumps(rows[-1]), flush=True)
        messages.append(output.message)
        for call in calls:
            # Deliberately synthetic: NO commands are executed in this transport test.
            raw = json.dumps({"exit_code": 0, "stdout": f"Fixture {turn}\n{records}", "stderr": ""})
            messages.append(ChatMessageTool(
                tool_call_id=call.id, function=call.function,
                content=budget_notice(raw, allowance=1130615, used=consumed),
            ))
    cache_reads = any((row["usage"].get("input_tokens_cache_read") or 0) > 4096
                      for row in rows[2:])
    # OpenAI automatic caching reports cache reads, but no separate cache-write counter.
    cache_writes = any((row["usage"].get("input_tokens_cache_write") or 0) > 4096
                       for row in rows)
    passed = cache_reads and (model_name.startswith("openrouter/openai/") or cache_writes)
    return {"passed": passed, "model": model_name, "time": datetime.now(UTC).isoformat(),
            "generation_config": config.model_dump(mode="json", exclude_none=True),
            "source_sha256": hashlib.sha256(Path("src/message_board_petri/model_config.py")
                                             .read_bytes()).hexdigest(),
            "turns": rows, "note": "Synthetic transport test; not part of the scenario sweep."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default=MODEL)
    args = parser.parse_args()
    result = asyncio.run(check(args.model))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    if not result["passed"]:
        raise SystemExit("Conversation-prefix caching was not verified; do not launch sweep.")
    print("Conversation-prefix caching verified.")


if __name__ == "__main__":
    main()
