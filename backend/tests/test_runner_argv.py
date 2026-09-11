"""What the container is actually asked to do.

These tests need no Docker daemon: they assert the argument vector, which is
where every isolation guarantee either exists or does not. The negative
assertions matter as much as the positive ones — the spec's forbidden list
(credentials, the host home directory, the database, the Docker socket) is
checked directly rather than by reading the code and hoping.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from app import runner
from app.runner import RunnerLimits, build_run_argv

# A synthetic value. Leakage tests never use the real credential.
SENTINEL_KEY = "sk-ant-SENTINEL-must-never-reach-a-container-000000"


@pytest.fixture
def argv(tmp_path):
    workspace = tmp_path / "ws"
    results = tmp_path / "res"
    workspace.mkdir()
    results.mkdir()
    return build_run_argv(
        workspace=str(workspace),
        results_dir=str(results),
        image_id="sha256:deadbeef",
        container_name="branchforge-verify-test",
        limits=RunnerLimits(),
    )


def test_it_is_an_argument_array_not_a_shell_string(argv):
    assert isinstance(argv, list)
    assert all(isinstance(item, str) for item in argv)
    # No element smuggles shell metacharacters that only matter under a shell.
    joined = " ".join(argv)
    assert ";" not in joined and "&&" not in joined and "|" not in joined


def test_network_is_disabled(argv):
    assert "--network=none" in argv


def test_it_runs_as_a_non_root_user(argv):
    assert "--user" in argv
    assert argv[argv.index("--user") + 1] == "65534:65534"


def test_the_root_filesystem_is_read_only_with_bounded_scratch(argv):
    assert "--read-only" in argv
    tmpfs = [a for a in argv if a.startswith("--tmpfs=")]
    assert len(tmpfs) == 1
    assert "/tmp:" in tmpfs[0]
    assert "size=" in tmpfs[0]
    assert "noexec" in tmpfs[0] and "nosuid" in tmpfs[0]


def test_resource_ceilings_are_present(argv):
    assert "--memory=512m" in argv
    assert "--cpus=1.0" in argv
    assert "--pids-limit=256" in argv


def test_capabilities_are_dropped_and_privileges_cannot_grow(argv):
    assert "--cap-drop=ALL" in argv
    assert "--security-opt=no-new-privileges" in argv


def test_daemon_logging_is_disabled(argv):
    """Bounding our own reader does not bound the daemon's log file."""
    assert "--log-driver=none" in argv


def test_the_container_is_named_and_removed(argv):
    assert "--rm" in argv
    assert "--name" in argv
    assert argv[argv.index("--name") + 1].startswith("branchforge-verify-")


def test_exactly_two_mounts_the_source_read_only_and_a_results_directory(argv, tmp_path):
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    assert len(mounts) == 2, f"unexpected mounts: {mounts}"

    workspace_mounts = [m for m in mounts if m.endswith(":/workspace:ro")]
    results_mounts = [m for m in mounts if m.endswith(":/results:rw")]
    assert len(workspace_mounts) == 1, "the source tree must be mounted exactly once"
    assert len(results_mounts) == 1
    # The source is read-only; only the runner-owned results directory is writable.
    assert ":/workspace:rw" not in " ".join(mounts)


def test_mount_paths_are_resolved_for_docker_desktop(tmp_path):
    """macOS mkdtemp yields /var/folders/...; Docker only shares /private/var/...."""
    argv = build_run_argv(
        workspace="/tmp/ws",
        results_dir="/tmp/res",
        image_id="sha256:x",
        container_name="c",
        limits=RunnerLimits(),
    )
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    for mount in mounts:
        host_path = mount.rsplit(":", 2)[0]
        assert host_path == os.path.realpath(host_path)


