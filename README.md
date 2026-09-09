# BranchForge

BranchForge investigates GitHub issues by running competing agent-generated fixes in isolated
environments and presenting verified patches.

**This repository currently contains milestone 1 only: run intake and persistence.**
A run is validated, given a server-side identity, and stored in the `pending` state. Nothing is
executed. There is no cloning, no GitHub API access, no model calls, no workers, no containers, and
no authentication — and no simulated progress or fabricated results standing in for them.

---

## Contents

- [What works today](#what-works-today)
- [Requirements](#requirements)
- [Setup](#setup)
- [Running locally](#running-locally)
- [Verification](#verification)
- [API](#api)
- [Configuration](#configuration)
- [Project layout](#project-layout)
- [Design notes](#design-notes)
- [Limitations](#limitations)

---

## What works today

- Create a run from the dashboard or the API; the backend validates it and persists it to SQLite.
- Repository URLs are checked to be HTTPS `github.com/<owner>/<repo>` addresses and normalized.
- Runs are listed newest-first with a bounded limit, and can be fetched individually by UUID.
- Data survives backend restarts; the schema is created and owned by an Alembic migration.
- The dashboard shows the actual persisted record it reads back from the API, with loading, empty,
  validation, and error states, and states plainly that runs are never executed.

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
| `GET` | `/api/runs/{run_id}` | Fetch one run. `404` if unknown. |

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
    routers/
      health.py      GET /api/health
      runs.py        Run endpoints; thin handlers delegating to repository
    time_utils.py    UTC helpers
  alembic/
    env.py           Resolves the URL from DATABASE_URL
    versions/        0001_create_runs_table.py — owns the schema
  tests/
    conftest.py      Temporary migrated database, dependency override, TestClient
    test_validators.py, test_runs_api.py, test_migrations.py

frontend/src/
  App.tsx            Page composition and all data fetching
  api.ts             Fetch wrapper; normalizes 422 and 404 error shapes
  types.ts           Types mirroring the backend schemas
  time.ts            Absolute and relative timestamp formatting
  components/        NewRunForm, RunList, RunDetail, StatusBadge, Callout,
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

## Limitations

Everything below is deliberately out of scope for milestone 1.

- **Runs never execute.** They are stored as `pending` and stay there. There is no worker, queue,
  scheduler, or container runtime, and no code path advances a run's status.
- **No GitHub contact of any kind.** URL validation is syntactic; the repository is never fetched,
  so an accepted URL may point at something that does not exist or is private.
- **No LLM or agent integration**, and no simulated activity standing in for it.
- **No authentication or authorization.** Every caller sees every run. Do not expose this beyond
  localhost.
- **SQLite and single-process only.** No connection pooling for concurrent writers, no Postgres
  configuration, no deployment tooling. `DATABASE_URL` is the seam where that changes.
- **`status` has only one value** (`pending`). The enum will grow when execution exists.
- **The run list is a single bounded page** — no pagination cursors, filtering, or search.
- **No automated frontend tests.** The frontend is covered by type checking and a production build.
