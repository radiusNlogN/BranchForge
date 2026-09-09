"""Bounded acquisition of an immutable source snapshot from GitHub.

The snapshot is a **source archive of one exact commit**, never a clone. That is a
security decision, not a convenience one: an extracted tarball has no `.git`
directory, so hooks, clean/smudge filters, submodule setup, and `.gitconfig`
inclusion cannot exist by construction rather than needing to be switched off.

Nothing here executes repository code. Extraction is the trust boundary and is
written defensively:

* Only regular files and directories are extracted. Symlinks, hardlinks, devices,
  and FIFOs are **rejected**, not sanitized — validating link targets is far
  easier to get subtly wrong than refusing them, and a repository that needs them
  is reported as an unsupported environment.
* Decompressed bytes are counted on the *decompressed* stream, including tar
  headers and padding, so a compression bomb is bounded by what it costs us to
  read rather than by the sum of the file sizes it declares.
* Paths are checked with `os.path.realpath` under the destination root, and
  duplicate paths, file/directory collisions, and any `.git` entry are refused.
* Members are written by hand. Archive ownership, permission, and mtime metadata
  is discarded; directories become 0755 and files 0644 so the container's
  non-root UID can traverse a read-only mount.

`tarfile`'s `data_filter` would cover some of this, but it does not exist on
Python 3.11.0 (it was backported in 3.11.4), so the checks are explicit.
"""

from __future__ import annotations

import gzip
import os
import tarfile
import time
import zlib
from dataclasses import dataclass, field
from typing import IO, Any

import httpx2

CODELOAD_HOST = "codeload.github.com"

# Entries we refuse outright, regardless of where they sit in the tree.
_FORBIDDEN_COMPONENTS = {".git"}


class SnapshotError(Exception):
    """Base class for snapshot acquisition failures."""

    kind = "snapshot_error"


class SnapshotUnavailable(SnapshotError):
    """The archive could not be retrieved (404, 451, upstream error)."""

    kind = "snapshot_unavailable"


class SnapshotTimeout(SnapshotError):
    """Acquisition exceeded its deadline."""

    kind = "snapshot_timeout"


class SnapshotTooLarge(SnapshotError):
    """The archive exceeded a transfer, decompression, or file-count budget."""

    kind = "snapshot_too_large"


class SnapshotRejected(SnapshotError):
    """An archive member was unsafe or the archive was malformed."""

    kind = "snapshot_rejected"


class SnapshotNetworkError(SnapshotError):
    """The archive host could not be reached."""

    kind = "snapshot_network_error"


@dataclass
class SnapshotLimits:
    """Every bound applied while acquiring a snapshot."""

    max_download_bytes: int = 80_000_000
    max_decompressed_bytes: int = 400_000_000
    max_files: int = 20_000
    deadline_seconds: float = 180.0
    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 60.0


@dataclass
class SnapshotResult:
    commit_sha: str
    root: str
    file_count: int
    decompressed_bytes: int
    download_bytes: int
    notes: list[str] = field(default_factory=list)


def archive_url(owner: str, repo: str, commit_sha: str) -> str:
    """Build the codeload archive URL from validated components only.

    Never accepts a repository-supplied URL. codeload serves an exact commit
    directly with **no redirect**, which is why the redirect ban in
    `github_client` does not need an exception for this path.
    """
    from app.github_client import _encode_segment, validate_commit_sha

    sha = validate_commit_sha(commit_sha)
    safe_owner = _encode_segment(owner, label="repository owner")
    safe_repo = _encode_segment(repo, label="repository name")
    return f"https://{CODELOAD_HOST}/{safe_owner}/{safe_repo}/tar.gz/{sha}"


class _CountingReader:
    """Wraps a stream, counting bytes read and enforcing a cap and a deadline.

    Placed on the **decompressed** side so tar headers and padding count toward
    the budget too — summing the file sizes an archive declares would let a
    crafted archive spend our time and disk without ever exceeding the total.
    """

    def __init__(self, stream: IO[bytes], *, limit: int, deadline: float, what: str) -> None:
        self._stream = stream
        self._limit = limit
        self._deadline = deadline
        self._what = what
        self.count = 0

    def read(self, size: int = -1) -> bytes:
        if time.monotonic() > self._deadline:
            raise SnapshotTimeout(f"Timed out while reading the {self._what}.")
        chunk = self._stream.read(size)
        self.count += len(chunk)
        if self.count > self._limit:
            raise SnapshotTooLarge(
                f"The {self._what} exceeded {self._limit:,} bytes while decompressing."
            )
        return chunk

    def close(self) -> None:  # pragma: no cover - tarfile may call this
        self._stream.close()