def test_no_credential_reaches_the_container(monkeypatch, tmp_path):
    """A synthetic key in the environment must not be passed through."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", SENTINEL_KEY)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    argv = build_run_argv(
        workspace=str(workspace),
        results_dir=str(tmp_path / "res"),
        image_id="sha256:x",
        container_name="c",
        limits=RunnerLimits(),
    )
    joined = "\n".join(argv)
    assert SENTINEL_KEY not in joined
    assert "ANTHROPIC" not in joined.upper()

    # Every -e is accounted for, so a credential cannot ride along unnoticed.
    passed_env = {argv[i + 1].split("=", 1)[0] for i, a in enumerate(argv) if a == "-e"}
    assert passed_env == {
        "HOME",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONPATH",
        "BF_REPORT_PATH",
        "BF_REPORT_MAX_TESTS",
        "BF_REPORT_MAX_MESSAGE_CHARS",
    }


def test_the_docker_socket_is_never_mounted(argv):
    joined = " ".join(argv)
    assert "docker.sock" not in joined
    assert "/var/run/docker" not in joined


def test_the_host_home_directory_is_never_mounted(argv):
    home = os.path.expanduser("~")
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    for mount in mounts:
        host_path = mount.rsplit(":", 2)[0]
        assert host_path != home
        assert not host_path.startswith(home + os.sep)


def test_the_application_database_is_never_mounted(argv):
    joined = " ".join(argv)
    assert "branchforge.db" not in joined
    assert ".env" not in joined


def test_home_points_at_writable_scratch(argv):
    """--read-only plus a non-root UID breaks unless HOME is writable."""
    env = {argv[i + 1].split("=", 1)[0]: argv[i + 1].split("=", 1)[1]
           for i, a in enumerate(argv) if a == "-e"}
    assert env["HOME"] == "/tmp"


def test_the_pytest_invocation_is_runner_owned(argv):
    """`-o addopts=` stops a repository's ini options joining our command line."""
    assert argv[-6:] == [
        "-o", "addopts=",
        "-p", "no:cacheprovider",
        "-p", "bf_report",
    ] or ("-o" in argv and argv[argv.index("-o") + 1] == "addopts=")
    assert "-p" in argv
    plugins = [argv[i + 1] for i, a in enumerate(argv) if a == "-p"]
    assert "bf_report" in plugins, "the runner-owned report plugin must be loaded"
    assert "no:cacheprovider" in plugins


def test_the_report_plugin_cannot_be_shadowed_by_the_repository(argv):
    """/opt/branchforge precedes the workspace on PYTHONPATH."""
    env = {argv[i + 1].split("=", 1)[0]: argv[i + 1].split("=", 1)[1]
           for i, a in enumerate(argv) if a == "-e"}
    entries = env["PYTHONPATH"].split(":")
    assert entries[0] == "/opt/branchforge"
    assert "/workspace" in entries and "/workspace/src" in entries


def test_the_resolved_image_id_is_used_not_a_tag(tmp_path):
    argv = build_run_argv(
        workspace=str(tmp_path),
        results_dir=str(tmp_path / "r"),
        image_id="sha256:abc123",
        container_name="c",
        limits=RunnerLimits(),
    )
    assert "sha256:abc123" in argv
    assert not any(a == "branchforge-runner-python:1" for a in argv)


def test_no_repository_or_model_supplied_text_reaches_the_argv(tmp_path):
    """Nothing from the issue, the model, or the repository is interpolated."""
    argv = build_run_argv(
        workspace=str(tmp_path),
        results_dir=str(tmp_path / "r"),
        image_id="sha256:x",
        container_name="c",
        limits=RunnerLimits(),
    )
    # The only variable parts are runner-owned paths, the name, and the image ID.
    variable = {str(tmp_path), str(tmp_path / "r"), "sha256:x", "c"}
    for item in argv:
        assert item in variable or not any(
            marker in item for marker in ("http://", "https://", "$(", "`")
        )


# --- Ownership labels (milestone 5) -----------------------------------------

ORCH_ID = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
ATTEMPT_ID = "11111111-2222-3333-4444-555555555555"


def test_ownership_labels_are_present_when_given(tmp_path):
    argv = build_run_argv(
        workspace=str(tmp_path), results_dir=str(tmp_path / "r"), image_id="sha256:x",
        container_name="branchforge-verify-x", limits=RunnerLimits(),
        labels={runner.LABEL_ORCHESTRATION: ORCH_ID, runner.LABEL_ATTEMPT: ATTEMPT_ID},
    )
    labels = [argv[i + 1] for i, a in enumerate(argv) if a == "--label"]
    assert labels == [
        f"branchforge.attempt={ATTEMPT_ID}",
        f"branchforge.orchestration={ORCH_ID}",
    ]
    # Still exactly the same isolation flags and mounts.
    assert "--network=none" in argv and len([a for a in argv if a == "-v"]) == 2


def test_no_labels_are_added_for_a_manual_verification(argv):
    assert "--label" not in argv


@pytest.mark.parametrize(
    "labels",
    [
        {"com.example.other": ORCH_ID},
        {runner.LABEL_ATTEMPT: "Robert'); DROP TABLE"},
        {runner.LABEL_ATTEMPT: "$(whoami)"},
        {runner.LABEL_ATTEMPT: ""},
    ],
)
def test_label_values_must_be_runner_owned_identifiers(tmp_path, labels):
    with pytest.raises(ValueError):
        build_run_argv(
            workspace=str(tmp_path), results_dir=str(tmp_path / "r"), image_id="sha256:x",
            container_name="c", limits=RunnerLimits(), labels=labels,
        )


