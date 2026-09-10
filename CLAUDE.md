# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project scope — read this first

BranchForge will eventually investigate GitHub issues by running competing agent-generated fixes in
isolated environments. **Milestones 1-4 exist: run intake, read-only repository inspection, one
bounded patch-proposal agent, and containerised verification of the proposed patch.** A run is stored
`pending`; `worker inspect` moves it to `ready`/`failed`; `worker propose` runs one agent that
proposes a patch; `worker verify` applies that patch to a throwaway copy of the inspected commit and
runs the repository's own tests before and after, in Docker.

**`ready` means the inspection finished.** A patch attempt's `succeeded` means a diff was produced,
not that it works.

**A verification result is NOT a correctness proof**, and the wording everywhere must keep that
distinction. It reports how the tests that actually ran behaved, on one commit, in one runner
profile. There is deliberately no outcome value, badge, or copy that says "verified".

Deliberately absent — do not add these while working on unrelated tasks:
repository dependency installation, running repository setup scripts or Dockerfiles, executing the
model's `suggested_test_command`, non-Python runner profiles, applying patches to the user's
checkout, competing parallel agents, context compaction, retries, crash recovery for stuck
runs/attempts/verifications, scheduling or polling, authentication, deployment tooling.

**Never add simulated agent activity or fabricated results** — no fake progress bars, spinners
implying work that isn't happening, placeholder attempt rows, or invented patch output. Never
fabricate token counts or costs: `input_tokens`/`output_tokens` come from the provider and stay
`NULL` when unavailable. The UI's honesty about what does not work yet is load-bearing, concentrated
in `frontend/src/components/NotImplementedNote.tsx`. When a milestone changes what is true, that text
is a **correctness** change, not copy editing: milestone 2 falsified "does not contact GitHub", and
milestone 3 falsified "does not call any AI model". Keep it, the README scope paragraph, and this
section in sync.

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

