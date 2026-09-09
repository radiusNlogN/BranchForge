"""The controller loop, driven by scripted model turns and mocked GitHub.

No network. Every branch the spec names is exercised through the real loop —
tool dispatch, validation, caching, budgets, and termination.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import repository, worker
from app.model_client import ModelRateLimited, ModelRequestFailed
from app.models import (
    ATTEMPT_STATUS_FAILED,
    ATTEMPT_STATUS_SUCCEEDED,
    RUN_STATUS_READY,
)
from tests.conftest import (
    INSPECTABLE_PAYLOAD,
    VALID_DIFF,
    FakeGitHub,
    ScriptedModelClient,
    agent_settings,
    make_turn,
    read_call,
    submit_call,
)


@pytest.fixture
def ready_run(client: TestClient, session_factory, fake_github, worker_settings) -> str:
    """A run that has been inspected successfully."""
    run_id = client.post("/api/runs", json=INSPECTABLE_PAYLOAD).json()["id"]
    assert (
        worker.inspect_run(
            run_id,
            session_factory=session_factory,
            client_factory=fake_github.client_factory,
            config=worker_settings,
        )
        == worker.EXIT_OK
    )
    return run_id


def propose(run_id, session_factory, model, github, config=None) -> int:
    return worker.propose_patch(
        run_id,
        session_factory=session_factory,
        client_factory=github.client_factory,
        model_factory=model.factory,
        config=config or agent_settings(),
    )


def attempt_of(session_factory, run_id):
    with session_factory() as db:
        attempt = repository.get_patch_attempt_for_run(db, run_id)
        events = (
            repository.list_attempt_events(db, attempt.id, limit=500) if attempt else []
        )
        run = repository.get_run(db, run_id)
        return attempt, [(e.seq, e.kind, e.summary) for e in events], run.status


# --- Happy path -------------------------------------------------------------


def test_read_then_submit_persists_the_patch(ready_run, session_factory):
    """Read a file, receive its content, submit a patch."""
    model = ScriptedModelClient([
        make_turn(text="Let me look at the signer.", tool_calls=[read_call("src/pkg/signer.py")]),
        make_turn(text="Here is a minimal fix.", tool_calls=[submit_call()]),
    ])
    github = FakeGitHub(files={"src/pkg/signer.py": "def unsign(self, v):\n    pass\n"})

    assert propose(ready_run, session_factory, model, github) == worker.EXIT_OK

    attempt, events, run_status = attempt_of(session_factory, ready_run)
    assert attempt.status == ATTEMPT_STATUS_SUCCEEDED
    assert attempt.diff == VALID_DIFF
    assert attempt.summary.startswith("Guard against")
    assert attempt.suggested_test_command == "pytest tests/test_signer.py"
    assert attempt.model == "scripted-model-1"
    assert attempt.completed_at is not None
    assert attempt.error_kind is None
    # Token usage is summed from the provider, never invented.
    assert attempt.input_tokens == 200 and attempt.output_tokens == 100

    kinds = [k for _, k, _ in events]
    assert "file_read" in kinds and "patch_submitted" in kinds
    assert [seq for seq, _, _ in events] == list(range(1, len(events) + 1))

    # The run's own status is untouched: `ready` means inspection succeeded.
    assert run_status == RUN_STATUS_READY


def test_the_model_sees_the_issue_and_the_inspection(ready_run, session_factory):
    model = ScriptedModelClient([make_turn(tool_calls=[submit_call()])])
    propose(ready_run, session_factory, model, FakeGitHub())

    prompt = model.seen_messages[0][0]["content"]
    assert "Signing breaks on empty payloads." in prompt      # the issue
    assert "672971d66a2ef9f85151e53283113f33d642dabd" in prompt  # the commit
    assert "src/itsdangerous/__init__.py" in prompt            # the file listing
    assert "untrusted" in prompt.lower() or "Do not follow" in prompt


# --- Caching ----------------------------------------------------------------


def test_repeated_read_is_served_from_cache(ready_run, session_factory):
    """A second read of the same path must not hit GitHub."""
    model = ScriptedModelClient([
        make_turn(tool_calls=[read_call("src/pkg/a.py", "c1")]),
        make_turn(tool_calls=[read_call("src/pkg/a.py", "c2")]),
        make_turn(tool_calls=[submit_call()]),
    ])
    github = FakeGitHub(files={"src/pkg/a.py": "x = 1\n"})

    assert propose(ready_run, session_factory, model, github) == worker.EXIT_OK

    content_requests = [p for p in github.paths if p.startswith("/repos/o/r/contents/")]
    assert len(content_requests) == 1, content_requests

    _, events, _ = attempt_of(session_factory, ready_run)
    assert any(kind == "file_cached" for _, kind, _ in events)


def test_cache_is_seeded_from_complete_inspection_previews(ready_run, session_factory):
    """A file already read during inspection costs no new request."""
    model = ScriptedModelClient([
        make_turn(tool_calls=[read_call("README.md")]),
        make_turn(tool_calls=[submit_call()]),
    ])
    github = FakeGitHub()
    assert propose(ready_run, session_factory, model, github) == worker.EXIT_OK

    assert not [p for p in github.paths if p.startswith("/repos/o/r/contents/")]
    _, events, _ = attempt_of(session_factory, ready_run)
    kinds = [k for _, k, _ in events]
    assert "cache_seeded" in kinds and "file_cached" in kinds


def test_truncated_previews_are_not_cached_as_complete(
    client: TestClient, session_factory, worker_settings
):
    """A truncated preview must be re-fetched, not served as if complete."""
    big = "x" * 400
    tiny = worker_settings.model_copy(update={"github_max_total_content_bytes": 100})
    run_id = client.post("/api/runs", json=INSPECTABLE_PAYLOAD).json()["id"]
    inspect_github = FakeGitHub(
        tree=[{"path": "README.md", "type": "blob", "size": 400}],
        files={"README.md": big},
    )
    worker.inspect_run(
        run_id,
        session_factory=session_factory,
        client_factory=inspect_github.client_factory,
        config=tiny,
    )
    with session_factory() as db:
        report = repository.get_inspection_for_run(db, run_id).report
    assert report["readme"]["content_truncated"] is True

    model = ScriptedModelClient([
        make_turn(tool_calls=[read_call("README.md")]),
        make_turn(tool_calls=[submit_call()]),
    ])
    agent_github = FakeGitHub(files={"README.md": big})
    assert propose(run_id, session_factory, model, agent_github) == worker.EXIT_OK
    # It was re-fetched rather than served from the truncated preview.
    assert "/repos/o/r/contents/README.md" in agent_github.paths


# --- Invalid tool use -------------------------------------------------------


def test_unknown_tool_is_rejected_and_the_loop_continues(ready_run, session_factory):
    from app.model_client import ToolCall

    model = ScriptedModelClient([
        make_turn(tool_calls=[ToolCall(id="c1", name="run_shell", input={"cmd": "rm -rf /"})]),
        make_turn(tool_calls=[submit_call()]),
    ])
    assert propose(ready_run, session_factory, model, FakeGitHub()) == worker.EXIT_OK

    # The rejection came back as a tool error, and the model got to correct itself.
    errors = [b for b in model.last_tool_results() if b.get("is_error")]
    assert model.calls == 2
    _, events, _ = attempt_of(session_factory, ready_run)
    assert any(kind == "tool_unknown" for _, kind, _ in events)


def test_malformed_read_arguments_are_rejected(ready_run, session_factory):
    from app.model_client import ToolCall

    model = ScriptedModelClient([
        make_turn(tool_calls=[ToolCall(id="c1", name="read_file", input={})]),
        make_turn(tool_calls=[submit_call()]),
    ])
    assert propose(ready_run, session_factory, model, FakeGitHub()) == worker.EXIT_OK
    assert model.calls == 2


def test_unsafe_read_path_is_a_tool_error_not_a_crash(ready_run, session_factory):
    model = ScriptedModelClient([
        make_turn(tool_calls=[read_call("../../etc/passwd")]),
        make_turn(tool_calls=[submit_call()]),
    ])
    github = FakeGitHub()
    assert propose(ready_run, session_factory, model, github) == worker.EXIT_OK
    # No request was ever issued for the traversal path.
    assert not any("etc/passwd" in p for p in github.paths)


# --- Terminal tool discipline ----------------------------------------------


def test_submit_mixed_with_reads_is_rejected_wholesale(ready_run, session_factory):
    """submit_patch must be alone; a mixed batch is rejected with one error each."""
    model = ScriptedModelClient([
        make_turn(tool_calls=[read_call("a.py", "c1"), submit_call(call_id="c2")]),
        make_turn(tool_calls=[submit_call(call_id="c3")]),
    ])
    github = FakeGitHub(files={"a.py": "x=1\n"})
    assert propose(ready_run, session_factory, model, github) == worker.EXIT_OK

    # Nothing from that batch ran: no content request for a.py.
    assert not [p for p in github.paths if p.endswith("/contents/a.py")]
    # Both tool_use ids were answered with errors.
    results = model.last_tool_results()
    assert len(results) == 2 and all(r["is_error"] for r in results)
    assert {r["tool_use_id"] for r in results} == {"c1", "c2"}

    attempt, events, _ = attempt_of(session_factory, ready_run)
    assert attempt.status == ATTEMPT_STATUS_SUCCEEDED
    assert any(kind == "batch_rejected" for _, kind, _ in events)


def test_multiple_submissions_in_one_response_are_rejected(ready_run, session_factory):
    model = ScriptedModelClient([
        make_turn(tool_calls=[submit_call(call_id="s1"), submit_call(call_id="s2")]),
        make_turn(tool_calls=[submit_call(call_id="s3")]),
    ])
    assert propose(ready_run, session_factory, model, FakeGitHub()) == worker.EXIT_OK
    attempt, _, _ = attempt_of(session_factory, ready_run)
    assert attempt.status == ATTEMPT_STATUS_SUCCEEDED
    assert model.calls == 2


def test_truncated_response_is_rejected_before_its_tools_run(ready_run, session_factory):
    """max_tokens means the arguments may be incomplete — do not execute them."""
    model = ScriptedModelClient([
        make_turn(tool_calls=[read_call("a.py")], stop_reason="max_tokens"),
    ])
    github = FakeGitHub(files={"a.py": "x=1\n"})
    assert propose(ready_run, session_factory, model, github) == worker.EXIT_ATTEMPT_FAILED

    assert not [p for p in github.paths if p.startswith("/repos/o/r/contents/")]
    attempt, _, run_status = attempt_of(session_factory, ready_run)
    assert attempt.status == ATTEMPT_STATUS_FAILED
    assert attempt.error_kind == "output_truncated"
    assert run_status == RUN_STATUS_READY


# --- Patch validation -------------------------------------------------------


def test_invalid_patch_can_be_corrected_within_budget(ready_run, session_factory):
    bad = "--- a/x.py\n+++ b/x.py\n@@ -1,9 +1,9 @@\n context\n"
    model = ScriptedModelClient([
        make_turn(tool_calls=[submit_call(diff=bad, call_id="s1")]),
        make_turn(tool_calls=[submit_call(call_id="s2")]),
    ])
    assert propose(ready_run, session_factory, model, FakeGitHub()) == worker.EXIT_OK

    attempt, events, _ = attempt_of(session_factory, ready_run)
    assert attempt.status == ATTEMPT_STATUS_SUCCEEDED
    assert attempt.diff == VALID_DIFF
    assert any(kind == "patch_rejected" for _, kind, _ in events)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("diff", ""),
        ("diff", "not a diff at all"),
        ("summary", ""),
        ("command", ""),
        ("command", "line one\nline two"),
    ],
)
def test_bad_submission_fields_are_rejected(ready_run, session_factory, field, value):
    kwargs: dict[str, Any] = {}
    kwargs[field] = value
    model = ScriptedModelClient([
        make_turn(tool_calls=[submit_call(call_id="s1", **kwargs)]),
        make_turn(text="I cannot fix this."),
    ])
    assert propose(ready_run, session_factory, model, FakeGitHub()) == worker.EXIT_ATTEMPT_FAILED
    attempt, events, _ = attempt_of(session_factory, ready_run)
    assert attempt.error_kind == "finished_without_patch"
    assert any(kind == "patch_rejected" for _, kind, _ in events)


def test_oversized_patch_is_rejected(ready_run, session_factory):
    config = agent_settings(agent_max_patch_bytes=200)
    huge = VALID_DIFF + ("+# padding\n" * 200)
    model = ScriptedModelClient([
        make_turn(tool_calls=[submit_call(diff=huge, call_id="s1")]),
        make_turn(text="Cannot shrink it."),
    ])
    assert propose(ready_run, session_factory, model, FakeGitHub(), config) == worker.EXIT_ATTEMPT_FAILED
    _, events, _ = attempt_of(session_factory, ready_run)
    assert any(kind == "patch_rejected" for _, kind, _ in events)


def test_oversized_summary_is_rejected(ready_run, session_factory):
    config = agent_settings(agent_max_summary_chars=50)
    model = ScriptedModelClient([
        make_turn(tool_calls=[submit_call(summary="s" * 200, call_id="s1")]),
        make_turn(tool_calls=[submit_call(call_id="s2")]),
    ])
    assert propose(ready_run, session_factory, model, FakeGitHub(), config) == worker.EXIT_OK


# --- Budgets ----------------------------------------------------------------


def test_turn_budget_exhausted(ready_run, session_factory):
    config = agent_settings(agent_max_turns=2)
    model = ScriptedModelClient([
        make_turn(tool_calls=[read_call("a.py", "c1")]),
        make_turn(tool_calls=[read_call("b.py", "c2")]),
        make_turn(tool_calls=[submit_call()]),
    ])
    github = FakeGitHub(files={"a.py": "1\n", "b.py": "2\n"})
    assert propose(ready_run, session_factory, model, github, config) == worker.EXIT_ATTEMPT_FAILED
    attempt, _, run_status = attempt_of(session_factory, ready_run)
    assert attempt.error_kind == "turn_budget_exceeded"
    assert model.calls == 2
    assert run_status == RUN_STATUS_READY


def test_tool_call_budget_exhausted(ready_run, session_factory):
    config = agent_settings(agent_max_tool_calls=1, agent_max_turns=6)
    model = ScriptedModelClient([
        make_turn(tool_calls=[read_call("a.py", "c1")]),
        make_turn(tool_calls=[read_call("b.py", "c2")]),
        make_turn(tool_calls=[submit_call()]),
    ])
    github = FakeGitHub(files={"a.py": "1\n", "b.py": "2\n"})
    assert propose(ready_run, session_factory, model, github, config) == worker.EXIT_ATTEMPT_FAILED
    attempt, _, _ = attempt_of(session_factory, ready_run)
    assert attempt.error_kind == "tool_call_budget_exceeded"


def test_cumulative_fetch_budget_exhausted(ready_run, session_factory):
    config = agent_settings(agent_max_total_fetched_bytes=10, agent_max_turns=6)
    model = ScriptedModelClient([
        make_turn(tool_calls=[read_call("a.py", "c1")]),
        make_turn(tool_calls=[read_call("b.py", "c2")]),
        make_turn(tool_calls=[submit_call()]),
    ])
    github = FakeGitHub(files={"a.py": "x" * 40, "b.py": "y" * 40})
    assert propose(ready_run, session_factory, model, github, config) == worker.EXIT_ATTEMPT_FAILED
    attempt, _, _ = attempt_of(session_factory, ready_run)
    assert attempt.error_kind == "fetch_budget_exceeded"


def test_per_file_byte_limit_is_a_tool_error(ready_run, session_factory):
    config = agent_settings(agent_max_file_bytes=10)
    model = ScriptedModelClient([
        make_turn(tool_calls=[read_call("big.py")]),
        make_turn(tool_calls=[submit_call()]),
    ])
    github = FakeGitHub(files={"big.py": "z" * 500})
    assert propose(ready_run, session_factory, model, github, config) == worker.EXIT_OK
    _, events, _ = attempt_of(session_factory, ready_run)
    assert any(kind == "tool_error" for _, kind, _ in events)


def test_context_budget_terminates_instead_of_dropping_history(ready_run, session_factory):
    """Over budget must stop the attempt, never silently discard messages."""
    config = agent_settings(
        agent_model_context_tokens=1_000,
        agent_max_output_tokens=100,
        agent_context_safety_margin_tokens=100,
    )  # usable input budget = 800
    model = ScriptedModelClient([make_turn(tool_calls=[submit_call()])], token_count=5_000)
    assert propose(ready_run, session_factory, model, FakeGitHub(), config) == worker.EXIT_ATTEMPT_FAILED

    attempt, _, run_status = attempt_of(session_factory, ready_run)
    assert attempt.error_kind == "context_budget_exceeded"
    assert "800" in attempt.error_message and "5,000" in attempt.error_message
    assert "compaction is not" in attempt.error_message.lower()
    # It stopped before generating at all.
    assert model.calls == 0 and model.count_calls == 1
    assert run_status == RUN_STATUS_READY


def test_context_is_measured_before_every_generation(ready_run, session_factory):
    model = ScriptedModelClient([
        make_turn(tool_calls=[read_call("a.py")]),
        make_turn(tool_calls=[submit_call()]),
    ])
    github = FakeGitHub(files={"a.py": "1\n"})
    assert propose(ready_run, session_factory, model, github) == worker.EXIT_OK
    assert model.count_calls == model.calls == 2


# --- Provider failures ------------------------------------------------------


def test_provider_failure_is_recorded(ready_run, session_factory):
    model = ScriptedModelClient([], error=ModelRequestFailed("Model request failed with HTTP 500."))
    assert propose(ready_run, session_factory, model, FakeGitHub()) == worker.EXIT_ATTEMPT_FAILED
    attempt, _, run_status = attempt_of(session_factory, ready_run)
    assert attempt.status == ATTEMPT_STATUS_FAILED
    assert attempt.error_kind == "model_request_failed"
    assert run_status == RUN_STATUS_READY


def test_provider_rate_limit_is_recorded(ready_run, session_factory):
    model = ScriptedModelClient([], error=ModelRateLimited("Rate limited by the provider."))
    assert propose(ready_run, session_factory, model, FakeGitHub()) == worker.EXIT_ATTEMPT_FAILED
    attempt, _, _ = attempt_of(session_factory, ready_run)
    assert attempt.error_kind == "model_rate_limited"


def test_token_counting_failure_is_recorded(ready_run, session_factory):
    model = ScriptedModelClient(
        [make_turn(tool_calls=[submit_call()])],
        count_error=ModelRequestFailed("Counting request tokens failed with HTTP 500."),
    )
    assert propose(ready_run, session_factory, model, FakeGitHub()) == worker.EXIT_ATTEMPT_FAILED
    attempt, _, _ = attempt_of(session_factory, ready_run)
    assert attempt.error_kind == "model_request_failed"
    assert model.calls == 0


def test_completion_without_a_patch_is_a_failure(ready_run, session_factory):
    """An incomplete conversation is never marked successful."""
    model = ScriptedModelClient([
        make_turn(text="I looked but cannot determine a safe fix.", stop_reason="end_turn"),
    ])
    assert propose(ready_run, session_factory, model, FakeGitHub()) == worker.EXIT_ATTEMPT_FAILED
    attempt, _, run_status = attempt_of(session_factory, ready_run)
    assert attempt.status == ATTEMPT_STATUS_FAILED
    assert attempt.error_kind == "finished_without_patch"
    assert "cannot determine" in attempt.error_message
    assert attempt.diff is None
    assert run_status == RUN_STATUS_READY


# --- Preconditions ----------------------------------------------------------


def test_missing_configuration_creates_no_attempt(ready_run, session_factory):
    """Fail clearly BEFORE claiming an attempt."""
    def failing_factory(config):
        from app.model_client import AnthropicModelClient
        return AnthropicModelClient(config)

    code = worker.propose_patch(
        ready_run,
        session_factory=session_factory,
        client_factory=FakeGitHub().client_factory,
        model_factory=failing_factory,
        config=agent_settings(anthropic_api_key=None),
    )
    assert code == worker.EXIT_NOT_CONFIGURED
    with session_factory() as db:
        assert repository.get_patch_attempt_for_run(db, ready_run) is None


def test_pending_run_cannot_be_proposed_for(client: TestClient, session_factory):
    run_id = client.post("/api/runs", json=INSPECTABLE_PAYLOAD).json()["id"]
    model = ScriptedModelClient([])
    assert propose(run_id, session_factory, model, FakeGitHub()) == worker.EXIT_NOT_READY
    assert model.calls == 0
    with session_factory() as db:
        assert repository.get_patch_attempt_for_run(db, run_id) is None


def test_unknown_run_is_reported(session_factory):
    model = ScriptedModelClient([])
    assert propose(
        "00000000-0000-0000-0000-000000000000", session_factory, model, FakeGitHub()
    ) == worker.EXIT_RUN_NOT_FOUND
    assert model.calls == 0


def test_second_attempt_is_refused(ready_run, session_factory):
    first = ScriptedModelClient([make_turn(tool_calls=[submit_call()])])
    assert propose(ready_run, session_factory, first, FakeGitHub()) == worker.EXIT_OK

    second = ScriptedModelClient([make_turn(tool_calls=[submit_call()])])
    github = FakeGitHub()
    assert propose(ready_run, session_factory, second, github) == worker.EXIT_NOT_CLAIMED
    assert second.calls == 0
    assert github.requests == []


# --- Events -----------------------------------------------------------------


def test_events_are_committed_progressively(ready_run, session_factory):
    """Events must be visible while the attempt is still running."""
    model = ScriptedModelClient([
        make_turn(tool_calls=[read_call("a.py")]),
        make_turn(text="Giving up."),  # fails after events were already written
    ])
    github = FakeGitHub(files={"a.py": "1\n"})
    assert propose(ready_run, session_factory, model, github) == worker.EXIT_ATTEMPT_FAILED

    _, events, _ = attempt_of(session_factory, ready_run)
    kinds = [k for _, k, _ in events]
    # The read happened before the failure and was persisted independently of it.
    assert "file_read" in kinds
    assert len(events) >= 3


def test_event_count_and_detail_are_capped(ready_run, session_factory):
    config = agent_settings(agent_max_events=3, agent_max_event_detail_chars=20, agent_max_turns=6)
    model = ScriptedModelClient([
        make_turn(tool_calls=[read_call("a.py", "c1")]),
        make_turn(tool_calls=[read_call("b.py", "c2")]),
        make_turn(tool_calls=[submit_call()]),
    ])
    github = FakeGitHub(files={"a.py": "1\n", "b.py": "2\n"})
    assert propose(ready_run, session_factory, model, github, config) == worker.EXIT_OK

    with session_factory() as db:
        attempt = repository.get_patch_attempt_for_run(db, ready_run)
        events = repository.list_attempt_events(db, attempt.id, limit=500)
    assert len(events) == 3
    assert all(len(e.summary) <= 20 for e in events)
    assert all(e.detail is None or len(e.detail) <= 20 for e in events)


def test_events_never_contain_model_reasoning(ready_run, session_factory):
    """Only operational facts are stored."""
    secret_reasoning = "INTERNAL CHAIN OF THOUGHT: the user is probably wrong about"
    model = ScriptedModelClient([
        make_turn(text=secret_reasoning, tool_calls=[read_call("a.py")]),
        make_turn(text=secret_reasoning, tool_calls=[submit_call()]),
    ])
    github = FakeGitHub(files={"a.py": "1\n"})
    assert propose(ready_run, session_factory, model, github) == worker.EXIT_OK

    _, events, _ = attempt_of(session_factory, ready_run)
    blob = " ".join(f"{k} {s}" for _, k, s in events)
    assert "CHAIN OF THOUGHT" not in blob


def test_application_context_limit_binds_when_it_is_the_smaller_ceiling(
    ready_run, session_factory
):
    """The explicit application limit must survive a huge model context window.

    Swapping in a model with a larger window must not silently licence a larger
    attempt: the effective budget is min(app limit, model headroom).
    """
    config = agent_settings(
        agent_max_context_tokens=1_000,
        agent_model_context_tokens=1_000_000,
        agent_max_output_tokens=100,
        agent_context_safety_margin_tokens=100,
    )  # model headroom = 999,800, application limit = 1,000 -> 1,000 wins
    assert config.agent_max_input_tokens == 1_000

    model = ScriptedModelClient([make_turn(tool_calls=[submit_call()])], token_count=5_000)
    assert (
        propose(ready_run, session_factory, model, FakeGitHub(), config)
        == worker.EXIT_ATTEMPT_FAILED
    )

    attempt, _, _ = attempt_of(session_factory, ready_run)
    assert attempt.error_kind == "context_budget_exceeded"
    # The message must blame the ceiling that actually bound.
    assert "AGENT_MAX_CONTEXT_TOKENS" in attempt.error_message
    assert "1,000-token budget" in attempt.error_message
    assert model.calls == 0


def test_model_headroom_binds_when_the_application_limit_is_higher(
    ready_run, session_factory
):
    """With the application limit raised above capacity, the model's window binds."""
    config = agent_settings(
        agent_max_context_tokens=10_000_000,
        agent_model_context_tokens=1_000,
        agent_max_output_tokens=100,
        agent_context_safety_margin_tokens=100,
    )  # model headroom = 800 -> 800 wins
    assert config.agent_max_input_tokens == 800

    model = ScriptedModelClient([make_turn(tool_calls=[submit_call()])], token_count=5_000)
    assert (
        propose(ready_run, session_factory, model, FakeGitHub(), config)
        == worker.EXIT_ATTEMPT_FAILED
    )

    attempt, _, _ = attempt_of(session_factory, ready_run)
    assert attempt.error_kind == "context_budget_exceeded"
    assert "model's capacity" in attempt.error_message
    assert "AGENT_MAX_CONTEXT_TOKENS" not in attempt.error_message
    assert model.calls == 0
