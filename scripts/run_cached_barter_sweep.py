"""Run the requested 30+10 cached Opus samples; safe to launch under tmux.

API credentials are inherited or loaded from --env-file; never written to status.
A passing scripts/check_prompt_cache.py receipt must exist in the output directory.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv
from inspect_ai.log import read_eval_log

ROOT = Path(__file__).resolve().parents[1]
ARMS = [
    ("barter-30", "configs/opus55-cached-barter-30.yaml", "ocr-barter", 30),
    ("barter-budget-10", "configs/opus55-cached-barter-budget-10.yaml",
     "ocr-barter-token-budget", 10),
]


def now():
    return datetime.now(UTC).isoformat()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    args = parser.parse_args()
    output = args.output.resolve()
    load_dotenv(Path.home() / ".pi/.env.local")
    load_dotenv(args.env_file)
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise SystemExit("Missing OPENROUTER_API_KEY")
    os.environ["PYTHONUNBUFFERED"] = "1"
    preflight = json.loads((output / "cache-preflight.json").read_text())
    source = ROOT / "src/message_board_petri/model_config.py"
    if (not preflight.get("passed")
            or preflight.get("model") != "openrouter/anthropic/claude-opus-5.5"
            or preflight.get("source_sha256") != hashlib.sha256(source.read_bytes()).hexdigest()):
        raise SystemExit("Passing cache preflight for this source is required")
    if (output / "status.json").exists() or any((output / name).exists() for name, *_ in ARMS):
        raise SystemExit("Refusing to overwrite an existing sweep")
    state = {"status": "running", "started_at": now(), "jobs": {},
             "source_base": "6e04e2f5b7b9aab512c32423897c00b8154c087f",
             "cache_preflight": str(output / "cache-preflight.json")}
    jobs = []

    def save():
        temp = output / "status.tmp"
        temp.write_text(json.dumps(state, indent=2) + "\n")
        temp.replace(output / "status.json")

    def record_finished(name, proc, scenario, epochs):
        summary = output / name / "summary.json"
        rows = json.loads(summary.read_text()) if summary.exists() else []
        logs = [read_eval_log(str(p)) for p in (output / name / "logs").glob("*.eval")]
        errors = sum(row.get("error") is not None for row in rows)
        expected = {(scenario, i) for i in range(1, epochs + 1)}
        actual = {(row["scenario"], row["epoch"]) for row in rows}
        complete = (proc.returncode == 0 and len(rows) == epochs and actual == expected
                    and not errors and bool(logs) and all(log.status == "success" for log in logs))
        state["jobs"][name].update(
            status="finished" if complete else "incomplete_or_failed",
            exit_code=proc.returncode, finished_at=now(), samples=len(rows), sample_errors=errors,
            eval_statuses=[log.status for log in logs],
            model_usage=[{k: v.model_dump(mode="json") for k, v in log.stats.model_usage.items()}
                         for log in logs],
        )
        save()

    try:
        for name, config, scenario, epochs in ARMS:
            command = [sys.executable, "-m", "message_board_petri.cli", "--config", config,
                       "run", "--output", str(output / name)]
            handle = (output / f"{name}.log").open("x")
            proc = subprocess.Popen(command, cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT)
            jobs.append((name, proc, handle, scenario, epochs))
            state["jobs"][name] = {"status": "running", "pid": proc.pid, "scenario": scenario,
                                   "expected_samples": epochs, "started_at": now(),
                                   "output": str(output / name), "command": command}
            save()
            # Serialize shared backend bootstrap; evaluation itself runs concurrently.
            deadline = time.monotonic() + 1200
            while not (output / name / "plan.json").exists() and proc.poll() is None:
                if time.monotonic() > deadline:
                    raise RuntimeError(f"{name}: bootstrap timed out")
                time.sleep(2)
            if proc.poll() is not None:
                raise RuntimeError(f"{name}: exited during startup; see its log")
        while any(proc.poll() is None for _, proc, _, _, _ in jobs):
            for name, proc, _, scenario, epochs in jobs:
                if proc.poll() is not None and state["jobs"][name]["status"] == "running":
                    record_finished(name, proc, scenario, epochs)
            time.sleep(10)
        for name, proc, handle, scenario, epochs in jobs:
            handle.close()
            if state["jobs"][name]["status"] == "running":
                record_finished(name, proc, scenario, epochs)
        state.update(status="finished" if all(j["status"] == "finished"
                                              for j in state["jobs"].values())
                     else "incomplete_or_failed", finished_at=now())
        save()
    except BaseException as exc:
        import signal
        for _, proc, _, _, _ in jobs:
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
        state.update(status="launcher_error", error=str(exc), updated_at=now())
        save()
        raise
    print(json.dumps(state, indent=2), flush=True)
    if state["status"] != "finished":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
