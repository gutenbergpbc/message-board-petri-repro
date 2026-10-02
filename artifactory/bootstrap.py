#!/usr/bin/env python3
"""Idempotently configure the patched Artifactory used by all samples in one run."""

import base64
import json
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://artifactory-backend:8082/artifactory"
EXPECTED_VERSION = "7.133.6"
EXPECTED_IMAGE_DIGEST = "sha256:55edcdeb55f92aba5ff223c526056db772e7d946669e74ceb27049750fc2c298"
CONFIG = Path("/app/repositories.yaml").read_bytes()


def request(url, *, method="GET", data=None, headers=None, accepted=(200)):
    accepted = (accepted,) if isinstance(accepted, int) else accepted
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            status, body = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, body = error.code, error.read()
    if status not in accepted:
        raise RuntimeError(f"{method} {url} returned {status}: {body[:1000]!r}")
    return status, body


def main():
    encoded = base64.b64encode(b"admin:password").decode()
    auth = {"Authorization": f"Basic {encoded}"}
    request(f"{BASE}/ui/jcr/eula/accept", method="POST", headers=auth, accepted=(200, 204, 409))
    request(
        f"{BASE}/api/system/configuration",
        method="PATCH",
        data=CONFIG,
        headers={**auth, "Content-Type": "application/yaml"},
    )
    _, body = request(f"{BASE}/api/system/version", headers=auth)
    version = json.loads(body)["version"]
    if version != EXPECTED_VERSION:
        raise RuntimeError(f"Expected patched Artifactory {EXPECTED_VERSION}, got {version}")
    sentinel = b"patched-artifactory-ready\n"
    request(
        f"{BASE}/petri-local/.bootstrap/version.txt",
        method="PUT",
        data=sentinel,
        headers={**auth, "X-Checksum-Sha256": "e455b4a58b8a351a2e6cd8b91359b9dc6b86597004734f0c3f2e75ce7f0d460e"},
        accepted=(200, 201),
    )
    _, fetched = request(f"{BASE}/petri-local/.bootstrap/version.txt", headers=auth)
    if fetched != sentinel:
        raise RuntimeError("Artifactory repository round-trip failed")
    print(
        json.dumps(
            {
                "ready": True,
                "version": version,
                "image_digest": EXPECTED_IMAGE_DIGEST,
                "repository": "petri-local",
            }
        )
    )


if __name__ == "__main__":
    main()
