"""Plain object storage, not a real Artifactory installation. No outbound I/O.

Seed data and the append-only request ledger are outside the served namespace.
Only the harness can read them through the separate registry container.
"""

import hashlib
import json
import os
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

MAX_BODY = 64 * 1024


class Registry:
    def __init__(self, objects=None, ledger=None, corpus_root=None, artifact_root=None):
        self.objects = {k: v.encode() for k, v in (objects or {}).items()}
        self.files = {}
        if corpus_root or artifact_root:
            root = Path(artifact_root or corpus_root).resolve()
            prefix = "" if artifact_root else "datasets-cache/"
            self.files = {
                prefix + str(p.relative_to(root)): p
                for p in root.rglob("*")
                if p.is_file() and not p.is_symlink() and p.resolve().is_relative_to(root)
            }
        self.seed_paths = set(self.objects) | set(self.files)
        self.repos = {"datasets-cache", "pypi-internal", "scratch-local"}
        self.ledger = ledger
        self.events = []
        self.lock = threading.RLock()

    def request(self, method, url, body=b""):
        with self.lock:
            return self._request(method, url, body)

    def _request(self, method, url, body):
        parsed = urlsplit(url)
        path = unquote(parsed.path).strip("/")
        status, result = 404, {"error": "not found"}
        kind = "miss"
        if parsed.query or any(p in ("..", ".") for p in path.split("/")):
            status, result = 400, {"error": "invalid object path"}
        elif method == "GET":
            if not path:
                status, result, kind = 200, {"repositories": sorted(self.repos)}, "list"
            elif path in self.objects:
                status, result, kind = 200, self.objects[path], "read"
            elif path in self.files:
                status, result, kind = 200, self.files[path].read_bytes(), "read"
            elif path in self.repos or any(
                p.startswith(path + "/") for p in self.seed_paths | self.objects.keys()
            ):
                status, result, kind = (
                    200,
                    {
                        "repo": path,
                        "objects": sorted(
                            p[len(path) + 1 :]
                            for p in self.seed_paths | self.objects.keys()
                            if p.startswith(path + "/")
                        ),
                    },
                    "list",
                )
        elif method in ("PUT", "POST"):
            if path.startswith(("datasets-cache/", "pypi-internal/")):
                status, result = 403, {"error": "repository is read-only"}
            elif len(body) > MAX_BODY:
                status, result = 413, {"error": "object too large"}
            elif len(PurePosixPath(path).parts) >= 2:
                self.objects[path] = body
                self.repos.add(path.split("/")[0])
                status, result, kind = 201, {"stored": "/" + path, "bytes": len(body)}, "write"
        else:
            status, result = 405, {"error": "method not allowed"}
        event = {
            "sequence": len(self.events),
            "method": method,
            "path": path,
            "status": status,
            "kind": kind,
            "seed_object": path in self.seed_paths,
        }
        if kind == "write":
            event.update(
                body=body.decode("utf-8", "replace"), sha256=hashlib.sha256(body).hexdigest()
            )
        self.events.append(event)
        if self.ledger:
            with open(self.ledger, "a") as f:
                f.write(json.dumps(event) + "\n")
        return status, result if isinstance(result, bytes) else json.dumps(result).encode()


def serve():
    registry = None
    seed_path = os.environ.get("SEED_PATH", "/tmp/seed.json")
    ledger = os.environ.get("LEDGER_PATH", "/tmp/requests.jsonl")

    class Handler(BaseHTTPRequestHandler):
        server_version = "SyntheticArtifactStore/1.0"

        def handle_request(self):
            nonlocal registry
            # Seeding is harness-only filesystem I/O, never an HTTP management endpoint.
            if registry is None:
                try:
                    with open(seed_path) as f:
                        registry = Registry(
                            json.load(f),
                            ledger,
                            os.environ.get("CORPUS_ROOT"),
                            os.environ.get("ARTIFACT_ROOT"),
                        )
                except FileNotFoundError:
                    self.send_error(503)
                    return
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                self.send_error(400)
                return
            if length < 0 or length > MAX_BODY:
                self.send_error(413)
                return
            status, body = registry.request(self.command, self.path, self.rfile.read(length))
            self.send_response(status)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_PUT = do_POST = do_DELETE = do_OPTIONS = handle_request

        def log_message(self, *_):
            pass

    # Requests serialize: this also makes read-after-write and seed initialization deterministic.
    from http.server import HTTPServer

    HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()


if __name__ == "__main__":
    serve()
