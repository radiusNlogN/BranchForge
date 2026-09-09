"""Read-only GitHub REST client for repository inspection.

Security posture, enforced structurally rather than by convention:

* The public methods take `owner`, `repo`, `path`, `ref` — **never a URL**. Every
  request path is built here from percent-encoded components, so a URL supplied
  by a repository or an upstream response can never be fetched. The `url`,
  `git_url`, `download_url` and `_links` fields in GitHub responses are ignored.
* Redirects are disabled. A redirect response is an error, not something to
  follow, since its Location comes from upstream.
* Response bodies are bounded twice: rejected up front if the declared
  Content-Length exceeds the cap, and cut off mid-stream otherwise. The cap is
  applied *before* JSON parsing, so an oversized body is never decoded.
* There is no retry loop.

Nothing here executes, imports, or evaluates repository content.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx2

# GitHub commit SHAs are 40 lowercase hex characters. Validated before a SHA is
# ever interpolated into a request path.
_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

_ACCEPT = "application/vnd.github+json"
_API_VERSION = "2022-11-28"


class GitHubError(Exception):
    """Base class for inspection failures. `kind` is persisted with the run."""

    kind = "github_error"


class RepositoryUnavailable(GitHubError):
    """Repository is missing, private, or otherwise not readable anonymously."""

    kind = "repository_unavailable"


class RateLimited(GitHubError):
    kind = "rate_limited"


class InspectionTimeout(GitHubError):
    kind = "timeout"


class UpstreamProtocolError(GitHubError):
    """Malformed or unexpected response: bad JSON, missing required fields."""

    kind = "upstream_protocol_error"


class ResponseTooLarge(GitHubError):
    kind = "response_too_large"


class RequestBudgetExceeded(GitHubError):
    kind = "request_budget_exceeded"


class UnexpectedRedirect(GitHubError):
    kind = "unexpected_redirect"


class NetworkError(GitHubError):
    kind = "network_error"


@dataclass(frozen=True)
class ClientLimits:
    max_requests: int
    max_response_bytes: int
    max_content_response_bytes: int


def build_client(
    *,
    base_url: str,
    user_agent: str,
    timeout_seconds: float,
    connect_timeout_seconds: float,
    transport: httpx2.BaseTransport | None = None,
) -> httpx2.Client:
    """Construct the underlying HTTP client. `transport` is for tests."""
    kwargs: dict[str, Any] = {
        "base_url": base_url,
        "headers": {
            "Accept": _ACCEPT,
            "X-GitHub-Api-Version": _API_VERSION,
            "User-Agent": user_agent,
        },
        "timeout": httpx2.Timeout(timeout_seconds, connect=connect_timeout_seconds),
        # Never follow a redirect: the target would come from upstream.
        "follow_redirects": False,
    }
    if transport is not None:
        kwargs["transport"] = transport
    return httpx2.Client(**kwargs)


def _encode_segment(value: str, *, label: str) -> str:
    """Percent-encode one path segment, escaping slashes."""
    if not value or "\x00" in value:
        raise UpstreamProtocolError(f"Invalid {label}: {value!r}")
    return quote(value, safe="")


def _encode_file_path(path: str) -> str:
    """Encode a repository file path, keeping `/` as the segment separator.

    Rejects absolute paths and any `..` segment so a crafted tree entry cannot
    escape the repository's contents namespace.
    """
    if not path or path.startswith("/") or "\x00" in path:
        raise UpstreamProtocolError(f"Invalid repository file path: {path!r}")
    segments = path.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise UpstreamProtocolError(f"Invalid repository file path: {path!r}")
    return "/".join(quote(segment, safe="") for segment in segments)


def validate_commit_sha(sha: object) -> str:
    """Return `sha` if it is a full 40-character hex commit SHA."""
    if not isinstance(sha, str) or not _COMMIT_SHA_RE.match(sha):
        raise UpstreamProtocolError(f"Upstream returned an invalid commit SHA: {sha!r}")
    return sha


class GitHubClient:
    """Bounded, read-only access to a public repository.

    Tracks a per-inspection request budget; `requests_made` is reported so the
    caller can show how much of the budget an inspection used.
    """

    def __init__(self, client: httpx2.Client, limits: ClientLimits) -> None:
        self._client = client
        self._limits = limits
        self.requests_made = 0

    # --- HTTP ---------------------------------------------------------------

    def _spend_request(self) -> None:
        if self.requests_made >= self._limits.max_requests:
            raise RequestBudgetExceeded(
                f"Inspection stopped after reaching its budget of "
                f"{self._limits.max_requests} GitHub requests."
            )
        self.requests_made += 1

    def _read_bounded(self, response: httpx2.Response, limit: int, what: str) -> bytes:
        """Read a response body, refusing to buffer more than `limit` bytes.

        Two guards: an oversized declared Content-Length is rejected without
        reading anything, and the running total is checked per chunk for
        responses whose length is unknown.
        """
        declared = response.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > limit:
                    raise ResponseTooLarge(
                        f"{what} response declares {int(declared):,} bytes, "
                        f"over the {limit:,}-byte limit."
                    )
            except ValueError:
                pass  # Unparseable header; fall through to the streaming guard.

        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > limit:
                raise ResponseTooLarge(
                    f"{what} response exceeded the {limit:,}-byte limit."
                )
        return bytes(body)

    def _check_status(self, response: httpx2.Response, what: str) -> None:
        status = response.status_code

        if status in (301, 302, 303, 307, 308):
            raise UnexpectedRedirect(
                f"{what} returned an unexpected redirect ({status}); refusing to "
                f"follow a location supplied by the upstream response."
            )

        if status == 200:
            return

        if status == 404:
            raise RepositoryUnavailable(
                f"{what} was not found. The repository may not exist, may have "
                f"been renamed, or may be private — this milestone reads public "
                f"repositories anonymously only."
            )

        if status in (403, 429):
            remaining = response.headers.get("x-ratelimit-remaining")
            retry_after = response.headers.get("retry-after")
            if remaining == "0":
                reset = response.headers.get("x-ratelimit-reset")
                raise RateLimited(
                    "GitHub's unauthenticated rate limit is exhausted "
                    f"(60 requests/hour per IP; resets at epoch {reset or 'unknown'}). "
                    "Wait for the window to reset and run the worker again."
                )
            if retry_after is not None:
                raise RateLimited(
                    f"GitHub applied a secondary rate limit; it asked to retry "
                    f"after {retry_after}s. No automatic retry is attempted."
                )
            raise RepositoryUnavailable(
                f"{what} was refused with HTTP {status}. Anonymous access may be "
                f"blocked for this repository."
            )

        raise UpstreamProtocolError(f"{what} returned an unexpected HTTP {status}.")

    def _get_json(self, path: str, *, params: dict[str, str] | None, limit: int, what: str) -> Any:
        self._spend_request()
        try:
            with self._client.stream("GET", path, params=params) as response:
                self._check_status(response, what)
                body = self._read_bounded(response, limit, what)
        except httpx2.TimeoutException as exc:
            raise InspectionTimeout(
                f"{what} timed out. Increase GITHUB_REQUEST_TIMEOUT_SECONDS if the "
                f"network is slow."
            ) from exc
        except httpx2.RequestError as exc:
            raise NetworkError(f"{what} failed: {type(exc).__name__}: {exc}") from exc

        try:
            return json.loads(body)
        except ValueError as exc:
            raise UpstreamProtocolError(
                f"{what} did not return valid JSON."
            ) from exc

    # --- API surface --------------------------------------------------------

    def get_repository(self, owner: str, repo: str) -> dict[str, Any]:
        """Repository metadata. Succeeding anonymously is what proves it public."""
        payload = self._get_json(
            f"/repos/{_encode_segment(owner, label='owner')}/{_encode_segment(repo, label='repository')}",
            params=None,
            limit=self._limits.max_response_bytes,
            what="Repository metadata",
        )
        if not isinstance(payload, dict):
            raise UpstreamProtocolError("Repository metadata was not a JSON object.")
        return payload

    def get_head_commit_sha(self, owner: str, repo: str, branch: str) -> str:
        """Resolve a branch to an immutable commit SHA.

        The branch is passed as a *query* parameter rather than a path segment:
        branch names may contain slashes, which would otherwise change the path
        shape no matter how they are encoded.
        """
        payload = self._get_json(
            f"/repos/{_encode_segment(owner, label='owner')}/{_encode_segment(repo, label='repository')}/commits",
            params={"sha": branch, "per_page": "1"},
            limit=self._limits.max_response_bytes,
            what=f"Commit resolution for branch {branch!r}",
        )
        if not isinstance(payload, list) or not payload:
            raise UpstreamProtocolError(
                f"No commits returned for branch {branch!r}."
            )
        first = payload[0]
        if not isinstance(first, dict):
            raise UpstreamProtocolError("Commit entry was not a JSON object.")
        return validate_commit_sha(first.get("sha"))

    def get_tree(self, owner: str, repo: str, commit_sha: str) -> dict[str, Any]:
        """The full recursive tree at one commit.

        GitHub resolves a commit SHA here, so no separate commit-to-tree lookup is
        needed. `truncated` is true when the tree exceeded GitHub's own limits
        (100,000 entries / 7 MB); the caller reports that rather than walking
        subtrees to work around it.
        """
        sha = validate_commit_sha(commit_sha)
        payload = self._get_json(
            f"/repos/{_encode_segment(owner, label='owner')}/"
            f"{_encode_segment(repo, label='repository')}/git/trees/{sha}",
            params={"recursive": "1"},
            limit=self._limits.max_response_bytes,
            what="Repository tree",
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("tree"), list):
            raise UpstreamProtocolError("Repository tree response was malformed.")
        return payload

    def get_file(self, owner: str, repo: str, path: str, commit_sha: str) -> dict[str, Any]:
        """One file's metadata and base64 content, pinned to `commit_sha`."""
        sha = validate_commit_sha(commit_sha)
        payload = self._get_json(
            f"/repos/{_encode_segment(owner, label='owner')}/"
            f"{_encode_segment(repo, label='repository')}/contents/{_encode_file_path(path)}",
            params={"ref": sha},
            # Base64 inflates by 4/3, plus newlines and the JSON envelope, so this
            # cap is larger than the decoded per-file budget.
            limit=self._limits.max_content_response_bytes,
            what=f"File contents for {path!r}",
        )
        if not isinstance(payload, dict):
            raise UpstreamProtocolError(f"File contents for {path!r} was not a JSON object.")
        return payload
