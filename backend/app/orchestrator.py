"""Bounded competing attempts: reserve, run, reconcile, compare.

    uv run python -m app.worker orchestrate --run-id <UUID>

One orchestrator process owns one run's attempts. It reserves every attempt slot
atomically, then runs one **child process** per attempt (`app.worker run-attempt`),
each covering the whole propose → verify pipeline and reusing the existing
synchronous agent and Docker code unchanged.

Concurrency is an `asyncio.Semaphore`. A slot is held until its child has exited,
its output reader has finished, the database has been reconciled, and container
cleanup for that attempt has been *confirmed* — releasing earlier would let a new
pipeline start while an orphaned container from the last one still runs. If
cleanup cannot be confirmed, no further queued work is launched and the failure is
recorded rather than claiming the bound still holds.

The limit applies to this one process. Two orchestrators for two runs each get
their own; this is not a global capacity limit.

What this does NOT do: schedule, retry, resume, or durably recover. Cancelling an
asyncio task does not stop a subprocess, and killing a `docker run` client does not
stop its container — so shutdown signals process groups explicitly and then sweeps
containers by this orchestration's ownership label. A coordinator killed with
SIGKILL (or a machine crash) cannot do any of that; recovering from it is out of
scope.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import sys
import tempfile
import threading
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy.engine import make_url

from app import agent, comparison, repository, runner, verification
from app.config import Settings, settings
from app.database import SessionLocal
from app.model_client import ModelNotConfigured
from app.models import (
    ORCH_STATUS_COMPLETED,
    ORCH_STATUS_FAILED,
    ORCH_STATUS_INTERRUPTED,
    RUN_STATUS_READY,
)
from app.worker import (
    EXIT_DOCKER_UNAVAILABLE,
    EXIT_INTERRUPTED,
    EXIT_LEGACY_ATTEMPTS,
    EXIT_NOT_CLAIMED,
    EXIT_NOT_CONFIGURED,
    EXIT_NOT_READY,
    EXIT_OK,
    EXIT_ORCHESTRATION_FAILED,
    EXIT_RUN_NOT_FOUND,
    ModelFactory,
    SessionFactory,
    default_model_factory,
)

BACKEND_ROOT = str(Path(__file__).resolve().parents[1])
READ_CHUNK_BYTES = 65_536


@dataclass(frozen=True)
class ChildSpec:
    """What a child needs on its command line. Everything else it reads from the DB."""

    orchestration_id: str
    attempt_id: str
    attempt_index: int


ChildCommand = Callable[[ChildSpec], list[str]]


def default_child_command(spec: ChildSpec) -> list[str]:
    return [
        sys.executable, "-m", "app.worker", "run-attempt",
        "--attempt-id", spec.attempt_id,
        "--orchestration-id", spec.orchestration_id,
    ]


@contextmanager
def _session(session_factory: SessionFactory) -> Iterator[Any]:
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


def describe_exit(returncode: int | None) -> str:
    if returncode is None:
        return "exit status unknown"
    if returncode < 0:
        try:
            name = signal.Signals(-returncode).name
        except ValueError:  # pragma: no cover
            name = str(-returncode)
        return f"killed by {name}"
    return f"exit code {returncode}"


def _signal_group(pid: int, signum: int) -> None:
    """Signal a child's whole process group (it leads its own session)."""
    try:
        os.killpg(pid, signum)
    except (ProcessLookupError, PermissionError):
        pass


# --- Output relay ------------------------------------------------------------


class OutputRelay:
    """Forward a child's output line by line, bounded, and never stop draining.

    Reads fixed-size chunks rather than lines, so one enormous line cannot grow a
    buffer without bound: at most `max_line_chars` bytes of a line are held, and
    the rest is discarded up to the next newline. If forwarding itself fails (a
    closed terminal, say), forwarding stops but draining continues — a child
    blocked on a full pipe would otherwise hang its slot forever.
    """

    def __init__(self, prefix: str, out: Any, max_line_chars: int) -> None:
        self.prefix = prefix
        self.out = out
        self.limit = max(16, max_line_chars)
        self.buffer = bytearray()
        self.dropping = False
        self.forwarding = True
        self.bytes_read = 0
        self.lines_truncated = 0

    def _forward(self, data: bytes, truncated: bool = False) -> None:
        if not self.forwarding:
            return
        text = data.decode("utf-8", errors="replace")
        if truncated:
            text += " … [line truncated]"
        try:
            print(f"{self.prefix} {text}", file=self.out, flush=True)
        except Exception:  # noqa: BLE001 - forwarding is best-effort by design
            self.forwarding = False

    def feed(self, chunk: bytes) -> None:
        self.bytes_read += len(chunk)
        parts = chunk.split(b"\n")
        for position, part in enumerate(parts):
            ends_line = position < len(parts) - 1
            if self.dropping:
                if ends_line:
                    self.dropping = False
                continue
            room = self.limit - len(self.buffer)
            if len(part) > room:
                self.buffer.extend(part[:room])
                self._forward(bytes(self.buffer), truncated=True)
                self.lines_truncated += 1
                self.buffer.clear()
                self.dropping = not ends_line
                continue
            self.buffer.extend(part)
            if ends_line:
                self._forward(bytes(self.buffer))
                self.buffer.clear()

    def close(self) -> None:
        if self.buffer:
            self._forward(bytes(self.buffer))
            self.buffer.clear()

    async def drain(self, stream: asyncio.StreamReader) -> None:
        while True:
            chunk = await stream.read(READ_CHUNK_BYTES)
            if not chunk:
                break
            self.feed(chunk)
        self.close()


