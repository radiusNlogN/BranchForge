# Deploying BranchForge to one Ubuntu VM

One Linux host, password-protected, running the app exactly as it behaves locally: Caddy serves the
built frontend and reverse-proxies `/api/*` to FastAPI; FastAPI and the dispatcher run as separate
systemd services; only the dispatcher's service account has Docker access. This is a single-host,
authenticated-demo setup for trusted users — it does not add multi-tenancy, in-app accounts, or a
distributed scheduler, none of which this codebase implements (see `CLAUDE.md`).

Nothing here changes agent, verification, cancellation, or comparison behavior. It only wires the
existing `uvicorn app.main:app` process and the existing `worker dispatch` process into systemd, and
puts Caddy in front of both.

## Layout this guide assumes

| Path | Purpose |
|---|---|
| `/opt/branchforge` | Git checkout (`backend/`, `frontend/`) |
| `/var/lib/branchforge/db/branchforge.db` | The SQLite database, plus its `-wal`/`-shm`/journal siblings and the dispatcher's lock file, all created alongside it |
| `/var/lib/branchforge/workspaces` | Orchestration workspaces (via `TMPDIR`) |
| `/etc/branchforge/api.env`, `/etc/branchforge/dispatcher.env` | Per-service environment files |
| `/etc/caddy/Caddyfile` | Copied from `deploy/Caddyfile` and filled in |

All of these are plain choices, not something the application hardcodes — rename them consistently
across the env files and unit files below if you prefer different paths.

## 1. Prerequisites (clean Ubuntu 24.04 LTS)

```bash
sudo apt-get update
sudo apt-get install -y git ca-certificates curl gnupg

# Docker Engine — official repo (docs.docker.com/engine/install/ubuntu/)
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io

# Node.js 20.x — satisfies frontend/package.json's
# engines.node: "^18.0.0 || ^20.0.0 || >=22.0.0"
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs

# uv — backend/pyproject.toml requires-python is ">=3.11" with no
# .python-version pin; uv resolves a suitable interpreter itself.
curl -LsSf https://astral.sh/uv/install.sh | sh

# Caddy — official repo (caddyserver.com/docs/install#debian-ubuntu-raspbian)
sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt-get update
sudo apt-get install -y caddy
```

## 2. Accounts and directories

```bash
sudo groupadd --system branchforge
sudo useradd --system --gid branchforge --no-create-home --shell /usr/sbin/nologin branchforge-api
sudo useradd --system --gid branchforge --no-create-home --shell /usr/sbin/nologin branchforge-dispatcher
# Only the dispatcher account gets Docker access — branchforge-api never does.
sudo usermod -aG docker branchforge-dispatcher

sudo mkdir -p /opt/branchforge
sudo mkdir -p /var/lib/branchforge/db /var/lib/branchforge/workspaces
sudo mkdir -p /etc/branchforge
sudo mkdir -p /var/log/caddy

sudo chown root:branchforge /var/lib/branchforge/db
sudo chown branchforge-dispatcher:branchforge /var/lib/branchforge/workspaces
sudo chmod 0750 /var/lib/branchforge/workspaces
sudo chown caddy:caddy /var/log/caddy
```

## 3. Get the code and install dependencies

```bash
sudo git clone <this-repo-url> /opt/branchforge
sudo chown -R root:root /opt/branchforge   # services only need to read it

cd /opt/branchforge/backend
sudo uv sync
```

## 4. Environment files

```bash
sudo cp /opt/branchforge/deploy/env/api.env.example /etc/branchforge/api.env
sudo cp /opt/branchforge/deploy/env/dispatcher.env.example /etc/branchforge/dispatcher.env
```

Edit both. At minimum: fill in `CORS_ORIGINS` in `api.env` and `ANTHROPIC_API_KEY` in
`dispatcher.env` with real values. **`DATABASE_URL` must be identical, byte-for-byte, in both
files** — there is no shared config or override setting that keeps them in sync (see the comments in
each file for why).

