"""The execution queue's dispatcher — a process separate from the API.

    uv run python -m app.worker dispatch

Drains `execution_jobs`: for each started run it runs the existing `inspect` and
`orchestrate` commands as managed child processes. It adds no execution logic of
its own — the inspector, the agents, and the verifier are reused unchanged.

**One dispatcher per database, on one host.** Exclusivity is an `flock` on a file
beside the SQLite database, held for the process lifetime. The kernel releases it
on death, including SIGKILL, so a crash can never wedge the queue — and because
the path is derived from the *resolved* database file rather than a setting, two
spellings of one database cannot produce two locks. `flock` does not carry those
semantics across networked filesystems, which is why this is a single-host,
localhost deployment.

Three things it deliberately does not do:

* **Trust exit codes.** Every stage is reconciled from persisted rows afterwards,
  exactly as the orchestrator reconciles its own children. A child can exit 0
  having done half the work.
* **Replay anything.** A job found `running` at startup is crash debris: the lock
  proves no dispatcher is alive, but proves nothing about the child processes and
  containers the dead one started. Such a job is flagged for manual recovery and
  the dispatcher refuses to launch further work rather than running alongside
  possibly-orphaned containers.
* **Assume a killed coordinator took its children with it.** Attempt children lead
  their own sessions, so they survive their coordinator. Their pids are recorded
  on the attempt rows, and after a forced kill they are stopped and reaped here
  before any container sweep — otherwise one could still call the model and start
  containers after we had reported the job cancelled.

Recovering automatically from an abrupt machine crash remains out of scope.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import signal
import socket
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app import comparison, repository, runner, verification
from app.config import Settings, settings
from app.database import SessionLocal
from app.model_client import ModelNotConfigured
from app.models import (
    JOB_STAGE_INSPECTING,
    JOB_STAGE_ORCHESTRATING,
    JOB_STATUS_CANCELLED,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_INTERRUPTED,
    ORCH_STATUS_COMPLETED,
    ORCH_STATUS_INTERRUPTED,
    RUN_STATUS_FAILED,
    RUN_STATUS_INSPECTING,
    RUN_STATUS_PENDING,
    RUN_STATUS_READY,
)
from app.orchestrator import (
    BACKEND_ROOT,
    OutputRelay,
    _absolute_sqlite_url,
    _candidate,
    _session,
    _signal_group,
    describe_exit,
)
from app.worker import (
    EXIT_DISPATCHER_LOCKED,
    EXIT_DOCKER_UNAVAILABLE,
    EXIT_INTERRUPTED,
    EXIT_NOT_CONFIGURED,
    EXIT_OK,
    EXIT_RECOVERY_REQUIRED,
    ModelFactory,
    SessionFactory,
    default_model_factory,
)

RunCommand = Callable[[str], list[str]]


def default_inspect_command(run_id: str) -> list[str]:
    return [sys.executable, "-m", "app.worker", "inspect", "--run-id", run_id]


def default_orchestrate_command(run_id: str) -> list[str]:
    return [sys.executable, "-m", "app.worker", "orchestrate", "--run-id", run_id]


def dispatcher_identity() -> str:
    """Who is running. Informational only — the lock is what enforces exclusivity."""
    return f"{socket.gethostname()}:{os.getpid()}"[:64]


# --- The single-instance lock --------------------------------------------------


class DispatcherLock:
    """An exclusive, non-blocking `flock` held for the dispatcher's lifetime.

    A lock file rather than a PID file on purpose: a PID file records an
    intention, and a crashed process leaves one behind that looks valid forever.
    An `flock` records a *fact* the kernel maintains, and it is dropped
    automatically when the holder dies however it dies.

    The file is never unlinked on release. Removing it would let another process
    that had already opened the old inode hold a lock on a file nobody else can
    reach any more, which is precisely the two-locks-one-database bug the derived
    path exists to prevent. An empty lock file left behind costs nothing.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._handle: Any = None

    def acquire(self) -> bool:
        handle = open(self.path, "a+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(f"{dispatcher_identity()}\n")
            handle.flush()
        except OSError:  # pragma: no cover - the lock is what matters, not the note
            pass
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle, fcntl.LOCK_UN)
        except OSError:  # pragma: no cover - released by close anyway
            pass
        self._handle.close()
        self._handle = None

    def __enter__(self) -> "DispatcherLock":
        return self

    def __exit__(self, *_: Any) -> None:
        self.release()


