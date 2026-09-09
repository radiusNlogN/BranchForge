"""Executing repository tests inside a disposable, locked-down container.

This is the only place in BranchForge where repository code runs at all, and it
runs nowhere except inside `docker run`. The orchestrating worker stays outside
the container.

**This is local container isolation, not a production multi-tenant security
guarantee.** It raises the cost of a hostile repository considerably — no
network, no root, no capabilities, a read-only source mount, and CPU/memory/PID/
wall-clock ceilings — but a container escape is a container escape. Do not treat
it as a sandbox for deliberately hostile code from strangers.

Two things that are easy to get wrong and are handled here explicitly:

* **Capping our own buffer does not cap the Docker daemon's logs.** Containers
  run with `--log-driver=none`; the attached stream is what we capture.
* **A capped reader must keep draining.** Once the retention limit is hit the
  reader keeps consuming and discarding, because a full pipe would otherwise
  block the container and turn a log cap into a hang.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

# Nobody:nogroup. Present in effectively every Linux base image.
CONTAINER_UID_GID = "65534:65534"
WORKSPACE_MOUNTPOINT = "/workspace"
RESULTS_MOUNTPOINT = "/results"
REPORT_FILENAME = "report.json"
PLUGIN_DIR = "/opt/branchforge"


class RunnerError(Exception):
    """Base class for runner failures."""

    kind = "runner_error"


class DockerUnavailable(RunnerError):
    """The Docker CLI or daemon could not be used."""

    kind = "docker_unavailable"


@dataclass
class RunnerLimits:
    """Resource ceilings applied to every container."""

    memory: str = "512m"
    cpus: str = "1.0"
    pids: int = 256
    tmpfs_bytes: int = 64 * 1024 * 1024
    timeout_seconds: float = 300.0
    max_log_bytes: int = 200_000
    max_report_bytes: int = 5_000_000
    docker_binary: str = "docker"
    cleanup_timeout_seconds: float = 30.0


@dataclass
class RunResult:
    """Everything one container run produced."""

    exit_code: int | None
    log: str
    log_truncated: bool
    duration_seconds: float
    timed_out: bool = False
    report: dict[str, Any] | None = None
    report_error: str | None = None
    argv: list[str] = field(default_factory=list)
    cleanup_errors: list[str] = field(default_factory=list)
    container_name: str = ""


def _docker(args: list[str], limits: RunnerLimits) -> subprocess.CompletedProcess[str]:
    """Run a short docker command, mapping absence and hangs to DockerUnavailable."""
    try:
        return subprocess.run(
            [limits.docker_binary, *args],
            capture_output=True,
            text=True,
            timeout=limits.cleanup_timeout_seconds,
        )
    except FileNotFoundError as error:
        raise DockerUnavailable(
            f"The Docker CLI ({limits.docker_binary!r}) was not found on PATH."
        ) from error
    except subprocess.TimeoutExpired as error:
        raise DockerUnavailable(f"Timed out running `docker {args[0]}`.") from error


def resolve_image_id(image_ref: str, *, limits: RunnerLimits) -> str:
    """Resolve a tag to an immutable image ID **once**, before any run.

    Baseline, comparison, and supplemental runs then all execute that exact ID,
    so a tag moving mid-verification cannot make the phases incomparable.

    Two lookups, because one is not enough in practice: with Docker Desktop's
    containerd image store, `docker image inspect` fails on an image built as a
    multi-platform *manifest list* (buildx adds attestation manifests by default),
    reporting "No such image" for a tag that `docker images` lists happily. So
    `docker image ls -q` is tried as well before concluding the image is missing.
    """
    completed = _docker(
        ["image", "inspect", image_ref, "--format", "{{.Id}}"], limits
    )
    image_id = completed.stdout.strip() if completed.returncode == 0 else ""

    if not image_id:
        listed = _docker(["image", "ls", "--no-trunc", "-q", image_ref], limits)
        if listed.returncode == 0:
            # Several lines can come back for a multi-arch tag; the first is ours.
            image_id = listed.stdout.strip().splitlines()[0].strip() if listed.stdout.strip() else ""

    if image_id:
        return image_id

    message = (completed.stderr or completed.stdout or "").strip()[:500]
    if "Cannot connect to the Docker daemon" in message:
        raise DockerUnavailable(
            "The Docker daemon is not reachable. Start Docker and try again."
        )
    raise DockerUnavailable(
        f"The runner image {image_ref!r} is not available locally. Build it with:\n"
        f"  docker build --provenance=false -t {image_ref} backend/runner/python-pytest\n"
        f"Docker said: {message}"
    )


def build_run_argv(
    *,
    workspace: str,
    results_dir: str,
    image_id: str,
    container_name: str,
    limits: RunnerLimits,
    report_max_tests: int = 5000,
    report_max_message_chars: int = 400,
) -> list[str]:
    """Build the `docker run` argument vector.

    An argument **array**, never an interpolated shell string. Nothing derived
    from the repository, the model, or the issue text reaches this list: the only
    variable parts are runner-owned paths and the resolved image ID.

    Paths are realpath'd because on macOS `mkdtemp()` returns `/var/folders/...`
    while Docker Desktop only shares the `/private/var/folders/...` it resolves to.
    """
    workspace_path = os.path.realpath(workspace)
    results_path = os.path.realpath(results_dir)

    return [
        limits.docker_binary,
        "run",
        "--rm",
        "--name", container_name,
        # The daemon's own log buffer is not bounded by our reader, so disable it.
        "--log-driver=none",
        # No network at all: repository tests cannot phone home or fetch anything.
        "--network=none",
        "--user", CONTAINER_UID_GID,
        "--read-only",
        f"--tmpfs=/tmp:rw,noexec,nosuid,size={limits.tmpfs_bytes}",
        f"--memory={limits.memory}",
        f"--cpus={limits.cpus}",
        f"--pids-limit={limits.pids}",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        # A read-only root filesystem plus a non-root UID means HOME must be
        # somewhere writable or Python and pytest fail before collection.
        "-e", "HOME=/tmp",
        "-e", "PYTHONDONTWRITEBYTECODE=1",
        "-e", f"PYTHONPATH={PLUGIN_DIR}:{WORKSPACE_MOUNTPOINT}:{WORKSPACE_MOUNTPOINT}/src",
        "-e", f"BF_REPORT_PATH={RESULTS_MOUNTPOINT}/{REPORT_FILENAME}",
        "-e", f"BF_REPORT_MAX_TESTS={report_max_tests}",
        "-e", f"BF_REPORT_MAX_MESSAGE_CHARS={report_max_message_chars}",
        # The source tree is mounted read-only; the only writable places are the
        # tmpfs and the runner-owned results directory.
        "-v", f"{workspace_path}:{WORKSPACE_MOUNTPOINT}:ro",
        "-v", f"{results_path}:{RESULTS_MOUNTPOINT}:rw",
        "-w", WORKSPACE_MOUNTPOINT,
        image_id,
        "python", "-m", "pytest",
        # A fixed, runner-owned invocation. `-o addopts=` is the load-bearing
        # flag: without it a repository's ini addopts could inject plugins or
        # arguments into a command that is supposed to be ours.
        "-o", "addopts=",
        "-p", "no:cacheprovider",
        "-p", "bf_report",
        "-q",
        "--tb=short",
        WORKSPACE_MOUNTPOINT,
    ]


class _BoundedDrain:
    """Reads a pipe to EOF, retaining only the first `limit` bytes.

    Draining continues past the limit on purpose: stopping would fill the pipe
    and block the container, converting an output cap into a hang.
    """

    def __init__(self, stream: Any, limit: int) -> None:
        self._stream = stream
        self._limit = limit
        self.buffer = bytearray()
        self.truncated = False
        self.total = 0
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            while True:
                chunk = self._stream.read(8192)
                if not chunk:
                    break
                self.total += len(chunk)
                remaining = self._limit - len(self.buffer)
                if remaining > 0:
                    self.buffer.extend(chunk[:remaining])
                if self.total > self._limit:
                    self.truncated = True
        except (ValueError, OSError):  # pragma: no cover - pipe closed under us
            pass

    def start(self) -> None:
        self.thread.start()

    def join(self, timeout: float) -> bool:
        self.thread.join(timeout)
        return not self.thread.is_alive()


def _force_remove(container_name: str, limits: RunnerLimits, errors: list[str]) -> None:
    """Remove a container, recording rather than raising on failure."""
    try:
        completed = subprocess.run(
            [limits.docker_binary, "rm", "-f", container_name],
            capture_output=True,
            text=True,
            timeout=limits.cleanup_timeout_seconds,
        )
        if completed.returncode != 0:
            message = (completed.stderr or "").strip()
            # A container that already exited with --rm is gone; not an error.
            if "No such container" not in message:
                errors.append(f"docker rm -f {container_name}: {message[:200]}")
    except subprocess.TimeoutExpired:
        errors.append(f"docker rm -f {container_name} timed out.")
    except FileNotFoundError:  # pragma: no cover - checked earlier
        errors.append("Docker CLI disappeared during cleanup.")


def read_report(results_dir: str, *, limits: RunnerLimits) -> tuple[dict[str, Any] | None, str | None]:
    """Read and parse the structured report. Returns (report, error_message).

    A missing, oversized, or malformed report is an explicit error. It must never
    be treated as "no failures" — that would turn a crashed run into a pass.
    """
    path = os.path.join(results_dir, REPORT_FILENAME)
    if not os.path.exists(path):
        return None, "The test run produced no structured report."

    try:
        size = os.path.getsize(path)
    except OSError as error:  # pragma: no cover - defensive
        return None, f"Could not stat the structured report: {error}"

    if size > limits.max_report_bytes:
        return None, (
            f"The structured report is {size:,} bytes, over the "
            f"{limits.max_report_bytes:,}-byte limit; refusing to parse it."
        )

    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as error:
        return None, f"The structured report could not be parsed: {error}"

    if not isinstance(payload, dict) or payload.get("schema") != "branchforge.report.v1":
        return None, "The structured report is missing or has an unrecognised schema."
    if not isinstance(payload.get("tests"), dict) or not isinstance(
        payload.get("collected"), list
    ):
        return None, "The structured report is malformed."
    return payload, None


def run_tests(
    *,
    workspace: str,
    results_dir: str,
    image_id: str,
    limits: RunnerLimits,
    report_max_tests: int = 5000,
    report_max_message_chars: int = 400,
) -> RunResult:
    """Run the fixed pytest invocation against `workspace` in a container."""
    container_name = f"branchforge-verify-{uuid.uuid4().hex[:16]}"
    argv = build_run_argv(
        workspace=workspace,
        results_dir=results_dir,
        image_id=image_id,
        container_name=container_name,
        limits=limits,
        report_max_tests=report_max_tests,
        report_max_message_chars=report_max_message_chars,
    )

    # The container writes the report here as UID 65534, so the directory has to
    # be writable by it. The directory is runner-owned and holds nothing else.
    os.makedirs(results_dir, mode=0o777, exist_ok=True)
    os.chmod(results_dir, 0o777)

    cleanup_errors: list[str] = []
    started = time.monotonic()

    try:
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            close_fds=True,
        )
    except FileNotFoundError as error:
        raise DockerUnavailable(
            f"The Docker CLI ({limits.docker_binary!r}) was not found on PATH."
        ) from error

    assert process.stdout is not None
    drain = _BoundedDrain(process.stdout, limits.max_log_bytes)
    drain.start()

    timed_out = False
    exit_code: int | None
    try:
        exit_code = process.wait(timeout=limits.timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        exit_code = None
        # Kill the container first: killing only the CLI would leave it running.
        _force_remove(container_name, limits, cleanup_errors)
        process.kill()
        try:
            exit_code = process.wait(timeout=limits.cleanup_timeout_seconds)
        except subprocess.TimeoutExpired:
            cleanup_errors.append("The Docker CLI process did not exit after being killed.")
            exit_code = None

    if not drain.join(limits.cleanup_timeout_seconds):
        cleanup_errors.append("The output reader thread did not finish.")

    try:
        process.stdout.close()
    except OSError:  # pragma: no cover - already closed
        pass

    duration = time.monotonic() - started
    log = drain.buffer.decode("utf-8", errors="replace")

    if timed_out:
        # A timed-out run has no trustworthy report even if a partial file exists.
        return RunResult(
            exit_code=exit_code,
            log=log,
            log_truncated=drain.truncated,
            duration_seconds=duration,
            timed_out=True,
            report=None,
            report_error=(
                f"The test run exceeded its {limits.timeout_seconds:g}s wall-clock limit "
                f"and the container was removed."
            ),
            argv=argv,
            cleanup_errors=cleanup_errors,
            container_name=container_name,
        )

    if exit_code is not None and exit_code >= 125 and "Cannot connect to the Docker daemon" in log:
        raise DockerUnavailable("The Docker daemon became unreachable during the run.")

    report, report_error = read_report(results_dir, limits=limits)
    return RunResult(
        exit_code=exit_code,
        log=log,
        log_truncated=drain.truncated,
        duration_seconds=duration,
        timed_out=False,
        report=report,
        report_error=report_error,
        argv=argv,
        cleanup_errors=cleanup_errors,
        container_name=container_name,
    )


def docker_available(limits: RunnerLimits | None = None) -> bool:
    """Cheap check used to skip live tests and to fail the worker early."""
    limits = limits or RunnerLimits()
    try:
        completed = subprocess.run(
            [limits.docker_binary, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=limits.cleanup_timeout_seconds,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0