# --- The coordinator -----------------------------------------------------------


@dataclass
class _Plan:
    orchestration_id: str
    run_id: str
    effective_concurrency: int
    specs: list[ChildSpec]
    commit_sha: str
    image_id: str
    profile: str


@dataclass
class _State:
    stop_launching: bool = False
    stop_reason: str | None = None
    interrupted_by: str | None = None
    kill_now: bool = False
    cleanup_unconfirmed: bool = False
    active: dict[str, asyncio.subprocess.Process] = field(default_factory=dict)
    max_active: int = 0
    notes: list[str] = field(default_factory=list)


class Coordinator:
    def __init__(
        self,
        plan: _Plan,
        *,
        session_factory: SessionFactory,
        config: Settings,
        child_command: ChildCommand,
        out: Any,
    ) -> None:
        self.plan = plan
        self.session_factory = session_factory
        self.config = config
        self.child_command = child_command
        self.out = out
        self.state = _State()
        self.limits = verification.runner_limits_from(config)
        self._terminator: asyncio.Task[None] | None = None

    def emit(self, message: str) -> None:
        try:
            print(message, file=self.out, flush=True)
        except Exception:  # noqa: BLE001
            pass

    # -- signals ------------------------------------------------------------

    def request_stop(self, reason: str) -> None:
        """Stop launching, terminate active children. A second request kills now."""
        if self.state.interrupted_by is not None:
            self.state.kill_now = True
            for proc in list(self.state.active.values()):
                _signal_group(proc.pid, signal.SIGKILL)
            return
        self.state.interrupted_by = reason
        self.state.stop_launching = True
        self.state.stop_reason = f"The orchestration was interrupted ({reason}) before this attempt was launched."
        self.emit(f"Received {reason}: stopping. No new attempts will start.")
        self._terminator = asyncio.get_running_loop().create_task(self._terminate_active())

    async def _terminate_active(self) -> None:
        procs = list(self.state.active.values())
        for proc in procs:
            _signal_group(proc.pid, signal.SIGTERM)
        if procs and not self.state.kill_now:
            waiters = [asyncio.ensure_future(p.wait()) for p in procs]
            await asyncio.wait(waiters, timeout=self.config.orchestrator_child_grace_seconds)
        for proc in procs:
            if proc.returncode is None:
                self.emit(f"Child {proc.pid} ignored SIGTERM; killing it.")
            _signal_group(proc.pid, signal.SIGKILL)

    # -- one slot -----------------------------------------------------------

    async def _slot(self, semaphore: asyncio.Semaphore, spec: ChildSpec) -> None:
        async with semaphore:
            if self.state.stop_launching:
                return  # left queued; recorded as not launched at the end
            prefix = f"[attempt {spec.attempt_index}]"
            proc = await asyncio.create_subprocess_exec(
                *self.child_command(spec),
                cwd=BACKEND_ROOT,
                env=self.config.subprocess_environment(DATABASE_URL=self.config.database_url),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            self.state.active[spec.attempt_id] = proc
            self.state.max_active = max(self.state.max_active, len(self.state.active))
            self.emit(f"{prefix} started (pid {proc.pid})")
            if self.state.interrupted_by is not None:
                # A stop arrived while this child was being spawned.
                _signal_group(proc.pid, signal.SIGKILL if self.state.kill_now else signal.SIGTERM)

            relay = OutputRelay(prefix, self.out, self.config.orchestrator_max_relayed_line_chars)
            assert proc.stdout is not None
            reader = asyncio.ensure_future(relay.drain(proc.stdout))
            try:
                returncode = await proc.wait()
            finally:
                # Anything left in the child's group (an orphaned `docker run`
                # client, say) goes now; the container itself is swept below.
                _signal_group(proc.pid, signal.SIGKILL)
                try:
                    await asyncio.wait_for(reader, timeout=self.config.orchestrator_drain_timeout_seconds)
                except asyncio.TimeoutError:
                    reader.cancel()
                    self.state.notes.append(
                        f"Attempt {spec.attempt_index}: its output reader did not reach EOF "
                        f"and was cancelled."
                    )
                self.state.active.pop(spec.attempt_id, None)

            description = describe_exit(returncode)
            interrupted = self.state.interrupted_by is not None
            pipeline = await asyncio.to_thread(
                self._reconcile, spec.attempt_id, interrupted, description
            )
            cleanup = await asyncio.to_thread(
                runner.remove_labelled_containers,
                runner.LABEL_ATTEMPT,
                spec.attempt_id,
                limits=self.limits,
            )
            if not cleanup.confirmed:
                self.state.cleanup_unconfirmed = True
                self.state.stop_launching = True
                self.state.stop_reason = self.state.stop_reason or (
                    "Not launched: container cleanup for a sibling attempt could not be "
                    "confirmed, so the concurrency bound could no longer be guaranteed."
                )
                self.state.notes.append(
                    f"Attempt {spec.attempt_index}: container cleanup could not be "
                    f"confirmed ({'; '.join(cleanup.errors) or 'unknown'}). No further "
                    f"attempts were launched."
                )
            if cleanup.removed:
                self.state.notes.append(
                    f"Attempt {spec.attempt_index}: removed {len(cleanup.removed)} leftover "
                    f"container(s) after its worker exited."
                )
            self.emit(f"{prefix} finished ({description}); pipeline: {pipeline}")
        # The slot is released only here: after exit, drain, reconciliation, and
        # confirmed (or recorded-as-unconfirmed) cleanup.

    def _reconcile(self, attempt_id: str, interrupted: bool, description: str) -> str:
        with _session(self.session_factory) as db:
            return repository.reconcile_attempt_pipeline(
                db,
                attempt_id=attempt_id,
                interrupted=interrupted,
                exit_description=description,
                max_error_chars=self.config.agent_max_error_chars,
            )

    # -- the whole run --------------------------------------------------------

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        handled: list[int] = []
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(signum, self.request_stop, signal.Signals(signum).name)
                handled.append(signum)

        failure: BaseException | None = None
        try:
            await asyncio.to_thread(self._start)
            semaphore = asyncio.Semaphore(self.plan.effective_concurrency)
            tasks = [asyncio.ensure_future(self._slot(semaphore, spec)) for spec in self.plan.specs]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            errors = [r for r in results if isinstance(r, BaseException)]
            if errors:
                failure = errors[0]
                # The coordinator itself failed; siblings were not cancelled by it,
                # but anything still alive must not outlive us.
                self.state.stop_launching = True
                for proc in list(self.state.active.values()):
                    _signal_group(proc.pid, signal.SIGKILL)
            if self._terminator is not None:
                await self._terminator
        finally:
            for signum in handled:
                loop.remove_signal_handler(signum)

        return await asyncio.to_thread(self._finish, failure)

    def _start(self) -> None:
        with _session(self.session_factory) as db:
            repository.start_orchestration(db, self.plan.orchestration_id)

    def _finish(self, failure: BaseException | None) -> int:
        # A final sweep by orchestration label: belt and braces after the
        # per-attempt sweeps, and the only sweep for a child that never got one.
        sweep = runner.remove_labelled_containers(
            runner.LABEL_ORCHESTRATION, self.plan.orchestration_id, limits=self.limits
        )
        if not sweep.confirmed:
            self.state.cleanup_unconfirmed = True
            self.state.notes.append(
                "Final container cleanup for this orchestration could not be confirmed: "
                + ("; ".join(sweep.errors) or "unknown")
            )
        elif sweep.removed:
            self.state.notes.append(
                f"Final sweep removed {len(sweep.removed)} container(s) owned by this orchestration."
            )

        if self.state.interrupted_by is not None:
            status, code = ORCH_STATUS_INTERRUPTED, EXIT_INTERRUPTED
            error_kind, error_message = "interrupted", f"Stopped by {self.state.interrupted_by}."
        elif failure is not None:
            status, code = ORCH_STATUS_FAILED, EXIT_ORCHESTRATION_FAILED
            error_kind = "coordinator_error"
            error_message = f"{type(failure).__name__}: {failure}"[: self.config.agent_max_error_chars]
        elif self.state.cleanup_unconfirmed:
            status, code = ORCH_STATUS_FAILED, EXIT_ORCHESTRATION_FAILED
            error_kind = "cleanup_unconfirmed"
            error_message = (
                "Container cleanup could not be confirmed, so the orchestration stopped "
                "launching attempts. Check `docker ps --filter "
                f"label={runner.LABEL_ORCHESTRATION}={self.plan.orchestration_id}`."
            )
        else:
            status, code = ORCH_STATUS_COMPLETED, EXIT_OK
            error_kind = error_message = None

        with _session(self.session_factory) as db:
            reason = self.state.stop_reason or "Not launched."
            unlaunched = repository.mark_unlaunched_attempts(
                db,
                orchestration_id=self.plan.orchestration_id,
                reason=reason,
                max_error_chars=self.config.agent_max_error_chars,
            )
            if unlaunched:
                self.state.notes.append(f"{unlaunched} attempt(s) were never launched.")

            candidates = [
                _candidate(attempt, record)
                for attempt, record in repository.list_attempts_with_verifications(
                    db, self.plan.run_id
                )
            ]
            result = comparison.evaluate(
                candidates,
                commit_sha=self.plan.commit_sha,
                image_id=self.plan.image_id,
                profile=self.plan.profile,
                complete=status == ORCH_STATUS_COMPLETED,
            )
            self.state.notes.append(
                f"At most {self.state.max_active} attempt pipeline(s) ran at once "
                f"(limit {self.plan.effective_concurrency}, per orchestrator process)."
            )
            repository.finish_orchestration(
                db,
                orchestration_id=self.plan.orchestration_id,
                status=status,
                comparison=result,
                recommended_attempt_index=result["recommended_attempt_index"],
                notes=self.state.notes,
                error_kind=error_kind,
                error_message=error_message,
            )

        self.emit("")
        self.emit(f"Orchestration {status}.")
        for row in result["candidates"]:
            verdict = "eligible" if row["eligible"] else "not eligible"
            self.emit(f"  attempt {row['attempt_index']}: {verdict}")
            for reason_text in row["reasons"]:
                self.emit(f"      - {reason_text}")
        self.emit(result["headline"])
        if result["recommended_attempt_index"] is not None:
            self.emit(f"  {comparison.TIE_BREAKER}")
        for note in self.state.notes:
            self.emit(f"  note: {note}")
        self.emit(
            "Results cover only the tests that ran, on one commit, in one runner "
            "profile. They are not proof any patch is correct."
        )
        return code


def _candidate(attempt: Any, record: Any) -> comparison.Candidate:
    verification_data = None
    if record is not None:
        verification_data = {
            "status": record.status,
            "outcome": record.outcome,
            "commit_sha": record.commit_sha,
            "patch_sha256": record.patch_sha256,
            "profile": record.profile,
            "image_id": record.image_id,
            "patch_applied": record.patch_applied,
            "comparison": record.comparison,
            "baseline_summary": record.baseline_summary,
            "patched_summary": record.patched_summary,
        }
    return comparison.Candidate(
        attempt_index=attempt.attempt_index,
        attempt_id=attempt.id,
        attempt_status=attempt.status,
        diff=attempt.diff,
        commit_sha=attempt.commit_sha,
        pipeline_error_kind=attempt.pipeline_error_kind,
        pipeline_error_message=attempt.pipeline_error_message,
        verification=verification_data,
    )


# --- Entry point -----------------------------------------------------------------


def _absolute_sqlite_url(database_url: str) -> str:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return database_url
    if os.path.isabs(url.database):
        return database_url
    return url.set(database=os.path.abspath(url.database)).render_as_string(hide_password=False)


def orchestrate_run(
    run_id: str,
    *,
    session_factory: SessionFactory | None = None,
    model_factory: ModelFactory | None = None,
    config: Settings | None = None,
    image_id: str | None = None,
    child_command: ChildCommand | None = None,
    out: Any = sys.stdout,
) -> int:
    """Reserve, run, reconcile, and compare every attempt for one ready run."""
    session_factory = session_factory or SessionLocal
    model_factory = model_factory or default_model_factory
    config = config or settings
    child_command = child_command or default_child_command
    # Children run with cwd=backend/. A relative SQLite path (the default) is
    # resolved here, against where this process was started, so a child always
    # writes to the same file its coordinator reads.
    config = config.model_copy(update={"database_url": _absolute_sqlite_url(config.database_url)})

    def emit(message: str) -> None:
        print(message, file=out, flush=True)

    # --- 1. Preconditions, read-only -------------------------------------------
    with _session(session_factory) as db:
        run = repository.get_run(db, run_id)
        if run is None:
            emit(f"No run found with id {run_id!r}.")
            return EXIT_RUN_NOT_FOUND
        if repository.get_orchestration_for_run(db, run_id) is not None:
            emit(f"Run {run_id} has already been orchestrated. One orchestration per run.")
            return EXIT_NOT_CLAIMED
        existing = repository.list_attempts_for_run(db, run_id)
        if existing:
            emit(
                f"Run {run_id} already has {len(existing)} manual patch attempt(s) created "
                f"with `propose`. Orchestration would mix them with new attempts that were "
                f"reserved and run differently, so it is refused. The existing attempt(s) "
                f"are unchanged. Create a new run to orchestrate."
            )
            return EXIT_LEGACY_ATTEMPTS
        inspection = repository.get_inspection_for_run(db, run_id)
        if run.status != RUN_STATUS_READY or inspection is None or not inspection.report:
            emit(
                f"Run {run_id} is {run.status!r} without a usable inspection. Orchestration "
                f"does not inspect repositories; run:\n"
                f"  uv run python -m app.worker inspect --run-id {run_id}"
            )
            return EXIT_NOT_READY
        if not inspection.commit_sha:
            emit(f"Run {run_id}'s inspection has no commit SHA.")
            return EXIT_NOT_READY
        requested = run.max_parallel_attempts
        commit_sha = inspection.commit_sha

    # --- 2. Preflight, BEFORE reserving anything ----------------------------------
    try:
        model = model_factory(config)
    except ModelNotConfigured as error:
        emit(f"Model is not configured: {error}")
        emit("Nothing was reserved.")
        return EXIT_NOT_CONFIGURED
    limits = verification.runner_limits_from(config)
    try:
        resolved_image_id = image_id or runner.resolve_image_id(config.verify_image, limits=limits)
    except runner.DockerUnavailable as error:
        emit(f"Docker is unavailable: {error}")
        emit("Nothing was reserved.")
        return EXIT_DOCKER_UNAVAILABLE

    effective = min(config.orchestrator_max_concurrency, requested)
    workspace_root = os.path.realpath(tempfile.mkdtemp(prefix="branchforge-orch-"))
    frozen = config.frozen_execution_config()
    frozen["anthropic_model"] = model.model

    # --- 3. Claim: orchestration + every slot, one commit ------------------------
    try:
        with _session(session_factory) as db:
            claimed = repository.claim_orchestration(
                db,
                run_id=run_id,
                requested_attempts=requested,
                concurrency_limit=config.orchestrator_max_concurrency,
                effective_concurrency=effective,
                model=model.model,
                commit_sha=commit_sha,
                profile=config.verify_profile,
                image_ref=config.verify_image,
                image_id=resolved_image_id,
                workspace_root=workspace_root,
                execution_config=frozen,
                emphases=[agent.emphasis_for(i) for i in range(1, requested + 1)],
            )
            if claimed is None:
                raced_orchestration = repository.get_orchestration_for_run(db, run_id) is not None
            else:
                plan = _Plan(
                    orchestration_id=claimed.id,
                    run_id=run_id,
                    effective_concurrency=effective,
                    specs=[
                        ChildSpec(claimed.id, a.id, a.attempt_index)
                        for a in repository.list_attempts_for_run(db, run_id)
                    ],
                    commit_sha=commit_sha,
                    image_id=resolved_image_id,
                    profile=config.verify_profile,
                )
        if claimed is None:
            shutil.rmtree(workspace_root, ignore_errors=True)
            if raced_orchestration:
                emit(f"Run {run_id} was orchestrated by another process first. Nothing launched.")
                return EXIT_NOT_CLAIMED
            emit(f"Run {run_id} gained a manual attempt first. Nothing launched.")
            return EXIT_LEGACY_ATTEMPTS

        emit(f"Orchestration {plan.orchestration_id} for run {run_id}")
        emit(
            f"  {requested} attempt(s), at most {effective} at once (limit "
            f"{config.orchestrator_max_concurrency} per orchestrator process)"
        )
        emit(f"  model {model.model} | commit {commit_sha} | image {resolved_image_id[:19]}")

        coordinator = Coordinator(
            plan,
            session_factory=session_factory,
            config=config,
            child_command=child_command,
            out=out,
        )
        return asyncio.run(coordinator.run())
    finally:
        # Covers children killed before their own `finally` could run.
        shutil.rmtree(workspace_root, ignore_errors=True)
