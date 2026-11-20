"""Regression tests for the ci-alert.yml cancellation supersession check.

bluefin-bling opens (and reopens) an issue whenever a ``ci`` run on main fails,
is cancelled, or times out. When a run is *cancelled* the alert job first asks
GitHub whether a newer main push has already superseded it, so it does not
raise an alert for a failure that no longer represents the current build.

That first step called ``gh api --paginate --slurp -G``. ``-G`` is not a flag
``gh api`` understands, so under ``set -euo pipefail`` the alert job stopped
mid-decision every time a main push was cancelled, and no issue was raised for
the cancelled run. The fix swaps the unsupported flag for an explicit ``-X GET``
method, leaving the cancellation policy, pagination and filtering untouched.

These tests lock in that fix:

* the workflow no longer uses the unsupported ``-G`` shorthand and instead uses
  an explicit GET, with the ``branch``, ``event`` and ``per_page`` query fields;
* the jq filter that decides supersession still picks the newest *newer* push
  and reports "nothing newer" correctly, when the runs come back across two
  paginated pages and include non-push and older/equal runs that must be ignored.
"""

import json
import re
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci-alert.yml"


def load_workflow():
    """Return the ci-alert.yml source as text."""
    return WORKFLOW_PATH.read_text()


def extract_supersession_filter(text):
    """Return the jq program that selects the newest newer push.

    The program is the single-quoted jq filter that contains
    ``max_by(.run_number)``. Extracting it from the workflow (rather than
    copying it) means a future edit that re-introduces the bug is caught by the
    same logic the workflow runs.
    """
    match = re.search(
        r'--argjson number "[^"]*"\s*\\\s*\n\s*\'([\s\S]*?)\'',
        text,
    )
    if not match:
        raise AssertionError("supersession jq filter not found in ci-alert.yml")
    return match.group(1).strip()


def run_supersession_filter(filter_program, base_run_number, fixture):
    """Run the extracted jq filter against paginated fixture runs.

    ``fixture`` is a list of pages, mirroring what ``gh api --paginate
    --slurp`` streams: each page is an object with a ``workflow_runs`` array.
    Returns the filter's stdout (an empty string when no newer push exists).
    """
    result = subprocess.run(
        ["jq", "-r", "--argjson", "number", str(base_run_number), filter_program],
        input=json.dumps(fixture),
        text=True,
        check=True,
        capture_output=True,
    )
    return result.stdout.strip()


# Two pages of runs, exactly how gh --paginate --slurp concatenates them.
# Base run (the cancelled one) is 346. Only a push with run_number > 346 should
# count as "newer"; schedule runs and pushes <= 346 must be ignored.
TWO_PAGES = [
    {
        "workflow_runs": [
            {"event": "push", "run_number": 345, "id": 1001},   # older push
            {"event": "schedule", "run_number": 400, "id": 1002},  # not a push
            {"event": "push", "run_number": 346, "id": 1003},   # equal to base
        ]
    },
    {
        "workflow_runs": [
            {"event": "push", "run_number": 347, "id": 1004},   # newer -> winner
            {"event": "push", "run_number": 344, "id": 1005},   # older push
        ]
    },
]

# Same runs but the base run is the newest, so nothing supersedes it.
NO_NEWER_PAGES = [
    {
        "workflow_runs": [
            {"event": "push", "run_number": 347, "id": 1004},   # equal to base
            {"event": "schedule", "run_number": 500, "id": 2001},  # not a push
        ]
    },
    {
        "workflow_runs": [
            {"event": "push", "run_number": 345, "id": 1001},   # older push
        ]
    },
]


class SupersessionFlagTests(unittest.TestCase):
    """The alert job must use a supported gh api method."""

    def test_does_not_use_unsupported_G_shorthand(self):
        text = load_workflow()
        # The original bug: "-G" is not a valid `gh api` flag and aborts the job.
        self.assertNotIn("--slurp -G", text)
        self.assertIsNone(
            re.search(r"(?<![\w-])-G(?![\w-])", text),
            "unsupported -G shorthand flag still present in workflow",
        )

    def test_uses_explicit_get_method(self):
        text = load_workflow()
        self.assertIn("--slurp -X GET", text)

    def test_includes_branch_event_and_per_page_fields(self):
        text = load_workflow()
        for field in ('-f branch=', '-f event=push', '-f per_page='):
            self.assertIn(field, text, f"missing query field {field!r}")


class SupersessionLogicTests(unittest.TestCase):
    """The jq filter must pick the newest newer push, ignoring the rest."""

    @classmethod
    def setUpClass(cls):
        cls.filter_program = extract_supersession_filter(load_workflow())

    def test_newer_push_is_detected(self):
        result = run_supersession_filter(
            self.filter_program, 346, TWO_PAGES
        )
        # Newest push with run_number > 346 is run 347 (id 1004).
        self.assertEqual(result, "1004")

    def test_no_newer_push_returns_empty(self):
        result = run_supersession_filter(self.filter_program, 347, NO_NEWER_PAGES)
        self.assertEqual(result, "")

    def test_ignores_non_push_and_older_or_equal_runs(self):
        # A schedule run with a higher run_number must never win over a push.
        result = run_supersession_filter(self.filter_program, 346, TWO_PAGES)
        self.assertEqual(result, "1004")
        self.assertNotIn("1002", result)  # the schedule run's id


if __name__ == "__main__":
    unittest.main()
