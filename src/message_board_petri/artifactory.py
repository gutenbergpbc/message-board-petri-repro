"""Lifecycle helpers for the persistent patched Artifactory integration backend."""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "artifactory/compose.yaml"
VERSION = "7.133.6"
IMAGE_DIGEST = "sha256:55edcdeb55f92aba5ff223c526056db772e7d946669e74ceb27049750fc2c298"
NETWORK = "message-board-artifactory-backend"


def compose(*args: str, timeout: int = 1200, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), *args],
        check=True,
        text=True,
        capture_output=capture,
        timeout=timeout,
    )


def ensure_running(*, pull: bool = False) -> dict:
    if pull:
        compose("pull", "postgres", "artifactory", timeout=1800)
    compose("up", "-d", "--wait", "postgres", "artifactory", timeout=1200)
    result = compose("--profile", "tools", "run", "--rm", "bootstrap", capture=True)
    line = next((line for line in reversed(result.stdout.splitlines()) if line.startswith("{")), None)
    if not line:
        raise RuntimeError(f"Artifactory bootstrap returned no receipt: {result.stdout[-1000:]}")
    receipt = json.loads(line)
    if receipt.get("version") != VERSION or receipt.get("image_digest") != IMAGE_DIGEST:
        raise RuntimeError(f"Unexpected Artifactory bootstrap receipt: {receipt}")
    return receipt


def stop(*, purge: bool = False) -> None:
    args = ["down", "--remove-orphans"]
    if purge:
        args.append("--volumes")
    compose(*args)
