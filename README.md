# BranchForge

BranchForge investigates GitHub issues by running competing agent-generated fixes in isolated
environments and presenting verified patches.

**This repository currently contains milestones 1-3: run intake, read-only repository inspection,
and one bounded patch-proposal agent.** A run is stored as `pending`; one worker command inspects the
repository through the GitHub API (`ready`/`failed`); a second runs a single agent that reads the
issue and the inspection, requests further files, and proposes a patch.

**Any proposed patch is unverified.** It is checked only for valid unified-diff syntax. BranchForge
never applies it, compiles it, or runs tests, and never clones the repository or executes repository
code. Competing parallel attempts and sandboxed verification are not implemented. Nothing is
scheduled automatically and no progress or results are simulated.

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
- [Configuration](#configuration)
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

## The workflow

Three steps. Nothing is automatic — you run the worker yourself.

```bash
# 1. Create a run (dashboard at http://localhost:5173, or the API directly)
curl -X POST http://localhost:8000/api/runs \
  -H 'Content-Type: application/json' \
  -d '{"repository_url":"https://github.com/pallets/itsdangerous",
       "issue_description":"Check whether the signer handles empty payloads.",
       "max_parallel_attempts":1}'
# -> {"id":"64fbb850-...","status":"pending", ...}

# 2. Inspect it with the worker, from backend/
cd backend
uv run python -m app.worker inspect --run-id 64fbb850-784e-44fa-8c73-9e0dce94c339

# 3. Propose a patch (needs ANTHROPIC_API_KEY)
uv run python -m app.worker propose --run-id 64fbb850-784e-44fa-8c73-9e0dce94c339

# 4. Refresh the run's detail view in the dashboard (or GET /api/runs/<id>)
```

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

Two processes, one per terminal.

**Terminal 1 — backend** (from `backend/`):

```bash
cd backend
uv run uvicorn app.main:app --reload --port 8000
```

- API: <http://localhost:8000>
- Interactive docs: <http://localhost:8000/docs>

**Terminal 2 — frontend** (from `frontend/`):

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
uv run pytest
```

Each test runs against its own temporary SQLite database, built by running the **real Alembic
migration** rather than `create_all`, so a broken migration fails the suite. Coverage includes the
create/retrieve round trip, URL and description validation, attempt bounds, 404s, list ordering and
limits, UTC timestamp round-tripping, and migration upgrade/downgrade.

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
uv run alembic current            # -> 0001 (head)
uv run alembic downgrade base     # exercises downgrade()
uv run alembic upgrade head

unset DATABASE_URL
```

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

`GET /api/runs/{run_id}` returns the run with nested `inspection` and `patch_attempt` objects, each
`null` until the corresponding worker command has run. The list endpoint deliberately omits both —
including them would make a list response unbounded. The detail response stays bounded because the
workers' budgets bound what they write, and the attempt's event list is capped as well
(`events_total` reports how many exist).

A run's own `status` reflects **inspection only**: it stays `ready` whether a patch attempt succeeds
or fails. Attempt state lives on `patch_attempt.status` (`running`, `succeeded`, `failed`).

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
| `AGENT_MAX_FILE_BYTES` | 60,000 | One `read_file` |
| `AGENT_MAX_TOTAL_FETCHED_BYTES` | 200,000 | All reads combined |
| `AGENT_MAX_OUTPUT_TOKENS` | 16,000 | Output per model call |
| `AGENT_MODEL_CONTEXT_TOKENS` | 1,000,000 | The model's context window |
| `AGENT_CONTEXT_SAFETY_MARGIN_TOKENS` | 8,000 | Held back from that window |
| `AGENT_MAX_PATCH_BYTES` | 60,000 | Submitted diff |
| `AGENT_MAX_EVENTS` | 200 | Stored events per attempt |

**Context is measured, not estimated.** Before every generation the worker calls the provider's
token-counting endpoint with the system prompt, the tool definitions, and the full message history.
The usable input budget is the context window minus the output reservation minus the safety margin —
976,000 tokens at the defaults. If a request would exceed it the attempt **stops with an
explanation**; conversation history is never silently discarded, because context compaction is not
implemented yet.

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

## Configuration

Nothing reads the environment directly except `backend/app/config.py`. Copy the example files and
edit them; neither contains secrets.

### `backend/.env.example`

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./branchforge.db` | SQLAlchemy URL, shared by the app and Alembic |
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
| `ANTHROPIC_API_KEY` | *(unset)* | Required by `worker propose`. Never commit a real key |
| `ANTHROPIC_MODEL` | `claude-opus-5` | A model your account can access |
| `AGENT_*` | see above | Agent budgets — [Model configuration and budgets](#model-configuration-and-budgets) |

The dashboard requests 25 runs per list call, so keep `RUN_LIST_MAX_LIMIT` at 25 or above —
lowering it below that makes every list request fail validation with a 422.

### `frontend/.env.example`

| Variable | Default | Purpose |
|---|---|---|
| `VITE_API_BASE_URL` | `http://localhost:8000` | Backend base URL used by the browser |

If you change the frontend's port or host, add its origin to `CORS_ORIGINS`.

## Project layout

```
backend/
  app/
    main.py          FastAPI app factory; CORS wired from configuration
    config.py        Pydantic-settings; the only reader of the environment
    database.py      Engine, session factory, declarative Base, get_db dependency
    models.py        SQLAlchemy Run model
    schemas.py       Request/response schemas; UTC timestamp serialization
    validators.py    GitHub repository URL validation and normalization
    repository.py    All database queries — no FastAPI imports
    worker.py        Worker CLI (inspect, propose); owns transaction boundaries
    github_client.py Bounded read-only GitHub client; typed errors
    inspection.py    File selection, pytest heuristic, report building
    model_client.py  The model boundary: ModelTurn, protocol, Anthropic adapter
    agent.py         Controller loop, tool dispatch, budgets, prompt building
    patch_validation.py  Unified-diff syntax, hunk counts, path safety
    routers/
      health.py      GET /api/health
      runs.py        Run endpoints; thin handlers delegating to repository
    time_utils.py    UTC helpers
  alembic/
    env.py           Resolves the URL from DATABASE_URL
    versions/        0001_create_runs_table.py, 0002_create_inspections_table.py,
                     0003_create_patch_attempts.py
  tests/
    conftest.py      Temporary migrated database, dependency override, TestClient
    test_validators.py, test_runs_api.py, test_migrations.py,
    test_worker.py (mocked GitHub), test_claim.py (claim contention),
    test_agent.py (scripted model), test_attempt_claim.py (contention + leakage)

frontend/src/
  App.tsx            Page composition and all data fetching
  api.ts             Fetch wrapper; normalizes 422 and 404 error shapes
  types.ts           Types mirroring the backend schemas
  time.ts            Absolute and relative timestamp formatting
  components/        NewRunForm, RunList, RunDetail, InspectionPanel,
                     PatchAttemptPanel, StatusBadge, Callout, NotImplementedNote
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
- **An attempt is claimed by inserting its row.** `patch_attempts.run_id` is `UNIQUE`, so the INSERT
  *is* the atomic claim: the loser of a race catches `IntegrityError` and exits having made zero
  model calls and zero GitHub requests. One mechanism satisfies both "atomic claim" and "one attempt
  per run, enforced in the database".
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
  works — nothing runs `git apply`.

## Limitations

Everything below is deliberately out of scope for milestone 1.

- **A proposed patch is unverified.** It is checked for unified-diff syntax and path safety only.
  Nothing applies it, compiles it, installs dependencies, or runs a test suite — so it may not apply
  cleanly and may not fix the issue. The suggested test command is text for a human to run.
- **One attempt per run, and no retries.** A failed attempt cannot be re-run; create a new run.
  Abrupt interruption can leave an attempt `running` forever, since recovery is not implemented.
- **One agent, not competing attempts.** `max_parallel_attempts` is still stored and unused; parallel
  agents and result comparison come later.
- **No context compaction.** If the conversation would exceed the input budget the attempt stops with
  an explanation rather than dropping history.
- **The repository is never cloned and its code is never executed.** Everything is read through the
  GitHub API. Repository content is treated strictly as untrusted data: it is stored as text and
  displayed as plain text, never evaluated, imported, or followed as instructions.
- **Nothing is scheduled.** You run the worker yourself, per run, and refresh the page. There is no
  queue, poller, or background scheduler.
- **An interrupted worker leaves a run stuck in `inspecting`.** If the process is killed mid-
  inspection, nothing resets the run and it cannot be claimed again. Durable recovery (a lease with
  a timeout, or a reset command) is deliberately out of scope for this milestone.
- **One inspection per run.** Only a `pending` run can be claimed, so a run cannot be re-inspected —
  including after a failure. Create a new run instead.
- **The Python/pytest assessment is a heuristic** based on filenames and configuration text. A
  project may use pytest without declaring it, and missing configuration is not evidence that pytest
  is unsupported. The report always ships the filenames behind its conclusions.
- **Unauthenticated GitHub access only**, so roughly 5 inspections per hour and public repositories
  only — see [GitHub rate limits](#github-rate-limits).
- **No authentication or authorization.** Every caller sees every run. Do not expose this beyond
  localhost.
- **SQLite and single-process only.** No connection pooling for concurrent writers, no Postgres
  configuration, no deployment tooling. `DATABASE_URL` is the seam where that changes.
- **`status` has only one value** (`pending`). The enum will grow when execution exists.
- **The run list is a single bounded page** — no pagination cursors, filtering, or search.
- **No automated frontend tests.** The frontend is covered by type checking and a production build.
- **No authentication on the API**, so anyone who can reach it can read every run. Keep it on
  localhost.
