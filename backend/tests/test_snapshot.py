"""Snapshot acquisition: the extraction boundary.

Every case here is an archive that should never land on disk. `tarfile` on this
Python has no `data_filter`, so these checks are the only thing between a hostile
archive and the filesystem.
"""

from __future__ import annotations

import io
import os
import tarfile
import time

import httpx2
import pytest

from app import snapshot
from app.github_client import UpstreamProtocolError
from tests.fixture_support import (
    build_tar_gz,
    dir_member,
    file_member,
    sample_repo_tar_gz,
    symlink_member,
)

PREFIX = "sample-repo-abc123"
SHA = "a" * 40


def extract(archive: bytes, destination: str, **kwargs):
    limits = snapshot.SnapshotLimits(**kwargs)
    return snapshot.extract_stream(
        io.BytesIO(archive),
        destination,
        limits=limits,
        deadline=time.monotonic() + limits.deadline_seconds,
    )


def test_extracts_a_normal_archive_and_strips_the_top_level_directory(tmp_path):
    destination = str(tmp_path / "out")
    files, decompressed = extract(sample_repo_tar_gz(PREFIX), destination)

    assert files == 4
    assert decompressed > 0
    # The `{repo}-{sha}/` wrapper is gone, not preserved.
    assert os.path.isfile(os.path.join(destination, "calc", "__init__.py"))
    assert os.path.isfile(os.path.join(destination, "tests", "test_calc.py"))
    assert not os.path.exists(os.path.join(destination, PREFIX))


def test_extracted_permissions_let_the_container_user_read_everything(tmp_path):
    """The workspace is mounted read-only into a container running as UID 65534."""
    destination = str(tmp_path / "out")
    extract(sample_repo_tar_gz(PREFIX), destination)

    for current, dirnames, filenames in os.walk(destination):
        for name in dirnames:
            mode = os.stat(os.path.join(current, name)).st_mode & 0o777
            assert mode == 0o755, f"{name} is not traversable by others"
        for name in filenames:
            mode = os.stat(os.path.join(current, name)).st_mode & 0o777
            assert mode == 0o644, f"{name} is not readable by others"


def test_archive_permissions_are_discarded_not_restored(tmp_path):
    """A setuid bit in the archive must not survive extraction."""
    data = b"#!/bin/sh\necho hi\n"
    archive = build_tar_gz(
        [dir_member(f"{PREFIX}/"), file_member(f"{PREFIX}/run.sh", data, mode=0o4777)],
    )
    destination = str(tmp_path / "out")
    extract(archive, destination)

    mode = os.stat(os.path.join(destination, "run.sh")).st_mode
    assert mode & 0o777 == 0o644
    assert not mode & 0o4000, "setuid bit survived extraction"


def test_rejects_symlink_members(tmp_path):
    archive = build_tar_gz(
        [dir_member(f"{PREFIX}/"), symlink_member(f"{PREFIX}/escape", "/etc/passwd")]
    )
    with pytest.raises(snapshot.SnapshotRejected, match="link"):
        extract(archive, str(tmp_path / "out"))


def test_rejects_hardlink_members(tmp_path):
    info = tarfile.TarInfo(f"{PREFIX}/hard")
    info.type = tarfile.LNKTYPE
    info.linkname = f"{PREFIX}/calc/__init__.py"
    archive = build_tar_gz([dir_member(f"{PREFIX}/"), info])
    with pytest.raises(snapshot.SnapshotRejected, match="link"):
        extract(archive, str(tmp_path / "out"))


def test_rejects_device_members(tmp_path):
    info = tarfile.TarInfo(f"{PREFIX}/dev")
    info.type = tarfile.CHRTYPE
    archive = build_tar_gz([dir_member(f"{PREFIX}/"), info])
    with pytest.raises(snapshot.SnapshotRejected, match="device|FIFO"):
        extract(archive, str(tmp_path / "out"))


def test_rejects_parent_directory_traversal(tmp_path):
    data = b"owned"
    archive = build_tar_gz(
        [dir_member(f"{PREFIX}/"), file_member(f"{PREFIX}/../escaped.txt", data)],
    )
    destination = str(tmp_path / "out")
    with pytest.raises(snapshot.SnapshotRejected, match="parent-directory"):
        extract(archive, destination)
    assert not os.path.exists(str(tmp_path / "escaped.txt"))


def test_rejects_members_outside_the_top_level_prefix(tmp_path):
    data = b"stray"
    archive = build_tar_gz(
        [dir_member(f"{PREFIX}/"), file_member("somewhere-else/file.txt", data)],
    )
    with pytest.raises(snapshot.SnapshotRejected, match="outside the expected"):
        extract(archive, str(tmp_path / "out"))


def test_rejects_absolute_paths(tmp_path):
    data = b"absolute"
    archive = build_tar_gz(
        [dir_member(f"{PREFIX}/"), file_member("/etc/cron.d/evil", data)],
    )
    with pytest.raises(snapshot.SnapshotRejected):
        extract(archive, str(tmp_path / "out"))


def test_rejects_git_directory_entries(tmp_path):
    """A `.git` directory would reintroduce hooks, filters, and config."""
    data = b"[core]\n\tfsmonitor = /tmp/pwn\n"
    archive = build_tar_gz(
        [dir_member(f"{PREFIX}/"), file_member(f"{PREFIX}/.git/config", data)],
    )
    with pytest.raises(snapshot.SnapshotRejected, match="forbidden"):
        extract(archive, str(tmp_path / "out"))


