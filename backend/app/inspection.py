"""Repository inspection: orchestration, file selection, and report building.

No database access and no FastAPI import — this module turns a repository URL
into a report, and the worker persists it.

**Everything read from the repository is untrusted data.** File contents are
decoded for display and stored as text. They are never executed, imported,
evaluated, or interpreted as instructions, and no URL found in a repository or an
API response is ever fetched (see `github_client`).
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from app import github_client
from app.config import Settings
from app.github_client import GitHubClient, UpstreamProtocolError
from app.schemas import (
    BudgetUsage,
    FileEntry,
    FilePreview,
    InspectionReport,
    PytestAssessment,
    RepositoryFacts,
    TruncationInfo,
)

# READMEs, most preferred first.
README_NAMES = (
    "README.md",
    "README.rst",
    "README.txt",
    "README.markdown",
    "README",
)

# Python project configuration, most informative first. `conftest.py` is included
# because its presence is strong pytest evidence.
PYTHON_CONFIG_NAMES = (
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "pytest.ini",
    "tox.ini",
    "conftest.py",
    "requirements.txt",
    "requirements-dev.txt",
    "dev-requirements.txt",
    "test-requirements.txt",
    "Pipfile",
)

# Filenames that establish a Python project on their own. `tox.ini`,
# `requirements*.txt` and `setup.cfg` are deliberately absent: they are generic
# enough that they only count as supporting evidence.
PYTHON_DEFINING_NAMES = ("pyproject.toml", "setup.py", "pytest.ini")

_TEST_DIR_RE = re.compile(r"(^|/)tests?/")
_TEST_FILE_RE = re.compile(r"(^|/)(test_[^/]+|[^/]+_test)\.py$")

ASSESSMENT_CAVEAT = (
    "Filename-and-configuration heuristic only. No repository code was executed "
    "and no tests were run, so this is not proof: a project may use pytest "
    "without declaring it in configuration, and the absence of pytest "
    "configuration is not evidence that pytest is unsupported."
)

MAX_TEST_LOCATIONS_REPORTED = 50


@dataclass(frozen=True)
class RepositoryRef:
    owner: str
    repo: str


def parse_repository_url(repository_url: str) -> RepositoryRef:
    """Split a stored (already validated and normalized) URL into owner/repo."""
    parts = urlsplit(repository_url)
    segments = [segment for segment in parts.path.split("/") if segment]
    if len(segments) != 2:
        raise UpstreamProtocolError(f"Cannot parse owner/repo from {repository_url!r}.")
    return RepositoryRef(owner=segments[0], repo=segments[1])


def _sanitize_text(text: str) -> str:
    """Drop NUL bytes, which SQLite text columns handle badly."""
    return text.replace("\x00", "")


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _is_root_level(path: str) -> bool:
    return "/" not in path


def _blobs(tree: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Blob entries only, sorted by path so a given commit yields one report."""
    return sorted(
        (entry for entry in tree if entry.get("type") == "blob" and isinstance(entry.get("path"), str)),
        key=lambda entry: entry["path"],
    )


