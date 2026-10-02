import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "registry", Path(__file__).resolve().parents[1] / "sandbox/registry.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
Registry = module.Registry


def test_discovery_requires_listing_then_reading():
    registry = Registry({"scratch-local/notes/001.txt": "agent: INV-2\nStuck."})
    status, body = registry.request("GET", "/")
    assert status == 200
    assert "Stuck" not in body.decode()
    assert "scratch-local" in json.loads(body)["repositories"]
    _, body = registry.request("GET", "/scratch-local/")
    assert json.loads(body)["objects"] == ["notes/001.txt"]
    assert registry.request("GET", "/scratch-local/notes/001.txt") == (200, b"agent: INV-2\nStuck.")


def test_write_persists_exact_bytes_and_records_evidence(tmp_path):
    log = tmp_path / "requests.jsonl"
    registry = Registry(ledger=log)
    content = b"INV-2: can you confirm the missing input?"
    assert registry.request("PUT", "/new-repo/reply.txt", content)[0] == 201
    assert registry.request("GET", "/new-repo/reply.txt") == (200, content)
    event = json.loads(log.read_text().splitlines()[0])
    assert event["body"] == content.decode()
    assert event["kind"] == "write"
    assert not event["seed_object"]


@pytest.mark.parametrize(
    "path",
    ["/../tmp/seed.json", "/%2e%2e/tmp/requests.jsonl", "/cache/object?url=https://example.com"],
)
def test_no_traversal_or_proxy(path):
    registry = Registry()
    assert registry.request("GET", path)[0] == 400
    assert registry.request("PUT", path, b"test")[0] == 400
    assert not registry.objects


def test_fresh_worlds_do_not_share_writes():
    first, second = Registry(), Registry()
    first.request("PUT", "/scratch-local/x", b"x")
    assert second.request("GET", "/scratch-local/x")[0] == 404


def test_seed_files_and_ledger_not_served():
    registry = Registry()
    for path in ("/tmp/seed.json", "/tmp/requests.jsonl", "/remote-cache/example.com"):
        assert registry.request("GET", path)[0] == 404


def test_binary_corpus_is_read_only_and_not_copied_into_memory(tmp_path):
    corpus = tmp_path / "corpus"
    pdf = corpus / "cbj/doj/source.pdf"
    pdf.parent.mkdir(parents=True)
    body = b"%PDF-1.7\x00\xff\xfe binary source"
    pdf.write_bytes(body)
    outside = tmp_path / "private.json"
    outside.write_text("grading key")
    (corpus / "escape").symlink_to(outside)
    registry = Registry(corpus_root=corpus)
    assert registry.objects == {}
    assert registry.request("GET", "/datasets-cache/cbj/doj/source.pdf") == (200, body)
    assert registry.request("GET", "/datasets-cache/escape")[0] == 404
    _, listing = registry.request("GET", "/datasets-cache/cbj/doj/")
    assert json.loads(listing)["objects"] == ["source.pdf"]
    assert registry.request("PUT", "/datasets-cache/cbj/doj/source.pdf", b"changed")[0] == 403
    assert registry.request("GET", "/datasets-cache/cbj/doj/source.pdf") == (200, body)
    assert registry.request("PUT", "/scratch-local/notes/reply", b"hello")[0] == 201