# --- Stopping a process we do not own the group of -----------------------------


def _process_command_line(pid: int, *, timeout: float = 5.0) -> str:
    """This pid's full command line, or "" if there is no such process.

    `-ww` is load-bearing, not decoration. Without it `ps` truncates the command
    line to the terminal width, and an attempt child's argv is long
    (`… -m app.worker run-attempt --attempt-id <uuid> --orchestration-id <uuid>`).
    A truncated line would drop the attempt id, the ownership check below would
    not match, and a genuine orphan would be left running while the job was
    reported stopped — exactly the case this reaping exists to prevent.
    """
    try:
        completed = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):  # pragma: no cover
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - someone else's process
        return True
    return True


@dataclass
class _State:
    stopping: bool = False
    stop_reason: str | None = None
    blocked: bool = False
    blocked_reason: str | None = None
    kill_now: bool = False
    active: asyncio.subprocess.Process | None = None


@dataclass
class _StageResult:
    """How a child ended: normally, or because we stopped it."""

    reason: str  # "exited" | "cancelled" | "stopped"
    returncode: int | None
    forced: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def description(self) -> str:
        return describe_exit(self.returncode)


class Dispatcher:
    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        config: Settings,
        model_factory: ModelFactory,
        image_resolver: Callable[[], str],
        inspect_command: RunCommand,
        orchestrate_command: RunCommand,
        out: Any,
    ) -> None:
        self.session_factory = session_factory
        self.config = config
        self.model_factory = model_factory
        self.image_resolver = image_resolver
        self.inspect_command = inspect_command
        self.orchestrate_command = orchestrate_command
        self.out = out
        self.state = _State()
        self.identity = dispatcher_identity()
        self.limits = verification.runner_limits_from(config)

    def emit(self, message: str) -> None:
        try:
            print(message, file=self.out, flush=True)
        except Exception:  # noqa: BLE001 - output is best effort
            pass

    # -- signals ---------------------------------------------------------------

    def request_stop(self, reason: str) -> None:
        if self.state.stopping:
            self.state.kill_now = True
            if self.state.active is not None:
                _signal_group(self.state.active.pid, signal.SIGKILL)
            return
        self.state.stopping = True
        self.state.stop_reason = reason
        self.emit(
            f"Received {reason}: no further jobs will be claimed. "
            f"Queued jobs are left for a later dispatcher."
        )

    # -- startup ---------------------------------------------------------------

    def startup_recovery_scan(self) -> int:
        """Flag jobs left `running` by a dead dispatcher. Returns how many block us.

        Reaching here means we hold the lock, so no other dispatcher is alive. A
        `running` job is therefore debris from one that died — but its orchestrator
        and attempt children may have been SIGKILLed along with it, leaving
        containers we cannot account for. Draining more work alongside them would
        break the concurrency bound and could confuse one job's containers for
        another's, so we annotate and stop rather than continue.
        """
        blocking = 0
        with _session(self.session_factory) as db:
            for job in repository.list_running_jobs(db):
                note = (
                    f"Found running (stage {job.stage or 'unknown'}) with no dispatcher "
                    f"holding the lock, so the previous dispatcher died. Not resumed and "
                    f"not declared stopped: its processes and containers may still be "
                    f"alive. Check `docker ps --filter "
                    f"label={runner.LABEL_ORCHESTRATION}` and see the README's manual "
                    f"recovery notes."
                )
                repository.mark_job_recovery_required(
                    db,
                    job_id=job.id,
                    note=note,
                    max_notes=self.config.dispatcher_max_job_notes,
                )
                self.emit(f"Job {job.id} (run {job.run_id}) needs manual recovery: {note}")
                blocking += 1
            if blocking == 0:
                blocking = repository.count_jobs_requiring_recovery(db)
        return blocking

    # -- children ---------------------------------------------------------------

    async def _spawn(self, argv: list[str], prefix: str) -> tuple[Any, Any, OutputRelay]:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=BACKEND_ROOT,
            env=self.config.subprocess_environment(DATABASE_URL=self.config.database_url),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        relay = OutputRelay(prefix, self.out, self.config.orchestrator_max_relayed_line_chars)
        assert proc.stdout is not None
        reader = asyncio.ensure_future(relay.drain(proc.stdout))
        self.state.active = proc
        return proc, reader, relay

    async def _supervise(self, proc: Any, job_id: str) -> _StageResult:
        """Wait for the child, watching for cancellation and shutdown.

        SIGTERM is sent **once**. The orchestrator treats a second signal as
        "kill everything now", which would throw away the very cleanup the long
        grace period exists to let it finish — so re-signalling on every poll tick
        would defeat the shutdown path rather than hurry it.
        """
        waiter = asyncio.ensure_future(proc.wait())
        reason = "exited"
        while True:
            done, _ = await asyncio.wait(
                {waiter}, timeout=self.config.dispatcher_cancel_poll_seconds
            )
            if waiter in done:
                return _StageResult(reason="exited", returncode=waiter.result())
            if self.state.stopping:
                reason = "stopped"
                break
            if await asyncio.to_thread(self._cancel_requested, job_id):
                reason = "cancelled"
                break

        notes: list[str] = []
        self.emit(
            f"Stopping the running child ({reason}); it has "
            f"{self.config.dispatcher_child_grace_seconds:g}s to finish its own cleanup."
        )
        _signal_group(proc.pid, signal.SIGTERM)
        forced = False
        try:
            returncode = await asyncio.wait_for(
                asyncio.shield(waiter), timeout=self.config.dispatcher_child_grace_seconds
            )
        except asyncio.TimeoutError:
            forced = True
            notes.append(
                "The child did not exit within its grace period and was killed, so it "
                "could not finish its own container cleanup."
            )
            self.emit("The child ignored SIGTERM; killing it.")
            _signal_group(proc.pid, signal.SIGKILL)
            returncode = await waiter
        return _StageResult(reason=reason, returncode=returncode, forced=forced, notes=notes)

    async def _finish_child(self, proc: Any, reader: Any) -> None:
        _signal_group(proc.pid, signal.SIGKILL)
        try:
            await asyncio.wait_for(reader, timeout=self.config.dispatcher_drain_timeout_seconds)
        except asyncio.TimeoutError:  # pragma: no cover - reader wedged
            reader.cancel()
        self.state.active = None

    def _cancel_requested(self, job_id: str) -> bool:
        with _session(self.session_factory) as db:
            return repository.job_cancel_requested(db, job_id=job_id)

    # -- descendants a killed coordinator left behind ---------------------------

    def reap_orphaned_workers(self, run_id: str) -> tuple[bool, list[str]]:
        """Stop and reap attempt children that outlived their coordinator.

        Only reached after a coordinator had to be force-killed. Each attempt child
        leads its own session, so nothing we signalled reached it, and it would
        otherwise carry on calling the model and starting containers after we had
        reported the job stopped.

        A recorded pid is a lead, never proof — pids are reused — so the process is
        only signalled when its command line still names that attempt. Returns
        (all_stopped, notes).
        """
        notes: list[str] = []
        with _session(self.session_factory) as db:
            orchestration = repository.get_orchestration_for_run(db, run_id)
            if orchestration is None:
                return True, notes
            recorded = repository.list_attempt_worker_pids(db, orchestration_id=orchestration.id)

        targets: list[tuple[str, int, int]] = []
        for attempt_id, index, pid in recorded:
            command_line = _process_command_line(pid)
            if not command_line:
                continue
            if attempt_id not in command_line:
                notes.append(
                    f"Attempt {index}: pid {pid} is now a different process; not signalled."
                )
                continue
            targets.append((attempt_id, index, pid))

        if not targets:
            return True, notes

        for _attempt_id, index, pid in targets:
            self.emit(f"Stopping orphaned attempt {index} worker (pid {pid}).")
            _signal_group(pid, signal.SIGTERM)

        deadline = self.config.dispatcher_worker_grace_seconds
        waited = 0.0
        step = 0.1
        while waited < deadline and any(_alive(pid) for _, _, pid in targets):
            threading.Event().wait(step)
            waited += step

        for _attempt_id, index, pid in targets:
            if _alive(pid):
                notes.append(f"Attempt {index}: worker pid {pid} ignored SIGTERM and was killed.")
                _signal_group(pid, signal.SIGKILL)

        waited = 0.0
        while waited < deadline and any(_alive(pid) for _, _, pid in targets):
            threading.Event().wait(step)
            waited += step

        survivors = [pid for _, _, pid in targets if _alive(pid)]
        if survivors:
            notes.append(
                f"Could not confirm {len(survivors)} attempt worker(s) stopped "
                f"(pid(s) {', '.join(str(p) for p in survivors)})."
            )
            return False, notes

        with _session(self.session_factory) as db:
            for attempt_id, _index, _pid in targets:
                repository.record_attempt_worker_pid(db, attempt_id=attempt_id, worker_pid=None)
        notes.append(f"Stopped {len(targets)} orphaned attempt worker(s).")
        return True, notes

    def sweep_orchestration_containers(self, run_id: str) -> tuple[bool, list[str]]:
        """Remove containers owned by this run's orchestration, and confirm it."""
        with _session(self.session_factory) as db:
            orchestration = repository.get_orchestration_for_run(db, run_id)
            orchestration_id = orchestration.id if orchestration is not None else None
        if orchestration_id is None:
            return True, []
        cleanup = runner.remove_labelled_containers(
            runner.LABEL_ORCHESTRATION, orchestration_id, limits=self.limits
        )
        notes: list[str] = []
        if cleanup.removed:
            notes.append(f"Removed {len(cleanup.removed)} leftover container(s).")
        if not cleanup.confirmed:
            notes.append(
                "Container cleanup could not be confirmed: "
                + ("; ".join(cleanup.errors) or "unknown")
                + f". Check `docker ps --filter "
                f"label={runner.LABEL_ORCHESTRATION}={orchestration_id}`."
            )
        return cleanup.confirmed, notes

    def reconcile_orchestration(self, run_id: str, exit_description: str) -> list[str]:
        """Write the ending a force-killed coordinator never got to write."""
        notes: list[str] = []
        with _session(self.session_factory) as db:
            orchestration = repository.get_orchestration_for_run(db, run_id)
            if orchestration is None:
                return notes
            for attempt in repository.list_attempts_for_run(db, run_id):
                repository.reconcile_attempt_pipeline(
                    db,
                    attempt_id=attempt.id,
                    interrupted=True,
                    exit_description=exit_description,
                    max_error_chars=self.config.agent_max_error_chars,
                )
            repository.mark_unlaunched_attempts(
                db,
                orchestration_id=orchestration.id,
                reason="Not launched: the orchestration was stopped.",
                max_error_chars=self.config.agent_max_error_chars,
            )
            candidates = [
                _candidate(attempt, record)
                for attempt, record in repository.list_attempts_with_verifications(db, run_id)
            ]
            result = comparison.evaluate(
                candidates,
                commit_sha=orchestration.commit_sha,
                image_id=orchestration.image_id,
                profile=orchestration.profile,
                complete=False,
            )
            existing = list(orchestration.notes or [])
            existing.append(
                "The orchestrator process was stopped by the dispatcher; this ending "
                "was reconciled from the database afterwards."
            )
            # Guarded: a coordinator that saved its own comparison microseconds
            # before it was killed already recorded the truth, and replacing that
            # with our reconstruction would swap evidence for a guess.
            if repository.reconcile_orchestration(
                db,
                orchestration_id=orchestration.id,
                status=ORCH_STATUS_INTERRUPTED,
                comparison=result,
                recommended_attempt_index=result["recommended_attempt_index"],
                notes=existing,
                error_kind="interrupted",
                error_message="Stopped by the dispatcher.",
            ):
                notes.append("Reconciled the orchestration as interrupted.")
        return notes

    # -- stages -----------------------------------------------------------------

    def _stage_refusal(self, run_id: str, stage: str) -> str | None:
        """Why this stage must not launch now — re-checked immediately before it does.

        The gap between enqueueing and dispatching is wide, and a manual
        `propose`/`orchestrate` can land inside it. Checking once at Start would
        mean launching a second orchestration over someone else's attempts.
        """
        with _session(self.session_factory) as db:
            run = repository.get_run(db, run_id)
            if run is None:
                return f"Run {run_id} no longer exists."
            if stage == JOB_STAGE_INSPECTING:
                if run.status != RUN_STATUS_PENDING:
                    return f"Run {run_id} is {run.status!r}, not 'pending'; it was inspected elsewhere."
                return None

            if repository.get_orchestration_for_run(db, run_id) is not None:
                return f"Run {run_id} was orchestrated by something else before this job ran."
            if repository.list_attempts_for_run(db, run_id):
                return f"Run {run_id} gained a manual patch attempt before this job ran."
            if run.status != RUN_STATUS_READY:
                return f"Run {run_id} is {run.status!r}, not 'ready'."
            inspection = repository.get_inspection_for_run(db, run_id)
            if inspection is None or not inspection.report or not inspection.commit_sha:
                return f"Run {run_id} has no usable inspection report."
            return None

    async def _run_stage(self, job_id: str, run_id: str, stage: str) -> _StageResult:
        with _session(self.session_factory) as db:
            repository.set_job_stage(db, job_id=job_id, stage=stage)
        command = (
            self.inspect_command if stage == JOB_STAGE_INSPECTING else self.orchestrate_command
        )
        prefix = f"[{stage}]"
        proc, reader, _relay = await self._spawn(command(run_id), prefix)
        self.emit(f"{prefix} run {run_id} (pid {proc.pid})")
        try:
            result = await self._supervise(proc, job_id)
        finally:
            await self._finish_child(proc, reader)
        self.emit(f"{prefix} finished ({result.description})")
        return result

    # -- one job ------------------------------------------------------------------

    async def run_job(self, job_id: str, run_id: str) -> None:
        notes: list[str] = []

        # --- inspection, unless the run is already inspected ---------------------
        with _session(self.session_factory) as db:
            run = repository.get_run(db, run_id)
            needs_inspection = run is not None and run.status == RUN_STATUS_PENDING

        if needs_inspection:
            refusal = self._stage_refusal(run_id, JOB_STAGE_INSPECTING)
            if refusal is not None:
                self._finish(job_id, JOB_STATUS_FAILED, repository.JOB_ERROR_RUN_STATE_CHANGED, refusal, notes)
                return
            result = await self._run_stage(job_id, run_id, JOB_STAGE_INSPECTING)
            notes.extend(result.notes)

            if result.reason != "exited":
                # The inspector has no SIGTERM handler, so stopping it leaves the
                # run mid-flight. Say so honestly rather than leaving a run that
                # looks like it is still being inspected by nothing.
                self._reconcile_inspection(run_id, result.reason)
                notes.append("Inspection was stopped before it finished.")
                self._finish_cancelled_or_interrupted(job_id, result.reason, notes)
                return

            with _session(self.session_factory) as db:
                run = repository.get_run(db, run_id)
                status = run.status if run is not None else None
                inspection = repository.get_inspection_for_run(db, run_id)
                reason = inspection.error_message if inspection is not None else None

            if status == RUN_STATUS_INSPECTING:
                # Exited without finishing: reconcile from the database rather
                # than believing the exit code.
                self._reconcile_inspection(run_id, "exited")
                status = RUN_STATUS_FAILED
                reason = f"The inspector exited before finishing ({result.description})."
            if status != RUN_STATUS_READY:
                # No model is called and no container is started for a run whose
                # inspection did not produce a usable report.
                self._finish(
                    job_id,
                    JOB_STATUS_FAILED,
                    repository.JOB_ERROR_INSPECTION_FAILED,
                    reason or f"Inspection did not make run {run_id} ready.",
                    notes,
                )
                return

        if self.state.stopping:
            self._finish_cancelled_or_interrupted(job_id, "stopped", notes)
            return
        if await asyncio.to_thread(self._cancel_requested, job_id):
            self._finish_cancelled_or_interrupted(job_id, "cancelled", notes)
            return

        # --- orchestration -------------------------------------------------------
        refusal = self._stage_refusal(run_id, JOB_STAGE_ORCHESTRATING)
        if refusal is not None:
            self._finish(job_id, JOB_STATUS_FAILED, repository.JOB_ERROR_RUN_STATE_CHANGED, refusal, notes)
            return

        result = await self._run_stage(job_id, run_id, JOB_STAGE_ORCHESTRATING)
        notes.extend(result.notes)

        if result.forced:
            # The coordinator was killed, so its children and containers are ours
            # to account for. Order matters: stop the processes that could still
            # start containers, then reconcile, then sweep and confirm.
            stopped, worker_notes = await asyncio.to_thread(self.reap_orphaned_workers, run_id)
            notes.extend(worker_notes)
            notes.extend(
                await asyncio.to_thread(self.reconcile_orchestration, run_id, result.description)
            )
            confirmed, sweep_notes = await asyncio.to_thread(
                self.sweep_orchestration_containers, run_id
            )
            notes.extend(sweep_notes)
            if not stopped or not confirmed:
                self._block(
                    "Cleanup after stopping an orchestration could not be confirmed."
                )
                self._finish(
                    job_id,
                    JOB_STATUS_FAILED,
                    repository.JOB_ERROR_CLEANUP_UNCONFIRMED,
                    "Owned processes or containers could not be confirmed stopped, so "
                    "no further job was launched.",
                    notes,
                )
                return
        elif result.reason != "exited":
            # It shut down on its own: it saved a comparison and swept its own
            # containers. Confirm the sweep anyway rather than taking that on trust.
            confirmed, sweep_notes = await asyncio.to_thread(
                self.sweep_orchestration_containers, run_id
            )
            notes.extend(sweep_notes)
            if not confirmed:
                self._block("Container cleanup after a cancellation could not be confirmed.")
                self._finish(
                    job_id,
                    JOB_STATUS_FAILED,
                    repository.JOB_ERROR_CLEANUP_UNCONFIRMED,
                    "Containers could not be confirmed removed, so no further job was launched.",
                    notes,
                )
                return

        if result.reason != "exited":
            self._finish_cancelled_or_interrupted(job_id, result.reason, notes)
            return

        # --- the job is done; what it found lives on the orchestration ----------
        with _session(self.session_factory) as db:
            orchestration = repository.get_orchestration_for_run(db, run_id)
            orchestration_status = orchestration.status if orchestration is not None else None
            error_kind = orchestration.error_kind if orchestration is not None else None
            error_message = orchestration.error_message if orchestration is not None else None

        if orchestration is None:
            self._finish(
                job_id,
                JOB_STATUS_FAILED,
                repository.JOB_ERROR_ORCHESTRATION_FAILED,
                f"The orchestrator exited ({result.description}) without creating an "
                f"orchestration for run {run_id}.",
                notes,
            )
            return
        if orchestration_status == ORCH_STATUS_COMPLETED:
            self._finish(job_id, JOB_STATUS_COMPLETED, None, None, notes)
            return
        if orchestration_status == ORCH_STATUS_INTERRUPTED:
            self._finish_cancelled_or_interrupted(job_id, "stopped", notes)
            return
        self._finish(
            job_id,
            JOB_STATUS_FAILED,
            error_kind or repository.JOB_ERROR_ORCHESTRATION_FAILED,
            error_message or f"The orchestration ended as {orchestration_status!r}.",
            notes,
        )

    def _reconcile_inspection(self, run_id: str, reason: str) -> None:
        with _session(self.session_factory) as db:
            repository.reconcile_inspection(
                db,
                run_id=run_id,
                error_kind="cancelled" if reason == "cancelled" else "interrupted",
                error_message=(
                    "The inspection was stopped before it finished, so this run has no "
                    "report. Inspection is not resumable: start a new run to inspect "
                    "this repository again."
                ),
                max_error_chars=self.config.agent_max_error_chars,
            )

    def _block(self, reason: str) -> None:
        self.state.blocked = True
        self.state.blocked_reason = reason
        self.state.stopping = True
        self.emit(f"{reason} No further jobs will be launched by this dispatcher.")

    def _finish_cancelled_or_interrupted(
        self, job_id: str, reason: str, notes: list[str]
    ) -> None:
        if reason == "cancelled":
            self._finish(job_id, JOB_STATUS_CANCELLED, None, None, notes)
        else:
            self._finish(
                job_id,
                JOB_STATUS_INTERRUPTED,
                repository.JOB_ERROR_DISPATCHER_STOPPED,
                "The dispatcher shut down while this job was running. Work already "
                "recorded is unchanged; nothing was resumed.",
                notes,
            )

    def _finish(
        self,
        job_id: str,
        status: str,
        error_kind: str | None,
        error_message: str | None,
        notes: list[str],
    ) -> None:
        with _session(self.session_factory) as db:
            repository.finish_job(
                db,
                job_id=job_id,
                status=status,
                error_kind=error_kind,
                error_message=error_message,
                max_error_chars=self.config.agent_max_error_chars,
                note="; ".join(notes) if notes else None,
                max_notes=self.config.dispatcher_max_job_notes,
            )
        self.emit(f"Job {job_id} -> {status}")
        for note in notes:
            self.emit(f"  note: {note}")

    # -- the loop ------------------------------------------------------------------

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        handled: list[int] = []
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(signum, self.request_stop, signal.Signals(signum).name)
                handled.append(signum)

        try:
            while not self.state.stopping:
                with _session(self.session_factory) as db:
                    job = repository.next_queued_job(db)
                    job_id = job.id if job is not None else None
                    run_id = job.run_id if job is not None else None
                    cancel_requested = bool(job.cancel_requested) if job is not None else False

                if job_id is None:
                    await asyncio.sleep(self.config.dispatcher_poll_seconds)
                    continue

                if cancel_requested:
                    with _session(self.session_factory) as db:
                        repository.cancel_unlaunched_job(db, job_id=job_id)
                    self.emit(f"Job {job_id} was cancelled before it started.")
                    continue

                # Docker is checked BEFORE the claim. A job may be started only
                # once, so failing claimed jobs because the daemon happened to be
                # asleep would burn every queued run's single chance on a
                # transient condition. Nothing is claimed; the queue waits.
                try:
                    await asyncio.to_thread(self.image_resolver)
                except runner.DockerUnavailable as error:
                    self.emit(f"Docker is unavailable: {error}")
                    self.emit("Nothing was claimed. Start Docker and run the dispatcher again.")
                    return EXIT_DOCKER_UNAVAILABLE

                with _session(self.session_factory) as db:
                    claimed = repository.claim_queued_job(
                        db, job_id=job_id, dispatcher_id=self.identity
                    )
                if not claimed:
                    continue

                self.emit(f"Claimed job {job_id} for run {run_id}")
                await self.run_job(job_id, str(run_id))
                if self.state.blocked:
                    break
        finally:
            for signum in handled:
                loop.remove_signal_handler(signum)

        if self.state.blocked:
            return EXIT_RECOVERY_REQUIRED
        if self.state.stop_reason is not None:
            return EXIT_INTERRUPTED
        return EXIT_OK


