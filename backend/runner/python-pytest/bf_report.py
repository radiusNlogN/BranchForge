"""Runner-owned pytest plugin: structured, bounded test results.

Lives in the **image**, not the workspace, and is loaded with `-p bf_report` from
a PYTHONPATH entry that precedes the repository, so a repository module cannot
shadow it.

It exists because comparing test outcomes by parsing terminal output is guesswork:
a test that disappears from the failure list may have been fixed, skipped,
deselected, renamed, or never collected at all, and those are very different
facts. This records collected node IDs and an explicit per-test outcome derived
from all three phases (setup/call/teardown), so "previously failing, now passing"
can be asserted rather than inferred.

Output is JSON at $BF_REPORT_PATH. Everything is bounded: per-message text, the
number of tests recorded, and the number of collection errors.
"""

import json
import os

SCHEMA = "branchforge.report.v1"

MAX_TESTS = int(os.environ.get("BF_REPORT_MAX_TESTS", "5000"))
MAX_MESSAGE_CHARS = int(os.environ.get("BF_REPORT_MAX_MESSAGE_CHARS", "400"))
MAX_COLLECT_ERRORS = int(os.environ.get("BF_REPORT_MAX_COLLECT_ERRORS", "200"))


class _Recorder:
    def __init__(self):
        self.collected = []
        self.collect_errors = []
        self.phases = {}
        self.messages = {}
        self.xfail = set()
        self.truncated = False

    def note_phase(self, nodeid, when, outcome, wasxfail, message):
        entry = self.phases.get(nodeid)
        if entry is None:
            if len(self.phases) >= MAX_TESTS:
                self.truncated = True
                return
            entry = {}
            self.phases[nodeid] = entry
        entry[when] = outcome
        if wasxfail:
            self.xfail.add(nodeid)
        if message and nodeid not in self.messages:
            self.messages[nodeid] = str(message)[:MAX_MESSAGE_CHARS]

    def outcome_for(self, nodeid):
        phases = self.phases.get(nodeid, {})
        setup = phases.get("setup")
        call = phases.get("call")
        teardown = phases.get("teardown")
        xfail = nodeid in self.xfail

        # A failure in setup or teardown is an error, not a test failure: the test
        # body never ran, so it says nothing about the code under test.
        if setup == "failed" or teardown == "failed":
            return "error"
        if setup == "skipped":
            return "xfailed" if xfail else "skipped"
        if call == "failed":
            return "failed"
        if call == "skipped":
            return "xfailed" if xfail else "skipped"
        if call == "passed":
            return "xpassed" if xfail else "passed"
        return "error"

    def payload(self, exitstatus):
        tests = {}
        for nodeid in self.phases:
            entry = {"outcome": self.outcome_for(nodeid), "phases": self.phases[nodeid]}
            message = self.messages.get(nodeid)
            if message:
                entry["message"] = message
            tests[nodeid] = entry

        counts = {}
        for entry in tests.values():
            counts[entry["outcome"]] = counts.get(entry["outcome"], 0) + 1

        return {
            "schema": SCHEMA,
            "exitstatus": int(exitstatus),
            "collected": self.collected[:MAX_TESTS],
            "collect_errors": self.collect_errors[:MAX_COLLECT_ERRORS],
            "tests": tests,
            "counts": counts,
            "truncated": self.truncated
            or len(self.collected) > MAX_TESTS
            or len(self.collect_errors) > MAX_COLLECT_ERRORS,
        }


_recorder = _Recorder()


def pytest_collection_modifyitems(session, config, items):
    """Record what was actually collected, after deselection."""
    for item in items:
        if len(_recorder.collected) >= MAX_TESTS:
            _recorder.truncated = True
            break
        _recorder.collected.append(item.nodeid)


def pytest_collectreport(report):
    if report.failed and len(_recorder.collect_errors) < MAX_COLLECT_ERRORS:
        _recorder.collect_errors.append(
            {
                "nodeid": str(report.nodeid),
                "message": str(getattr(report, "longrepr", ""))[:MAX_MESSAGE_CHARS],
            }
        )


def pytest_runtest_logreport(report):
    message = ""
    if report.outcome in ("failed", "skipped") and getattr(report, "longrepr", None):
        message = str(report.longrepr)
    _recorder.note_phase(
        report.nodeid,
        report.when,
        report.outcome,
        hasattr(report, "wasxfail"),
        message,
    )


def pytest_sessionfinish(session, exitstatus):
    path = os.environ.get("BF_REPORT_PATH")
    if not path:
        return
    payload = _recorder.payload(exitstatus)
    tmp = path + ".partial"
    # Written then renamed so a truncated file is never mistaken for a whole one.
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.replace(tmp, path)
