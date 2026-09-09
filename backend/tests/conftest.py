"""Test fixtures.

Every test runs against a temporary, file-backed SQLite database created by
running the real Alembic migrations — not `create_all`. That way the migration is
exercised by the whole suite and a broken migration fails the tests.
"""

# `sample_repo/` is a fixture *repository*, not part of this suite: it contains a
# deliberately failing test used to verify the verifier. Collecting it here would
# fail our own run.
collect_ignore_glob = ["sample_repo/*", "sample_repo/**/*"]


import base64
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.database import get_db
from app.github_client import ClientLimits, GitHubClient
from app.main import create_app
from app.model_client import ModelError, ModelTurn, ToolCall

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def alembic_config(database_url: str) -> Config:
    """An Alembic config pointed at an explicit database URL."""
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'test_branchforge.db'}"


@pytest.fixture
def engine(database_url: str) -> Iterator[Engine]:
    """A migrated, throwaway database."""
    command.upgrade(alembic_config(database_url), "head")
    engine = create_engine(database_url, connect_args={"check_same_thread": False})
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture
def db(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as session:
        yield session


@pytest.fixture
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    """A TestClient whose requests hit the temporary database."""
    app = create_app()

    def override_get_db() -> Iterator[Session]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


VALID_PAYLOAD = {
    "repository_url": "https://github.com/octocat/Hello-World",
    "issue_description": "Sorting breaks on empty input.",
    "max_parallel_attempts": 2,
}


# --- Fake GitHub ------------------------------------------------------------
#
# Mocked at the transport layer so the real client code — URL construction,
# streaming byte caps, status handling — is exercised rather than stubbed out.

COMMIT_SHA = "672971d66a2ef9f85151e53283113f33d642dabd"


def base64_json_content(text: str) -> dict[str, Any]:
    """A contents-endpoint payload the way GitHub returns one."""
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return {
        "type": "file",
        "size": len(text.encode("utf-8")),
        "encoding": "base64",
        # GitHub wraps base64 at 60 characters; keep that in the fixture.
        "content": "\n".join(encoded[i : i + 60] for i in range(0, len(encoded), 60)) + "\n",
    }


class FakeGitHub:
    """Routes GitHub API paths to canned responses and records every request."""

    def __init__(
        self,
        *,
        repo: dict[str, Any] | None = None,
        commit_sha: str = COMMIT_SHA,
        tree: list[dict[str, Any]] | None = None,
        truncated: bool = False,
        files: dict[str, str] | None = None,
        responses: dict[str, httpx2.Response] | None = None,
    ) -> None:
        self.repo = repo if repo is not None else {
            "name": "itsdangerous",
            "full_name": "pallets/itsdangerous",
            "description": "Safely pass trusted data to untrusted environments.",
            "default_branch": "main",
            "private": False,
            "fork": False,
            "archived": False,
        }
        self.commit_sha = commit_sha
        self.tree = tree if tree is not None else [
            {"path": "README.md", "type": "blob", "size": 40},
            {"path": "pyproject.toml", "type": "blob", "size": 60},
            {"path": "src/itsdangerous/__init__.py", "type": "blob", "size": 20},
            {"path": "tests/test_signer.py", "type": "blob", "size": 30},
        ]
        self.truncated = truncated
        self.files = files if files is not None else {
            "README.md": "# itsdangerous\n\nA small library.\n",
            "pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
        }
        self.responses = responses or {}
        self.requests: list[tuple[str, dict[str, str]]] = []
        self._lock = threading.Lock()

    @property
    def paths(self) -> list[str]:
        return [path for path, _ in self.requests]

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        with self._lock:
            self.requests.append((path, dict(request.url.params)))

        for prefix, response in self.responses.items():
            if path == prefix or path.startswith(prefix):
                return response

        if path == "/repos/o/r":
            return httpx2.Response(200, json=self.repo)
        if path == "/repos/o/r/commits":
            return httpx2.Response(200, json=[{"sha": self.commit_sha}])
        if path.startswith("/repos/o/r/git/trees/"):
            return httpx2.Response(
                200, json={"sha": "t", "truncated": self.truncated, "tree": self.tree}
            )
        if path.startswith("/repos/o/r/contents/"):
            file_path = path[len("/repos/o/r/contents/") :]
            if file_path in self.files:
                return httpx2.Response(200, json=base64_json_content(self.files[file_path]))
            return httpx2.Response(404, json={"message": "Not Found"})
        return httpx2.Response(404, json={"message": "Not Found"})

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handler)

    def client_factory(self, config: Settings) -> GitHubClient:
        """A client_factory for the worker, wired to this fake."""
        from app import github_client as gh

        http = gh.build_client(
            base_url=config.github_api_base_url,
            user_agent=config.github_user_agent,
            timeout_seconds=config.github_request_timeout_seconds,
            connect_timeout_seconds=config.github_connect_timeout_seconds,
            transport=self.transport(),
        )
        return GitHubClient(
            http,
            ClientLimits(
                max_requests=config.github_max_requests,
                max_response_bytes=config.github_max_response_bytes,
                max_content_response_bytes=config.github_max_content_response_bytes,
            ),
        )


