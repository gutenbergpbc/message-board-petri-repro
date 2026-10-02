import io
import shutil
import tarfile

import pytest

from message_board_petri.dataset import load_bundle, prepare_dataset
from message_board_petri.scenarios import COMPONENTS


def archive_at(path, entries):
    with tarfile.open(path, "w:gz") as tar:
        for name, body, kind in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.size = len(body)
            if kind == tarfile.SYMTYPE:
                member.linkname = "/etc/passwd"
            tar.addfile(member, io.BytesIO(body))
    return path


@pytest.mark.parametrize(
    "entries",
    [
        [("../escape", b"x", tarfile.REGTYPE)],
        [("/cbj/absolute", b"x", tarfile.REGTYPE)],
        [("cbj/link", b"", tarfile.SYMTYPE)],
        [("cbj/x", b"a", tarfile.REGTYPE), ("cbj/x", b"b", tarfile.REGTYPE)],
    ],
)
def test_untrusted_archives_leave_no_partial_bundle(tmp_path, entries):
    if not shutil.which("pdftotext"):
        pytest.skip("pdftotext required")
    archive = archive_at(tmp_path / "bad.tar.gz", entries)
    output = tmp_path / "prepared"
    with pytest.raises(ValueError):
        prepare_dataset(archive, output)
    assert not output.exists()
    assert not (tmp_path / "escape").exists()
    assert not list(tmp_path.glob(".cbj-*"))


def test_prepare_reuse_and_integrity(tmp_path, monkeypatch):
    # Replace only PDF conversion: exercise actual tar streaming, hashes and bundle publishing.
    import subprocess

    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/pdftotext")

    def convert(args, **kwargs):
        if "-v" in args:
            return subprocess.CompletedProcess(args, 0, stderr="pdftotext test")
        from pathlib import Path

        Path(args[-1]).write_text("Summary of Requirements\fsecond page\f")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(subprocess, "run", convert)
    entries = [
        (f"cbj/doj/doj_{c.lower()}_fy26_exhibits.pdf", b"%PDF binary\xff", tarfile.REGTYPE)
        for c in COMPONENTS
    ]
    archive = archive_at(tmp_path / "cbj.tar.gz", entries)
    output = tmp_path / "prepared"
    first = prepare_dataset(archive, output)
    assert first == prepare_dataset(archive, output)
    assert first["file_count"] == len(COMPONENTS)
    assert len(first["manifest"]["sources"]) == len(COMPONENTS)
    assert not first["packages"]
    expected = {source["component"]: source["sha256"] for source in first["manifest"]["sources"]}
    assert first["fixture"] == {
        "local_corrupt_component": "DEA",
        "both_corrupt_component": "EOIR",
    }
    assert (
        __import__("hashlib").sha256(
            (output / "local/cbj/doj/doj_dea_fy26_exhibits.pdf").read_bytes()
        ).hexdigest()
        != expected["DEA"]
    )
    assert "budget_reference" not in (output / "compose.yaml").read_text()
    assert (output / "compose.artifactory.yaml").is_file()
    (output / "corpus/cbj/doj/doj_dea_fy26_exhibits.pdf").write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        load_bundle(output)


def test_missing_required_source_fails_before_publish(tmp_path):
    if not shutil.which("pdftotext"):
        pytest.skip("pdftotext required")
    archive = archive_at(tmp_path / "partial.tar.gz", [("cbj/x", b"x", tarfile.REGTYPE)])
    with pytest.raises(ValueError, match="Required source missing"):
        prepare_dataset(archive, tmp_path / "out")
    assert not (tmp_path / "out").exists()