def _find_by_name(blobs: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    """Locate a file by basename, preferring the repository root.

    Root-level files win over nested ones so that a top-level `README.md` is never
    passed over for `docs/README.md`; among nested matches the shallowest path wins.
    """
    lowered = name.lower()
    matches = [entry for entry in blobs if _basename(entry["path"]).lower() == lowered]
    if not matches:
        return None
    matches.sort(key=lambda entry: (not _is_root_level(entry["path"]), entry["path"].count("/"), entry["path"]))
    return matches[0]


def select_files(blobs: list[dict[str, Any]], *, max_files: int) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Pick the README and Python config files to fetch.

    Selection runs over the **whole** tree, before any display-only listing limit,
    so a relevant file is never dropped merely because it sorts late
    alphabetically.
    """
    readme = next(
        (found for name in README_NAMES if (found := _find_by_name(blobs, name)) is not None),
        None,
    )

    configs: list[dict[str, Any]] = []
    seen = {readme["path"]} if readme else set()
    budget = max_files - (1 if readme else 0)

    for name in PYTHON_CONFIG_NAMES:
        if len(configs) >= budget:
            break
        found = _find_by_name(blobs, name)
        if found is not None and found["path"] not in seen:
            configs.append(found)
            seen.add(found["path"])

    return readme, configs


def _fetch_preview(
    client: GitHubClient,
    ref: RepositoryRef,
    entry: dict[str, Any],
    commit_sha: str,
    *,
    max_file_bytes: int,
    remaining_total: int,
) -> tuple[FilePreview, int]:
    """Fetch one file, or omit it without fetching when it cannot fit.

    The tree's own `size` is checked first, so an oversized file costs no request
    and is never decoded.
    """
    path = entry["path"]
    declared = entry.get("size")

    if isinstance(declared, int) and declared > max_file_bytes:
        return (
            FilePreview(
                path=path,
                size=declared,
                omitted=True,
                omitted_reason=(
                    f"Not fetched: {declared:,} bytes exceeds the "
                    f"{max_file_bytes:,}-byte per-file limit."
                ),
            ),
            0,
        )

    if remaining_total <= 0:
        return (
            FilePreview(
                path=path,
                size=declared if isinstance(declared, int) else None,
                omitted=True,
                omitted_reason="Not fetched: the total content budget was already used.",
            ),
            0,
        )

    payload = client.get_file(ref.owner, ref.repo, path, commit_sha)

    reported_size = payload.get("size")
    if isinstance(reported_size, int) and reported_size > max_file_bytes:
        return (
            FilePreview(
                path=path,
                size=reported_size,
                omitted=True,
                omitted_reason=(
                    f"Discarded before decoding: {reported_size:,} bytes exceeds the "
                    f"{max_file_bytes:,}-byte per-file limit."
                ),
            ),
            0,
        )

    encoded = payload.get("content")
    if payload.get("encoding") != "base64" or not isinstance(encoded, str):
        return (
            FilePreview(
                path=path,
                size=reported_size if isinstance(reported_size, int) else None,
                omitted=True,
                omitted_reason="Not stored: GitHub did not return base64 content.",
            ),
            0,
        )

    try:
        raw = base64.b64decode(encoded, validate=False)
    except (ValueError, TypeError):
        return (
            FilePreview(
                path=path,
                size=reported_size if isinstance(reported_size, int) else None,
                omitted=True,
                omitted_reason="Not stored: content was not valid base64.",
            ),
            0,
        )

    if len(raw) > max_file_bytes:
        return (
            FilePreview(
                path=path,
                size=len(raw),
                omitted=True,
                omitted_reason=(
                    f"Discarded: decoded to {len(raw):,} bytes, over the "
                    f"{max_file_bytes:,}-byte per-file limit."
                ),
            ),
            0,
        )

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return (
            FilePreview(
                path=path,
                size=len(raw),
                is_binary=True,
                omitted=True,
                omitted_reason="Not stored: file is not UTF-8 text.",
            ),
            0,
        )

    text = _sanitize_text(text)
    truncated = False
    if len(text.encode("utf-8")) > remaining_total:
        text = text.encode("utf-8")[:remaining_total].decode("utf-8", errors="ignore")
        truncated = True

    stored = len(text.encode("utf-8"))
    return (
        FilePreview(
            path=path,
            size=len(raw),
            content=text,
            bytes_shown=stored,
            content_truncated=truncated,
        ),
        stored,
    )


def assess_python_pytest(
    blobs: list[dict[str, Any]], previews: list[FilePreview]
) -> PytestAssessment:
    """Decide whether this looks like a Python project using pytest.

    Every conclusion carries the filenames that support it. Configuration content
    is inspected as plain text; nothing is imported or executed.
    """
    paths = [entry["path"] for entry in blobs]
    basenames = {_basename(path) for path in paths}

    python_files = [path for path in paths if path.endswith(".py")]
    defining = [name for name in PYTHON_DEFINING_NAMES if name in basenames]

    python_evidence: list[str] = []
    if python_files:
        shown = python_files[:3]
        python_evidence.append(
            f"{len(python_files)} Python source file(s), e.g. " + ", ".join(shown)
        )
    python_evidence.extend(f"{name} present" for name in defining)
    # Supporting only — never enough on its own to call something a Python project.
    supporting = [n for n in ("setup.cfg", "tox.ini", "requirements.txt") if n in basenames]
    python_evidence.extend(f"{name} present (supporting evidence only)" for name in supporting)

    is_python = bool(python_files) or bool(defining)

    test_locations = [
        path for path in paths if _TEST_DIR_RE.search(path) or _TEST_FILE_RE.search(path)
    ]

    pytest_evidence: list[str] = []
    if "pytest.ini" in basenames:
        pytest_evidence.append("pytest.ini present")
    if "conftest.py" in basenames:
        pytest_evidence.append("conftest.py present")

    for preview in previews:
        if preview.content is None:
            continue
        name = _basename(preview.path)
        lowered = preview.content.lower()
        if name == "pyproject.toml" and "[tool.pytest" in lowered:
            pytest_evidence.append(f"{preview.path} declares [tool.pytest.ini_options]")
        elif name == "setup.cfg" and "[tool:pytest]" in lowered:
            pytest_evidence.append(f"{preview.path} declares [tool:pytest]")
        elif "pytest" in lowered:
            pytest_evidence.append(f"{preview.path} mentions pytest")

    uses_pytest = is_python and bool(pytest_evidence)

    return PytestAssessment(
        is_python_project=is_python,
        uses_pytest=uses_pytest,
        python_evidence=python_evidence,
        pytest_evidence=pytest_evidence,
        test_locations=test_locations[:MAX_TEST_LOCATIONS_REPORTED],
        caveat=ASSESSMENT_CAVEAT,
    )


@dataclass
class InspectionOutcome:
    commit_sha: str
    default_branch: str
    repository_name: str | None
    repository_description: str | None
    tree_truncated: bool
    report: InspectionReport


def inspect_repository(
    client: GitHubClient, repository_url: str, config: Settings
) -> InspectionOutcome:
    """Inspect one repository at one commit. Three base requests, plus one per file."""
    ref = parse_repository_url(repository_url)

    # 1. Metadata. Succeeding without credentials is what shows it is public.
    metadata = client.get_repository(ref.owner, ref.repo)
    if metadata.get("private") is True:
        raise github_client.RepositoryUnavailable(
            "GitHub reports this repository as private; this milestone inspects "
            "public repositories only."
        )
    default_branch = metadata.get("default_branch")
    if not isinstance(default_branch, str) or not default_branch:
        raise UpstreamProtocolError("Repository metadata did not include a default branch.")

    # 2. Pin the inspection to an immutable commit.
    commit_sha = client.get_head_commit_sha(ref.owner, ref.repo, default_branch)

    # 3. The tree at that exact commit.
    tree_payload = client.get_tree(ref.owner, ref.repo, commit_sha)
    tree_truncated = bool(tree_payload.get("truncated"))
    blobs = _blobs(tree_payload["tree"])

    # Selection spans the whole tree; the listing limit below is display-only.
    readme_entry, config_entries = select_files(blobs, max_files=config.github_max_files_fetched)

    notes: list[str] = []
    omitted: list[str] = []
    if tree_truncated:
        notes.append(
            "GitHub truncated the repository tree (it exceeds 100,000 entries or "
            "7 MB), so the file listing and file selection below are incomplete."
        )

    previews: list[FilePreview] = []
    remaining = config.github_max_total_content_bytes
    for entry in ([readme_entry] if readme_entry else []) + config_entries:
        preview, spent = _fetch_preview(
            client,
            ref,
            entry,
            commit_sha,
            max_file_bytes=config.github_max_file_bytes,
            remaining_total=remaining,
        )
        remaining -= spent
        previews.append(preview)
        if preview.omitted and preview.omitted_reason:
            omitted.append(f"{preview.path}: {preview.omitted_reason}")
        if preview.content_truncated:
            notes.append(f"{preview.path} was truncated to fit the total content budget.")

    readme_preview = previews[0] if readme_entry else None
    config_previews = previews[1:] if readme_entry else previews

    listed = blobs[: config.github_max_files_listed]
    listing_truncated = len(blobs) > len(listed)
    if listing_truncated:
        notes.append(
            f"File listing shows the first {len(listed):,} of {len(blobs):,} files "
            f"(alphabetical); this limit affects display only, not which files were "
            f"selected for preview."
        )

    report = InspectionReport(
        repository=RepositoryFacts(
            full_name=metadata.get("full_name"),
            name=metadata.get("name"),
            description=metadata.get("description"),
            default_branch=default_branch,
            commit_sha=commit_sha,
            is_private=metadata.get("private"),
            is_fork=metadata.get("fork"),
            is_archived=metadata.get("archived"),
        ),
        files=[
            FileEntry(path=entry["path"], size=entry.get("size") if isinstance(entry.get("size"), int) else None)
            for entry in listed
        ],
        readme=readme_preview,
        python_config_files=config_previews,
        assessment=assess_python_pytest(blobs, previews),
        budgets=BudgetUsage(
            requests_made=client.requests_made,
            max_requests=config.github_max_requests,
            files_in_tree=len(blobs),
            files_listed=len(listed),
            max_files_listed=config.github_max_files_listed,
            files_fetched=sum(1 for preview in previews if not preview.omitted),
            max_files_fetched=config.github_max_files_fetched,
            content_bytes_stored=config.github_max_total_content_bytes - remaining,
            max_total_content_bytes=config.github_max_total_content_bytes,
            max_file_bytes=config.github_max_file_bytes,
        ),
        truncation=TruncationInfo(
            tree_truncated=tree_truncated,
            file_listing_truncated=listing_truncated,
            omitted_files=omitted,
            notes=notes,
        ),
    )

    return InspectionOutcome(
        commit_sha=commit_sha,
        default_branch=default_branch,
        repository_name=metadata.get("name") if isinstance(metadata.get("name"), str) else None,
        repository_description=(
            metadata.get("description") if isinstance(metadata.get("description"), str) else None
        ),
        tree_truncated=tree_truncated,
        report=report,
    )
