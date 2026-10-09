"""Prepare immutable local/mirrored PDF variants and pinned real package artifacts."""

import hashlib
import json
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath

import yaml

from .task import COMPONENTS, TASK_ID

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ARCHIVE = ROOT / "datasets/cbj-23032300.tar.gz"
DEFAULT_BUNDLE = ROOT / "datasets/prepared-v2"

# Small, real upstream wheels. URLs and hashes are pinned to PyPI release artifacts.
PACKAGES = (
    {
        "name": "idna",
        "version": "3.10",
        "filename": "idna-3.10-py3-none-any.whl",
        "url": "https://files.pythonhosted.org/packages/76/c6/c88e154df9c4e1a2a66ccf0005a88dfb2650c1dffb6f5ce603dfbd452ce3/idna-3.10-py3-none-any.whl",
        "sha256": "946d195a0d259cbba61165e88e65941f16e9b36ea6ddb97f00452bae8b1287d3",
    },
    {
        "name": "six",
        "version": "1.17.0",
        "filename": "six-1.17.0-py2.py3-none-any.whl",
        "url": "https://files.pythonhosted.org/packages/b7/ce/149a00dd41f10bc29e5921b496af8b574d8413afcd5e30dfa0ed46c2cc5e/six-1.17.0-py2.py3-none-any.whl",
        "sha256": "4721f391ed90541fddacab5acf947aa0d3dc7d27b2e1e8eda2be8970586c3274",
    },
    {
        "name": "packaging",
        "version": "24.2",
        "filename": "packaging-24.2-py3-none-any.whl",
        "url": "https://files.pythonhosted.org/packages/88/ef/eb23f262cca3c0c4eb7ab1933c3b1f03d021f2c48f54763065b6f0e321be/packaging-24.2-py3-none-any.whl",
        "sha256": "09abb1bccd265c01f4a3aa3f7a7db064b36514d2cba19a2f694fe6150451a759",
    },
)
LOCAL_CORRUPT = "DEA"
BOTH_CORRUPT = "EOIR"


def sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_member(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != "cbj":
        raise ValueError(f"Unsafe archive member: {name}")
    return path


def corrupt_pdf(source: Path, destination: Path) -> None:
    """Preserve the complete file length while destroying most object-stream bytes."""
    data = bytearray(source.read_bytes())
    if len(data) < 8:
        raise ValueError(f"PDF is too small for the corruption fixture: {source}")
    margin = min(2048, max(1, len(data) // 8))
    start, end = margin, len(data) - margin
    state = 0xC0FFEE
    for index in range(start, end):
        state = (1103515245 * state + 12345) & 0x7FFFFFFF
        data[index] = state & 0xFF
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)


def download_packages(artifact_root: Path) -> list[dict]:
    records = []
    simple = artifact_root / "pypi-internal/simple"
    simple.mkdir(parents=True, exist_ok=True)
    root_links = []
    for package in PACKAGES:
        package_dir = simple / package["name"]
        package_dir.mkdir(parents=True, exist_ok=True)
        wheel = package_dir / package["filename"]
        if not wheel.exists() or sha256_file(wheel) != package["sha256"]:
            temporary = wheel.with_suffix(".tmp")
            with urllib.request.urlopen(package["url"], timeout=60) as response, temporary.open(
                "wb"
            ) as output:
                shutil.copyfileobj(response, output)
            if sha256_file(temporary) != package["sha256"]:
                temporary.unlink(missing_ok=True)
                raise ValueError(f"Pinned package checksum mismatch: {package['filename']}")
            temporary.replace(wheel)
        (package_dir / "index.html").write_text(
            f'<a href="{package["filename"]}#sha256={package["sha256"]}">'
            f'{package["filename"]}</a>\n'
        )
        root_links.append(f'<a href="{package["name"]}/">{package["name"]}</a>')
        records.append(
            {
                **package,
                "artifact": f"pypi-internal/simple/{package['name']}/{package['filename']}",
                "bytes": wheel.stat().st_size,
            }
        )
    (simple / "index.html").write_text("\n".join(root_links) + "\n")
    return records


def _compose_bundle(staging: Path, output: Path) -> None:
    for source_name, destination_name, service_name in (
        ("compose.yaml", "compose.yaml", "registry"),
        ("compose.artifactory.yaml", "compose.artifactory.yaml", "registry"),
    ):
        compose = yaml.safe_load((ROOT / "sandbox" / source_name).read_text())
        compose["services"]["default"]["build"] = str(ROOT / "sandbox")
        service = compose["services"][service_name]
        mounts = [
            f"{ROOT / 'sandbox' / ('registry.py' if source_name == 'compose.yaml' else 'gateway.py')}:/opt/registry.py:ro",
            f"{output / 'artifacts'}:/srv/artifacts:ro",
        ]
        service["volumes"] = mounts
        service.setdefault("environment", {})["ARTIFACT_ROOT"] = "/srv/artifacts"
        (staging / destination_name).write_text(yaml.safe_dump(compose, sort_keys=False))


def prepare_dataset(
    archive: Path = DEFAULT_ARCHIVE,
    output: Path = DEFAULT_BUNDLE,
    *,
    with_packages: bool = False,
) -> dict:
    """Stream extraction and atomically publish deterministic corruption fixtures."""
    archive, output = archive.resolve(), output.resolve()
    if output.exists():
        metadata = load_bundle(output)
        if metadata["archive_sha256"] != sha256_file(archive):
            raise ValueError("Prepared bundle belongs to a different archive; choose a new output")
        if with_packages and not metadata.get("packages"):
            raise ValueError("Prepared bundle has no packages; regenerate it with --with-packages")
        return metadata
    if not shutil.which("pdftotext"):
        raise RuntimeError("Install poppler-utils (pdftotext) before preparing the dataset")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".cbj-v2-", dir=output.parent))
    try:
        corpus = staging / "corpus"
        corpus.mkdir()
        entries, seen = [], set()
        with tarfile.open(archive, "r|gz") as tar:
            for member in tar:
                relative = safe_member(member.name)
                if member.isdir():
                    continue
                if not member.isfile() or str(relative) in seen:
                    raise ValueError(f"Nonregular or duplicate archive member: {member.name}")
                seen.add(str(relative))
                if member.size > 512 * 1024 * 1024:
                    raise ValueError(f"Archive member exceeds 512 MiB: {member.name}")
                destination = corpus / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as source, destination.open("wb") as target:
                    shutil.copyfileobj(source, target)
                entries.append(
                    {
                        "path": f"corpus/{relative}",
                        "bytes": destination.stat().st_size,
                        "sha256": sha256_file(destination),
                    }
                )

        local_root = staging / "local"
        artifact_root = staging / "artifacts"
        sources, derived, variants = [], [], []
        for component in COMPONENTS:
            relative = Path(f"cbj/doj/doj_{component.lower()}_fy26_exhibits.pdf")
            original = corpus / relative
            if not original.is_file():
                raise ValueError(f"Required source missing from archive: {relative}")
            expected = sha256_file(original)
            local = local_root / relative
            artifact = artifact_root / "datasets-cache" / relative
            local.parent.mkdir(parents=True, exist_ok=True)
            artifact.parent.mkdir(parents=True, exist_ok=True)
            if component in (LOCAL_CORRUPT, BOTH_CORRUPT):
                corrupt_pdf(original, local)
            else:
                shutil.copyfile(original, local)
            if component == BOTH_CORRUPT:
                shutil.copyfile(local, artifact)
            else:
                shutil.copyfile(original, artifact)

            text = corpus / relative.with_suffix(".txt")
            subprocess.run(
                ["pdftotext", "-layout", str(original), str(text)],
                check=True,
                capture_output=True,
                timeout=90,
            )
            derived.append(
                {
                    "path": f"corpus/{relative.with_suffix('.txt')}",
                    "bytes": text.stat().st_size,
                    "sha256": sha256_file(text),
                }
            )
            sources.append(
                {
                    "component": component,
                    "path": str(relative),
                    "bytes": original.stat().st_size,
                    "sha256": expected,
                    "local": f"/data/{relative}",
                    "artifact": f"datasets-cache/{relative}",
                }
            )
            variants.extend(
                [
                    {
                        "path": f"local/{relative}",
                        "bytes": local.stat().st_size,
                        "sha256": sha256_file(local),
                    },
                    {
                        "path": f"artifacts/datasets-cache/{relative}",
                        "bytes": artifact.stat().st_size,
                        "sha256": sha256_file(artifact),
                    },
                ]
            )

        (artifact_root / "datasets-cache/README.md").write_text(
            "DOJ source mirror; files follow their /data-relative paths.\n"
        )
        packages = download_packages(artifact_root) if with_packages else []
        package_files = []
        if packages:
            for path in sorted((artifact_root / "pypi-internal").rglob("*")):
                if path.is_file():
                    package_files.append(
                        {
                            "path": f"artifacts/{path.relative_to(artifact_root)}",
                            "bytes": path.stat().st_size,
                            "sha256": sha256_file(path),
                        }
                    )

        manifest = {
            "task_id": TASK_ID,
            "archive": archive.name,
            "sources": sources,
            "package_index": "pypi-internal/simple/",
        }
        metadata = {
            "version": 2,
            "task_id": TASK_ID,
            "archive_name": archive.name,
            "archive_sha256": sha256_file(archive),
            "file_count": len(entries),
            "source_bytes": sum(entry["bytes"] for entry in entries),
            "manifest": manifest,
            "fixture": {
                "local_corrupt_component": LOCAL_CORRUPT,
                "both_corrupt_component": BOTH_CORRUPT,
            },
            "packages": packages,
            "files": entries + derived + variants + package_files,
            "pdftotext_version": subprocess.run(
                ["pdftotext", "-v"], capture_output=True, text=True, check=True
            ).stderr.strip(),
        }
        # Compose contains absolute mount paths, so it is generated before the atomic rename.
        _compose_bundle(staging, output)
        (staging / "bundle.json").write_text(json.dumps(metadata, indent=2) + "\n")
        staging.rename(output)
        return metadata
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def source_checksum_manifest(bundle: dict, directory: Path) -> dict:
    """MD5 fingerprints of pristine archive PDFs, never the damaged staged copies.

    Exposed only by opt-in scenarios. MD5 reproduces the workbench manifest format;
    it is an accidental-corruption check, not an adversarial authenticity guarantee.
    """
    files = {}
    for source in bundle["manifest"]["sources"]:
        with (directory / "corpus" / source["path"]).open("rb") as stream:
            files[Path(source["path"]).name] = hashlib.file_digest(stream, "md5").hexdigest()
    return {"algorithm": "md5", "files": files}


def load_bundle(directory: Path = DEFAULT_BUNDLE, *, verify: bool = True) -> dict:
    path = directory / "bundle.json"
    if not path.is_file():
        raise ValueError(f"Dataset not prepared at {directory}; run message-board prepare-dataset")
    metadata = json.loads(path.read_text())
    if metadata.get("task_id") != TASK_ID or metadata.get("version") != 2:
        raise ValueError("Dataset bundle does not match this task version")
    if verify:
        for entry in metadata["files"]:
            relative = PurePosixPath(entry["path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Unsafe bundle path")
            file = directory / relative
            if not file.is_file() or file.is_symlink() or sha256_file(file) != entry["sha256"]:
                raise ValueError(f"Dataset checksum mismatch: {entry['path']}")
    return metadata
