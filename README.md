# BranchForge

BranchForge investigates GitHub issues by running competing agent-generated fixes in isolated
environments and presenting verified patches.

**This repository currently contains milestones 1-6: run intake, read-only repository inspection,
bounded patch-proposal agents, containerised verification of each proposed patch, bounded competing
attempts with an evidence-based comparison, and a persistent execution queue started from the
dashboard.** A run is stored as `pending`. Pressing **Start** (or `POST /api/runs/{id}/start`)
enqueues one job and returns immediately — the API never does the work. A separate **dispatcher**
process claims queued jobs one at a time and runs the existing steps as child processes: inspection
through the GitHub API (skipped when the run is already `ready`), then `orchestrate`, which runs the
run's `max_parallel_attempts` (1-3) competing attempts as separate processes, a bounded number at a
time — each proposing a patch with its own agent and verifying it by applying it to a throwaway copy
of the exact inspected commit and running the repository's own tests before and after, inside a
container with no network access — and saves a comparison. The dashboard polls while a job is active.
The `inspect`, `propose`, `verify`, and `orchestrate` commands still work and bypass the queue.

**A queued job is not work in progress.** Nothing happens until a dispatcher is running, and the
dashboard says so rather than implying activity.

**A verification result is not a proof of correctness.** It reports how the tests that actually ran
behaved, on one commit, in one fixed runner profile. Repository dependencies are never installed and
no repository setup script is ever run, so a project needing more than the profile provides is
reported as an *environment limitation* rather than as a failing patch. **A recommendation is not a
judgement that a patch is right:** an attempt is recommended only when its own verification
demonstrated previously failing original tests now passing with nothing regressed, missing, or
truncated; otherwise the result is "No demonstrated fix" and nothing is recommended. Retries,
restarting a cancelled or failed run, cron-style scheduling, durable crash recovery, and a global
capacity limit are not implemented, and no progress or results are simulated — every state shown is
one the database actually holds.

---

## Contents