# Workers (separate processes; same DATABASE_URL as the API)
uv run python -m app.worker inspect --run-id <UUID>   # pending -> ready | failed
uv run python -m app.worker propose --run-id <UUID>   # needs ANTHROPIC_API_KEY
uv run python -m app.worker verify  --run-id <UUID>   # needs Docker + the runner image
```

The runner image must exist before `verify`. Build it from the repo root:

```bash
docker build --provenance=false -t branchforge-runner-python:1 backend/runner/python-pytest
```

`--provenance=false` keeps it a plain single-platform image. Without it buildx adds attestation
manifests, and with Docker Desktop's containerd image store `docker image inspect` then intermittently
reports "No such image" for a tag `docker images` lists — which is why `resolve_image_id` falls back to
`docker image ls -q`. Do not remove that fallback.

`inspect` exit codes: `0` ok, `1` failed (recorded on the run), `2` no such run, `3` not claimable.
`propose` exit codes: `0` ok, `1` attempt failed (recorded on the attempt), `2` no such run, `3` the
run already has an attempt, `4` the run is not `ready`, `5` model not configured (**no attempt row is
created**).

`verify` exit codes: `0` the verification **ran** — whatever it found, including "the patch fixes
nothing" — `1` the verification itself failed (recorded on the row), `2` no such run, `3` already
verified, `4` no succeeded attempt with a diff and a commit SHA, `6` Docker unavailable (**no
verification row is created**). Do not "fix" outcome-is-bad-news into a non-zero exit: the exit code
reports whether the check ran, not what it concluded.

Each step happens at most once per run.

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

### Test suite shape

272 tests. All offline except the 15 marked `docker`, which build real containers.

```bash
uv run pytest -m "not docker"    # no daemon needed
uv run pytest -m docker          # only the real container scenarios
```

Three mocking boundaries. Reuse them rather than inventing a fourth:

- **GitHub** is mocked at the *transport* layer (`FakeGitHub` in `tests/conftest.py` →
  `httpx2.MockTransport`), so real URL construction, byte caps, and status handling execute.
- **The model** is mocked at the *interface* layer (`ScriptedModelClient`), which replays canned
  `ModelTurn`s and records the message history it was sent — use `.seen_messages` and
  `.last_tool_results()` for assertions.
- **The container runner** is mocked at the *callable* layer: `run_verification(test_runner=...)`
  takes anything with the `TestRunner` signature, so orchestration tests script one `RunResult` per
  workspace phase (`baseline` / `comparison` / `patched_full`). Snapshot acquisition is injected the
  same way, and `git apply` plus test-environment restoration stay **real** in those tests.

`tests/test_docker_runner.py` marks itself `docker` and skips when the daemon or image is missing —
**never** relax that into a fake pass. It is the only evidence the isolation flags take effect, and it
asserts from inside the container that `os.getuid() == 65534`, that `/workspace` is unwritable, and
that sockets fail.

`tests/test_runner_argv.py` needs no daemon and asserts the argv both positively (every isolation
flag) and negatively (no credential, no `docker.sock`, no host home, no database, exactly one source
mount). Write argv assertions there, not in the Docker tier — they should hold even when Docker does
not exist.

Scenario patches live in `tests/fixture_support.py` and are generated with `difflib`, so hunk headers
are correct by construction; do not hand-write diffs (an earlier hand-written one was rejected by our
own validator for a miscounted hunk). `tests/sample_repo/` is a fixture *repository* with a
deliberately failing test, excluded from our own run by `collect_ignore_glob` in `tests/conftest.py`.

**Never add a test that reaches the network or spends API credit.** Live checks are manual commands
documented in the README. Contention tests use a barrier plus *independent* engines against one
temporary file database (`tests/test_claim.py`, `tests/test_attempt_claim.py`,
`tests/test_verify_worker.py`); that pattern has teeth — a naive read-then-write claim produces two
winners under it.

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

### The agent loop (milestone 3)

`app/agent.py` is a hand-written controller loop, deliberately **not** the SDK's tool runner, because
the Python side must validate and dispatch every tool call. Invariants to preserve:

- **Two tools only**: `read_file(path)` and `submit_patch(diff, summary, suggested_test_command)`.
  The commit SHA comes from the persisted inspection — the model cannot change which commit is read,
  which is what keeps the inspection immutable. Paths reuse `github_client`'s traversal rejections.
- **`submit_patch` must be the only call in its response.** A response mixing it with reads, or
  containing two submissions, is rejected wholesale with one `is_error` tool_result **per tool_use
  id**, and the model may correct itself within remaining budgets. Ordinary read batches also return
  one result per id, all in a single user message.
- **A `max_tokens` response is rejected before its tool calls run** — the arguments may be truncated.
- **Never mark an incomplete conversation successful.** Budget exhaustion, provider failure, refusal,
  and "ended without a patch" are all recorded failures with their own `error_kind`.
- **Context is measured, never estimated**: `count_input_tokens` (behind the model interface, so
  tests need no network) is called before every generation with system + tools + full history. Over
  budget ⇒ terminate with an explanation. **Never drop or summarize messages** — compaction is a
  later milestone.
- **Cache reads per attempt**, seeded from inspection previews that are *complete*; a truncated
  preview is re-fetched rather than served as if whole.
- Events record operational facts only — never model reasoning, and never thinking-block content,
  even though thinking blocks are echoed back in the conversation verbatim.

`app/patch_validation.py` runs even though the tool schema is `strict: true`: a schema constrains
argument shape, not diff validity. It checks hunk line counts, rejects absolute/`..` paths, and
refuses binary patches, renames, and copies. **Passing validation is not evidence the patch applies
or works** — keep the unverified framing everywhere.

### Anthropic SDK facts (verified against the live API, not recalled)

**Before changing anything that calls the model, load the `claude-api` skill** rather than working
from memory — this API has drifted repeatedly (thinking config, structured outputs, prefill removal).
The notes below were confirmed by real calls in this repository and are safe to rely on:

- `anthropic` 1.x is built on **`httpx2`**, not `httpx`; `anthropic.Timeout` *is* `httpx2.Timeout`.
- Echoing assistant turns back as `[block.model_dump() for block in response.content]` **is accepted**
  on replay, thinking blocks included. That is why `ModelTurn.assistant_content` holds dicts.
- `client.messages.count_tokens(model=, system=, tools=, messages=)` works and returns
  `.input_tokens`. This is the measured context accounting the agent relies on.
- `claude-opus-5` reports `max_input_tokens=1,000,000` and `max_tokens=128,000` via
  `client.models.retrieve(...)`, which is where `AGENT_MODEL_CONTEXT_TOKENS` comes from.
- Thinking is **adaptive by default** on this model with `display: "omitted"`, so `thinking` blocks
  arrive with empty text and must still be echoed back unchanged. Do not persist their content.
- **Use the exact model ID string** (`claude-opus-5`). Never append a date suffix, and never invent an
  identifier — `ANTHROPIC_MODEL` exists so the user picks.
- `stop_reason` values the loop handles: `tool_use`, `end_turn`, `max_tokens`, `refusal`.

### Verification (milestone 4)

`worker verify` is the only code path that executes repository code, and it executes it **only** inside
`docker run`. Three modules, each with one job:

- **`app/snapshot.py`** — downloads the source archive of one exact commit from
  `codeload.github.com` and extracts it. Extraction is the trust boundary.
- **`app/runner.py`** — builds the `docker run` argv, runs it, captures bounded output, cleans up.
- **`app/verification.py`** — orchestrates baseline → apply → comparison, and owns the outcome
  vocabulary.

#### A tarball, not a clone — and why

An extracted archive has **no `.git` directory**, so hooks, clean/smudge filters, submodule setup, and
repository-supplied git config cannot exist *by construction* rather than needing to be switched off
one flag at a time. `git apply` works outside a repository, which is what makes this practical. Do not
replace this with `git clone` to "simplify" it.

Extraction is hand-written because **`tarfile.data_filter` does not exist on Python 3.11.0** (it was
backported in 3.11.4). Do not delete the manual checks assuming the stdlib covers them. They reject:
links (symlink *and* hardlink — refused, never target-validated), devices and FIFOs, `..` traversal,
absolute paths, members outside the archive's own top-level prefix, duplicate normalized paths,
file/directory collisions, and any `.git` entry.

Byte accounting is on the **decompressed** stream (`gzip.GzipFile` wrapped in a counting reader feeding
`tarfile` in `mode="r|"`), so tar headers and padding count too. Summing the file sizes an archive
declares is not a bound — a bomb can declare anything. There is also a file count and an overall
deadline. Extraction discards archive ownership, permissions, and mtimes, then normalizes to 0755
directories and 0644 files, because the container's non-root UID must be able to traverse a read-only
mount.

#### `git apply` hermeticity

Every inherited `GIT_*` variable is stripped, config points at `/dev/null`, and
`GIT_CEILING_DIRECTORIES` stops repository discovery walking up into whatever repository contains the
temp directory. Without this the developer's own git config can change whitespace handling, which makes
the persisted `runner_args` non-reproducible. Never pass `--unsafe-paths`.

#### Container invariants

`build_run_argv` is an argument **array**; nothing from the repository, the model, or the issue text
reaches it. The flags are not decoration — `tests/test_runner_argv.py` asserts each one, so removing
any of them fails a test:

`--network=none`, `--user 65534:65534`, `--read-only`, a bounded `--tmpfs` for `/tmp`,
`--memory/--cpus/--pids-limit`, `--cap-drop=ALL`, `--security-opt=no-new-privileges`,
`--log-driver=none`, `--rm` plus a `--name` for forced removal.

Three easy mistakes, all already handled:

- **`-e HOME=/tmp` is load-bearing.** `--read-only` plus a non-root UID breaks before collection
  without a writable HOME.
- **Mount paths must be `realpath`'d.** macOS `mkdtemp()` returns `/var/folders/...`; Docker Desktop
  only shares the `/private/var/folders/...` it resolves to.
- **`--log-driver=none` is separate from our own cap.** Bounding the buffer we read does not bound the
  daemon's log file.
- **The reader keeps draining after the retention cap.** Stopping would fill the pipe and block the
  container, turning an output cap into a hang.

`-o addopts=` is what makes the pytest invocation genuinely runner-owned: without it a repository's ini
`addopts` could inject plugins into our command line. The image **ID** is resolved once up front and
that exact ID runs all phases, so a moving tag cannot make the phases incomparable.

#### Structured results, not parsed output

`runner/python-pytest/bf_report.py` is a runner-owned pytest plugin baked into the **image** (not the
workspace) and loaded via `-p bf_report` from a PYTHONPATH entry that precedes the repository, so a
repository module cannot shadow it. It writes collected node IDs and an explicit per-test outcome
across setup/call/teardown to a runner-owned `/results` mount.

This exists because terminal output cannot distinguish a test that was **fixed** from one that was
skipped, deselected, renamed, or never collected — and that distinction is the entire point. Do not
replace it with output parsing.

#### The four rules that decide a fix

Changing any of these changes what BranchForge claims, so change them deliberately:

1. **Collected identities must match.** The comparison run must collect exactly the baseline's node
   IDs, else `collection_mismatch`.
2. **A fix requires an explicit `passed`** for that same node ID. Skipped, xfailed, deselected, and
   missing are recorded in `no_longer_exercised` and are *not* fixes.
3. **A previously passing test that stops running** is a weakened yardstick ⇒ `inconclusive`, never a
   fix.
4. **A missing, malformed, oversized, or truncated report is an error**, never an absence of
   failures. The first three fail in `runner.read_report`; the fourth reaches `compare_runs`, because
   the plugin caps itself at `BF_REPORT_MAX_TESTS` and sets `truncated`. **That flag must stay wired
   into the verdict** — it was persisted but unread once, which meant a suite over the cap could have
   two partial reports compare equal and yield `fix_demonstrated`.

And the asymmetry that matters most: **a patched-only collection or import failure is
`patched_collection_error`, never an improvement** — the failing set went empty because nothing ran.
This is verified live against a real repository, not only in mocks.

The general shape of every rule above: an absence of evidence is never evidence of a fix. When adding
a new code path here, ask what it does when the data is *missing* rather than merely bad.

#### Four workspaces, and why `comparison` is not `patched_full`

`snapshot/` (pristine, never mounted) → `baseline/`, `patched_full/` (patch applied, untouched),
`comparison/` (patched source + **original** test environment restored).

`restore_original_test_environment` copies the original tests, every `conftest.py`, fixture data, and
collection config back over the patched tree **and deletes test-environment files the patch added**.
Both directions are needed: restoring alone would leave a patch-added `conftest.py` free to change
collection.

Supplemental runs of the patch's own tests use `patched_full` and persist to their own columns. Two
properties to preserve: a supplemental failure or timeout must **never** overwrite or take down the
baseline-versus-patched verdict (its errors are swallowed into `notes` on purpose), and it runs on
**both** exit paths — including `tests_only_patch`, where the patch changed no source and its own
tests are therefore the only evidence that exists. `run_supplemental()` is a closure called twice for
exactly that reason; do not inline it back into one branch.

#### Ordering rules

Docker availability and the image ID are resolved **before** the claim, so a stopped daemon cannot
leave a claimed verification stranded — the same rule as checking model configuration before claiming
an attempt. An unusable baseline short-circuits before the comparison container runs. No session is
held open across a download or a container run.

## Frontend rules

- **Never `dangerouslySetInnerHTML`, and no markdown-to-HTML library.** Repository text renders as
  plain `{content}` inside `<pre>`; React escapes it. A "render the README nicely" request must not
  change this.
- Collapsible previews use native `<details>`/`<summary>` — no JS, accessible by default.
- There is no polling. Refresh is a manual button, because nothing schedules the workers.
- The diff is model output: render it as plain text. `PatchAttemptPanel` classifies lines for colour
  by inspecting the first character and emitting React elements — never by generating HTML. Do not
  add a syntax-highlighting or markdown library here.
- **"Unverified patch — not applied or tested" must stay adjacent to the diff**, not in a footer.
- The suggested test command is display-only. Do not add a run button or anything that reads as
  executable.
- **`VerificationPanel` must never render a pass/fail badge.** The outcome is stated in words from
  `OUTCOME_LABELS`, and only `fix_demonstrated` / `partial_fix` are styled as good news. The word
  "verified" does not appear in the panel, and an SSR check asserts that.
- Container logs are untrusted program output: plain text in `<pre>`, inside `<details>`. No
  highlighting, no HTML.
- The scope sentence next to the outcome (which commit, which profile, "not a proof of correctness")
  is part of the result, not decoration.

## Constraints on dependencies

- **`httpx2` is httpx v2**, and installs under the module name `httpx2` — which is why imports read
  `import httpx2` and `import httpx` fails. Three things want it: the GitHub client, Starlette's
  `TestClient`, and `anthropic` 1.x (whose `anthropic.Timeout` *is* `httpx2.Timeout`). Do not add the
  older `httpx` package alongside it.
- **`anthropic` SDK**: retries are disabled (`max_retries=0`) so a transparent retry cannot re-spend
  an attempt's budget. The request timeout is per HTTP request, **not** a whole-attempt deadline.
- **Credentials**: the key is a `SecretStr` in settings, read from `ANTHROPIC_API_KEY`. Never log it,
  persist it, or return it. Leakage tests use a synthetic sentinel value — never the real key.

- **Docker is required only by `worker verify`.** Everything else — the API, `inspect`, `propose`, and
  every test except the `docker`-marked ones — runs without it. Keep it that way: do not make the API
  or the test suite depend on a daemon.
- **The runner image pins pytest** (`ARG PYTEST_VERSION`) so a verification is reproducible. The image
  deliberately contains nothing else; adding repository dependencies to it would defeat the point of
  the profile.

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

## Three status enums, each spanning four places

All are **literal unions** in `frontend/src/types.ts`, so a new value arriving from the API against a
stale type is a runtime mismatch `tsc` cannot catch. Update all four places together.

| Enum | Values | Defined in | Mirrored in | Styled in |
|---|---|---|---|---|
| Run | `pending → inspecting → ready \| failed` | `models.py` `RUN_STATUS_*`, `schemas.py` `RunStatus` | `types.ts` `RunStatus` | `StatusBadge.tsx` |
| Attempt | `running → succeeded \| failed` | `models.py` `ATTEMPT_STATUS_*`, `schemas.py` `AttemptStatus` | `types.ts` `AttemptStatus` | `PatchAttemptPanel.tsx` |
| Verification | `running → completed \| failed` | `models.py` `VERIFY_STATUS_*`, `schemas.py` `VerificationStatus` | `types.ts` `VerificationStatus` | `VerificationPanel.tsx` |

All three are independent on purpose — see the note under Database schema.

**A verification's `outcome` is a fourth vocabulary and is deliberately NOT an enum type.** It is a
plain string column, defined once in `app/verification.py` as `OUTCOME_*` constants with
`OUTCOME_DESCRIPTIONS`, and labelled for display in `VerificationPanel.tsx`'s `OUTCOME_LABELS`. Adding
an outcome means touching those two places. Never add one called `verified`, and never collapse the
set into a boolean.

## Database schema

Four migrations: `0001` (runs), `0002` (inspections), `0003` (`patch_attempts` + `attempt_events`),
`0004` (`verifications`). Every child FK is `ON DELETE CASCADE`. Three columns are `UNIQUE`, and in each
case the uniqueness **is** the claim mechanism rather than merely a constraint:
`inspections.run_id`, `patch_attempts.run_id`, and `verifications.attempt_id`.

Note the parent differs: a verification hangs off the **patch attempt**, not the run, because it
verifies a specific proposed patch. Deleting a run therefore cascades runs → attempts → verifications.

`app/database.py` sets `PRAGMA foreign_keys=ON` for every SQLite connection via an `Engine` `"connect"`
listener, so foreign keys are genuinely enforced for the API, the workers, and the tests.
`tests/test_migrations.py` asserts all of it, including that a verification cannot reference a missing
attempt.

**The three statuses are independent, and this is asserted.** `Run.status` describes inspection only;
`PatchAttempt.status` describes whether a diff was produced. A run stays `ready` and an attempt stays
`succeeded` however badly a verification turns out — **no verification code path may write `Run.status`
or `PatchAttempt.status`.**
