"""Harness-only filesystem checkpoints; never exposed as a target tool.

The archive lives in the registry container between attempts, not on the workstation.
Only the initial (pre-generation) state is supported. Restore kills post-checkpoint
agent processes and restores all persistent agent-writable directories in this image.
"""

import json
import os
import signal
import sys
import tarfile
from pathlib import Path

ROOTS = ("workspace", "data", "home/agent", "tmp", "var/tmp", "dev/shm", "run/lock")


def processes():
    result = {}
    for path in Path("/proc").glob("[0-9]*"):
        try:
            if path.stat().st_uid == os.getuid():
                # starttime protects against PID reuse.
                result[path.name] = path.joinpath("stat").read_text().rsplit(")", 1)[1].split()[19]
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            pass
    return result


def snapshot(archive):
    baseline = processes()
    with tarfile.open(archive, "w:gz") as tar:
        for root in ROOTS:
            path = Path("/" + root)
            # /tmp and shared-memory roots belong to root; a non-root restore cannot
            # chmod/utime them. Save their contents, not their directory attributes.
            paths = [path] if path.stat().st_uid == os.getuid() else path.iterdir()
            for item in paths:
                tar.add(str(item), arcname=str(item).lstrip("/"),
                        filter=lambda info: None if "/" + info.name == archive else info)
    print(json.dumps({"processes": baseline, "roots": ROOTS}))


def quiesce(baseline):
    keep = set()
    pid = os.getpid()
    while pid:
        keep.add(str(pid))
        pid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
    for pid, started in processes().items():
        if pid not in keep and baseline.get(pid) != started:
            try:
                os.kill(int(pid), signal.SIGKILL)
            except ProcessLookupError:
                pass


def remove(path):
    if path.is_symlink() or not path.is_dir():
        path.unlink()
    else:
        path.chmod(0o700)
        for child in path.iterdir():
            remove(child)
        path.rmdir()


def restore(archive):
    # Hold the archive open while clearing /tmp, so it leaves no target-visible file.
    with tarfile.open(archive, "r:gz") as tar:
        for root in ROOTS:
            path = Path("/" + root)
            if path.stat().st_uid == os.getuid():
                path.chmod(0o700)
            for child in path.iterdir():
                remove(child)
        # This is the immutable, hash-verified pre-target archive, not model data.
        # Restore directory modes too (the data filter strips them).
        tar.extractall("/", filter="fully_trusted")
    print(json.dumps({"restored": True, "roots": ROOTS}))


if __name__ == "__main__":
    mode = sys.argv[1]
    if mode == "snapshot":
        snapshot(sys.argv[2])
    elif mode == "quiesce":
        quiesce(json.load(sys.stdin)["processes"])
    elif mode == "restore":
        restore(sys.argv[2])
    else:
        raise SystemExit("unknown checkpoint command")