def run_dispatcher(
    *,
    session_factory: SessionFactory | None = None,
    config: Settings | None = None,
    model_factory: ModelFactory | None = None,
    image_resolver: Callable[[], str] | None = None,
    inspect_command: RunCommand | None = None,
    orchestrate_command: RunCommand | None = None,
    out: Any = sys.stdout,
) -> int:
    """Hold the lock, drain the queue, and stop cleanly on a signal."""
    session_factory = session_factory or SessionLocal
    config = config or settings
    model_factory = model_factory or default_model_factory
    inspect_command = inspect_command or default_inspect_command
    orchestrate_command = orchestrate_command or default_orchestrate_command
    # Children run with cwd=backend/, so a relative SQLite path is resolved here,
    # against where this process was started — the same rule the orchestrator uses.
    config = config.model_copy(update={"database_url": _absolute_sqlite_url(config.database_url)})

    def emit(message: str) -> None:
        print(message, file=out, flush=True)

    limits = verification.runner_limits_from(config)
    if image_resolver is None:
        def image_resolver() -> str:
            return runner.resolve_image_id(config.verify_image, limits=limits)

    try:
        lock_path = config.dispatcher_lock_path()
    except ValueError as error:
        emit(str(error))
        return EXIT_NOT_CONFIGURED

    lock = DispatcherLock(lock_path)
    if not lock.acquire():
        emit(
            f"Another dispatcher already holds {lock_path}. One dispatcher per database:\n"
            f"  stop the running one, or use a different DATABASE_URL.\n"
            f"Nothing was claimed."
        )
        return EXIT_DISPATCHER_LOCKED

    try:
        # Preflight before touching the queue, so a missing key or a stopped
        # daemon costs nothing: no job is claimed and none is failed.
        try:
            model = model_factory(config)
        except ModelNotConfigured as error:
            emit(f"Model is not configured: {error}")
            emit("Nothing was claimed.")
            return EXIT_NOT_CONFIGURED
        try:
            image_id = image_resolver()
        except runner.DockerUnavailable as error:
            emit(f"Docker is unavailable: {error}")
            emit("Nothing was claimed.")
            return EXIT_DOCKER_UNAVAILABLE

        dispatcher = Dispatcher(
            session_factory=session_factory,
            config=config,
            model_factory=model_factory,
            image_resolver=image_resolver,
            inspect_command=inspect_command,
            orchestrate_command=orchestrate_command,
            out=out,
        )
        emit(f"Dispatcher {dispatcher.identity} holding {lock_path}")
        emit(f"  model {model.model} | image {image_id[:19]}")

        blocked = dispatcher.startup_recovery_scan()
        if blocked:
            emit(
                f"{blocked} job(s) need manual recovery. Refusing to start work: their "
                f"processes and containers may still be running, and nothing here can "
                f"tell. Resolve them (see the README) before dispatching again."
            )
            return EXIT_RECOVERY_REQUIRED

        emit("Waiting for started runs. Press Ctrl+C to stop.")
        return asyncio.run(dispatcher.run())
    finally:
        lock.release()
