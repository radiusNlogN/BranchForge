# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project scope — read this first

BranchForge will eventually investigate GitHub issues by running competing agent-generated fixes in
isolated environments. **Milestones 1 and 2 exist: run intake, and read-only repository
inspection.** A run is stored `pending`; a separate worker claims it and inspects the repository
through the GitHub API, ending at `ready` or `failed`.

**`ready` means the inspection finished — not that a fix exists.**

Deliberately absent — do not add these while working on unrelated tasks:
LLM/agent integration, patch generation, test execution, repository cloning or code execution,
sandboxing, scheduling or polling, crash recovery for stuck runs, authentication, deployment
tooling.

**Never add simulated agent activity or fabricated results** — no fake progress bars, spinners
implying work that isn't happening, placeholder attempt rows, or invented patch output. The UI's
honesty about what does not work yet is load-bearing, concentrated in
`frontend/src/components/NotImplementedNote.tsx`. When a milestone changes what is true, that text
is a **correctness** change, not copy editing: milestone 2 made the old "does not contact GitHub"
claim false. Keep it, the README scope paragraph, and this section in sync.

## Commands

Run all backend commands from `backend/` — the default `DATABASE_URL` is a relative SQLite path that
resolves against the launch directory.

```bash
# Backend setup
cd backend
uv sync
uv run alembic upgrade head          # creates backend/branchforge.db

# Backend dev server → http://localhost:8000 (docs at /docs)
uv run uvicorn app.main:app --reload --port 8000

# Backend tests (no network: GitHub is mocked at the transport layer)
uv run pytest
uv run pytest tests/test_runs_api.py                                  # one file
uv run pytest tests/test_runs_api.py::test_unknown_run_id_returns_404  # one test
uv run pytest -k "validator and reject"                                # by expression

# Inspection worker (separate process; same DATABASE_URL as the API)
uv run python -m app.worker inspect --run-id <UUID>
```

Exit codes: `0` inspected, `1` inspection failed (recorded on the run), `2` no such run, `3` not
claimable. Only a `pending` run can be claimed, so a run is inspected at most once.

```bash
# Frontend setup, dev server → http://localhost:5173
cd frontend
npm install
npm run dev

# Type check and production build (tsc failure fails the build)
npm run typecheck
npm run build
```

### Migrations

```bash
cd backend
uv run alembic revision --autogenerate -m "description"
uv run alembic upgrade head
uv run alembic current
```

### Verifying without destroying the dev database

Migration and restart checks must target a throwaway database. Never drop or downgrade
`backend/branchforge.db` to test something:

```bash
cd backend
export DATABASE_URL="sqlite:///$(mktemp -d)/check.db"
uv run alembic upgrade head
uv run alembic downgrade base
unset DATABASE_URL
```

## Architecture

### The worker is a real second process

`app/worker.py` shares `DATABASE_URL`, `app.database`, and `app.repository` with the API but imports
nothing from `app.main` or `app.routers`. Its transaction discipline is the part to preserve:

1. A short session claims the run and **commits**, then closes.
2. All GitHub work happens with **no session open** — never hold a transaction across HTTP.
3. A second short session stores the outcome.

On success the `Inspection` insert and the `ready` status share **one commit**, so a run can never
be `ready` without its report; likewise for a failure and its error. `inspect_run` takes an
injectable `session_factory` (default `SessionLocal`) — the contention tests depend on that to point
two workers at one temporary database.

The claim is a conditional `UPDATE ... WHERE id = ? AND status = 'pending'`, and `rowcount == 1`
means this worker won. `tests/test_claim.py` races two independent engines; a naive read-then-write
claim produces two winners under the same test, so it genuinely has teeth.

### Layering exists so a worker can be added later

`routers/ → repository.py → models.py` is enforced, not incidental:

- **`app/repository.py` holds every SQLAlchemy query** and imports nothing from FastAPI. A future
  worker process imports it directly. Do not write queries in route handlers.