def test_labelled_cleanup_filters_on_exactly_one_label_and_confirms(monkeypatch):
    calls: list[list[str]] = []
    listings = iter(["abc123\ndef456\n", ""])

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[1] == "ps":
            return subprocess.CompletedProcess(args, 0, next(listings), "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = runner.remove_labelled_containers(runner.LABEL_ORCHESTRATION, ORCH_ID, limits=RunnerLimits())

    assert result.confirmed is True and result.removed == ["abc123", "def456"]
    ps_calls = [c for c in calls if c[1] == "ps"]
    assert len(ps_calls) == 2, "removal must be confirmed by a second listing"
    for call in ps_calls:
        assert call[call.index("--filter") + 1] == f"label=branchforge.orchestration={ORCH_ID}"
    assert [c[1:] for c in calls if c[1] == "rm"] == [["rm", "-f", "abc123"], ["rm", "-f", "def456"]]


def test_labelled_cleanup_is_unconfirmed_when_containers_remain_or_docker_fails(monkeypatch):
    def still_there(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, "abc123\n" if args[1] == "ps" else "", "")

    monkeypatch.setattr(subprocess, "run", still_there)
    assert runner.remove_labelled_containers(
        runner.LABEL_ATTEMPT, ATTEMPT_ID, limits=RunnerLimits()
    ).confirmed is False

    def broken(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, "", "Cannot connect to the Docker daemon")

    monkeypatch.setattr(subprocess, "run", broken)
    result = runner.remove_labelled_containers(runner.LABEL_ATTEMPT, ATTEMPT_ID, limits=RunnerLimits())
    assert result.confirmed is False and result.errors


def test_labelled_cleanup_refuses_an_invalid_label():
    with pytest.raises(ValueError):
        runner.remove_labelled_containers("branchforge.attempt", "", limits=RunnerLimits())


# --- Report reading ---------------------------------------------------------


def test_a_missing_report_is_an_error_not_an_absence_of_failures(tmp_path):
    report, error = runner.read_report(str(tmp_path), limits=RunnerLimits())
    assert report is None
    assert error and "no structured report" in error.lower()


def test_an_oversized_report_is_refused_before_parsing(tmp_path):
    path = tmp_path / runner.REPORT_FILENAME
    path.write_text("x" * 5000)
    report, error = runner.read_report(str(tmp_path), limits=RunnerLimits(max_report_bytes=100))
    assert report is None
    assert error and "refusing to parse" in error


def test_a_malformed_report_is_refused(tmp_path):
    (tmp_path / runner.REPORT_FILENAME).write_text("{not json")
    report, error = runner.read_report(str(tmp_path), limits=RunnerLimits())
    assert report is None
    assert error and "could not be parsed" in error


def test_a_report_with_the_wrong_schema_is_refused(tmp_path):
    (tmp_path / runner.REPORT_FILENAME).write_text('{"schema": "something.else", "tests": {}}')
    report, error = runner.read_report(str(tmp_path), limits=RunnerLimits())
    assert report is None
    assert error and "schema" in error


def test_a_valid_report_is_returned(tmp_path):
    (tmp_path / runner.REPORT_FILENAME).write_text(
        '{"schema": "branchforge.report.v1", "tests": {}, "collected": [], "counts": {}}'
    )
    report, error = runner.read_report(str(tmp_path), limits=RunnerLimits())
    assert error is None
    assert report is not None and report["schema"] == "branchforge.report.v1"


# --- Image resolution -------------------------------------------------------


def test_image_id_falls_back_when_inspect_cannot_read_a_manifest_list(monkeypatch):
    """Docker Desktop's containerd store fails `image inspect` on a manifest list.

    A tag built by buildx with default attestations reports "No such image" from
    `docker image inspect` while `docker images` lists it. Resolution must not
    conclude the image is missing on the strength of the first lookup alone.
    """
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(
                args, 1, "", 'Error response from daemon: {"message":"No such image: x:1"}'
            )
        return subprocess.CompletedProcess(args, 0, "sha256:feedface\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    resolved = runner.resolve_image_id("x:1", limits=RunnerLimits())

    assert resolved == "sha256:feedface"
    assert [c[1:3] for c in calls] == [["image", "inspect"], ["image", "ls"]]


def test_a_genuinely_missing_image_still_raises_with_build_instructions(monkeypatch):
    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, "", "No such image")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(runner.DockerUnavailable, match="docker build"):
        runner.resolve_image_id("missing:1", limits=RunnerLimits())


def test_an_unreachable_daemon_is_named_as_such(monkeypatch):
    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(
            args, 1, "", "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(runner.DockerUnavailable, match="daemon is not reachable"):
        runner.resolve_image_id("x:1", limits=RunnerLimits())


def test_a_missing_docker_cli_is_reported_clearly(monkeypatch):
    def fake_run(args, **kwargs):
        raise FileNotFoundError(args[0])

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(runner.DockerUnavailable, match="not found on PATH"):
        runner.resolve_image_id("x:1", limits=RunnerLimits(docker_binary="nope"))
