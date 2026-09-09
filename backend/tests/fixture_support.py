"""Helpers for building the deterministic verification fixtures.

Patches are generated with `difflib` rather than written by hand: hunk headers
and line counts are then correct by construction, so a fixture cannot fail for a
reason unrelated to what it is testing. (An earlier hand-written diff in this
project was rejected by our own validator for exactly that reason.)
"""

from __future__ import annotations

import difflib
import io
import os
import shutil
import tarfile
from pathlib import Path

SAMPLE_REPO = Path(__file__).parent / "sample_repo"

CALC = "calc/__init__.py"
TEST_FILE = "tests/test_calc.py"


def copy_sample_repo(destination: str | Path) -> str:
    """Copy the fixture repository template to a fresh directory."""
    destination = str(destination)
    shutil.copytree(SAMPLE_REPO, destination)
    return destination


def read_sample(relative_path: str) -> str:
    return (SAMPLE_REPO / relative_path).read_text()


def make_patch(relative_path: str, new_content: str, *, original: str | None = None) -> str:
    """Build a valid unified diff replacing `relative_path`'s contents."""
    old = original if original is not None else read_sample(relative_path)
    return "".join(
        difflib.unified_diff(
            old.splitlines(True),
            new_content.splitlines(True),
            fromfile=f"a/{relative_path}",
            tofile=f"b/{relative_path}",
        )
    )


def make_delete_patch(relative_path: str) -> str:
    """Build a diff that deletes a file outright."""
    old = read_sample(relative_path)
    return "".join(
        difflib.unified_diff(
            old.splitlines(True), [], fromfile=f"a/{relative_path}", tofile="/dev/null"
        )
    )


def make_add_patch(relative_path: str, content: str) -> str:
    """Build a diff that creates a new file."""
    return "".join(
        difflib.unified_diff(
            [], content.splitlines(True), fromfile="/dev/null", tofile=f"b/{relative_path}"
        )
    )


# --- The scenario patches ---------------------------------------------------


def correct_patch() -> str:
    """Fixes the bug: subtraction becomes addition."""
    return make_patch(CALC, read_sample(CALC).replace("return a - b", "return a + b"))


def incorrect_patch() -> str:
    """Applies cleanly, fixes nothing, and breaks a previously passing test."""
    return make_patch(CALC, read_sample(CALC).replace("return a - b", "return a * b"))


def ineffective_patch() -> str:
    """Applies cleanly and changes nothing observable — still failing."""
    return make_patch(CALC, read_sample(CALC).replace("return a - b", "return a - b + 0"))


def non_applying_patch() -> str:
    """Valid diff syntax, but its context does not match the real file."""
    fabricated = "def add(a, b):\n    return completely_different(a, b)\n"
    return make_patch(CALC, "def add(a, b):\n    return a + b\n", original=fabricated)


def deletes_failing_test_patch() -> str:
    """Removes the test that exposes the bug — the yardstick-moving attack."""
    return make_delete_patch(TEST_FILE)


def skips_failing_test_patch() -> str:
    """Makes the failing test skip itself, via a source-level flag.

    The test file is untouched, so restoring the original tests cannot undo this:
    the skip has to be caught by requiring an explicit pass.
    """
    return make_patch(CALC, read_sample(CALC).replace("FEATURE_ENABLED = True", "FEATURE_ENABLED = False"))


def uncollects_tests_patch() -> str:
    """Shrinks the parametrize source so fewer tests are collected."""
    return make_patch(CALC, read_sample(CALC).replace('CASES = ["alpha", "beta"]', 'CASES = ["alpha"]'))


def breaks_import_patch() -> str:
    """Makes the package fail to import: collection error, not an improvement."""
    return make_patch(CALC, read_sample(CALC) + "\nraise RuntimeError('broken import')\n")


def hanging_source_patch(seconds: int = 600) -> str:
    """Makes the application function hang, so the patched run must time out."""
    new = read_sample(CALC).replace(
        "    # The bug: subtraction where addition is meant.\n    return a - b",
        f"    import time\n    time.sleep({seconds})\n    return a + b",
    )
    return make_patch(CALC, new)


def noisy_patch(megabytes: int = 4) -> str:
    """Fixes the bug but floods stdout, to exercise the output cap."""
    new = read_sample(CALC).replace(
        "    # The bug: subtraction where addition is meant.\n    return a - b",
        f"    print('x' * 1024 * 1024 * {megabytes})\n    return a + b",
    )
    return make_patch(CALC, new)


def adds_conftest_patch() -> str:
    """Adds a conftest that would suppress the failing test if it survived."""
    return make_add_patch(
        "conftest.py",
        "collect_ignore_glob = ['tests/test_calc.py']\n",
    )


# --- Archive helpers for snapshot tests -------------------------------------


Member = "tarfile.TarInfo | tuple[tarfile.TarInfo, bytes]"


def build_tar_gz(entries: list) -> bytes:
    """Build a .tar.gz in memory from explicit members.

    Each entry is either a bare `TarInfo` (directories, links) or a
    `(TarInfo, payload)` pair. Payloads travel with their member rather than in a
    name-keyed map, so an archive with duplicate names — which is exactly what a
    hostile archive may contain — can be represented faithfully.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for entry in entries:
            if isinstance(entry, tuple):
                info, data = entry
                tar.addfile(info, io.BytesIO(data))
            else:
                tar.addfile(entry, None)
    return buffer.getvalue()


def file_member(name: str, data: bytes, mode: int = 0o644):
    """A regular-file member paired with its payload."""
    info = tarfile.TarInfo(name)
    info.type = tarfile.REGTYPE
    info.size = len(data)
    info.mode = mode
    return (info, data)


def dir_member(name: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    info.mode = 0o755
    return info


def symlink_member(name: str, target: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.SYMTYPE
    info.linkname = target
    return info


def sample_repo_tar_gz(prefix: str = "sample-repo-abc123") -> bytes:
    """A tarball of the fixture repository, shaped the way codeload ships one."""
    members: list = [dir_member(f"{prefix}/")]
    for current, _dirs, files in os.walk(SAMPLE_REPO):
        for name in sorted(files):
            full = Path(current) / name
            relative = full.relative_to(SAMPLE_REPO).as_posix()
            members.append(file_member(f"{prefix}/{relative}", full.read_bytes()))
    return build_tar_gz(members)
