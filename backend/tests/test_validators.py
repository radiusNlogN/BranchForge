"""Repository URL validation."""

import pytest

from app.validators import RepositoryUrlError, validate_github_repository_url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Plain form.
        ("https://github.com/octocat/Hello-World", "https://github.com/octocat/Hello-World"),
        # A trailing .git and trailing slashes are normalized away.
        ("https://github.com/octocat/Hello-World.git", "https://github.com/octocat/Hello-World"),
        ("https://github.com/octocat/Hello-World/", "https://github.com/octocat/Hello-World"),
        ("  https://github.com/octocat/Hello-World  ", "https://github.com/octocat/Hello-World"),
        # Host case is normalized, but owner/repo case is preserved.
        ("https://GitHub.COM/OctoCat/Hello-World", "https://github.com/OctoCat/Hello-World"),
        # Dots, underscores and hyphens are legal in repository names.
        ("https://github.com/my-org/repo.name_v2", "https://github.com/my-org/repo.name_v2"),
    ],
)
def test_accepts_and_normalizes(raw: str, expected: str) -> None:
    assert validate_github_repository_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        # Wrong scheme.
        "http://github.com/octocat/Hello-World",
        "ftp://github.com/octocat/Hello-World",
        "git://github.com/octocat/Hello-World",
        # SSH form has no scheme at all.
        "git@github.com:octocat/Hello-World.git",
        "github.com/octocat/Hello-World",
        # Embedded credentials.
        "https://user:token@github.com/octocat/Hello-World",
        "https://token@github.com/octocat/Hello-World",
        # Explicit port.
        "https://github.com:8443/octocat/Hello-World",
        # Unrelated and lookalike hosts.
        "https://gitlab.com/octocat/Hello-World",
        "https://notgithub.com/octocat/Hello-World",
        "https://github.com.evil.com/octocat/Hello-World",
        "https://evil.com/github.com/octocat/Hello-World",
        "https://www.github.com/octocat/Hello-World",
        "https://raw.githubusercontent.com/octocat/Hello-World",
        # Wrong path shape.
        "https://github.com/octocat",
        "https://github.com/",
        "https://github.com/octocat/Hello-World/pull/1",
        "https://github.com/octocat/Hello-World/tree/main",
        # Query strings and fragments.
        "https://github.com/octocat/Hello-World?tab=readme",
        "https://github.com/octocat/Hello-World#install",
        # Malformed path segments.
        "https://github.com/-octocat/Hello-World",
        "https://github.com/octocat-/Hello-World",
        "https://github.com/octo cat/Hello-World",
        "https://github.com/octocat/..",
        "https://github.com/octocat/.",
        "https://github.com/octocat/repo!name",
        f"https://github.com/octocat/{'r' * 200}",
    ],
)
def test_rejects(raw: str) -> None:
    with pytest.raises(RepositoryUrlError):
        validate_github_repository_url(raw)
