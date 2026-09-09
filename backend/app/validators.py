"""Validation of user-supplied GitHub repository URLs.

Scope note: this module only establishes that a string is an acceptable address
for a repository on github.com. It performs no network access and therefore makes
no claim that the repository exists, is public, or is reachable. Verifying that
against GitHub belongs to a later milestone.
"""

import re
from urllib.parse import urlsplit

GITHUB_HOST = "github.com"

# GitHub owners (users and organisations): alphanumeric plus single hyphens,
# 1-39 characters, never leading or trailing a hyphen.
_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")

# Repository names additionally allow underscores and dots, up to 100 characters.
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")

MAX_URL_LENGTH = 500


class RepositoryUrlError(ValueError):
    """Raised when a repository URL is not an acceptable github.com HTTPS URL."""


def validate_github_repository_url(raw: str) -> str:
    """Validate and normalize a public GitHub repository URL.

    Returns the canonical form `https://github.com/<owner>/<repo>`.

    Normalization is deliberately minimal: the host is lowercased (hostnames are
    case-insensitive), a trailing `.git` and trailing slashes are dropped, but the
    case of the owner and repository path segments is preserved as the user typed
    it, since that is how GitHub displays them.
    """
    if not isinstance(raw, str):
        raise RepositoryUrlError("Repository URL must be a string.")

    candidate = raw.strip()
    if not candidate:
        raise RepositoryUrlError("Repository URL must not be empty.")
    if len(candidate) > MAX_URL_LENGTH:
        raise RepositoryUrlError(
            f"Repository URL must be at most {MAX_URL_LENGTH} characters."
        )
    if any(ch.isspace() for ch in candidate):
        raise RepositoryUrlError("Repository URL must not contain whitespace.")

    # An SSH-style address (git@github.com:owner/repo) has no scheme and would
    # otherwise be reported only as a vague scheme error.
    if "://" not in candidate:
        raise RepositoryUrlError(
            "Repository URL must be an HTTPS URL beginning with https://github.com/."
        )

    try:
        parts = urlsplit(candidate)
    except ValueError as exc:  # malformed IPv6 brackets, bad port, etc.
        raise RepositoryUrlError("Repository URL is malformed.") from exc

    if parts.scheme != "https":
        raise RepositoryUrlError("Repository URL must use the https:// scheme.")

    # Credentials embedded in the URL are rejected outright rather than stripped.
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise RepositoryUrlError("Repository URL must not contain credentials.")

    try:
        port = parts.port
    except ValueError as exc:
        raise RepositoryUrlError("Repository URL has an invalid port.") from exc
    if port is not None:
        raise RepositoryUrlError("Repository URL must not specify a port.")

    hostname = (parts.hostname or "").lower()
    if hostname != GITHUB_HOST:
        raise RepositoryUrlError(
            f"Repository URL host must be exactly {GITHUB_HOST} "
            f"(got {hostname or 'no host'})."
        )

    if parts.query:
        raise RepositoryUrlError("Repository URL must not contain a query string.")
    if parts.fragment:
        raise RepositoryUrlError("Repository URL must not contain a fragment.")

    segments = [segment for segment in parts.path.split("/") if segment]
    if len(segments) != 2:
        raise RepositoryUrlError(
            "Repository URL path must be exactly <owner>/<repository>, "
            "for example https://github.com/octocat/Hello-World."
        )

    owner, repo = segments
    if repo.lower().endswith(".git"):
        repo = repo[: -len(".git")]

    if not _OWNER_RE.match(owner):
        raise RepositoryUrlError(f"Invalid repository owner segment: {owner!r}.")
    if repo in {".", ".."} or not _REPO_RE.match(repo):
        raise RepositoryUrlError(f"Invalid repository name segment: {repo!r}.")

    return f"https://{GITHUB_HOST}/{owner}/{repo}"
