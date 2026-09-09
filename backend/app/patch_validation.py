"""Local validation of a model-submitted unified diff.

Runtime validation runs even though the tool schema is `strict: true` — a schema
guarantees argument *shape*, never that the diff is well formed or that its paths
are safe. The model's output is untrusted input.

**What this does not establish:** that the patch applies cleanly, compiles, or
fixes anything. Nothing here runs `patch`, `git apply`, or any repository code.
A diff that passes validation is still an unverified proposal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# @@ -oldStart,oldCount +newStart,newCount @@ optional heading
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

_NULL_PATH = "/dev/null"

# Headers that are metadata we can safely ignore.
_IGNORABLE_PREFIXES = (
    "diff --git ",
    "index ",
    "new file mode ",
    "deleted file mode ",
    "old mode ",
    "new mode ",
    "similarity index ",
    "dissimilarity index ",
)

# Operations this milestone does not support.
_UNSUPPORTED_PREFIXES = {
    "rename from ": "file renames",
    "rename to ": "file renames",
    "copy from ": "file copies",
    "copy to ": "file copies",
    "GIT binary patch": "binary patches",
    "Binary files ": "binary patches",
}


class PatchInvalid(ValueError):
    """The submitted diff is unusable. The message is shown to the model."""


@dataclass(frozen=True)
class PatchedFile:
    old_path: str
    new_path: str
    hunks: int

    @property
    def display_path(self) -> str:
        return self.new_path if self.new_path != _NULL_PATH else self.old_path


def _strip_prefix(path: str) -> str:
    """Drop a leading `a/` or `b/` that git-style diffs carry."""
    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            return path[len(prefix) :]
    return path


def _clean_header_path(raw: str) -> str:
    """Take the path from a `---`/`+++` line, dropping any trailing timestamp."""
    value = raw.strip()
    # Unified diffs may append a tab-separated timestamp.
    if "\t" in value:
        value = value.split("\t", 1)[0].strip()
    if value.startswith('"') and value.endswith('"') and len(value) > 1:
        value = value[1:-1]
    return value


def _validate_path(raw: str, *, side: str) -> str:
    """Reject paths that escape the repository or are otherwise unsafe."""
    path = _clean_header_path(raw)
    if path == _NULL_PATH:
        return path

    path = _strip_prefix(path)
    if not path:
        raise PatchInvalid(f"The {side} path in a diff header is empty.")
    if path.startswith("/"):
        raise PatchInvalid(f"Absolute paths are not allowed: {path!r}.")
    if "\x00" in path or "\n" in path:
        raise PatchInvalid(f"The {side} path contains an illegal character: {path!r}.")
    if path.startswith("~"):
        raise PatchInvalid(f"Home-relative paths are not allowed: {path!r}.")

    segments = path.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise PatchInvalid(
            f"Path {path!r} is not allowed: it must be a plain repository-relative "
            f"path with no empty or '..' segments."
        )
    return path


def validate_unified_diff(diff: str, *, max_bytes: int) -> list[PatchedFile]:
    """Parse and check a unified diff, returning the files it touches.

    Raises `PatchInvalid` with a message suitable for returning to the model as a
    tool error, so it can correct the submission within its remaining budget.
    """
    if not diff or not diff.strip():
        raise PatchInvalid("The diff is empty. Provide a unified diff.")

    encoded_size = len(diff.encode("utf-8"))
    if encoded_size > max_bytes:
        raise PatchInvalid(
            f"The diff is {encoded_size:,} bytes, over the {max_bytes:,}-byte limit. "
            f"Submit a smaller, more focused patch."
        )

    lines = diff.splitlines()
    files: list[PatchedFile] = []

    index = 0
    old_path: str | None = None
    current: dict[str, object] | None = None

    while index < len(lines):
        line = lines[index]

        for prefix, label in _UNSUPPORTED_PREFIXES.items():
            if line.startswith(prefix):
                raise PatchInvalid(
                    f"This patch uses {label}, which is not supported. Submit a "
                    f"plain unified diff that modifies, creates, or deletes text files."
                )

        if line.startswith(_IGNORABLE_PREFIXES):
            index += 1
            continue

        if line.startswith("--- "):
            old_path = _validate_path(line[4:], side="original")
            index += 1
            continue

        if line.startswith("+++ "):
            if old_path is None:
                raise PatchInvalid(
                    "Found a '+++' header with no matching '---' header before it."
                )
            new_path = _validate_path(line[4:], side="new")
            if old_path == _NULL_PATH and new_path == _NULL_PATH:
                raise PatchInvalid("A file section cannot have /dev/null on both sides.")
            current = {"old": old_path, "new": new_path, "hunks": 0}
            files.append(PatchedFile(old_path=old_path, new_path=new_path, hunks=0))
            old_path = None
            index += 1
            continue

        match = _HUNK_RE.match(line)
        if match:
            if current is None:
                raise PatchInvalid(
                    "Found a hunk header (@@) before any file header (--- / +++)."
                )
            declared_old = int(match.group(2)) if match.group(2) is not None else 1
            declared_new = int(match.group(4)) if match.group(4) is not None else 1

            index += 1
            seen_old = seen_new = 0
            while index < len(lines) and (seen_old < declared_old or seen_new < declared_new):
                body = lines[index]
                if body.startswith("\\"):  # "\ No newline at end of file"
                    index += 1
                    continue
                if body.startswith("+"):
                    seen_new += 1
                elif body.startswith("-"):
                    seen_old += 1
                elif body.startswith(" ") or body == "":
                    # A fully blank context line is often emitted without its space.
                    seen_old += 1
                    seen_new += 1
                else:
                    raise PatchInvalid(
                        f"Unexpected line inside a hunk for "
                        f"{current['new']!r}: {body[:60]!r}. Hunk body lines must start "
                        f"with ' ', '+', or '-'."
                    )
                index += 1

            if seen_old != declared_old or seen_new != declared_new:
                raise PatchInvalid(
                    f"Hunk counts do not match for {current['new']!r}: the header "
                    f"declares {declared_old} original and {declared_new} new lines, "
                    f"but the body has {seen_old} and {seen_new}."
                )

            # Replace the tracked file with an incremented hunk count.
            last = files[-1]
            files[-1] = PatchedFile(last.old_path, last.new_path, last.hunks + 1)
            current["hunks"] = files[-1].hunks
            continue

        # Anything else between sections is prose the model added; skip it.
        index += 1

    if not files:
        raise PatchInvalid(
            "No file sections found. A unified diff needs '--- <path>' and "
            "'+++ <path>' headers."
        )

    empty = [f.display_path for f in files if f.hunks == 0]
    if empty:
        raise PatchInvalid(
            f"These file sections have no hunks: {', '.join(empty)}. Every changed "
            f"file needs at least one '@@' hunk."
        )

    return files
