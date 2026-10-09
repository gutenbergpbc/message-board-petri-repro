"""Explicit prepare -> inspect fixtures -> run -> summarize workflow."""

import argparse
import asyncio
import hashlib
import json
import os
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv
from inspect_ai import eval as inspect_eval
from inspect_ai.log import read_eval_log
from inspect_ai.model import get_model

from .artifactory import IMAGE_DIGEST, VERSION, ensure_running, stop
from .dataset import DEFAULT_ARCHIVE, DEFAULT_BUNDLE, load_bundle, prepare_dataset
from .harness import ROOT, seeded_board
from .model_config import generation_config
from .scenarios import SCENARIOS
from .seeds import load_seed, prepare_seeds, seed_path


def write_results(log, output: Path):
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    for sample in log.samples or []:
        row = {
            "scenario": sample.id,
            "epoch": sample.epoch,
            "error": sample.error.message if sample.error else None,
            "seed_sha256": (sample.metadata or {}).get("seed", {}).get("sha256"),
            "task_id": (sample.metadata or {}).get("dataset", {}).get("task_id"),
            "archive_sha256": (sample.metadata or {}).get("dataset", {}).get("archive_sha256"),
            "scores": {
                name: score.model_dump(mode="json") for name, score in (sample.scores or {}).items()
            },
        }
        rows.append(row)
        (output / f"{sample.id}-epoch{sample.epoch}.json").write_text(
            json.dumps(
                {
                    **row,
                    "evidence": (sample.metadata or {}).get("evidence"),
                    "seed": (sample.metadata or {}).get("seed"),
                },
                indent=2,
            )
            + "\n"
        )
    (output / "summary.json").write_text(json.dumps(rows, indent=2) + "\n")
    for row in rows:
        values = next(iter(row["scores"].values()), {}).get("value", {})
        print(
            json.dumps(
                {
                    "scenario": row["scenario"],
                    "epoch": row["epoch"],
                    "error": row["error"],
                    **values,
                }
            )
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "configs/seeded.yaml")
    sub = parser.add_subparsers(dest="command", required=True)
    dataset = sub.add_parser("prepare-dataset", help="Index and stage the local CBJ archive")
    dataset.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    dataset.add_argument("--output", type=Path, default=DEFAULT_BUNDLE)
    dataset.add_argument(
        "--with-packages", action="store_true", help="Download checksum-pinned real wheels"
    )
    prepare = sub.add_parser("prepare", help="Generate and freeze GLM peer messages")
    prepare.add_argument("--force", action="store_true", help="Replace existing fixtures")
    for p in (prepare, sub.add_parser("run", help="Run fixed seeded-state Petri audits")):
        p.add_argument("--scenario", choices=[n for n, s in SCENARIOS.items() if not s.adaptive], action="append")
        p.add_argument(
            "--seed-dir",
            type=Path,
            help="Override seeds with <dir>/<scenario>.json (default: scenarios/<scenario>/seed.json)",
        )
    run = sub.choices["run"]
    run.add_argument("--dataset-dir", type=Path, default=DEFAULT_BUNDLE)
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--max-target-turns", type=int)
    run.add_argument("--epochs", type=int)
    run.add_argument("--output", type=Path)
    run.add_argument("--artifact-backend", choices=("synthetic", "artifactory"))
    sub.add_parser("infra-up", help="Start and verify patched Artifactory/PostgreSQL")
    infra_down = sub.add_parser("infra-down", help="Stop persistent Artifactory infrastructure")
    infra_down.add_argument("--purge", action="store_true", help="Also delete database/artifacts")
    summary = sub.add_parser("summarize", help="Export evidence and scores from an Inspect log")
    summary.add_argument("log", type=Path)
    summary.add_argument("--output", type=Path, default=ROOT / "runs/export")
    args = parser.parse_args()
    if args.command == "prepare-dataset":
        bundle = prepare_dataset(args.archive, args.output, with_packages=args.with_packages)
        print(
            json.dumps(
                {k: bundle[k] for k in ("task_id", "archive_sha256", "file_count", "source_bytes")},
                indent=2,
            )
        )
        return
    if args.command == "infra-up":
        print(json.dumps(ensure_running(), indent=2))
        return
    if args.command == "infra-down":
        stop(purge=args.purge)
        return
    if args.command == "summarize":
        write_results(read_eval_log(str(args.log)), args.output)
        return
    config = yaml.safe_load(args.config.read_text())
    names = args.scenario or config["scenarios"]
    load_dotenv(ROOT / ".env")
    if args.command == "prepare":
        needs_actor = any(
            args.force or not seed_path(n, args.seed_dir).exists()
            for n in names
        )
        if needs_actor and not os.environ.get("OPENROUTER_API_KEY"):
            parser.error("Set OPENROUTER_API_KEY in .env")
        asyncio.run(
            prepare_seeds(args.seed_dir, names, config["models"]["actor"], force=args.force)
        )
        return
    turns = (
        args.max_target_turns if args.max_target_turns is not None else config["max_target_turns"]
    )
    epochs = args.epochs if args.epochs is not None else config["epochs"]
    if turns < 1 or epochs < 1:
        parser.error("turns and epochs must be positive")
    max_epochs = config.get("max_epochs")
    if max_epochs is not None and epochs > max_epochs:
        parser.error(f"epochs cannot exceed configured max_epochs={max_epochs}")
    seeds = [load_seed(seed_path(n, args.seed_dir), n) for n in names]
    bundle = load_bundle(args.dataset_dir)
    artifact_backend = args.artifact_backend or config["artifacts"]["backend"]
    if artifact_backend == "artifactory" and not bundle.get("packages"):
        parser.error("Artifactory backend requires prepare-dataset --with-packages")
    from .budget import validate_reference

    validate_reference(bundle)
    plan = {
        "task_id": bundle["task_id"],
        "archive": bundle["archive_name"],
        "archive_sha256": bundle["archive_sha256"],
        "dataset_file_count": bundle["file_count"],
        "source_manifest": bundle["manifest"],
        "models": config["models"],
        "generation_configs": {
            role: generation_config(model, config).model_dump(mode="json", exclude_none=True)
            for role, model in config["models"].items()
            if role != "actor"
        },
        "scenarios": names,
        "scenario_specs": {name: asdict(SCENARIOS[name]) for name in names},
        "epochs": epochs,
        "max_epochs": max_epochs,
        "max_target_turns": turns,
        "max_tokens": config["max_tokens"],
        "reasoning_effort": config["reasoning_effort"],
        "time_limit": config["time_limit"],
        "max_samples": config["max_samples"],
        "max_sandboxes": config.get("max_sandboxes"),
        "max_subprocesses": config.get("max_subprocesses"),
        "max_connections": config.get("max_connections", 2),
        "fail_on_error": config.get("fail_on_error"),
        "retry_on_error": config.get("retry_on_error"),
        "seeds": {s["scenario"]: s["sha256"] for s in seeds},
        "token_budgets": config.get("token_budgets", {}),
        "token_budget_calibration": config.get("token_budget_calibration"),
        "peer_mode": "frozen; no live peers",
        "network": "Docker internal target network; gateway-only artifact access",
        "artifact_backend": artifact_backend,
        "artifactory_version": VERSION if artifact_backend == "artifactory" else None,
        "artifactory_image_digest": IMAGE_DIGEST if artifact_backend == "artifactory" else None,
        "real_package_artifacts": bundle.get("packages", []),
    }
    task_definition = seeded_board(
        str(args.seed_dir) if args.seed_dir is not None else None,
        names,
        turns,
        dataset_dir=str(args.dataset_dir),
        artifact_backend=artifact_backend,
        token_budgets=config.get("token_budgets"),
    )
    print(json.dumps(plan, indent=2), flush=True)
    if args.dry_run:
        return
    if not os.environ.get("OPENROUTER_API_KEY"):
        parser.error("Set OPENROUTER_API_KEY in .env")
    if artifact_backend == "artifactory":
        receipt = ensure_running()
        plan["artifactory_bootstrap"] = receipt
    output = args.output or ROOT / "runs" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output.mkdir(parents=True, exist_ok=False)
    plan["source_sha256"] = {
        str(path.relative_to(ROOT) if path.is_relative_to(ROOT) else path): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(
            [
                *ROOT.glob("src/**/*.py"),
                *(ROOT.parents[2] / "agent-rollouts/src").glob("**/*.py"),
                *ROOT.glob("scenarios/*/scenario.py"),
                *ROOT.glob("sandbox/*.py"),
                *ROOT.glob("artifactory/*.py"),
                *ROOT.glob("artifactory/*.yaml"),
                *ROOT.glob("configs/*calibration.json"),
                ROOT / "sandbox/compose.yaml",
                ROOT / "sandbox/compose.artifactory.yaml",
                ROOT / "sandbox/Dockerfile",
                ROOT / "sandbox/download_data.sh",
                ROOT / "uv.lock",
                ROOT / "src/message_board_petri/budget_reference.json",
                args.config.resolve(),
            ]
        )
    }
    (output / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    roles = {
        role: get_model(
            model,
            config=generation_config(model, config),
        )
        for role, model in config["models"].items()
        if role != "actor"
    }
    logs = inspect_eval(
        task_definition,
        model=roles["auditor"],
        model_roles=roles,
        epochs=epochs,
        max_samples=config["max_samples"],
        max_sandboxes=config.get("max_sandboxes"),
        max_subprocesses=config.get("max_subprocesses"),
        fail_on_error=config.get("fail_on_error"),
        retry_on_error=config.get("retry_on_error"),
        time_limit=config["time_limit"],
        log_dir=str(output / "logs"),
        log_realtime=True,
        display="plain",
    )
    for log in logs:
        write_results(log, output)
    print(f"Results: {output}")
    if any(log.status != "success" or any(s.error for s in log.samples or []) for log in logs):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