```bash
sudo chown root:branchforge-api /etc/branchforge/api.env
sudo chmod 0640 /etc/branchforge/api.env
sudo chown root:branchforge-dispatcher /etc/branchforge/dispatcher.env
sudo chmod 0640 /etc/branchforge/dispatcher.env
```

## 5. First migration and database permissions

Run the migration before enabling either service. Running it interactively creates the file with
your shell's umask (typically `0644`, owned by whoever ran the command) — neither service account
can write that, so fix the permissions immediately after:

```bash
cd /opt/branchforge/backend
sudo env DATABASE_URL=sqlite:////var/lib/branchforge/db/branchforge.db uv run alembic upgrade head

sudo chgrp branchforge /var/lib/branchforge/db/branchforge.db
sudo chmod 0664 /var/lib/branchforge/db/branchforge.db
sudo chmod 2775 /var/lib/branchforge/db
```

`2775` with the setgid bit means anything created later in that directory — the dispatcher's
`branchforge.db.dispatcher.lock`, and SQLite's own `-wal`/`-shm`/journal files — inherits group
`branchforge`. Both systemd units below set `UMask=0002`, so those new files come out group-writable
(`0664`) rather than owner-only, which is what lets both services keep sharing the file. If you ever
re-run a table-rebuild migration (one that drops and recreates a table) after this, the file may be
replaced — repeat the `chgrp`/`chmod` afterwards.

## 6. Build the runner image

Exactly the existing command, run from the repo root:

```bash
cd /opt/branchforge
sudo docker build --provenance=false -t branchforge-runner-python:1 backend/runner/python-pytest
```

## 7. Build the frontend for same-origin production requests

This is the step most likely to produce a silently broken deploy: an **unset**
`VITE_API_BASE_URL` bakes in `http://localhost:8000` at build time (Vite inlines it into the bundle;
it cannot be changed at runtime afterward). Set it to an empty string so requests are relative and
go through Caddy same-origin:

```bash
cd /opt/branchforge/frontend
sudo npm install
sudo env VITE_API_BASE_URL="" npm run build
```

Verify it actually took effect before moving on:

```bash
grep -r "localhost:8000" dist/assets/*.js && echo "BROKEN: rebuild with VITE_API_BASE_URL set" || echo "OK"
```

## 8. Caddy

```bash
sudo cp /opt/branchforge/deploy/Caddyfile /etc/caddy/Caddyfile
```

Edit `/etc/caddy/Caddyfile`: replace `REPLACE_WITH_YOUR_DOMAIN` with the real domain (its DNS record
must already point at this host, with ports 80/443 reachable). Generate credentials and replace
`REPLACE_WITH_USERNAME` / `REPLACE_WITH_BCRYPT_HASH`:

```bash
caddy hash-password
```

Validate before reloading:

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
```

## 9. Install and start the services

```bash
sudo cp /opt/branchforge/deploy/systemd/branchforge-api.service /etc/systemd/system/
sudo cp /opt/branchforge/deploy/systemd/branchforge-dispatcher.service /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/branchforge-api.service
sudo systemd-analyze verify /etc/systemd/system/branchforge-dispatcher.service

sudo systemctl daemon-reload
sudo systemctl enable --now branchforge-api
sudo systemctl enable --now branchforge-dispatcher
sudo systemctl reload caddy
```

## Logs

```bash
journalctl -u branchforge-api -f
journalctl -u branchforge-dispatcher -f
tail -f /var/log/caddy/branchforge-access.log
```

## Shutdown

```bash
sudo systemctl stop branchforge-dispatcher branchforge-api
```

Stop the dispatcher first: it stops claiming new jobs immediately and gets its full
`TimeoutStopSec=180` to finish any in-flight job's cleanup (stop the orchestrator/attempt processes,
reconcile the database, sweep labelled containers, then report the job stopped) before the API goes
down. `TimeoutStopSec` is set well above `DISPATCHER_CHILD_GRACE_SECONDS` (90s by default) precisely
so that cleanup is never cut short by the stop itself.

## Upgrading

```bash
sudo systemctl stop branchforge-dispatcher branchforge-api

