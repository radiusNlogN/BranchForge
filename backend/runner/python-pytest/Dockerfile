# BranchForge test runner: the ONLY environment repository code is executed in.
#
# Deliberately minimal. Repository dependencies are NOT installed and repository
# setup is never run — a repository needing more than this is reported as an
# environment limitation rather than silently accommodated, because installing
# what a repository asks for would mean executing what a repository asks for.
FROM python:3.11-slim

# Pinned so a verification run is reproducible.
ARG PYTEST_VERSION=8.4.2
RUN pip install --no-cache-dir "pytest==${PYTEST_VERSION}"

# The runner-owned reporting plugin. Kept out of the workspace so the repository
# cannot modify or shadow it.
RUN mkdir -p /opt/branchforge
COPY bf_report.py /opt/branchforge/bf_report.py
RUN chmod 0644 /opt/branchforge/bf_report.py

# The container is additionally run with --user 65534:65534 at runtime; this is
# defence in depth for anyone running the image by hand.
USER 65534:65534

LABEL org.branchforge.profile="python-pytest"
