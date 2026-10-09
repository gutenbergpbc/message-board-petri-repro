"""Run an eight-prefix replay preflight, or its gated 6x8 live continuation sweep."""
import argparse
import hashlib
import json
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv
from inspect_ai import eval as inspect_eval
from inspect_ai.model import get_model

from message_board_petri.cli import write_results
from message_board_petri.harness import ROOT
from message_board_petri.model_config import generation_config
from message_board_petri.prefill import prefilled_board


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["preflight", "live"], required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "configs/sol61-retrieval-prefill-48.yaml")
    parser.add_argument("--dataset-dir", type=Path, default=ROOT / "datasets/prepared-v2")
    args = parser.parse_args()
    config = yaml.safe_load(args.config.read_text())
    preflight = args.mode == "preflight"
    output = args.run_dir / args.mode
    output.mkdir(parents=True, exist_ok=False)
    task = prefilled_board(args.run_dir / "prefixes", str(args.dataset_dir), preflight=preflight)
    assert len(task.dataset) == 8
    source_hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in sorted([*ROOT.glob("src/**/*.py"), *ROOT.glob("scenarios/*/scenario.py"),
                                      ROOT / "scripts/run_prefilled_sweep.py", args.config.resolve()])}
    plan = {"mode": args.mode, "config": config, "source_sha256": source_hashes,
            "prefix_manifest": json.loads((args.run_dir / "prefixes/manifest.json").read_text()),
            "replicates_per_prefix": 1 if preflight else 6,
            "samples": 8 if preflight else 48,
            "turn_limit_policy": "200 total turns, including historical prefix",
            "response_cache": False,
            "restoration": "replay historical tool calls in each isolated fresh sandbox; gate on observation equivalence"}
    (output / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    if preflight:
        model, roles = "mockllm/model", None
    else:
        receipt = json.loads((args.run_dir / "preflight/receipt.json").read_text())
        if not receipt["passed"] or receipt["source_sha256"] != source_hashes:
            raise RuntimeError("Missing, failed, or stale preflight")
        if receipt["prefix_manifest"] != plan["prefix_manifest"]:
            raise RuntimeError("Prefixes changed since preflight")
        load_dotenv(ROOT / ".env")
        if not os.environ.get("OPENROUTER_API_KEY"):
            raise RuntimeError("OPENROUTER_API_KEY not configured")
        roles = {role: get_model(name, config=generation_config(name, config))
                 for role, name in config["models"].items() if role != "actor"}
        model = roles["auditor"]
    logs = inspect_eval(task, model=model, model_roles=roles,
                        epochs=1 if preflight else 6,
                        max_samples=config["max_samples"],
                        max_sandboxes=config["max_sandboxes"],
                        max_subprocesses=config["max_subprocesses"],
                        fail_on_error=False, retry_on_error=0,
                        time_limit=config["time_limit"],
                        log_dir=str(output / "logs"), log_realtime=True, display="plain")
    for log in logs:
        write_results(log, output)
    samples = [s for log in logs for s in (log.samples or [])]
    passed = all(log.status == "success" for log in logs) and len(samples) == plan["samples"]
    for sample in samples:
        passed = passed and not sample.error
        if preflight:
            replay = (sample.metadata or {}).get("prefix_replay", [])
            passed = passed and bool(replay) and all(r["normalized_match"] for r in replay)
    receipt = {**plan, "passed": bool(passed), "saved_samples": len(samples)}
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    if not passed:
        raise SystemExit("Sweep/preflight failed; inspect saved receipts")


if __name__ == "__main__":
    main()