class _BoundedHTTPStream:
    """File-like view over an httpx2 streaming response with a transfer cap."""

    def __init__(self, response: Any, *, limit: int, deadline: float) -> None:
        self._iter = response.iter_bytes()
        self._limit = limit
        self._deadline = deadline
        self._buffer = bytearray()
        self._eof = False
        self.count = 0

    def read(self, size: int = -1) -> bytes:
        while not self._eof and (size < 0 or len(self._buffer) < size):
            if time.monotonic() > self._deadline:
                raise SnapshotTimeout("Timed out while downloading the source archive.")
            try:
                chunk = next(self._iter)
            except StopIteration:
                self._eof = True
                break
            self.count += len(chunk)
            if self.count > self._limit:
                raise SnapshotTooLarge(
                    f"The source archive exceeded the {self._limit:,}-byte download limit."
                )
            self._buffer.extend(chunk)
        if size < 0:
            data = bytes(self._buffer)
            self._buffer.clear()
            return data
        data = bytes(self._buffer[:size])
        del self._buffer[:size]
        return data

    def close(self) -> None:  # pragma: no cover
        self._eof = True


def _normalize_member_path(name: str, *, expected_prefix: str) -> str | None:
    """Strip the archive's top-level directory and validate what remains.

    Returns None for the root directory entry itself. Raises SnapshotRejected for
    anything unsafe.
    """
    if "\x00" in name:
        raise SnapshotRejected("Archive member name contains a NUL byte.")

    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts:
        return None
    if parts[0] != expected_prefix:
        raise SnapshotRejected(
            f"Archive member {name!r} is outside the expected top-level directory."
        )
    rest = parts[1:]
    if not rest:
        return None
    if any(part == ".." for part in rest):
        raise SnapshotRejected(f"Archive member {name!r} contains a parent-directory segment.")
    if any(part in _FORBIDDEN_COMPONENTS for part in rest):
        raise SnapshotRejected(f"Archive member {name!r} contains a forbidden path component.")
    if os.path.isabs("/".join(rest)) or rest[0].startswith("/"):
        raise SnapshotRejected(f"Archive member {name!r} is an absolute path.")
    return "/".join(rest)


def _check_contained(destination_root: str, target: str) -> None:
    """Confirm `target` really resolves inside `destination_root`."""
    root = os.path.realpath(destination_root)
    resolved = os.path.realpath(target)
    if resolved != root and not resolved.startswith(root + os.sep):
        raise SnapshotRejected(f"Archive member would escape the workspace: {target!r}")


def extract_stream(
    raw: IO[bytes],
    destination: str,
    *,
    limits: SnapshotLimits,
    deadline: float,
) -> tuple[int, int]:
    """Extract a gzipped tar stream into `destination`. Returns (files, bytes).

    The caller owns `destination`; it must already exist and be empty.
    """
    decompressed = gzip.GzipFile(fileobj=raw)  # type: ignore[arg-type]
    counter = _CountingReader(
        decompressed, limit=limits.max_decompressed_bytes, deadline=deadline, what="source archive"
    )

    seen: dict[str, str] = {}
    file_count = 0
    expected_prefix: str | None = None

    # mode="r|" is a non-seekable stream: the archive can never be buffered whole.
    try:
        with tarfile.open(fileobj=counter, mode="r|") as tar:  # type: ignore[arg-type]
            for member in tar:
                if time.monotonic() > deadline:
                    raise SnapshotTimeout("Timed out while extracting the source archive.")

                if expected_prefix is None:
                    first = [p for p in member.name.replace("\\", "/").split("/") if p not in ("", ".")]
                    if not first:
                        raise SnapshotRejected("Archive begins with an unnamed member.")
                    # Derived from the archive, not hardcoded to `{repo}-{sha}`.
                    expected_prefix = first[0]

                relative = _normalize_member_path(member.name, expected_prefix=expected_prefix)
                if relative is None:
                    continue

                if member.issym() or member.islnk():
                    raise SnapshotRejected(
                        f"Archive contains a link ({member.name!r}); links are not supported. "
                        f"Repositories using symlinks are outside the supported profile."
                    )
                if member.ischr() or member.isblk() or member.isfifo() or member.isdev():
                    raise SnapshotRejected(
                        f"Archive contains a device or FIFO entry ({member.name!r})."
                    )
                if not (member.isfile() or member.isdir()):
                    raise SnapshotRejected(
                        f"Archive member {member.name!r} has unsupported type {member.type!r}."
                    )

                kind = "dir" if member.isdir() else "file"
                previous = seen.get(relative)
                if previous is not None:
                    raise SnapshotRejected(
                        f"Archive contains a duplicate or colliding path: {relative!r} "
                        f"appears as both {previous} and {kind}."
                    )
                seen[relative] = kind

                target = os.path.join(destination, *relative.split("/"))
                parent = os.path.dirname(target)
                if parent:
                    os.makedirs(parent, mode=0o755, exist_ok=True)
                _check_contained(destination, target)

                if member.isdir():
                    os.makedirs(target, mode=0o755, exist_ok=True)
                    continue

                file_count += 1
                if file_count > limits.max_files:
                    raise SnapshotTooLarge(
                        f"The source archive holds more than {limits.max_files:,} files."
                    )

                source = tar.extractfile(member)
                if source is None:  # pragma: no cover - defensive
                    raise SnapshotRejected(f"Archive member {member.name!r} has no content.")

                # Written by hand: no chown, no mode restore, no mtime restore.
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
                try:
                    with os.fdopen(fd, "wb") as handle:
                        while True:
                            chunk = source.read(65536)
                            if not chunk:
                                break
                            handle.write(chunk)
                finally:
                    os.chmod(target, 0o644)
    except (tarfile.TarError, gzip.BadGzipFile, zlib.error, EOFError) as error:
        # Covers a truncated stream and a body that is not gzip at all.
        raise SnapshotRejected(f"The source archive is malformed: {error}") from error

    if file_count == 0:
        raise SnapshotRejected("The source archive contained no files.")

    _normalize_tree_permissions(destination)
    return file_count, counter.count