- [What works today](#what-works-today)
- [The workflow](#the-workflow)
- [Requirements](#requirements)
- [Setup](#setup)
- [Running locally](#running-locally)
- [Verification](#verification)
- [API](#api)
- [GitHub rate limits](#github-rate-limits)
- [Model configuration and budgets](#model-configuration-and-budgets)
- [Verification: the runner and what a result means](#verification-the-runner-and-what-a-result-means)
- [Configuration](#configuration)
- [Deployment](#deployment)
- [Project layout](#project-layout)
- [Design notes](#design-notes)
- [Limitations](#limitations)

---

## What works today

- Create a run from the dashboard or the API; the backend validates it and persists it to SQLite.
- Repository URLs are checked to be HTTPS `github.com/<owner>/<repo>` addresses and normalized.
- Runs are listed newest-first with a bounded limit, and can be fetched individually by UUID.
- Data survives backend restarts; the schema is created and owned by Alembic migrations.
- A separate worker process claims a pending run atomically and inspects its repository through the
  GitHub API — resolving the default branch to an immutable commit SHA, then reading the file tree,
  README, and Python configuration from that one commit.
- The report records the repository description, inspected commit, a bounded file listing, source
  previews, likely test locations, and a Python/pytest assessment with the filenames supporting it.
- Failures (missing repository, rate limiting, timeouts, oversized or malformed responses) are
  stored as readable errors against the run.
- The dashboard displays the persisted report with collapsible previews, truncation notices, and a
  manual Refresh button.
- A second worker command runs one bounded agent against an inspected run: it reads the issue and the
  inspection, can request further files from the same immutable commit with a `read_file` tool, and
  finishes by submitting a unified diff, an explanation, and a suggested test command.
- Submissions are validated locally — diff syntax, hunk line counts, path safety — and a rejected
  submission is returned to the model as a tool error so it can correct itself within its budgets.
- Attempt activity is stored as ordered events and shown in the dashboard beside the diff, under a
  prominent "unverified" label.
- A third worker command verifies that patch for real: it downloads the source archive of the exact
  inspected commit, runs the repository's own pytest suite to get a baseline, applies the patch to a
  separate copy, and runs the same original tests again — every test executing inside a disposable
  container with no network, a non-root user, a read-only source mount, and CPU/memory/PID/wall-clock
  ceilings.
- The comparison is made on explicit per-test outcomes, so a previously failing test counts as fixed
  only when that same test **passes** afterwards; skipped, xfailed, deselected, and uncollected are
  recorded as *not fixed*.
- The original tests, `conftest.py` files, fixture data, and collection configuration are restored
  over the patched source before the comparison, and any test file the patch *added* is removed — so
  a patch cannot alter the yardstick it is measured against. Tests the patch adds run separately as
  labelled supplemental evidence.
- The dashboard shows the baseline and patched results side by side, the patch-application status,
  which tests changed state, collapsible container logs, and the exact runner command — with the
  outcome stated in words rather than as a "verified" badge.
- `orchestrate` runs competing attempts: it reserves every attempt slot for a run in one atomic
  commit, then runs one child process per attempt — each a full propose → verify pipeline with its own
  conversation, budgets, events, patch, workspaces, and containers — under an asyncio semaphore. All
  attempts share the inspected commit, the report, the issue text, the model, and frozen budgets; they
  differ only in a small, recorded *investigation emphasis*, which does not guarantee different fixes.
- A failed attempt does not stop its siblings. If a child exits unexpectedly — even with exit code 0
  — the orchestrator reads what was actually persisted and records an accurate failure for whatever
  was left unfinished, including a saved patch whose verification never started.
- Ctrl+C or SIGTERM stops launching work, terminates active children by process group, removes
  exactly the containers carrying this orchestration's ownership label, and records interrupted
  outcomes.
- The saved comparison recommends at most one attempt, and only one whose evidence demonstrates a fix
  for its exact patch on the inspected commit. Ties are broken by fewer changed lines, then attempt
  number — a stated preference, not proof of better code.
- **Start** in the dashboard enqueues the whole workflow and returns immediately; a separate
  dispatcher process claims queued jobs one at a time and runs inspection and orchestration as child
  processes. No per-run terminal commands are needed.
- Repeated or concurrent Start requests return the **same** job — one per run, enforced by a unique
  constraint rather than by the button's disabled state.
- The detail pane updates itself while a job is active, polling a small endpoint that carries statuses
  only; the full payload is re-fetched when something actually changes, and polling stops at a
  terminal state. Manual Refresh still works.
- **Cancel** stops a queued job outright, and an active one only once its processes have stopped and
  its containers are confirmed removed. Work already recorded is preserved and still shown.
- Only one dispatcher can run per database, enforced by an OS file lock — a second exits immediately
  having claimed nothing.
- After a dispatcher is killed outright, its in-flight job is flagged as needing manual recovery and
  the queue stops rather than running new work beside containers nobody can account for.

## The workflow

With a dispatcher running, the whole workflow is two actions: create a run, press **Start**.

```bash
# 1. Start the dispatcher once, from backend/, and leave it running.
#    (needs ANTHROPIC_API_KEY, Docker, and the runner image)
cd backend
uv run python -m app.worker dispatch

# 2. Create a run in the dashboard at http://localhost:5173 and press Start.
#    The detail pane then updates itself: inspecting -> proposing and verifying
#    -> the comparison. No terminal commands per run.
```

The same thing over HTTP, if you prefer:

```bash
# Create
curl -X POST http://localhost:8000/api/runs \
  -H 'Content-Type: application/json' \
  -d '{"repository_url":"https://github.com/pallets/itsdangerous",
       "issue_description":"Check whether the signer handles empty payloads.",
       "max_parallel_attempts":1}'
# -> {"id":"64fbb850-...","status":"pending", ...}

# Start (202, immediately — this enqueues, it does not execute)
curl -X POST http://localhost:8000/api/runs/64fbb850-.../start

# Watch cheaply, or just watch the dashboard
curl http://localhost:8000/api/runs/64fbb850-.../progress

# Change your mind
curl -X POST http://localhost:8000/api/runs/64fbb850-.../cancel
```

Every step can still be driven by hand, bypassing the queue entirely:

```bash
cd backend
uv run python -m app.worker inspect     --run-id <UUID>
uv run python -m app.worker orchestrate --run-id <UUID>   # competing attempts
# ...or a single manual attempt instead
uv run python -m app.worker propose --run-id <UUID>
uv run python -m app.worker verify  --run-id <UUID>
```

A run can be started **once**. There is no restart: cancelling keeps everything already produced,
but the run is then finished. Create a new run to try again.

`dispatch` exit codes: `0` clean shutdown (Ctrl+C/SIGTERM — it stops claiming, winds down its
current job, and leaves queued jobs for a later dispatcher), `5` the model is not configured, `6`
Docker or the runner image is unavailable, `9` another dispatcher already holds the lock, `10` a job
needs manual recovery (see [Limitations](#limitations)), `130` interrupted. For `5`, `6`, `9`, and
`10` **nothing is claimed**.

Cancellation is honest about timing. A *queued* job is cancelled immediately — it has no processes
and no containers, so cancelling it needs neither a dispatcher nor Docker. A *running* job records
the request and stays non-terminal until its children have actually stopped and its containers are
confirmed removed; reporting "cancelled" while a container is still running would be a claim we
cannot support. If the completion commits first, that result stands and the cancel returns it
unchanged.

Each step happens at most once per run. `orchestrate` and `propose` are mutually exclusive for a run:
orchestration is refused (exit `7`) for a run that already has manual attempts, and `propose` is
refused for a run that has been orchestrated. `verify --run-id` works when the run has exactly one
attempt; with several it exits `8` and prints the `verify --attempt-id <UUID>` command for each.

`verify` exits `0` whenever the verification *ran* — including when its finding is "the patch does not
fix anything". A non-zero exit means the verification itself could not be carried out. `orchestrate`
follows the same rule: `0` means it ran to the end, including when the answer is "No demonstrated
fix".

`orchestrate` exit codes: `0` ran to completion, `1` the orchestration itself failed (recorded — a
coordinator error, or container cleanup that could not be confirmed), `2` no such run, `3` already
orchestrated or claimed by another orchestrator, `4` the run is not `ready`, `5` the model is not
configured, `6` Docker or the runner image is unavailable, `7` the run already has manual attempts,
`130` interrupted by Ctrl+C or SIGTERM (recorded). For `5`, `6`, and `7` **nothing is reserved** —
configuration and Docker are checked before any row is written.

The number of attempts is the run's `max_parallel_attempts`. How many run *at once* is
`min(ORCHESTRATOR_MAX_CONCURRENCY, max_parallel_attempts)` — a limit on **one orchestrator process**,
not a global capacity limit: two orchestrators for two runs each get their own. A concurrency slot
covers an attempt's whole propose → verify pipeline and is released only after the child has exited,
its output has been drained, the database has been reconciled, and removal of its containers has been
confirmed; if that confirmation fails, no further queued attempts are launched.

Building the runner image once, before the first `verify`:

```bash
docker build --provenance=false -t branchforge-runner-python:1 backend/runner/python-pytest
```

(`--provenance=false` keeps it a plain single-platform image; buildx otherwise adds attestation
manifests that Docker Desktop's containerd store cannot always `inspect`.)

Real output from a `verify` run against a real repository, with a patch that breaks an import:

```
Claimed verification for run 603fe439-0920-4c50-9f00-9895fb33042c
Profile: python-pytest | image branchforge-runner-python:1 (sha256:9929aa2db795)
Commit: c8e394065cd541a16c040515dc0afb85cf22a7c3
Repository code runs only inside a disposable container with no network.
  · Fetching source for commit c8e394065cd5
  · Snapshot: 16 files at c8e394065cd5
  · Running the original test suite (baseline)
  · Baseline: ok (0 failing of 200 collected)
  · Patch applied to 1 file(s)
  · Running the original tests against the patched source
  · Patched: collection_error (0 failing of 0 collected)

Outcome: patched_collection_error
  The baseline collected successfully but the patched source did not. The patch breaks import or
  collection.
  baseline: ok — 0 failing of 200 collected
  patched : collection_error — 0 failing of 0 collected

This covers only the tests that ran, on this commit, in this runner profile. It is not proof the
patch is correct.
```

Note what that example does *not* say. The patch emptied the set of failing tests, because nothing
could be imported at all. Reporting that as an improvement is the single most tempting mistake a
patch verifier can make, so a patched-only collection failure has its own outcome and is never
counted as progress.

The worker prints what it did:

```
Claimed run 64fbb850-784e-44fa-8c73-9e0dce94c339 -> inspecting
Inspecting https://github.com/pallets/itsdangerous (read-only; no clone, no code execution)
Inspected itsdangerous at 672971d66a2e on main
  50 files in tree, 50 listed, 2 previewed, 5 GitHub request(s)
  Python project: True | uses pytest: True (heuristic)
Run 64fbb850-784e-44fa-8c73-9e0dce94c339 -> ready (inspection complete; no fix attempted)
```

`inspect` exit codes: `0` inspected, `1` inspection failed (recorded against the run), `2` no such
run, `3` not claimable (already inspecting, finished, or taken by another worker).

`propose` exit codes: `0` patch proposed, `1` the attempt failed (recorded against the attempt),
`2` no such run, `3` the run already has an attempt, `4` the run is not `ready`, `5` the model is not
configured — in which case **no attempt row is created at all**.

Only a `pending` run can be inspected, and only a `ready` run can be proposed for, so each step
happens at most once per run. Both claims are race-safe: inspection uses a conditional `UPDATE`, and
an attempt is claimed by inserting a row whose `run_id` is `UNIQUE`. In both cases the losing worker
exits without contacting GitHub or the model.

A successful `propose` prints what the agent did, for example:

```
Claimed patch attempt for run 08bd92bc-…
Model: claude-opus-5 | commit 672971d66a2ef9f85151e53283113f33d642dabd
Proposing a patch (read-only; nothing is applied or executed)
  · Seeded 2 file(s) from the inspection
  · Context: 4,935 of 976,000 input tokens
  · Model turn 1 (tool_use)
  · Read src/itsdangerous/signer.py
  …
  · Patch submitted touching 2 file(s)
Proposed a patch touching 2 file(s): src/itsdangerous/signer.py, CHANGES.rst
  tokens: in=105557 out=7602
UNVERIFIED: the patch was not applied and no tests were run.
```

## Requirements

| Tool | Version used here | Notes |
|---|---|---|
| [uv](https://docs.astral.sh/uv/) | 0.11.7 | Manages the backend environment and lockfile |
| Python | 3.11.0 | `requires-python = ">=3.11"`; uv selects an interpreter for you |
| Node.js | 20.10.0 | See the note below |
| npm | 10.2.3 | Ships with Node |
| Docker | 27.4.0 | Only for `worker verify`; everything else runs without it |

> **Note on the Vite version.** The frontend pins the Vite 6 line (`vite@^6.4.3`) rather than the
> latest major. Vite 7+ and `@vitejs/plugin-react` 5+ require Node `^20.19.0 || >=22.12.0`, which
> this project's Node 20.10 does not satisfy. `package.json` declares
> `engines.node: "^18.0.0 || ^20.0.0 || >=22.0.0"` to match what is actually installed. If you move
> to Node 22.12+, Vite can be upgraded — the pin is a compatibility decision, not neglect.

## Setup

Clone the repository, then set up each side once.

### Backend

```bash
cd backend
cp .env.example .env          # optional; the defaults work as-is
uv sync                       # creates .venv and installs from uv.lock
uv run alembic upgrade head   # creates backend/branchforge.db
```

### Frontend

```bash
cd frontend
cp .env.example .env.local    # optional; defaults to http://localhost:8000
npm install                   # installs from package-lock.json
```

## Running locally

Three processes, one per terminal. The dispatcher is separate from the API on purpose: work that
outlives an HTTP response must not live inside the web process.

**Terminal 1 — API server** (from `backend/`):

```bash
cd backend
uv run uvicorn app.main:app --reload --port 8000
```

- API: <http://localhost:8000>
- Interactive docs: <http://localhost:8000/docs>

**Terminal 2 — dispatcher** (from `backend/`):

```bash
cd backend
uv run python -m app.worker dispatch
```

- Drains the queue that **Start** writes to, one job at a time.
- Needs `ANTHROPIC_API_KEY`, Docker, and the runner image. Both are checked at startup, before
  anything is claimed, so a misconfiguration costs nothing.
- **One dispatcher per database, on one host.** Exclusivity is an OS file lock (`flock`) on a file
  beside the SQLite database; a second dispatcher exits `9` having done nothing. The lock path is
  derived from the resolved database file, so two spellings of one database cannot yield two locks.
  `flock` does not carry these semantics across networked filesystems — this is a single-host
  arrangement.
- Without it, runs simply sit at `queued`. That is the honest state, and the dashboard says so.

**Terminal 3 — frontend** (from `frontend/`):

```bash
cd frontend
npm run dev
```

- Dashboard: <http://localhost:5173>

Run backend commands from `backend/`. The default `DATABASE_URL` is a relative SQLite path, so it
resolves against the directory you launch from.

## Verification

### Backend tests

```bash
cd backend
uv run pytest                      # everything, including real Docker runs
uv run pytest -m "not docker"      # offline only, no daemon needed
uv run pytest -m docker            # only the real container scenarios
```

Each test runs against its own temporary SQLite database, built by running the **real Alembic
migration** rather than `create_all`, so a broken migration fails the suite. Coverage includes the
create/retrieve round trip, URL and description validation, attempt bounds, 404s, list ordering and
limits, UTC timestamp round-tripping, and migration upgrade/downgrade.

Tests marked `docker` build real containers and skip automatically when the daemon or the runner
image is missing — they are never silently faked. Everything else runs offline: GitHub is mocked at
the transport layer, the model at the interface layer, and the container runner is injected.

### Frontend type check and production build

```bash
cd frontend
npm run typecheck    # tsc --noEmit
npm run build        # tsc --noEmit && vite build  (type errors fail the build)
```

### Worker tests

`tests/test_worker.py` and `tests/test_claim.py` mock GitHub at the **transport** layer
(`httpx2.MockTransport`), so real URL construction, response-byte caps, and status handling all run
— no network access. They cover a successful inspection, an unavailable repository, primary and
secondary rate limiting, timeouts, malformed JSON, refused redirects, an invalid commit SHA, a
truncated tree, an oversized response body, an oversized file omitted without being fetched, request
budgets, and claim contention between two independent database connections.

### Agent tests

`tests/test_agent.py` and `tests/test_attempt_claim.py` drive the real controller loop with scripted
model turns and mocked GitHub — no network, no API spend. They cover the read-then-submit path, cache
reuse (asserting no GitHub request), cache seeding from complete-but-not-truncated inspection
previews, unknown tools, malformed arguments, unsafe paths, `submit_patch` mixed with reads,
duplicate submissions, `max_tokens` truncation rejected before tool calls run, invalid diffs
corrected within budget, every budget being exhausted, context-budget termination, provider and
token-counting failures, completion without a patch, concurrent claims permitting exactly one model
caller, and a synthetic-sentinel credential never reaching the database or the API.

### Verification tests

```bash
cd backend
uv run pytest tests/test_snapshot.py       # archive extraction hardening (offline)
uv run pytest tests/test_runner_argv.py    # the docker argv, no daemon required
uv run pytest tests/test_verification.py   # comparison logic and orchestration (offline)
uv run pytest tests/test_verify_worker.py  # the worker command and claim contention
uv run pytest tests/test_docker_runner.py  # REAL containers (skips without Docker)
```

`tests/test_runner_argv.py` asserts the isolation flags directly — `--network=none`, `--user`,
`--read-only`, `--cap-drop=ALL`, `--security-opt=no-new-privileges`, the memory/CPU/PID ceilings —
and, negatively, that the argument vector carries no credential, no `docker.sock`, no host home
directory, no database file, and exactly one mount of the source tree. Those assertions hold whether
or not Docker is installed, which is why they exist separately from the live runs.

`tests/test_docker_runner.py` proves the flags actually take effect: a test inside the container
asserts `os.getuid() == 65534`, that writing to `/workspace` fails, and that opening a socket fails.
It also drives the deterministic fixture in `tests/sample_repo/` through every scenario — a correct
patch, a patch that does not apply, an incorrect patch that causes regressions, an ineffective patch,
a patch deleting the failing test, a patch that makes the failing test skip itself from source, a
patch that changes which tests are collected, a patch that breaks import, a hanging test (asserting
the container is force-removed and nothing is left behind), and a test flooding stdout (asserting the
log is capped without the run stalling).

The fixture repository under `backend/tests/sample_repo/` contains a deliberately failing test. It is
excluded from our own suite via `collect_ignore_glob` in `tests/conftest.py` — otherwise it would fail
the run it is meant to support.

### Orchestration tests

```bash
cd backend
uv run pytest tests/test_comparison.py             # eligibility and recommendation (pure)
uv run pytest tests/test_orchestration_claim.py    # claims, preflight, frozen settings, ambiguity
uv run pytest tests/test_orchestrator.py           # REAL child processes, scripted proposals
uv run pytest tests/test_docker_orchestration.py   # REAL containers under a real orchestrator
```

`test_orchestrator.py` launches genuine child processes (`tests/orchestration_child.py`, which runs the
real `run-attempt` code path with the model, GitHub, snapshot, and — unless a scenario says otherwise —
container runner scripted). Overlap is proven with a file **barrier** sized to the effective
concurrency, which can only release if that many children are mid-proposal at the same moment; an
flock-guarded counter records the peak number of live pipelines, which is asserted to equal the limit
and never exceed it. No assertion depends on elapsed time. The suite also covers: duplicate
orchestrators (barrier plus independent engines — the loser launches no child), a failed sibling, a
child crashing mid-proposal, a child exiting with code `0` or `3` **between saving its patch and
claiming its verification**, a child dying mid-verification, unconfirmed container cleanup stopping
further launches, per-attempt isolation of events, diffs, emphasis, and workspaces, SIGTERM and SIGINT
delivered to a real coordinator process (`tests/orchestration_driver.py`), and a child that ignores
SIGTERM being killed after the grace period. `test_docker_orchestration.py` runs a real three-attempt
orchestration, then interrupts one while two hanging patched runs sit inside real containers, and
asserts that every container carrying its label is gone while decoy containers — one labelled for a
different orchestration, one unlabelled — are still running.

### Execution queue and dispatcher tests

```bash
cd backend
uv run pytest tests/test_execution_jobs_api.py   # start/cancel contract, no execution
uv run pytest tests/test_progress_api.py         # the poll payload stays small
uv run pytest tests/test_dispatcher.py           # the dispatcher as a REAL process
uv run pytest -m browser                         # Playwright (skips when unavailable)
```

`test_execution_jobs_api.py` covers repeated Start returning the same job (including *after* the job
has started running and after it has completed), concurrent Starts from four independent engines
released by a barrier producing exactly one row, every refusal (manual attempts, an existing
orchestration, `inspecting`, `failed`, `ready`-without-a-commit), cancellation of queued, running,
and completed jobs, and the cancel-versus-claim race — asserting that *either* cancellation prevents
the launch *or* the claim wins and the job is left running for the dispatcher to notice, never both.

`test_progress_api.py` has teeth rather than checking a schema: it builds a run with three attempts,
60 KB diffs, 200 KB container logs and a full comparison, then asserts the detail payload exceeds a
megabyte while the progress payload stays under 4 KB, and that no diff or log marker string appears
anywhere in it.

`test_dispatcher.py` runs the dispatcher as a genuine process (`tests/dispatcher_driver.py`) with
only the model, image resolver, and child commands injected. It covers a second dispatcher exiting
without doing work, the lock being released on exit, a pending run being inspected before it is
orchestrated, a `ready` run skipping inspection, a failed inspection preventing any model call,
cancellation before launch and during work, cancellation *during inspection* being recorded honestly
rather than leaving a run `inspecting`, shutdown leaving untouched queued jobs `queued`, a failed job
not blocking the next, a job left `running` by a dead dispatcher being flagged and never replayed
(with notes that do not duplicate across restarts), and unconfirmed cleanup stopping further work.

The case that matters most: **an attempt child that survives a force-killed orchestrator.** Attempt
children lead their own sessions, so signalling the coordinator's process group cannot reach them. A
test makes the child ignore SIGTERM so the dispatcher must kill the orchestrator outright, then
asserts the orphan is stopped and reaped before the container sweep — because otherwise it could
still call the model and start containers after the job had been reported stopped.

### Local scripted benchmark

`uv run python -m tests.orchestration_bench [--docker]` runs the same three-attempt workload at
concurrency 1 and 3. Proposals are scripted, so **no model is called**; this measures process
management and verification only, on one machine and one toy fixture. Recorded here on an Apple M1
Pro (10 cores; Docker Desktop VM with 10 CPUs), three runs each:

| Verification | Concurrency 1 | Concurrency 3 |
|---|---|---|
| scripted runner | 3.3s, 3.3s, 3.5s (median 3.3s) | 1.4s, 1.3s, 2.0s (median 1.4s) |
| real Docker containers | 7.3s, 5.8s, 6.4s (median 6.4s) | 2.3s, 2.4s, 2.2s (median 2.3s) |

**This is not a claim about real-model speedups.** A real orchestration is dominated by model latency,
provider rate limits, and cost, none of which this benchmark exercises.

### Live verification check (optional — uses your GitHub rate limit, no API credit)

Verification needs no model call: a deterministic patch is better evidence than a generated one. To
exercise the real snapshot download and real containers end to end against a public repository, create
a run, inspect it, store a patch, and verify — see the workflow section above. A live check performed
this way against `benjaminp/six` produced a 16-file snapshot, ran the repository's 200-test suite in a
container (185 passed, 15 skipped), and reported `no_bug_demonstrated` for a comment-only patch and
`patched_collection_error` for a patch that broke an import.

### Live agent smoke test (optional — spends money and GitHub rate limit)

```bash
cd backend
export ANTHROPIC_API_KEY=sk-ant-...
export DATABASE_URL="sqlite:///$(mktemp -d)/live.db"
uv run alembic upgrade head
RID=$(uv run python -c "
from app.database import SessionLocal
from app import repository
with SessionLocal() as db:
    print(repository.create_run(db,
        repository_url='https://github.com/pallets/itsdangerous',
        issue_description='Signer.unsign() raises a confusing ValueError for an empty payload instead of BadSignature.',
        max_parallel_attempts=1).id)")
uv run python -m app.worker inspect --run-id "$RID"
uv run python -m app.worker propose --run-id "$RID"
unset DATABASE_URL
```

### Live inspection (optional, uses your GitHub rate limit)

```bash
cd backend
export DATABASE_URL="sqlite:///$(mktemp -d)/live.db"
uv run alembic upgrade head
RID=$(uv run python -c "
from app.database import SessionLocal
from app import repository
with SessionLocal() as db:
    print(repository.create_run(db,
        repository_url='https://github.com/pallets/itsdangerous',
        issue_description='Live check.', max_parallel_attempts=1).id)")
uv run python -m app.worker inspect --run-id "$RID"
unset DATABASE_URL
```

### Migration up and down, without touching your dev database

Point `DATABASE_URL` at a throwaway file so your normal `branchforge.db` is never dropped:

```bash
cd backend
export DATABASE_URL="sqlite:///$(mktemp -d)/check.db"

uv run alembic upgrade head
uv run alembic current            # -> 0006 (head)
uv run alembic downgrade base     # exercises downgrade()
uv run alembic upgrade head

unset DATABASE_URL
```

**Migration `0006` only adds** — `CREATE TABLE execution_jobs` plus
`ALTER TABLE patch_attempts ADD COLUMN worker_pid` — both of which SQLite performs in place, so the
cascade hazard below does not arise on the way up. Its *downgrade* does rebuild `patch_attempts` (a
column cannot be dropped in place), so it carries the same guards, and it **refuses to downgrade at
all while any execution job exists**: revision 0005 has nowhere to put those rows, and deleting them
to make the downgrade succeed would destroy the record of what was started or cancelled.

**Migration `0005` rebuilds the `patch_attempts` table** (SQLite cannot relax its old unique index or
`NOT NULL` in place). Run it with the API and every worker stopped. `alembic/env.py` disables SQLite
foreign keys on the migration connection only — otherwise the rebuild's `DROP` would cascade-delete
attempt events and verifications — makes the DDL genuinely transactional, and restores the pragma in a
`finally` block whether or not the migration succeeds. The migration refuses to run if foreign keys
are still on, and runs `PRAGMA foreign_key_check` before committing, so a dangling reference rolls it
back to `0004`. Existing attempts become `attempt_index = 1` with their events and verifications
intact (`tests/test_migrations.py` checks this against a populated `0004` database). Downgrading below
`0005` is refused, changing nothing, while any orchestration or multi-attempt run exists.

### Restart persistence

Also against a temporary database:

```bash
cd backend
export DATABASE_URL="sqlite:///$(mktemp -d)/restart.db"
uv run alembic upgrade head

uv run uvicorn app.main:app --port 8123 &   # start
curl -sX POST http://127.0.0.1:8123/api/runs \
  -H 'Content-Type: application/json' \
  -d '{"repository_url":"https://github.com/octocat/Hello-World","issue_description":"Crash on empty input.","max_parallel_attempts":2}'

kill %1                                      # stop
uv run uvicorn app.main:app --port 8123 &    # start again
curl -s http://127.0.0.1:8123/api/runs       # the run is still there
kill %1

unset DATABASE_URL
```

## API

Base path `/api`. Interactive documentation is at `/docs` while the backend is running.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | Liveness. Does not touch the database. |
| `POST` | `/api/runs` | Create a run. `201` with the persisted record. |
| `GET` | `/api/runs?limit=` | List runs, newest first. `limit` is 1–100, default 25. |
| `GET` | `/api/runs/{run_id}` | Fetch one run **plus its inspection**. `404` if unknown. |
| `GET` | `/api/runs/{run_id}/progress` | Lightweight state for polling: scalars only. `404` if unknown. |
| `POST` | `/api/runs/{run_id}/start` | Enqueue the workflow. `202` with the job; `409` if the run cannot be started. |
| `POST` | `/api/runs/{run_id}/cancel` | Request cancellation. `202` with the job; `404` if never started. |

`POST /start` writes **one row** and returns. It never inspects, calls a model, starts a process, or
uses a BackgroundTask — work that outlives the response would die with the API process. Repeated
starts return the **existing** job rather than a second one or an error, so a double-clicked button
is harmless; two concurrent requests collapse onto one job because `execution_jobs.run_id` is
`UNIQUE` and the INSERT *is* the claim.

`GET /progress` exists so polling is cheap. It carries the run's status, the job, the orchestration's
status, and one row per attempt (status, pipeline error, event **count**, verification status and
outcome) — and deliberately no diffs, logs, events, inspection report, or comparison. For a run with
three attempts the detail payload is measured in megabytes; this stays a few hundred bytes. The
dashboard fetches the full detail only when a status actually changes.

`GET /api/runs/{run_id}` returns the run with a nested `inspection` (or `null`), an `attempts` list
ordered by `attempt_index` — each attempt carrying its own nested `verification` (or `null`) — and an
`orchestration` object (or `null` for a manual run). The list endpoint deliberately omits all of them:
including them would make a list response unbounded. The detail response stays bounded: at most three
attempts, the workers' budgets bound what they write, and each attempt's event list is capped
(`events_total` reports how many exist).

Four lifecycles are kept separate on purpose:

- A run's `status` reflects **inspection only**: it stays `ready` however its attempts turn out.
- An attempt's `status` reflects **the proposal only**: `queued` (a reserved slot), `running`,
  `succeeded` (a diff was produced — not that it works), `failed`, `interrupted`. If an orchestrated
  pipeline stopped before its verification finished, `pipeline_error_kind` / `pipeline_error_message`
  say so without changing the proposal's status.
- A verification's `status` reflects whether it ran (`running`, `completed`, `failed`,
  `interrupted`); its `outcome` says what it found.
- An orchestration's `status` (`queued`, `running`, `completed`, `interrupted`, `failed`) reflects
  whether it ran to the end. Its `comparison` holds each attempt's eligibility and the reasons, the
  `headline` ("No demonstrated fix" when nothing qualifies), and `recommended_attempt_index` (`null`
  unless an attempt demonstrated a fix). `complete: false` marks a comparison over only the attempts
  that finished, and its recommendation is then labelled "among completed attempts only".

### Creating a run

```bash
curl -X POST http://localhost:8000/api/runs \
  -H 'Content-Type: application/json' \
  -d '{
    "repository_url": "https://github.com/octocat/Hello-World",
    "issue_description": "Sorting breaks on empty input.",
    "max_parallel_attempts": 2
  }'
```

```json
{
  "id": "f880b42e-625c-45db-b9f5-32be8f57dfa3",
  "repository_url": "https://github.com/octocat/Hello-World",
  "issue_description": "Sorting breaks on empty input.",
  "max_parallel_attempts": 2,
  "status": "pending",
  "created_at": "2026-09-09T04:09:53.320666Z",
  "updated_at": "2026-09-09T04:09:53.320670Z"
}
```

`id`, `status`, and both timestamps are assigned by the server; supplying them is rejected.

### Validation rules

**`repository_url`** must be an HTTPS URL whose host is exactly `github.com` and whose path is
exactly `<owner>/<repository>`. Rejected: any other scheme (`http`, `git`, SSH `git@github.com:…`),
embedded credentials, an explicit port, other or lookalike hosts (`www.github.com`,
`github.com.evil.com`), query strings, fragments, deeper paths (`/owner/repo/pull/1`), and malformed
owner or repository segments.

It is **normalized before storage**: the host is lowercased and a trailing `.git` or `/` is removed,
while the case of the owner and repository segments is preserved. `https://github.com/OctoCat/Hello-World.git/`
is stored as `https://github.com/OctoCat/Hello-World`. Because the dashboard displays the persisted
record, it shows this normalized form.

Validation establishes only that the string is an acceptable GitHub repository address. **No network
request is made**, so acceptance does not mean the repository exists, is public, or is reachable.
Checking that against GitHub belongs to a later milestone.

**`issue_description`** must be non-empty after trimming (whitespace-only is rejected) and at most
10,000 characters. It is stored trimmed.

**`max_parallel_attempts`** is an integer from 1 to 3, defaulting to 1.

### Error responses

Invalid request bodies and query parameters return **422** with FastAPI's field-level detail array;
an unknown run id returns **404** with a string `detail`. The frontend normalizes both shapes in
`frontend/src/api.ts`.

## GitHub rate limits

The worker reads GitHub **without authentication**, which GitHub limits to roughly **60 requests per
hour per IP address**.

One inspection costs **3 + n requests**:

| Request | Count |
|---|---|
| Repository metadata | 1 |
| Commit resolution (default branch → commit SHA) | 1 |
| Recursive file tree at that commit | 1 |
| File contents | 1 per file fetched, up to `GITHUB_MAX_FILES_FETCHED` (default 8) |

So an inspection uses between 3 and 11 requests at the default settings — **roughly 5 inspections
per hour**. Treat that as approximate: the allowance is per IP, so anything else on your network
using the GitHub API shares it, and inspections that fetch fewer files cost less.

`propose` draws from the **same** allowance: one request per uncached `read_file`, capped by
`AGENT_MAX_TOOL_CALLS`. Files already read during inspection are seeded into the agent's cache and
cost nothing, and a repeated read within one attempt is served from cache. Even so, an attempt that
reads a lot can exhaust what an inspection left — the live example above used 5 requests to inspect
and 11 more to propose, 16 of the hour's 60.

When the limit is exhausted the worker fails the run with a `rate_limited` error naming the reset
time. It does **not** retry — rerunning immediately would just burn the next window.

## Model configuration and budgets

`worker propose` needs an Anthropic API key and a model identifier:

```bash
export ANTHROPIC_API_KEY=sk-ant-...        # from https://console.anthropic.com/
export ANTHROPIC_MODEL=claude-opus-5       # or any model your account can use
```

Either export them or put them in `backend/.env` (gitignored). **Never commit a real key.** If the
key is missing or the model is unset, `propose` fails with exit code `5` **before** creating an
attempt row, so a misconfiguration never leaves a half-claimed attempt behind.

The key is held as a `SecretStr`, so it does not appear in a settings `repr`. It is never logged,
never written to a database row or event, and never included in an API response.

### Budgets

Every limit is configurable (see `backend/.env.example`). Each has its own failure kind, recorded on
the attempt:

| Setting | Default | Bounds |
|---|---|---|
| `AGENT_MAX_TURNS` | 8 | Model round-trips per attempt |
| `AGENT_MAX_TOOL_CALLS` | 12 | Tool calls per attempt |
| `AGENT_MAX_PATCH_REPAIR_TURNS` | 2 | Extra submit-only turns after a patch is rejected on the final turn (`0` disables) |
| `AGENT_MAX_FILE_BYTES` | 60,000 | One `read_file` |
| `AGENT_MAX_TOTAL_FETCHED_BYTES` | 200,000 | All reads combined |
| `AGENT_MAX_OUTPUT_TOKENS` | 16,000 | Output per model call |
| `AGENT_MAX_CONTEXT_TOKENS` | 150,000 | Application limit on input tokens |
| `AGENT_MODEL_CONTEXT_TOKENS` | 1,000,000 | The model's context window |
| `AGENT_CONTEXT_SAFETY_MARGIN_TOKENS` | 8,000 | Held back from that window |
| `AGENT_MAX_PATCH_BYTES` | 60,000 | Submitted diff |
| `AGENT_MAX_EVENTS` | 200 | Stored events per attempt |

**Context is measured, not estimated.** Before every generation the worker calls the provider's
token-counting endpoint with the system prompt, the tool definitions, and the full message history.

Two independent ceilings apply and the **smaller** one wins:

1. `AGENT_MAX_CONTEXT_TOKENS` — an explicit application limit (150,000 by default), and
2. `AGENT_MODEL_CONTEXT_TOKENS` minus the output reservation minus the safety margin (976,000 at the
   defaults).

Keeping the application limit explicit matters: moving to a model with a larger window should not
silently licence a much larger, slower, and more expensive attempt. At the defaults the application
limit is the binding one, and the failure message names whichever ceiling actually applied. If a
request would exceed the budget the attempt **stops with an explanation**; conversation history is
never silently discarded, because context compaction is not implemented yet.

**Retries are disabled.** The SDK is configured with `max_retries=0`, so a failed call is never
transparently repeated at the cost of the attempt's budget. Note that
`AGENT_REQUEST_TIMEOUT_SECONDS` applies to **one HTTP request**, not to the attempt as a whole — the
turn and tool-call limits are what bound total work. A long attempt can therefore run for several
minutes; the live example above took about 110 seconds.

### What the agent can and cannot do

The agent gets exactly two tools, and the Python controller validates and dispatches every call:

| Tool | Arguments | Notes |
|---|---|---|
| `read_file` | `path` | Reads from the **inspected commit**, which the model cannot change |
| `submit_patch` | `diff`, `summary`, `suggested_test_command` | Must be the only call in its response |

It cannot choose a URL, run a command, read local files, or move to a different commit. Unknown
tools, malformed arguments, unsafe paths, and invalid diffs come back as bounded tool errors so the
model can correct itself within its remaining budgets. A response truncated at `max_tokens` is
rejected **before** its tool calls run, since the arguments may be incomplete.

Repository content — READMEs included — is given to the model as data, with an explicit instruction
that any directions found inside it must be ignored.

## Verification: the runner and what a result means

### Supported repositories

One profile: **Python, tested with pytest, needing no dependencies beyond pytest itself.** The runner
image is `python:3.11-slim` plus a pinned pytest and nothing else.

This is narrow on purpose. Installing what a repository asks for means *executing* what a repository
asks for — `setup.py`, a `Dockerfile`, a requirements file naming an arbitrary index. So BranchForge
does none of it. A repository that needs more is reported as an `environment_limitation`, which says
nothing at all about whether the patch is correct.

Also unsupported, and reported rather than worked around:

- Repositories whose archive contains symlinks, hardlinks, or device entries. Links are **refused**,
  not sanitized: validating link targets is easy to get subtly wrong.
- Test suites that write into their own source tree — the source is mounted read-only. Tests needing
  scratch space must use `tmp_path`, which lands on the container's tmpfs.
- Suites needing network access, which have none.

### What runs where

The orchestrating worker stays outside the container. Repository code runs **only** inside
`docker run`, with:

| Constraint | Flag |
|---|---|
| No network at all | `--network=none` |
| Non-root user | `--user 65534:65534` |
| Read-only root filesystem | `--read-only` |
| Bounded scratch space | `--tmpfs=/tmp:rw,noexec,nosuid,size=64m` |
| Memory / CPU / process ceilings | `--memory=512m --cpus=1.0 --pids-limit=256` |
| No capabilities, no privilege growth | `--cap-drop=ALL --security-opt=no-new-privileges` |
| No daemon log growth | `--log-driver=none` |
| Only the source tree (read-only) plus a runner-owned results directory | two `-v` mounts |
| Removed on exit and on timeout | `--rm`, plus `docker rm -f` after a wall-clock overrun |

No credential, no host home directory, no database file, and no Docker socket is ever passed in. The
pytest invocation is fixed and runner-owned, including `-o addopts=` so a repository's own ini options
cannot inject plugins or flags into it. The model's `suggested_test_command` remains display-only and
is never executed.

**This is local container isolation, not a production multi-tenant security guarantee.** It raises the
cost of a hostile repository considerably. A container escape is still a container escape.

### The source snapshot

The archive of one exact commit is fetched from `codeload.github.com`, which serves a specific SHA
directly with no redirect — so the redirect ban in the GitHub client needs no exception. A tarball,
not a clone: an extracted archive has no `.git` directory, so hooks, clean/smudge filters, submodule
setup, and repository-supplied git config cannot exist *by construction*.

Extraction is the trust boundary and is hand-written, because `tarfile.data_filter` does not exist on
Python 3.11.0. It rejects links, devices, traversal, absolute paths, duplicate paths, file/directory
collisions, and any `.git` entry; counts **decompressed** bytes including tar headers, so a
compression bomb is bounded by what reading it costs rather than by the sizes it declares; enforces a
file count and an overall deadline; and discards archive ownership, permissions, and mtimes.

`git apply` runs with every inherited `GIT_*` variable stripped, config pointed at `/dev/null`, and
`GIT_CEILING_DIRECTORIES` set so repository discovery cannot walk up into a parent repository. Without
that, your own global git config could change whitespace handling and make a stored result
irreproducible.

### Four workspaces

| Workspace | Contents | Purpose |
|---|---|---|
| `snapshot/` | the commit, untouched | source of truth, never mounted |
| `baseline/` | copy of the snapshot | the "before" run |
| `patched_full/` | snapshot + patch, untouched | supplemental run of the patch's own tests |
| `comparison/` | patched source + **original** test environment | the "after" run — the one that counts |

Before the comparison run, the original tests, every `conftest.py`, fixture and data files, and
collection configuration (`pytest.ini`, `tox.ini`, `setup.cfg`, `pyproject.toml`) are copied back over
the patched tree, and any test-environment file the patch *added* is deleted. A patch therefore cannot
change the tests it is measured against — neither by weakening them nor by adding configuration that
quietly deselects them.

Your own BranchForge checkout is never touched. All four workspaces are runner-owned temporary
directories, removed in a `finally`.

### How a fix is decided

Results come from a runner-owned pytest plugin baked into the image (not from parsing terminal
output), which records the collected node IDs and an explicit outcome per test across the setup, call,
and teardown phases.

1. The comparison run must collect **exactly** the same node IDs as the baseline. Otherwise the runs
   are not comparable and the outcome is `collection_mismatch`, however green they look.
2. A previously failing test counts as fixed only when **that same node ID reports `passed`**.
   Skipped, xfailed, deselected, and missing are all "not fixed".
3. A previously passing test that stops running weakens the yardstick, so the outcome is downgraded to
   `inconclusive` rather than reported as a fix.
4. A missing, malformed, or oversized report is an error — never an absence of failures.

The claim this supports is narrow: *these tests behaved this way, on this commit, in this profile.* It
is not a proof of correctness, and it is not protection against a patch deliberately engineered to
forge results.

### Outcomes

`status` says whether the verification ran (`running` → `completed` | `failed`). `outcome` says what
it found. They are separate because "the verification completed" and "the patch works" are different
claims, and a single pass/fail field is how a green badge comes to mean nothing.

| Outcome | Meaning |
|---|---|
| `fix_demonstrated` | Every originally failing test now passes; nothing else regressed |
| `partial_fix` | Some originally failing tests now pass, but not all |
| `still_failing` | None of the originally failing tests now pass |
| `regressions` | The patch made previously passing tests fail |
| `no_bug_demonstrated` | Existing tests pass; bug fix not demonstrated — nothing failed to begin with |
| `tests_only_patch` | The patch changes only tests or configuration, so the comparison equals the baseline |
| `patch_did_not_apply` | The patch does not apply to the inspected commit |
| `collection_mismatch` | The runs collected different tests and cannot be compared |
| `patched_collection_error` | The patched source fails to import or collect — a defect, not an improvement |
| `baseline_unusable` | The baseline produced no usable result |
| `unsupported_layout` | No original pytest suite was found |
| `environment_limitation` | The profile cannot build this repository; says nothing about the patch |
| `timeout` | A run exceeded its wall-clock limit and was terminated |
| `inconclusive` | The runs completed but could not be compared reliably |

pytest's exit codes are kept distinct rather than collapsed: `no_tests_collected` (5), `interrupted`
(2), `usage_error` (4), `internal_error` (3), and `tests_failed` (1) are different facts, and a
collection error is not a test failure.

## Configuration

Nothing reads the environment directly except `backend/app/config.py`. Copy the example files and
edit them; neither contains secrets.

### `backend/.env.example`

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./branchforge.db` | SQLAlchemy URL, shared by the app and Alembic. A plain `postgres://` / `postgresql://` URL is rewritten to use psycopg 3 |
| `DISPATCHER_AVAILABLE` | `true` | Set `false` on a deployment with no dispatcher: Start is refused, polling stops, and the dashboard says why |
| `CORS_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173` | Comma-separated allowed browser origins |
| `RUN_LIST_DEFAULT_LIMIT` | `25` | Default `GET /api/runs` page size |
| `RUN_LIST_MAX_LIMIT` | `100` | Hard ceiling for `limit` |
| `GITHUB_API_BASE_URL` | `https://api.github.com` | GitHub API root |
| `GITHUB_USER_AGENT` | `BranchForge/0.2` | Sent with every request |
| `GITHUB_REQUEST_TIMEOUT_SECONDS` | `10.0` | Per-request timeout |
| `GITHUB_CONNECT_TIMEOUT_SECONDS` | `5.0` | Connection timeout |
| `GITHUB_MAX_REQUESTS` | `20` | Hard ceiling on requests per inspection |
| `GITHUB_MAX_RESPONSE_BYTES` | `8000000` | Raw response body cap (GitHub caps a tree at 7 MB) |
| `GITHUB_MAX_CONTENT_RESPONSE_BYTES` | `262144` | Raw cap for file contents (base64 inflates by 4/3) |
| `GITHUB_MAX_FILE_BYTES` | `100000` | Largest single file stored, decoded |
| `GITHUB_MAX_TOTAL_CONTENT_BYTES` | `400000` | Cap across all previews combined |
| `GITHUB_MAX_FILES_LISTED` | `500` | File-listing size in the report (display only) |
| `GITHUB_MAX_FILES_FETCHED` | `8` | How many files to preview |
| `ANTHROPIC_API_KEY` | *(unset)* | Required by `propose` and `orchestrate`. Never commit a real key |
| `ANTHROPIC_MODEL` | `claude-opus-5` | A model your account can access |
| `AGENT_*` | see above | Agent budgets — [Model configuration and budgets](#model-configuration-and-budgets) |
| `VERIFY_*` | see `.env.example` | Runner image, container ceilings, output bounds |
| `ORCHESTRATOR_MAX_CONCURRENCY` | `2` | Attempt pipelines one orchestrator runs at once (1-3); per process, not global |
| `ORCHESTRATOR_CHILD_GRACE_SECONDS` | `10.0` | On Ctrl+C/SIGTERM, time a child gets before SIGKILL |
| `ORCHESTRATOR_DRAIN_TIMEOUT_SECONDS` | `10.0` | How long a finished child's output reader may take to reach EOF |
| `ORCHESTRATOR_MAX_RELAYED_LINE_CHARS` | `2000` | Longest child output line relayed; the rest is discarded |
| `DISPATCHER_POLL_SECONDS` | `2.0` | How often the dispatcher checks an empty queue |
| `DISPATCHER_CANCEL_POLL_SECONDS` | `0.5` | How often an active job's cancellation flag is re-read |
| `DISPATCHER_CHILD_GRACE_SECONDS` | `90.0` | Time a child gets after SIGTERM before SIGKILL |
| `DISPATCHER_DRAIN_TIMEOUT_SECONDS` | `10.0` | How long a finished child's output reader may take to reach EOF |
| `DISPATCHER_WORKER_GRACE_SECONDS` | `10.0` | Time an orphaned attempt worker gets before SIGKILL |
| `DISPATCHER_MAX_JOB_NOTES` | `20` | Cap on operator notes kept on one job |

`DISPATCHER_CHILD_GRACE_SECONDS` is large deliberately. On SIGTERM an orchestrator spends up to
`ORCHESTRATOR_CHILD_GRACE_SECONDS` + `ORCHESTRATOR_DRAIN_TIMEOUT_SECONDS` +
`VERIFY_CLEANUP_TIMEOUT_SECONDS` saving its comparison and sweeping its own containers. Killing it
sooner throws that work away and leaves containers for the dispatcher to clean up, so keep this
comfortably above their sum. There is deliberately **no** lock-path setting: the dispatcher's lock is
derived from the resolved database file, because an override is exactly what would let one database
have two locks.

An orchestration **freezes** `ANTHROPIC_MODEL` and every `AGENT_*`, `GITHUB_*`, and `VERIFY_*` value
when it starts (never the API key), together with the resolved runner image ID. Its children use
those stored values, so editing `.env` mid-orchestration cannot make siblings run differently.

The dashboard requests 25 runs per list call, so keep `RUN_LIST_MAX_LIMIT` at 25 or above —
lowering it below that makes every list request fail validation with a 422.

### `frontend/.env.example`

| Variable | Default | Purpose |
|---|---|---|
| `VITE_API_BASE_URL` | `http://localhost:8000` | Backend base URL used by the browser |

If you change the frontend's port or host, add its origin to `CORS_ORIGINS`.

## Deployment

`deploy/` documents one supported way to run this on a single Ubuntu LTS host: Caddy serves the
built frontend and reverse-proxies `/api/*` to FastAPI, HTTPS and a shared password come from Caddy,
and FastAPI and the dispatcher run as separate systemd services with only the dispatcher's account
given Docker access. See `deploy/README.md` for exact install, upgrade, rollback, and
recovery-required commands. It changes nothing about the agent, verification, cancellation, or
comparison behavior described elsewhere in this file — only how the existing processes are launched
and reached.

## Project layout

```
backend/
  app/
    main.py          FastAPI app factory; CORS wired from configuration
    config.py        Pydantic-settings; the only reader of the environment
    database.py      Engine, session factory, declarative Base, get_db dependency
    models.py        SQLAlchemy models: Run, Inspection, Orchestration, PatchAttempt, ...
    schemas.py       Request/response schemas; UTC timestamp serialization
    validators.py    GitHub repository URL validation and normalization
    repository.py    All database queries — no FastAPI imports
    worker.py        Worker CLI (inspect, propose, verify, orchestrate, hidden run-attempt);
                     owns transaction boundaries
    orchestrator.py  Asyncio coordinator: child processes, semaphore, reconciliation, shutdown
    dispatcher.py    The execution queue's dispatcher: OS lock, claim, stages, cancellation,
                     orphan reaping, container sweep — reuses the commands above unchanged
    comparison.py    Pure cross-attempt eligibility and recommendation
    github_client.py Bounded read-only GitHub client; typed errors
    inspection.py    File selection, pytest heuristic, report building
    model_client.py  The model boundary: ModelTurn, protocol, Anthropic adapter
    agent.py         Controller loop, tool dispatch, budgets, prompt building
    patch_validation.py  Unified-diff syntax, hunk counts, path safety
    snapshot.py      Bounded source-archive download and hardened extraction
    runner.py        Docker argv, execution, bounded output capture, cleanup
    verification.py  Baseline/apply/compare orchestration and the outcome vocabulary
    routers/
      health.py      GET /api/health
      runs.py        Run endpoints; thin handlers delegating to repository
    time_utils.py    UTC helpers
  alembic/
    env.py           Resolves the URL from DATABASE_URL
    versions/        0001_create_runs_table.py, 0002_create_inspections_table.py,
                     0003_create_patch_attempts.py, 0004_create_verifications.py,
                     0005_orchestrations.py (rebuilds patch_attempts — see Migration),
                     0006_execution_jobs.py (adds only; the downgrade rebuilds)
  runner/
    python-pytest/   The ONLY environment repository code runs in
      Dockerfile     python:3.11-slim + pinned pytest; nothing else
      bf_report.py   Runner-owned pytest plugin emitting structured results
  tests/
    conftest.py      Temporary migrated database, dependency override, TestClient
    test_validators.py, test_runs_api.py, test_migrations.py,
    test_worker.py (mocked GitHub), test_claim.py (claim contention),
    test_agent.py (scripted model), test_attempt_claim.py (contention + leakage),
    test_snapshot.py (extraction hardening), test_runner_argv.py (isolation flags),
    test_verification.py (comparison logic), test_verify_worker.py (worker + claim),
    test_docker_runner.py (REAL containers; skips without Docker),
    test_comparison.py, test_orchestration_claim.py, test_orchestrator.py (real child
    processes), test_docker_orchestration.py (REAL containers under an orchestrator),
    test_execution_jobs_api.py (start/cancel contract), test_progress_api.py (the
    poll payload stays small), test_dispatcher.py (the dispatcher as a real process),
    test_browser_dashboard.py (Playwright; skips when unavailable)
    orchestration_child.py / orchestration_driver.py   Test-only child and coordinator
                         processes (not collected); orchestration_bench.py  benchmark
    dispatcher_driver.py / dispatcher_inspect_child.py  Test-only dispatcher and inspect
                         children (not collected)
    fixture_support.py   Scenario patches, generated with difflib
    sample_repo/         Fixture repository with a known bug (not collected)

frontend/src/
  App.tsx            Page composition and all data fetching
  api.ts             Fetch wrapper; normalizes 422 and 404 error shapes
  types.ts           Types mirroring the backend schemas
  time.ts            Absolute and relative timestamp formatting
  components/        NewRunForm, RunList, RunDetail, InspectionPanel, OrchestrationPanel,
                     PatchAttemptPanel, VerificationPanel, StatusBadge, Callout,
                     NotImplementedNote
  styles.css         Theme and layout (light and dark)
```

## Design notes

Choices made now so a worker process can be added next without rework:

- **All database access lives in `repository.py`**, which imports no FastAPI. Route handlers only
  validate, delegate, and shape the response, so a future worker can reuse the same functions.
- **Alembic owns the schema.** Nothing calls `Base.metadata.create_all()` — not even the tests,
  which run the migration instead. A broken migration cannot be masked by the app building its own
  tables at startup.
- **Database handlers are sync `def`, not `async def`.** SQLAlchemy's session is synchronous, so
  FastAPI runs these in a worker thread and blocking queries never stall the event loop.
- **Timestamps are UTC end to end.** SQLite has no timezone-aware datetime type and can return naive
  values even from a `DateTime(timezone=True)` column. Since every write uses
  `datetime.now(timezone.utc)`, `app/time_utils.as_utc` restores that awareness on the way out and
  converts anything already aware, so the API always emits an explicit `...Z`. A test asserts the
  value is the same *instant* after a round trip, not merely that the string ends in `Z`.
- **Listing is ordered by `created_at DESC, id DESC`.** The `id` tiebreaker keeps ordering stable
  for runs created within the same clock tick.
- **The worker holds no transaction across the network.** A short session claims the run and
  commits; all GitHub work happens with no session open; a second short session stores the result.
  On success the report and the `ready` status share **one commit**, so a run can never be `ready`
  without its report. The same is true of a failure and its error.
- **The claim is a conditional `UPDATE`** (`WHERE id = ? AND status = 'pending'`), so exactly one of
  two racing workers can match a row. A test proves this: the same test against a naive
  read-then-write claim produces two winners.
- **The GitHub client never accepts a URL.** Its methods take `owner`, `repo`, `path`, and `ref`, and
  build every request path from percent-encoded components, so a URL supplied by a repository or an
  upstream response can never be fetched. The `url`, `git_url`, `download_url`, and `_links` fields
  in GitHub responses are ignored, and redirects are refused rather than followed.
- **Branch names are passed as a query parameter**, not a path segment, because a branch may contain
  slashes (`release/v2`) that would otherwise change the shape of the request path.
- **Response bodies are bounded before parsing.** File-size metadata is not a download limit, so
  responses are rejected up front on a declared `Content-Length` over the cap and cut off mid-stream
  otherwise — the cap applies before any JSON is parsed or base64 decoded.
- **Oversized files cost nothing.** The tree lists each blob's size, so a file over the per-file
  budget is reported as omitted without a content request ever being issued.
- **File selection runs over the whole tree**, before the display-only listing limit, so a README
  that sorts late alphabetically is still chosen. Root-level files win over nested ones.
- **`httpx2` is httpx v2.** It installs under the module name `httpx2`, which is why imports read
  `import httpx2`. Do not add the older `httpx` package alongside it — one HTTP stack is enough.
  Starlette's `TestClient` wants `httpx2`, and so does `anthropic` 1.x, so all three share it.
- **The model sits behind a narrow interface** (`app/model_client.py`): one `ModelTurn` dataclass and
  a `ModelClient` protocol with `create_message` and `count_input_tokens`. Tests inject a scripted
  client, so the controller loop is fully testable offline. This is deliberately *not* a
  multi-provider framework — there is one adapter.
- **The controller loop is hand-written, not the SDK's tool runner**, because this milestone requires
  the Python side to validate and dispatch every tool call, enforce budgets, and cache reads.
- **A manual attempt is claimed by inserting its row.** `(run_id, attempt_index)` is `UNIQUE` and a
  manual attempt is always index 1, so the INSERT *is* the atomic claim: the loser of a race catches
  `IntegrityError` and exits having made zero model calls and zero GitHub requests. An orchestration
  reserves indices 1..N in the same commit as its own row (`orchestrations.run_id` is `UNIQUE`), so a
  manual attempt and an orchestration contend for index 1 and cannot both win.
- **Attempt status is separate from run status.** A run stays `ready` — meaning inspection
  succeeded — through a successful *and* a failed attempt. None of the attempt persistence functions
  touch `Run.status`.
- **Events are committed as they happen**, each in its own short transaction, so the dashboard's
  Refresh shows progress while an attempt is still running. The final patch and the `succeeded`
  status are written together in a single transaction instead.
- **Patch submissions are validated at runtime even though the tool schema is `strict: true`.** A
  schema guarantees argument shape, never that a diff is well formed or its paths safe;
  `app/patch_validation.py` checks hunk line counts, rejects absolute and `..` paths, and refuses
  binary patches, renames, and copies. Passing validation does **not** mean the patch applies or
  works — that is the verification step's job, which revalidates the diff before `git apply` touches a
  throwaway copy.

- **A tarball, not a clone.** An extracted source archive has no `.git` directory, so hooks,
  clean/smudge filters, submodule setup, and repository-supplied git config cannot exist by
  construction rather than needing to be disabled by flag. `git apply` works fine outside a
  repository, which is what makes this practical.
- **`status` and `outcome` are separate columns on a verification.** "The verification ran" and "the
  patch works" are different claims. Collapsing them into one boolean is how a badge stops meaning
  anything, and there is deliberately no value named `verified`.
- **Test results come from a runner-owned plugin, not from parsing output.** Terminal text cannot
  distinguish a test that was fixed from one that was skipped, deselected, renamed, or never
  collected, and those differences are the whole point.
- **The unique `attempt_id` INSERT is the verification claim**, the same mechanism as the patch
  attempt: the second worker gets an `IntegrityError`, rolls back, and exits having downloaded nothing
  and started no container.
- **Docker and the image are checked before the claim**, so a stopped daemon cannot leave a claimed
  verification stranded — the same ordering rule as checking model configuration before claiming an
  attempt. `orchestrate` applies the same rule before reserving any slot.

- **Child processes, not threads or tasks, run attempts.** Each attempt reuses the synchronous agent
  and Docker code unchanged inside its own process; the coordinator is asyncio only for waiting.
  Children start in their own session, so terminal Ctrl+C reaches only the coordinator, which then
  signals each child's whole process group.
- **Cancelling a task stops nothing; killing `docker run` does not stop its container.** So every
  container carries `branchforge.orchestration=<id>` and `branchforge.attempt=<id>` labels (validated
  runner-owned UUIDs, never repository text), and cleanup is a label-scoped `docker rm -f` whose
  success is confirmed by listing again. Other owners' and unlabelled containers are never touched.
- **A slot is released only after cleanup is confirmed**, so a new pipeline cannot start while an
  orphaned container from the last one still runs. Unconfirmed cleanup stops further launches and is
  recorded as an orchestration failure rather than silently weakening the bound.
- **Completion is read from the database, never inferred from an exit code.** After every child exit
  the coordinator reconciles what was persisted: an unfinished proposal is failed or interrupted, a
  saved patch whose verification never started keeps its `succeeded` proposal status but gains a
  pipeline error, and a verification left `running` is failed or interrupted. Every write is
  conditional, so a result stored just before the exit is never overwritten.
- **Child arguments are checked, not trusted.** The hidden `run-attempt` command takes only IDs; it
  verifies the attempt belongs to that active orchestration before claiming it, and reads the
  workspace root, image ID, labels, and frozen settings from the orchestration row.
- **Recommendation evidence must belong to the candidate.** Beyond `fix_demonstrated`, the
  verification's commit SHA and patch hash must equal the attempt's commit and diff, its image ID and
  profile must match the orchestration's, and eligible candidates' baselines must agree; otherwise no
  cross-attempt recommendation is made.

## Limitations

- **A verification result is not a correctness proof.** It reports how the tests that actually ran
  behaved, on one commit, in one runner profile. Passing tests can coexist with a wrong patch, and the
  comparison is not protection against a patch deliberately engineered to forge results.
- **One runner profile: Python plus pytest, no repository dependencies.** Repository dependency
  installation and setup scripts are never run, because running them means executing repository code
  on our terms rather than none. A project needing more is reported as an `environment_limitation`.
- **Repositories using symlinks, hardlinks, or device entries in their archive are refused.** Links
  are rejected rather than validated.
- **The source tree is mounted read-only**, so a suite that writes into its own directory reports an
  environment limitation. Tests must use `tmp_path` for scratch space.
- **Local container isolation, not a multi-tenant security boundary.** No network, non-root,
  no capabilities, read-only root, and resource ceilings — but a container escape is still an escape.
  Do not point this at repositories you have reason to distrust.
- **One verification per patch attempt, and no retries.** A manual `verify` or `propose` killed
  abruptly leaves its row `running` forever; recovery is not implemented. Create a new run.
- **An orchestrator reconciles its children, but nothing reconciles the orchestrator.** If the
  `orchestrate` process itself is killed with SIGKILL (or the machine crashes), its orchestration and
  unfinished attempts stay `running`/`queued`, and labelled containers may outlive it — find them with
  `docker ps --filter label=branchforge.orchestration=<id>`. Durable recovery is out of scope.
- **The concurrency limit is per orchestrator process, not global.** Two orchestrators for two runs
  each run up to their own limit. There is no scheduler or shared capacity pool.
- **Competing attempts are not guaranteed to differ.** They share the issue, report, commit, model,
  and budgets, and differ only in a short recorded investigation emphasis. There is no context sharing
  between attempts.
- **The comparison is conservative by design.** Only `fix_demonstrated` evidence tied to the exact
  patch and commit is eligible; `partial_fix` is shown but never recommended. The tie-breaker (fewer
  changed lines, then attempt number) is a preference, not evidence of better code. The saved
  comparison is not recomputed if someone later runs a manual `verify --attempt-id`.
- **No SSE.** The dashboard polls a lightweight endpoint every 2s while a job is active and stops at
  a terminal state; manual Refresh remains. Streaming updates are out of scope.
- **A queued job needs a dispatcher.** There is a queue, but no cron-style scheduling and no
  supervisor that starts a dispatcher for you. If none is running, jobs wait indefinitely.
- **No context compaction.** If the conversation would exceed the input budget the attempt stops with
  an explanation rather than dropping history.
- **The patch is never applied to your checkout.** It is applied only inside runner-owned temporary
  workspaces, which are deleted afterwards.
- **The repository is never cloned and its code never runs on the host.** Inspection reads through the
  GitHub API; verification extracts a source archive and executes it only inside a container.
  Repository content is treated as untrusted data everywhere: stored as text, displayed as plain text,
  never evaluated or followed as instructions.
- **A run can be started once, and never restarted.** `execution_jobs.run_id` is `UNIQUE` and retries
  are not implemented, so a cancelled or failed run is finished. Everything it produced is kept and
  still shown; create a new run to try again.
- **Cancelling during inspection fails the run.** The inspector installs no SIGTERM handler, so it
  dies mid-flight; the dispatcher then records the run as `failed` with a reason rather than leaving
  it looking as though something is still inspecting it. Inspection is not resumable.
- **A killed dispatcher stops the queue until a person intervenes.** Holding the lock proves no
  dispatcher is alive; it proves nothing about the processes and containers the dead one started. So
  a job found `running` at startup is **not** replayed and **not** declared stopped — either would be
  a claim we cannot support. It is flagged `recovery_required`, annotated once (repeated restarts do
  not duplicate the note), shown as such in the dashboard, and the dispatcher refuses to launch
  further work, exiting `10`. **Manual recovery:** check for survivors with
  `docker ps --filter label=branchforge.orchestration=<id>` and `ps -ef | grep run-attempt`, remove or
  kill what you find, then decide what the job's row should say. Durable automatic recovery from an
  abrupt crash is out of scope.
- **A worker killed outside the queue still leaves a run stuck in `inspecting`.** The dispatcher
  reconciles the children *it* started; a hand-run `inspect` killed abruptly is not reconciled by
  anything.
- **One inspection per run.** Only a `pending` run can be claimed, so a run cannot be re-inspected,
  including after a failure. Create a new run instead.
- **The Python/pytest assessment is a heuristic** based on filenames and configuration text. A project
  may use pytest without declaring it, and missing configuration is not evidence that pytest is
  unsupported. The report always ships the filenames behind its conclusions.
- **Unauthenticated GitHub access only**, so roughly 5 inspections per hour and public repositories
  only — see [GitHub rate limits](#github-rate-limits). The source archive download does not count
  against the REST allowance.
- **No in-app authentication or authorization.** The FastAPI app itself has no login, sessions, or
  per-user access control — every caller who reaches it sees every run and can act as every other
  caller. `deploy/` documents putting a single shared password (HTTP Basic Auth, in Caddy) in front
  of the whole app for a small trusted group; that is a reverse-proxy gate, not application accounts
  or multi-tenancy. Without such a gate in front of it, do not expose this beyond localhost.
- **SQLite, with a handful of local writers.** An orchestrator and its children are several processes
  writing short transactions to one SQLite file, relying on SQLite's busy timeout (30s); that is
  adequate for three attempts on one machine and not a design for many concurrent orchestrations.
  The API and the migrations also run on Postgres (checked against Postgres 17), which is enough for
  a view-only deployment such as Vercel plus Supabase with `DISPATCHER_AVAILABLE=false`. Execution is
  still SQLite-only: the dispatcher refuses any other database, and the workers are untested on
  Postgres. `deploy/` documents one
  supported way to run this on a single Ubuntu host (systemd units plus a Caddy reverse proxy); it
  does not change the SQLite/single-writer-process model described above.
- **The run list is a single bounded page** — no pagination cursors, filtering, or search.
- **Frontend coverage is type checking, a production build, and a Playwright tier.**
  `tests/test_browser_dashboard.py` drives the real stack — real API, production frontend build, real
  dispatcher — through Start, duplicate clicks, self-updating progress, polling stopping at a terminal
  state, switching runs mid-poll, and a failed Start surfacing in the page. All six pass against a
  real Chromium. It is marked `browser` and **skips cleanly** when Playwright is absent, exactly as
  the `docker` tier does; it is never faked into passing — with Playwright missing pytest exits `5`
  ("no tests ran"), which is a skipped tier and not a pass. `playwright==1.60.0` is a dev dependency
  (pinned to match the cached chromium-1223 build). There are still no component-level unit tests,
  and these tests assert against copy written alongside the components, so they check that the flow
  works rather than independently pinning the wording.
  Earlier milestone reports describe a server-side render check that asserted hostile strings are
  escaped and that "verified" never appears; that script is **not in this repository**, so it remains a
  historical, non-reproducible check.