- **Route handlers are thin**: validate via schema, call `repository`, return the response. The only
  `Session` they touch is the one injected by `Depends(get_db)`.
- **`app/config.py` is the only module that reads the environment.** Everything else takes values
  from `settings`. Do not introduce `os.environ` reads elsewhere.

### Handlers are sync `def`, not `async def` — intentionally

SQLAlchemy's `Session` is synchronous. Sync handlers run in FastAPI's worker threadpool, so blocking
queries never stall the event loop. Converting a database handler to `async def` would block it. This
is also why the SQLite engine sets `check_same_thread: False` in `app/database.py` — pooled
connections legitimately cross threads.

### Alembic owns the schema — nothing calls `create_all`

There is no `Base.metadata.create_all()` anywhere, **including the tests**: `tests/conftest.py`
builds each temporary database by running the real migration via `command.upgrade`. This means a
broken migration fails the whole suite rather than being masked. Adding `create_all` for convenience
would silently defeat that, so don't.

`alembic/env.py` resolves the URL from `Settings().database_url` (i.e. `DATABASE_URL`) unless a caller
explicitly sets `sqlalchemy.url` on the config object, which is what the tests do. `alembic.ini`
deliberately has no `sqlalchemy.url` key.

### UTC timestamps need the explicit round-trip handling

SQLite has no timezone-aware datetime type and returns **naive** values even from a
`DateTime(timezone=True)` column. Every write uses `time_utils.utcnow()` (aware UTC), and
`time_utils.as_utc()` restores that awareness when serializing — naive values are assumed UTC, aware
values are converted. Without this, the browser's `new Date(...)` reads a stored UTC value as local
time and shows a wrong "N hours ago". `tests/test_runs_api.py::test_timestamps_survive_the_round_trip_as_the_same_instant`
asserts the *instant* is preserved, not just that the string ends in `Z`; keep that property when
touching timestamps.

### The GitHub client is hardened by signature, not by comment

`app/github_client.py` methods take `owner`, `repo`, `path`, `ref` and **never a URL**, building
every request path from percent-encoded components. That makes "do not follow repository-provided
URLs" a type-level invariant. Keep it that way:

- Ignore `url`, `git_url`, `download_url`, `_links` in GitHub responses — never fetch them.
- Redirects are disabled and treated as an error; their `Location` comes from upstream.
- Branch names go in a **query parameter**, not the path — a branch may contain slashes.
- Commit SHAs are validated (`^[0-9a-f]{40}$`) before being interpolated into a path.
- File paths reject absolute paths and `..` segments.
- Response bodies are capped twice — on a declared `Content-Length` and cumulatively while
  streaming — **before** JSON parsing. Size metadata alone is not a download limit.
- Oversized files are detected from the tree's own `size` and skipped without spending a request.

Repository content is untrusted data: stored as text, rendered as plain text, never executed,
imported, evaluated, or treated as instructions.

### The pytest assessment is a heuristic, and must stay honest

`inspection.py` reports `is_python_project` / `uses_pytest` **with the filenames behind them** plus a
caveat. `tox.ini`, `requirements*.txt`, and `setup.cfg` are supporting evidence only — they never
establish a Python project alone (see `PYTHON_DEFINING_NAMES`). File selection runs over the whole
tree *before* the display-only listing limit, and root-level files beat nested ones, so a README is
never lost to alphabetical truncation.

### URL validation is syntactic only

`app/validators.py` performs **no network access**. Acceptance does not mean the repository exists,
is public, or is reachable — checking that against GitHub is a later milestone, and neither code nor
docs should claim otherwise.

It **normalizes** as it validates: host lowercased, trailing `.git` and `/` stripped, while owner and
repository path case is preserved. So `https://github.com/OctoCat/Hello-World.git/` is stored as
`https://github.com/OctoCat/Hello-World`. Because the UI displays the persisted record, it shows the
normalized form — that is correct behavior, not a bug to "fix".

