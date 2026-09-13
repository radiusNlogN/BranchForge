"""The dashboard in a real browser.

Marked `browser` and skipped cleanly when Playwright or its browsers are missing
— the same rule the `docker` tier follows: a tier that cannot run says so and is
never faked into passing.

    uv run pytest -m browser

`playwright==1.60.0` is a dev dependency, pinned because it expects the cached
`chromium-1223` build. If the browser itself is missing:

    uv run playwright install chromium

With Playwright absent entirely this module skips at import, so pytest exits `5`
("no tests ran") — a skipped tier, never a pass.

What runs here is the real stack: the real API server, the production frontend
build, and the real dispatcher. Only the three seams the dispatcher already
exposes are substituted (model, image resolver, child commands), through the same
`tests.dispatcher_driver` the dispatcher tests use. There is deliberately no
production switch that selects mocked execution.

The frontend is served as a **static production build**, not the dev server:
`VITE_API_BASE_URL` is inlined at build time, and `vite.config.ts` pins
`strictPort: true` on 5173, which would collide.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from alembic import command

from tests.conftest import INSPECTABLE_PAYLOAD, alembic_config

sync_playwright = pytest.importorskip(
    "playwright.sync_api", reason="Playwright is not installed in this environment"
).sync_playwright

pytestmark = pytest.mark.browser

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
FRONTEND_ROOT = REPO_ROOT / "frontend"
IMAGE_ID = "sha256:testimage"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def wait_for_http(url: str, *, timeout: float = 120.0, log_path: Path | None = None) -> None:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status < 500:
                    return
        except Exception as error:  # noqa: BLE001 - any failure just means "not yet"
            last = error
        time.sleep(0.2)
    # Include the server's own output: without it "connection refused" says only
    # that nothing is listening, not why.
    detail = ""
    if log_path is not None and log_path.exists():
        detail = "\n--- server log ---\n" + log_path.read_text()[-2000:]
    raise AssertionError(f"{url} did not become reachable: {last}{detail}")


def post_json(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def get_json(url: str) -> dict | list:
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read())


# --- The stack under test --------------------------------------------------------


@pytest.fixture(scope="session")
def ports() -> dict[str, int]:
    return {"api": free_port(), "static": free_port()}


@pytest.fixture(scope="session")
def built_frontend(ports: dict[str, int]) -> Path:
    """The production build, with the API base URL inlined at build time."""
    if not (FRONTEND_ROOT / "node_modules").exists():
        pytest.skip("frontend dependencies are not installed (npm install)")
    environment = dict(os.environ)
    environment["VITE_API_BASE_URL"] = f"http://127.0.0.1:{ports['api']}"
    completed = subprocess.run(
        ["npm", "run", "build"],
        cwd=FRONTEND_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if completed.returncode != 0:
        pytest.fail(f"frontend build failed:\n{completed.stdout}\n{completed.stderr}")
    return FRONTEND_ROOT / "dist"


@pytest.fixture(scope="session")
def browser_db(tmp_path_factory) -> str:
    url = f"sqlite:///{tmp_path_factory.mktemp('browserdb') / 'browser.db'}"
    command.upgrade(alembic_config(url), "head")
    return url


@pytest.fixture(scope="session")
def api_server(browser_db: str, ports: dict[str, int], tmp_path_factory):
    """One API server for the whole session.

    Session-scoped deliberately. `VITE_API_BASE_URL` is inlined into the frontend
    bundle at build time, so the API port cannot vary between tests — which means
    a per-test server would rebind that one fixed port over and over and race the
    previous socket's teardown. That is exactly what made the first two tests
    error with "connection refused" while later ones, on the very same port,
    succeeded.

    Its output is captured rather than discarded: "connection refused" alone says
    only that nothing is listening, never why.
    """
    log_path = tmp_path_factory.mktemp("apilog") / "uvicorn.log"
    environment = dict(os.environ)
    environment["DATABASE_URL"] = browser_db
    environment["CORS_ORIGINS"] = f"http://127.0.0.1:{ports['static']}"
    handle = open(log_path, "w")
    process = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "app.main:app",
            "--host", "127.0.0.1", "--port", str(ports["api"]), "--log-level", "warning",
        ],
        cwd=BACKEND_ROOT,
        env=environment,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{ports['api']}"
    try:
        wait_for_http(f"{base}/api/health", log_path=log_path)
        yield base
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()
        handle.close()


@pytest.fixture(scope="session")
def site(built_frontend: Path, ports: dict[str, int]):
    # Session-scoped for the same reason as `api_server`: one fixed port, served
    # from one build, for the whole session.
    process = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(ports["static"]), "--bind", "127.0.0.1"],
        cwd=built_frontend,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{ports['static']}"
    try:
        wait_for_http(base)
        yield base
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:  # pragma: no cover
            process.kill()


@pytest.fixture
def page():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1400, "height": 1000})
        page = context.new_page()
        try:
            yield page
        finally:
            context.close()
            browser.close()


@pytest.fixture
def scenario(tmp_path: Path) -> Path:
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    path = tmp_path / "scenario.json"
    path.write_text(
        json.dumps(
            {
                "workdir": str(workdir),
                "image_id": IMAGE_ID,
                "attempts": {"1": {"patch": "correct"}},
            }
        )
    )
    return path


@pytest.fixture
def fake_docker(tmp_path: Path) -> str:
    script = tmp_path / "docker"
    script.write_text('#!/bin/sh\nif [ "$1" = "ps" ]; then :; fi\nexit 0\n')
    script.chmod(0o755)
    return str(script)


@pytest.fixture
def dispatcher(browser_db: str, scenario: Path, fake_docker: str, tmp_path: Path):
    """The real dispatcher, started on demand by a test."""
    started: list[subprocess.Popen] = []

    def start() -> subprocess.Popen:
        handle = open(tmp_path / "dispatcher.log", "w")
        process = subprocess.Popen(
            [
                sys.executable, "-m", "tests.dispatcher_driver",
                "--scenario", str(scenario),
                "--database-url", browser_db,
                "--docker-binary", fake_docker,
                "--poll", "0.2",
            ],
            cwd=BACKEND_ROOT,
            stdout=handle,
            stderr=subprocess.STDOUT,
        )
        started.append(process)
        return process

    yield start
    for process in started:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:  # pragma: no cover
                process.kill()


def create_run(api: str, **overrides) -> str:
    payload = dict(INSPECTABLE_PAYLOAD, **overrides)
    return post_json(f"{api}/api/runs", payload)["id"]


def open_run(page, run_id: str, *, index: int = 0) -> None:
    """Click a run in the list, then confirm the detail pane really switched to it.

    Selection has to be positional. The list renders `owner/repo`, a relative
    time, and the attempt count — never the run id — and every run these tests
    create shares one repository URL, so the rows are textually identical. The
    list is ordered newest-first (`created_at DESC, id DESC`), so index 0 is the
    most recently created run.

    The click is then *verified* against the full UUID the detail pane shows in
    its Run ID row. That is stronger than matching list text would have been: it
    proves the pane actually switched to the intended run rather than merely that
    something was clicked.
    """
    page.locator(".runList__item").nth(index).click()
    # Wait for the Run ID cell in the detail pane's properties list, not for the
    # id as free text. The id also appears inside the collapsed "Run the steps
    # manually" <details> (in the orchestrate/propose command blocks), and a bare
    # `text=` selector resolves to those first. They are never visible while the
    # <details> is closed, so the wait times out even though the pane switched
    # correctly. Restricting to `code` also excludes those <pre> blocks outright.
    page.locator(".properties code").filter(has_text=run_id).first.wait_for(
        state="visible", timeout=20_000
    )


# --- Tests ------------------------------------------------------------------------


def test_start_queues_a_job_and_the_page_says_it_needs_a_dispatcher(
    page, site, api_server, browser_db
) -> None:
    run_id = create_run(api_server)
    page.goto(site)
    open_run(page, run_id)

    page.get_by_role("button", name="Start").click()

    # The queued state is shown, and it says plainly that nothing is running yet.
    page.wait_for_selector("text=waiting for a dispatcher", timeout=15_000)
    assert page.get_by_text("Queued — nothing is running yet").is_visible()
    assert "app.worker dispatch" in page.content()

    job = get_json(f"{api_server}/api/runs/{run_id}")["job"]
    assert job is not None and job["status"] == "queued"


def test_rapid_duplicate_start_clicks_create_exactly_one_job(
    page, site, api_server, browser_db
) -> None:
    """The button disables itself, but the backend is what guarantees this."""
    run_id = create_run(api_server)
    page.goto(site)
    open_run(page, run_id)

    start = page.get_by_role("button", name="Start")
    start.click()
    for _ in range(4):
        try:
            start.click(timeout=250, force=True)
        except Exception:  # noqa: BLE001 - the button legitimately disappears
            break

    page.wait_for_selector("text=waiting for a dispatcher", timeout=15_000)
    first = get_json(f"{api_server}/api/runs/{run_id}")["job"]["id"]
    # Every start returned the same job; there is no second one to find.
    again = post_json(f"{api_server}/api/runs/{run_id}/start", {})
    assert again["id"] == first


def test_progress_advances_without_a_manual_refresh(
    page, site, api_server, browser_db, dispatcher
) -> None:
    run_id = create_run(api_server, max_parallel_attempts=1)
    page.goto(site)
    open_run(page, run_id)
    page.get_by_role("button", name="Start").click()
    page.wait_for_selector("text=waiting for a dispatcher", timeout=15_000)

    dispatcher()

    # Nothing below clicks Refresh: the page must advance on its own.
    page.wait_for_selector("text=Competing attempts", timeout=180_000)
    page.wait_for_selector("text=completed", timeout=180_000)

    job = get_json(f"{api_server}/api/runs/{run_id}")["job"]
    assert job["status"] == "completed"


def test_polling_stops_once_the_job_reaches_a_terminal_state(
    page, site, api_server, browser_db, dispatcher
) -> None:
    polls: list[str] = []
    page.on(
        "request",
        lambda request: polls.append(request.url) if "/progress" in request.url else None,
    )

    run_id = create_run(api_server, max_parallel_attempts=1)
    page.goto(site)
    open_run(page, run_id)
    page.get_by_role("button", name="Start").click()
    dispatcher()
    page.wait_for_selector("text=Competing attempts", timeout=180_000)

    # Wait for the job to finish, then let several poll intervals pass.
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if get_json(f"{api_server}/api/runs/{run_id}")["job"]["status"] == "completed":
            break
        time.sleep(0.5)
    page.wait_for_timeout(3_000)

    settled = len(polls)
    page.wait_for_timeout(6_000)
    assert len(polls) == settled, (
        f"polling continued after the job finished: {len(polls) - settled} extra request(s)"
    )


def test_switching_runs_does_not_show_the_previous_run(
    page, site, api_server, browser_db, dispatcher
) -> None:
    """A late response about the previous run must never land on this one."""
    first = create_run(api_server, max_parallel_attempts=1)
    second = create_run(api_server, max_parallel_attempts=1)

    page.goto(site)
    # `second` was created last, so it is index 0 and `first` is index 1.
    open_run(page, first, index=1)
    page.get_by_role("button", name="Start").click()
    page.wait_for_selector("text=waiting for a dispatcher", timeout=15_000)
    dispatcher()

    # Switch while the first run is actively polling.
    open_run(page, second, index=0)

    # The second run was never started, so its own state is what must be shown.
    page.wait_for_timeout(4_000)
    content = page.content()
    assert second in content
    assert first not in content, "the previous run's data is still on screen"
    assert "Ready to start" in content or "Start" in content


def test_a_failed_start_shows_the_error_in_the_page(
    page, site, api_server, browser_db, ports
) -> None:
    """A failing Start must surface in the UI, not vanish.

    The failure is real rather than mocked: the API is stopped, so the request
    genuinely cannot complete. Asserting on the response object instead of the
    rendered page would pass even if the dashboard swallowed every error, which
    is the bug this is meant to catch.
    """
    run_id = create_run(api_server)
    page.goto(site)
    open_run(page, run_id)
    page.wait_for_selector("text=Ready to start", timeout=15_000)

    # Stop the API behind the page's back, then act.
    api_server_process_stopped = _stop_listener(ports["api"])
    assert api_server_process_stopped, "could not stop the API for the failure case"

    page.get_by_role("button", name="Start").click()

    page.wait_for_selector("text=That request was refused", timeout=30_000)
    assert "Could not reach the BranchForge API" in page.content()
    # The button is usable again: a failed request must not leave it stuck.
    assert page.get_by_role("button", name="Start").is_enabled()


def _stop_listener(port: int) -> bool:
    """Kill whatever is listening on `port`. Returns True if something was killed."""
    completed = subprocess.run(
        ["lsof", "-ti", f"tcp:{port}"], capture_output=True, text=True, timeout=30
    )
    pids = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    for pid in pids:
        subprocess.run(["kill", "-9", pid], capture_output=True, timeout=10)
    if not pids:
        return False
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1):
                time.sleep(0.2)
        except Exception:  # noqa: BLE001 - unreachable is exactly what we want
            return True
    return False