# Consistent backup regardless of which SQLite journal mode is active:
sudo mkdir -p /var/backups/branchforge
sudo sqlite3 /var/lib/branchforge/db/branchforge.db \
  ".backup /var/backups/branchforge/branchforge-$(date +%Y%m%d%H%M%S).db"
# Belt-and-braces: also copy any WAL/SHM siblings if present.
sudo cp -a /var/lib/branchforge/db/branchforge.db-wal /var/backups/branchforge/ 2>/dev/null || true
sudo cp -a /var/lib/branchforge/db/branchforge.db-shm /var/backups/branchforge/ 2>/dev/null || true

cd /opt/branchforge
sudo git fetch
sudo git checkout <release-tag-or-commit>

cd backend
sudo uv sync
sudo env DATABASE_URL=sqlite:////var/lib/branchforge/db/branchforge.db uv run alembic upgrade head
# Only if the migration rebuilt the table:
#   sudo chgrp branchforge /var/lib/branchforge/db/branchforge.db
#   sudo chmod 0664 /var/lib/branchforge/db/branchforge.db

# Only if backend/runner/python-pytest/Dockerfile changed:
#   cd /opt/branchforge && sudo docker build --provenance=false -t branchforge-runner-python:1 backend/runner/python-pytest

cd /opt/branchforge/frontend
sudo npm install
sudo env VITE_API_BASE_URL="" npm run build
grep -r "localhost:8000" dist/assets/*.js && echo "BROKEN: rebuild" || echo "OK"

sudo systemctl start branchforge-api branchforge-dispatcher
```

## Rolling back

```bash
sudo systemctl stop branchforge-dispatcher branchforge-api

cd /opt/branchforge
sudo git checkout <previous-release-tag-or-commit>
cd backend
sudo uv sync

# Restore the matching backup taken during the upgrade you're rolling back —
# never mix a newer schema's data with older code.
sudo cp /var/backups/branchforge/branchforge-<timestamp>.db /var/lib/branchforge/db/branchforge.db
sudo rm -f /var/lib/branchforge/db/branchforge.db-wal /var/lib/branchforge/db/branchforge.db-shm
sudo chgrp branchforge /var/lib/branchforge/db/branchforge.db
sudo chmod 0664 /var/lib/branchforge/db/branchforge.db

sudo systemctl start branchforge-api branchforge-dispatcher
```

Do not attempt `alembic downgrade` as a substitute for restoring a backup. Table-rebuild migrations
(see `CLAUDE.md`'s "Table-rebuild migrations" section) already refuse to downgrade while data that
doesn't fit the older shape exists — restoring the timestamped backup taken before the upgrade is
the supported rollback path, not schema downgrade.

## Recovery-required: what actually happens, and the actual fix

This is not a workaround for an inconvenient check — it is the procedure the codebase itself
documents (`README.md`'s "Manual recovery" note) and the only thing that clears the flag, because
nothing in the application does. Do **not** delete the dispatcher's lock file or hand-edit a job's
`status` to make the check go away without doing the verification below first — that would be
declaring the job's outcome without evidence, which is exactly what this flag exists to prevent.

**What happened:** a dispatcher process was killed outright (host crash, `SIGKILL`, `kill -9`) while
a job was `running`. The *next* dispatcher to start holds the lock — proving no dispatcher is
alive — but that proves nothing about the orchestrator or attempt child processes, or the containers,
the dead one may have left behind. Rather than guess, it flags the job `recovery_required = true`
(never changing its `status`), refuses to claim any further work, and exits `10`.
`RestartPreventExitStatus=10` on the dispatcher unit means systemd will **not** auto-restart it —
if it did, the same startup scan would find the flag still set (nothing clears it automatically) and
refuse again, forever, which is exactly the endless restart loop this is guarding against.

**The actual fix:**

1. `systemctl status branchforge-dispatcher` exiting `10` and staying stopped is the symptom.
   `journalctl -u branchforge-dispatcher` names the job id and its last known `stage`.
2. Look for real survivors — don't assume there are none, and don't assume there are:
   ```bash
   docker ps --filter label=branchforge.orchestration=<orchestration-id>
   ps -ef | grep run-attempt
   ```
   Stop/remove anything you find (`docker stop`/`docker rm`, `kill` the listed PIDs).
3. Back up the database exactly as in "Upgrading" above.
4. With both services stopped, decide what the job's row should actually say, based on what you
   found in step 2, and apply it directly — this `UPDATE` is the only thing that ever clears
   `recovery_required`:
   ```bash
   sqlite3 /var/lib/branchforge/db/branchforge.db <<'SQL'
   UPDATE execution_jobs
   SET status = 'failed',
       error_kind = 'manual_recovery',
       error_message = 'Resolved manually after dispatcher crash; see notes column.',
       recovery_required = 0
   WHERE id = '<job-id>';
   SQL
   ```
   Adjust `status`/`error_kind`/`error_message` to reflect what you actually observed — the point of
   this step is an operator's informed judgment call, not a fixed script.
5. Confirm no job is still flagged before restarting:
   ```bash
   sqlite3 /var/lib/branchforge/db/branchforge.db \
     "SELECT id, status, recovery_required FROM execution_jobs WHERE recovery_required = 1;"
   ```
   This should return nothing.
6. `sudo systemctl start branchforge-dispatcher`.

Durable, automatic recovery from an abrupt crash is out of scope for this project (see
`CLAUDE.md`/`README.md`'s Limitations) — this manual procedure is the supported path, not a stopgap
for a missing feature.

## Access protection notes

- The FastAPI app itself has no in-app authentication, sessions, or per-user authorization — see the
  updated notes in `CLAUDE.md` and `README.md`. Basic Auth in Caddy is a single shared password in
  front of the *entire* site (frontend and `/api/*` alike), suitable for a small trusted group, not a
  substitute for real accounts.
- `branchforge-api.service` binds `127.0.0.1:8000` only. There is no other listener for the backend
  on this host — bypassing Caddy from off-host is not possible unless something else is
  misconfigured to bind `0.0.0.0:8000`, which nothing here does.
- Keep the Docker daemon listening only on its local Unix socket (the default) — never add a `-H
  tcp://` flag to `dockerd`. Only `branchforge-dispatcher` is a member of the `docker` group;
  `branchforge-api` is not and never should be.
- The runner's isolation flags (`--network=none`, `--user 65534:65534`, `--read-only`,
  `--cap-drop=ALL`, etc. — see `app/runner.py` / `CLAUDE.md`) are unchanged by this deployment. This
  guide does not touch `app/runner.py`, `app/verification.py`, or the runner Dockerfile.

## Verification you should run once a server exists

Everything above can be prepared without a target host. Once one is available:

- `curl -i https://<domain>/` and `https://<domain>/api/runs` **without** `-u` return `401`;
  **with** `-u <user>:<password>` return the app / a JSON list.
- `curl -i -u <user>:<password> https://<domain>/api/does-not-exist` returns FastAPI's own JSON
  `404`, not `index.html` — confirms the Caddyfile's route ordering.
- Open a run's URL directly (`https://<domain>/#/runs/<uuid>`) in a fresh browser tab and refresh it.
- Confirm the dashboard's progress polling keeps working once credentials are cached by the browser —
  a `401` on `/api/runs/{id}/progress` would look exactly like a hung job in the UI, which is the
  kind of misleading state this project explicitly avoids elsewhere.
- `sudo systemctl restart branchforge-api branchforge-dispatcher`, then confirm previously created
  runs and their attempts/verifications are still present and unchanged.
- Run exactly one real attempt (`max_parallel_attempts=1`) against the established `add()` issue in
  `https://github.com/radiusNlogN/branchforge-demo` and confirm the baseline of 3 passing/1 failing
  becomes 4 passing in the demonstrated fix — do not launch further attempts automatically.
