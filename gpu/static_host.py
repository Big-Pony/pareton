"""Scoped housekeeping on a dedicated static GPU host (stdlib only).

Invoked remotely by orchestration before image pulls and after the harness.
Only Pareton bench resources and explicitly tracked candidate image references
are removed. Model downloads, baseline compile caches and unrelated Docker
resources are never pruned.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)
REMOTE_LOCK = "/opt/pareton/.static-host.lock"
REMOTE_IMAGES = "/opt/pareton/.static-host-images.json"
_CONTAINER = re.compile(r"^pareton-bench-[0-9a-f]{12}-[a-zA-Z0-9_.-]+$")
_NETWORK = re.compile(r"^pareton-bench-[0-9a-f]{12}$")
_OUTPUT = re.compile(r"^static-pt-\d{14}-[0-9]+(?:\.[0-9]+)?h-[0-9a-f]{8}$")


class HostBusyError(RuntimeError):
    """An active harness owns this host; idle housekeeping must skip it."""


@contextmanager
def host_lock(path: Path) -> Iterator[None]:
    import fcntl

    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise HostBusyError(
                "static GPU host is busy with another Pareton run"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _docker(*args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=120, check=False
    )
    if result.returncode:
        raise RuntimeError(f"docker {args[0]} failed: {result.stderr.strip()}")
    return result


def _image_refs(raw: object) -> set[str]:
    if not isinstance(raw, list) or any(
        not isinstance(ref, str) or not ref or ref.startswith("-") for ref in raw
    ):
        raise ValueError("image tracking data must be a list of image references")
    return set(raw)


def cleanup_containers(*, docker=_docker) -> int:
    """Remove only abandoned Pareton containers/networks while holding host_lock."""
    removed = 0
    for args, pattern, kind in (
        (("ps", "-a", "--format", "{{.Names}}"), _CONTAINER, "container"),
        (("network", "ls", "--format", "{{.Name}}"), _NETWORK, "network"),
    ):
        for name in docker(*args).stdout.splitlines():
            if pattern.fullmatch(name):
                if kind == "container":
                    docker("rm", "-f", "-v", name)
                    removed += 1
                else:
                    docker("network", "rm", name)
                logger.info("removed stale Pareton %s %s", kind, name)
    return removed


def check_idle_gpu(*, runner=subprocess.run, sleep=time.sleep) -> None:
    """Detect remaining compute processes; never kill unknown processes or reset GPUs."""
    for attempt in range(3):
        result = runner(
            ["nvidia-smi", "--query-compute-apps=pid", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(f"cannot verify idle GPU: {result.stderr.strip()}")
        pids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if not pids:
            return
        if attempt < 2:
            sleep(1)
    raise RuntimeError(f"GPU compute processes remain after cleanup: {', '.join(pids)}")


def reap_idle_containers(
    *, lock_path: Path = Path(REMOTE_LOCK), docker=_docker, verify=check_idle_gpu
) -> dict:
    """Periodic cleanup independent of the worker, without touching images or reports."""
    try:
        with host_lock(lock_path):
            removed = cleanup_containers(docker=docker)
            verify()
            return {"status": "cleaned", "containers_removed": removed}
    except HostBusyError:
        return {"status": "busy", "containers_removed": 0}


def cleanup(
    *,
    tracked_path: Path,
    candidates: set[str],
    keep: set[str],
    docker=_docker,
    output_root: Path = Path("/opt/pareton/out"),
) -> None:
    """Reclaim previous rounds, then register candidates before they are pulled.

    Failed image removals remain tracked for the next attempt. Never force image
    deletion: an unrelated container may still reference the same image.
    """
    tracked = (
        _image_refs(json.loads(tracked_path.read_text()))
        if tracked_path.exists()
        else set()
    )
    cleanup_containers(docker=docker)
    remaining = set(tracked)
    failures = []
    for ref in sorted(tracked - keep):
        try:
            docker("image", "rm", ref)
        except RuntimeError as exc:
            if "No such image" in str(exc):
                remaining.discard(ref)
            else:
                failures.append(str(exc))
        else:
            remaining.discard(ref)
    remaining.update(candidates)
    tmp = tracked_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(remaining)) + "\n")
    tmp.replace(tracked_path)
    if output_root.is_dir():
        for child in output_root.iterdir():
            if (
                _OUTPUT.fullmatch(child.name)
                and child.is_dir()
                and not child.is_symlink()
            ):
                shutil.rmtree(child)
    if failures:
        raise RuntimeError(
            "candidate image cleanup needs retry: " + "; ".join(failures)
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", default="[]")
    parser.add_argument("--keep", default="[]")
    parser.add_argument("--idle-containers", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    if args.idle_containers:
        print(json.dumps(reap_idle_containers()))
        return 0
    with host_lock(Path(REMOTE_LOCK)):
        cleanup(
            tracked_path=Path(REMOTE_IMAGES),
            candidates=_image_refs(json.loads(args.candidates)),
            keep=_image_refs(json.loads(args.keep)),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
