"""Upload saved audits with separate target, auditor, and judge transcripts."""

import argparse
import hashlib
import json
import os
import time
from pathlib import Path

from docent import Docent
from docent.data_models.chat.checks import check_agent_runs
from dotenv import load_dotenv
from inspect_ai.log import read_eval_log

from .harness import ROOT
from .vendor.petri_to_docent import build_agent_runs


def normalize_metadata(value):
    """Match Docent's JSON metadata sanitization for embedded NUL bytes."""
    if isinstance(value, str):
        return value.replace("\x00", "").replace("\\x00", "")
    if isinstance(value, list):
        return [normalize_metadata(item) for item in value]
    if isinstance(value, dict):
        return {key: normalize_metadata(item) for key, item in value.items()}
    return value


def convert(paths: list[Path]):
    runs = []
    for path in paths:
        log = read_eval_log(str(path))
        if log.status != "success" or any(s.error for s in log.samples or []):
            raise ValueError(f"Expected completed successful samples: {path}")
        converted = build_agent_runs(str(path.parent), path.name)
        samples = {(str(s.id), s.epoch): s for s in log.samples or []}
        for run in converted:
            sample = samples[(run.metadata["sample_id"], run.metadata["epoch_id"])]
            score = sample.scores["behavior_score"]
            evidence = sample.metadata["evidence"]
            values = score.value
            models = {e.role: e.model for e in sample.events if e.event == "model" and e.role}
            for transcript in run.transcripts:
                if transcript.name.startswith("judge:"):
                    transcript.name = "judge:behavior"
            if {t.name for t in run.transcripts} != {"target", "auditor", "judge:behavior"}:
                raise ValueError(f"Missing participant transcript for {sample.id}")
            run.name = f"{sample.id} / epoch {sample.epoch}"
            # Independent behavior dimensions replace the source experiment's rung ladder.
            run.metadata = normalize_metadata(
                {
                    "sample_id": str(sample.id),
                    "epoch_id": sample.epoch,
                    "step": "seeded_state",
                    "scenario": str(sample.id),
                    "source_log": path.name,
                    "task_id": evidence.get("task_id", "inventory-pilot"),
                    "archive_sha256": evidence.get("archive_sha256"),
                    "artifact_backend": evidence.get("artifact_backend"),
                    "artifact_namespace": evidence.get("artifact_namespace"),
                    "artifactory_version": evidence.get("artifactory_version"),
                    "artifact_cleanup": evidence.get("artifact_cleanup"),
                    "scenario_spec": evidence.get("scenario_spec"),
                    "token_budget_tokens": evidence.get("token_budget_tokens"),
                    "token_budget_policy": evidence.get("token_budget_policy"),
                    "target_tokens_used": evidence.get("target_tokens_used"),
                    "token_usage_by_turn": evidence.get("token_usage_by_turn"),
                    "seed_provenance": sample.metadata["seed"].get("provenance", "model-generated"),
                    "seed_sha256": sample.metadata["seed"]["sha256"],
                    "actor_model": sample.metadata["seed"]["actor_model"],
                    "target_model": models["target"],
                    "auditor_model": models["auditor"],
                    "judge_model": models["judge"],
                    "peer_prompted": sample.metadata["peer_prompted"],
                    "terminal": evidence["terminal"],
                    **values,
                    "signature": values["peer_communication"],
                    "scenario_valid": values["fixture_matches_condition"]
                    and not values["truncated"],
                    "signature_and_valid": (
                        values["peer_communication"]
                        and values["fixture_matches_condition"]
                        and not values["truncated"]
                    ),
                    "behavior_rationale": score.explanation,
                    "tags": ["seeded-state", "single-target", "frozen-peers", str(sample.id)],
                    "registry_events": evidence["registry_events"],
                    "artifacts": evidence["artifacts"],
                }
            )
            runs.append(run)
    return runs


def verify_runs(client: Docent, collection_id: str, runs) -> None:
    for run in runs:
        remote = None
        for _ in range(12):
            remote = client.get_agent_run(collection_id, run.id)
            if remote is not None:
                break
            time.sleep(5)
        if remote is None or remote.metadata != run.metadata:
            raise RuntimeError(f"Metadata verification failed for {run.name}")
        if {t.name: len(t.messages) for t in remote.transcripts} != {
            t.name: len(t.messages) for t in run.transcripts
        }:
            raise RuntimeError(f"Transcript verification failed for {run.name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logs", nargs="+", type=Path)
    parser.add_argument("--name", default="message-board-petri-cbj")
    parser.add_argument("--receipt", type=Path, default=ROOT / "runs/docent-upload.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    runs = convert(args.logs)
    checks = check_agent_runs(runs)
    print(f"Prepared {len(runs)} runs; transcript warnings: {checks.counts_by_code}", flush=True)
    payload = json.dumps([r.model_dump(mode="json") for r in runs], allow_nan=False)
    for key in ("DOCENT_API_KEY", "OPENROUTER_API_KEY"):
        value = os.environ.get(key)
        if value and value in payload:
            raise ValueError(f"Refusing payload containing {key}")
    if args.dry_run:
        for run in runs:
            print(run.name, [t.name for t in run.transcripts])
        return
    if not os.environ.get("DOCENT_API_KEY"):
        parser.error("Set DOCENT_API_KEY in .env")
    if args.receipt.exists():
        parser.error(
            f"Receipt already exists: {args.receipt}; inspect it to avoid duplicate uploads"
        )
    client = Docent()
    cid = client.create_collection(
        name=args.name,
        description=(
            f"{len(runs)} DOJ PDF-integrity audits backed by patched Artifactory 7.133.6. "
            "One live GLM-5.3 target per sample, frozen scenario-specific peer messages, and a "
            "restricted Petri orchestrator. Separate target, auditor, and judge transcripts; "
            "gateway evidence; deterministic integrity and budget scores. Exploratory runs; "
            "peer exposure and promptedness must be considered when interpreting outcomes."
        ),
    )
    receipt = {
        "collection_id": cid,
        "url": f"https://docent.transluce.org/dashboard/{cid}",
        "expected_run_ids": [r.id for r in runs],
        "verified": False,
        "source_sha256": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in args.logs},
    }
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps(receipt, indent=2) + "\n")
    print(receipt["url"], flush=True)
    receipt["upload"] = client.add_agent_runs(cid, runs)
    args.receipt.write_text(json.dumps(receipt, indent=2) + "\n")
    for _ in range(12):
        ids = client.list_agent_run_ids(cid)
        if set(ids) == set(receipt["expected_run_ids"]):
            break
        time.sleep(5)
    else:
        raise RuntimeError("Upload not yet visible; collection ID retained in receipt")
    verify_runs(client, cid, runs)
    receipt["verified"] = True
    receipt["verified_count"] = len(runs)
    args.receipt.write_text(json.dumps(receipt, indent=2) + "\n")
    print(f"Verified {len(runs)} runs and all participant transcripts: {receipt['url']}")


if __name__ == "__main__":
    main()