def _normalize_tree_permissions(root: str) -> None:
    """Make every directory traversable and every file readable (0755 / 0644).

    The workspace is bind-mounted read-only into a container running as a
    non-root UID, which can only read what is world-readable.
    """
    for current, dirnames, filenames in os.walk(root):
        for name in dirnames:
            path = os.path.join(current, name)
            if not os.path.islink(path):
                os.chmod(path, 0o755)
        for name in filenames:
            path = os.path.join(current, name)
            if not os.path.islink(path):
                # 0o644 unconditionally, which also clears any setuid/setgid/sticky
                # bit the archive asked for.
                os.chmod(path, 0o644)


def fetch_snapshot(
    *,
    owner: str,
    repo: str,
    commit_sha: str,
    destination: str,
    limits: SnapshotLimits | None = None,
    transport: Any = None,
) -> SnapshotResult:
    """Download and extract one commit's source tree into `destination`."""
    limits = limits or SnapshotLimits()
    deadline = time.monotonic() + limits.deadline_seconds
    url = archive_url(owner, repo, commit_sha)

    timeout = httpx2.Timeout(
        limits.read_timeout_seconds, connect=limits.connect_timeout_seconds
    )
    client_kwargs: dict[str, Any] = {
        "timeout": timeout,
        "follow_redirects": False,
        "headers": {"Accept": "application/x-gzip", "User-Agent": "BranchForge/0.4"},
    }
    if transport is not None:
        client_kwargs["transport"] = transport

    os.makedirs(destination, mode=0o755, exist_ok=True)

    with httpx2.Client(**client_kwargs) as client:
        try:
            with client.stream("GET", url) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    raise SnapshotUnavailable(
                        f"The archive host returned an unexpected redirect "
                        f"({response.status_code}); refusing to follow it."
                    )
                if response.status_code == 404:
                    raise SnapshotUnavailable(
                        f"No source archive for commit {commit_sha[:12]} — the repository "
                        f"or commit is not available anonymously."
                    )
                if response.status_code != 200:
                    raise SnapshotUnavailable(
                        f"The archive host returned HTTP {response.status_code}."
                    )

                stream = _BoundedHTTPStream(
                    response, limit=limits.max_download_bytes, deadline=deadline
                )
                file_count, decompressed = extract_stream(
                    stream, destination, limits=limits, deadline=deadline
                )
                downloaded = stream.count
        except httpx2.TimeoutException as error:
            raise SnapshotTimeout(f"Timed out retrieving the source archive: {error}") from error
        except httpx2.HTTPError as error:
            raise SnapshotNetworkError(
                f"Could not reach the archive host: {error}"
            ) from error

    return SnapshotResult(
        commit_sha=commit_sha,
        root=destination,
        file_count=file_count,
        decompressed_bytes=decompressed,
        download_bytes=downloaded,
    )
