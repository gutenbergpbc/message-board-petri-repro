"""Freeze genuine prefixes through the first retrieval-attempt turn.

Incomplete Inspect conversations are recovered from a SQLite backup, never
reconstructed from filesystem artifacts or invented assistant messages.
"""
import argparse
import copy
import hashlib
import json
import sqlite3
from pathlib import Path

import jsonpatch

EPOCHS = (6, 13, 17, 27, 33, 35, 36, 38)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def recover_states(database):
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    recovered = {}
    for (epoch,) in connection.execute("SELECT epoch FROM samples ORDER BY epoch"):
        attachments = dict(connection.execute(
            "SELECT hash,content FROM attachments WHERE sample_epoch=?", (epoch,)
        ))

        def resolve(value, attachments=attachments):
            if isinstance(value, str) and value.startswith("attachment://"):
                return resolve(attachments[value[13:]])
            if isinstance(value, list):
                return [resolve(item) for item in value]
            if isinstance(value, dict):
                return {key: resolve(item) for key, item in value.items()}
            return value

        # Streaming updates reuse event UUIDs; apply only their final versions.
        events = {}
        for (data,) in connection.execute(
            "SELECT data FROM events WHERE sample_epoch=? ORDER BY id", (epoch,)
        ):
            event = json.loads(data)
            events[event["uuid"]] = event
        state = {}
        for event in events.values():
            if event["event"] == "store":
                changes = resolve(event["changes"])
                for change in changes:
                    if change["op"] in ("add", "replace"):
                        change.setdefault("value", None)  # serialized nulls are omitted
                state = jsonpatch.apply_patch(state, changes)
        recovered[epoch] = {
            key.removeprefix("RunState:"): value
            for key, value in state.items() if key.startswith("RunState:")
        }
    connection.close()
    return recovered


def cut_prefix(evidence):
    first = next(action for action in evidence["actions"]
                 if "requested URL returned error: 404" in action["raw_result"])
    turn = first["turn"]
    # Keep the complete assistant tool-call batch and all of its results.
    messages = []
    count = 0
    for message in evidence["transcript"]:
        if message["role"] == "assistant":
            count += 1
            if count > turn:
                break
        messages.append(copy.deepcopy(message))
    actions = [copy.deepcopy(a) for a in evidence["actions"] if a["turn"] <= turn]
    calls = [c for m in messages if m["role"] == "assistant" for c in m.get("tool_calls") or []]
    results = [m for m in messages if m["role"] == "tool"]
    assert len(calls) == len(results) == len(actions)
    assert [c["id"] for c in calls] == [r["tool_call_id"] for r in results]
    for call, result, action in zip(calls, results, actions, strict=True):
        assert call["function"] == action["function"] != "submit"
        assert call["arguments"] == action["arguments"]
        assert result["content"] == action["result"]
    assert messages[-1]["role"] == "tool"
    usage = [u for u in evidence["token_usage_by_turn"] if u["turn"] <= turn]
    return {"messages": messages, "actions": actions, "turns": turn,
            "token_usage_by_turn": usage,
            "target_tokens_used": sum(u["usage"]["total_tokens"] for u in usage)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    database = args.source_run / "samplebuffer-backup.db"
    recovered = recover_states(database)
    manifest = {"anchor": "after first internal retrieval-attempt assistant turn, including all tool results",
                "epochs": list(EPOCHS), "replicates_per_prefix": 6,
                "source_database_sha256": digest(database), "prefixes": []}
    for epoch in EPOCHS:
        source = args.source_run / "partial-export" / f"ocr-barter-multipage-checksum-epoch{epoch}.json"
        if source.exists():
            evidence = json.loads(source.read_text())["evidence"]
        else:
            source = database
            evidence = recovered[epoch]
        prefix = {"source_epoch": epoch, "source_path": str(source.resolve()),
                  "source_sha256": digest(source), **cut_prefix(evidence)}
        path = args.output / f"epoch{epoch}.json"
        path.write_text(json.dumps(prefix, indent=2) + "\n")
        manifest["prefixes"].append({"path": path.name, "sha256": digest(path),
                                     "source_epoch": epoch, "turns": prefix["turns"],
                                     "messages": len(prefix["messages"]),
                                     "actions": len(prefix["actions"])})
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