### Error contract between backend and frontend

Body and query validation failures are Pydantic/FastAPI **422** with a nested
`detail: [{loc, msg, ...}]`; an unknown run id is **404** with a string `detail`. Do not hand-roll
400s to tidy this up.

`frontend/src/api.ts` is the single place that normalizes both shapes into `ApiError` with a readable
`message` plus a `fieldErrors` map keyed by request field name. Components never parse error
payloads. It also strips Pydantic's `Value error, ` prefix. If you add a field, add it to
`FIELD_LABELS` there or its server-side message will not reach the right input.

### Frontend data flow

`App.tsx` owns all fetching and state; components are presentational. After a successful create, the
app selects the new run and the detail pane **re-fetches it from the backend** — the UI shows the
persisted record rather than anything synthesized locally. Preserve that when changing the create
flow.

The submit guard clears in a `finally` block so a failed request cannot leave the form permanently
disabled. Per-field server errors are cleared when the user edits that field (`handleEdit` in
`NewRunForm.tsx`, wired to `dismissFieldError` in `App.tsx`), so stale messages don't linger.

## Frontend rules

- **Never `dangerouslySetInnerHTML`, and no markdown-to-HTML library.** Repository text renders as
  plain `{content}` inside `<pre>`; React escapes it. A "render the README nicely" request must not
  change this.
- Collapsible previews use native `<details>`/`<summary>` — no JS, accessible by default.
- There is no polling. Refresh is a manual button, because nothing schedules the worker.

## Constraints on dependencies

- **`httpx2` is httpx v2**, and installs under the module name `httpx2` — which is why imports read
  `import httpx2` and `import httpx` fails. It is a runtime dependency (the worker) *and* what
  Starlette's `TestClient` wants. Do not add the older `httpx` package alongside it.

- **Vite is pinned to the 6 line on purpose.** Vite 7+ and `@vitejs/plugin-react` 5+ require Node
  `^20.19.0 || >=22.12.0`; this environment has Node 20.10.0. `package.json`'s `engines.node`
  reflects what actually works. Do not "modernize" this pin without first confirming the installed
  Node version, and treat a Vite upgrade as a Node upgrade decision.
- `backend/pyproject.toml` sets `[tool.uv] package = false` (run from source, not installed) and
  `pytest.ini_options.pythonpath = ["."]` so `app` imports resolve.
- Commit both lockfiles (`backend/uv.lock`, `frontend/package-lock.json`) when dependencies change.

## Configuration

`backend/.env.example` and `frontend/.env.example` document every variable; both are placeholder-only
and tracked, while real `.env` files are gitignored.

`DATABASE_URL` is the seam for moving off SQLite. `CORS_ORIGINS` is a comma-separated string parsed by
`Settings.cors_origin_list` — it is stored as a string rather than a list because pydantic-settings
would otherwise require JSON for a list-typed field.

**`RUN_LIST_MAX_LIMIT` must stay ≥ 25**: the dashboard requests 25 runs per list call, so a lower
ceiling makes every list request fail with a 422.

## Status enum touches four places

`pending → inspecting → ready | failed`, defined in `app/models.py` (`RUN_STATUS_*`) and
`app/schemas.py` (`RunStatus`), mirrored in `frontend/src/types.ts`, and styled in
`StatusBadge.tsx`. `types.ts`'s `RunStatus` is a **literal union**, so a new status arriving from the
API against a stale type is a runtime mismatch `tsc` cannot catch. Update all four together.

## Database schema

Two migrations: `0001` (runs), `0002` (inspections, FK to `runs.id` with `ON DELETE CASCADE`, unique
`run_id` — one inspection per run). `app/database.py` sets `PRAGMA foreign_keys=ON` for every SQLite
connection via an `Engine` `"connect"` listener, so foreign keys are genuinely enforced for the API,
the worker, and the tests. `tests/test_migrations.py` asserts that.