def test_rejects_duplicate_paths(tmp_path):
    data = b"first"
    other = b"second"
    archive = build_tar_gz(
        [
            dir_member(f"{PREFIX}/"),
            file_member(f"{PREFIX}/a.txt", data),
            file_member(f"{PREFIX}/a.txt", other),
        ],
    )
    with pytest.raises(snapshot.SnapshotRejected, match="duplicate|colliding"):
        extract(archive, str(tmp_path / "out"))


def test_rejects_file_and_directory_collisions(tmp_path):
    data = b"content"
    archive = build_tar_gz(
        [
            dir_member(f"{PREFIX}/"),
            file_member(f"{PREFIX}/thing", data),
            dir_member(f"{PREFIX}/thing"),
        ],
    )
    with pytest.raises(snapshot.SnapshotRejected, match="duplicate|colliding"):
        extract(archive, str(tmp_path / "out"))


def test_bounds_decompressed_bytes_not_just_declared_sizes(tmp_path):
    """A compression bomb must be stopped by what it costs us to read."""
    payload = b"\0" * 5_000_000  # compresses to almost nothing
    archive = build_tar_gz(
        [dir_member(f"{PREFIX}/"), file_member(f"{PREFIX}/big.bin", payload)],
    )
    assert len(archive) < 100_000, "the fixture should be highly compressible"

    with pytest.raises(snapshot.SnapshotTooLarge, match="decompressing"):
        extract(archive, str(tmp_path / "out"), max_decompressed_bytes=1_000_000)


def test_bounds_the_file_count(tmp_path):
    members = [dir_member(f"{PREFIX}/")]
    for index in range(30):
        members.append(file_member(f"{PREFIX}/f{index}.txt", b"x"))
    archive = build_tar_gz(members)

    with pytest.raises(snapshot.SnapshotTooLarge, match="more than"):
        extract(archive, str(tmp_path / "out"), max_files=10)


def test_an_empty_archive_is_rejected(tmp_path):
    archive = build_tar_gz([dir_member(f"{PREFIX}/")])
    with pytest.raises(snapshot.SnapshotRejected, match="no files"):
        extract(archive, str(tmp_path / "out"))


def test_a_malformed_archive_is_rejected(tmp_path):
    with pytest.raises(snapshot.SnapshotRejected, match="malformed|not.*gzip|Not a gzipped"):
        extract(b"this is not a tarball at all", str(tmp_path / "out"))


def test_the_deadline_is_enforced(tmp_path):
    limits = snapshot.SnapshotLimits()
    with pytest.raises(snapshot.SnapshotTimeout):
        snapshot.extract_stream(
            io.BytesIO(sample_repo_tar_gz(PREFIX)),
            str(tmp_path / "out"),
            limits=limits,
            deadline=time.monotonic() - 1.0,  # already expired
        )


# --- URL construction -------------------------------------------------------


def test_archive_url_is_built_from_validated_components():
    url = snapshot.archive_url("pallets", "click", "b" * 40)
    assert url == f"https://codeload.github.com/pallets/click/tar.gz/{'b' * 40}"


def test_archive_url_rejects_a_non_sha_ref():
    """Only a full commit SHA may be interpolated — never a branch name."""
    with pytest.raises(UpstreamProtocolError):
        snapshot.archive_url("pallets", "click", "main")


def test_archive_url_escapes_path_segments():
    url = snapshot.archive_url("own/er", "re po", "c" * 40)
    assert "own%2Fer" in url and "re%20po" in url


# --- HTTP behaviour ---------------------------------------------------------


def _transport(handler):
    return httpx2.MockTransport(handler)


def test_fetch_snapshot_downloads_and_extracts(tmp_path):
    archive = sample_repo_tar_gz(PREFIX)

    def handler(request):
        assert request.url.host == "codeload.github.com"
        return httpx2.Response(200, content=archive)

    result = snapshot.fetch_snapshot(
        owner="o",
        repo="r",
        commit_sha=SHA,
        destination=str(tmp_path / "out"),
        transport=_transport(handler),
    )
    assert result.file_count == 4
    assert os.path.isfile(str(tmp_path / "out" / "calc" / "__init__.py"))


def test_fetch_snapshot_refuses_to_follow_a_redirect(tmp_path):
    def handler(request):
        return httpx2.Response(302, headers={"location": "https://evil.test/payload.tar.gz"})

    with pytest.raises(snapshot.SnapshotUnavailable, match="redirect"):
        snapshot.fetch_snapshot(
            owner="o",
            repo="r",
            commit_sha=SHA,
            destination=str(tmp_path / "out"),
            transport=_transport(handler),
        )


def test_fetch_snapshot_reports_a_missing_commit(tmp_path):
    def handler(request):
        return httpx2.Response(404)

    with pytest.raises(snapshot.SnapshotUnavailable, match="not available anonymously"):
        snapshot.fetch_snapshot(
            owner="o",
            repo="r",
            commit_sha=SHA,
            destination=str(tmp_path / "out"),
            transport=_transport(handler),
        )


def test_fetch_snapshot_bounds_the_download(tmp_path):
    def handler(request):
        return httpx2.Response(200, content=b"\0" * 200_000)

    limits = snapshot.SnapshotLimits(max_download_bytes=1_000)
    with pytest.raises(snapshot.SnapshotTooLarge, match="download limit"):
        snapshot.fetch_snapshot(
            owner="o",
            repo="r",
            commit_sha=SHA,
            destination=str(tmp_path / "out"),
            limits=limits,
            transport=_transport(handler),
        )