@pytest.fixture
def fake_github() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def worker_settings() -> Settings:
    """Settings for worker tests. The fake transport ignores the host."""
    return Settings(
        database_url="sqlite://",
        github_api_base_url="https://api.github.test",
        github_max_files_fetched=8,
    )


INSPECTABLE_PAYLOAD = {
    "repository_url": "https://github.com/o/r",
    "issue_description": "Signing breaks on empty payloads.",
    "max_parallel_attempts": 1,
}


def big_json_tree(entries: int) -> bytes:
    """A tree response large enough to trip a response-byte cap."""
    return json.dumps(
        {
            "sha": "t",
            "truncated": False,
            "tree": [
                {"path": f"file_{i:06d}.py", "type": "blob", "size": 10}
                for i in range(entries)
            ],
        }
    ).encode()


# --- Scripted model ---------------------------------------------------------
#
# The controller loop is driven by canned turns so every branch is testable with
# no network. Token counting sits behind the same interface for the same reason.

SCRIPTED_MODEL = "scripted-model-1"

# A syntactically valid unified diff used by the happy-path tests.
VALID_DIFF = """--- a/src/pkg/signer.py
+++ b/src/pkg/signer.py
@@ -10,2 +10,4 @@ def unsign(self, signed_value):
     def unsign(self, signed_value):
+        if not signed_value:
+            raise BadSignature("empty payload")
         value, sig = signed_value.rsplit(b".", 1)
"""


def make_turn(
    *,
    text: str = "",
    tool_calls: list[ToolCall] | None = None,
    stop_reason: str | None = None,
    input_tokens: int | None = 100,
    output_tokens: int | None = 50,
) -> ModelTurn:
    """Build a ModelTurn shaped like a real provider response."""
    calls = tool_calls or []
    if stop_reason is None:
        stop_reason = "tool_use" if calls else "end_turn"

    content: list[dict[str, Any]] = []
    if text:
        content.append({"type": "text", "text": text})
    for call in calls:
        content.append(
            {"type": "tool_use", "id": call.id, "name": call.name, "input": call.input}
        )

    return ModelTurn(
        stop_reason=stop_reason,
        text=text,
        tool_calls=calls,
        assistant_content=content,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def read_call(path: str, call_id: str = "call_read") -> ToolCall:
    return ToolCall(id=call_id, name="read_file", input={"path": path})


def submit_call(
    diff: str = VALID_DIFF,
    summary: str = "Guard against an empty payload before splitting.",
    command: str = "pytest tests/test_signer.py",
    call_id: str = "call_submit",
) -> ToolCall:
    return ToolCall(
        id=call_id,
        name="submit_patch",
        input={"diff": diff, "summary": summary, "suggested_test_command": command},
    )


class ScriptedModelClient:
    """Replays a fixed list of turns and records what it was sent."""

    def __init__(
        self,
        turns: list[ModelTurn],
        *,
        token_count: int = 500,
        error: ModelError | None = None,
        count_error: ModelError | None = None,
    ) -> None:
        self._turns = list(turns)
        self._token_count = token_count
        self._error = error
        self._count_error = count_error
        self.calls = 0
        self.count_calls = 0
        # A snapshot of the message history for every generation request.
        self.seen_messages: list[list[dict[str, Any]]] = []

    @property
    def model(self) -> str:
        return SCRIPTED_MODEL

    def count_input_tokens(self, *, system, messages, tools) -> int:
        self.count_calls += 1
        if self._count_error is not None:
            raise self._count_error
        # Grows with history so a context-budget test can be driven realistically.
        if callable(self._token_count):
            return self._token_count(messages)
        return self._token_count

    def create_message(self, *, system, messages, tools, max_tokens) -> ModelTurn:
        self.calls += 1
        self.seen_messages.append([dict(m) for m in messages])
        if self._error is not None:
            raise self._error
        if not self._turns:
            raise AssertionError("ScriptedModelClient ran out of scripted turns")
        return self._turns.pop(0)

    def factory(self, config: Settings):
        """A model_factory for the worker."""
        return self

    # Convenience for assertions
    def last_tool_results(self) -> list[dict[str, Any]]:
        """tool_result blocks from the most recent user message sent back."""
        if not self.seen_messages:
            return []
        for message in reversed(self.seen_messages[-1]):
            if message["role"] == "user" and isinstance(message["content"], list):
                blocks = [
                    b for b in message["content"]
                    if isinstance(b, dict) and b.get("type") == "tool_result"
                ]
                if blocks:
                    return blocks
        return []


def agent_settings(**overrides: Any) -> Settings:
    """Settings for agent tests; the scripted client ignores the key."""
    base = {
        "database_url": "sqlite://",
        "anthropic_api_key": "sk-scripted-not-a-real-key",
        "anthropic_model": SCRIPTED_MODEL,
        "github_api_base_url": "https://api.github.test",
    }
    base.update(overrides)
    return Settings(**base)
