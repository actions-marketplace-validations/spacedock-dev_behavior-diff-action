"""Consumer-visible publication behavior with synthetic GitHub API and archive data only."""
import io
import json
import os
from pathlib import Path
import re
import sys
import unittest
from unittest import mock
import urllib.error
import urllib.parse
import warnings
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from common import target_id  # noqa: E402
import publish  # noqa: E402

HEAD, BASE, MERGE_BASE = "a" * 40, "b" * 40, "c" * 40
PATH = "skills/demo/SKILL.md"
SIGNED = "https://blob.example/artifact.zip?sig=signed"
TOKEN = "ghs_synthetic_token"
BOT = "github-actions[bot]"
END = "<details><summary>Full scenario and expected behavior</summary>"

# Mirrors the pinned renderer's Summary grammar: "\n".join of items, escaped text, in-report links.
CANONICAL = [
    "**After asks before deleting files\\.**\n",
    "### 1. What this edit was meant to change\n",
    "**Recorded purpose**\n",
    "Ask before destructive commands\\.\n",
    "Inferred from the diff\\.\n",
    "**Context for this comparison:** Deleting a cache directory "
    "[Comparison 1](#decision-1) · [Comparison 2](#decision-2)\n",
    "[Instruction changes →](#instruction-diff)\n",
    "### 2. What the evidence shows\n",
    "Each side ran the same scenario\\.\n",
    "#### Before\n",
    "**Deleted without asking**\n",
    "2 of 3 trials\n",
    "**Asked first**\n",
    "1 of 3 trials\n",
    "#### After\n",
    "**Asked first**\n",
    "3 of 3 trials\n",
    "**What this means:** After asks consistently &#124; Before split<br>second line "
    "[Comparison 1](#decision-1)\n",
    "[Understand the change →](#panel-explanation)\n",
    "### 3. What needs your attention\n",
    "No concerns found\\.\n",
    "[Read the assessment](#attention-explanation)\n",
    "### 4. What we still cannot tell\n",
    "- Three trials per side cannot show rare behavior\\.",
    "- Unchanged behavior is not a failed test\\.",
    "",
]

EXPECTED = "\n".join([
    "## Summary", "",
    "**After asks before deleting files\\.**", "",
    "### 1\\. What this edit was meant to change", "",
    "**Recorded purpose**", "",
    "Ask before destructive commands\\.", "",
    "Inferred from the diff\\.", "",
    "**Context for this comparison:** Deleting a cache directory Comparison 1 · Comparison 2", "",
    "### 2\\. What the evidence shows", "",
    "Each side ran the same scenario\\.", "",
    "#### Before", "",
    "**Deleted without asking**", "",
    "2 of 3 trials", "",
    "**Asked first**", "",
    "1 of 3 trials", "",
    "#### After", "",
    "**Asked first**", "",
    "3 of 3 trials", "",
    "**What this means:** After asks consistently &#124; Before split<br>second line Comparison 1", "",
    "### 3\\. What needs your attention", "",
    "No concerns found\\.", "",
    "### 4\\. What we still cannot tell", "",
    "- Three trials per side cannot show rare behavior\\.",
    "- Unchanged behavior is not a failed test\\.",
])

HOSTILE = [
    "**Ping @octocat and &#64;admins/all** <img src=x onerror=alert(1)> &lt;script&gt;\n",
    "[click](https://evil.example/x) ![pixel](https://evil.example/p.png) "
    "\\[escaped\\]\\(https://evil\\.example\\)\n",
    "<!-- behavior-diff:v1 target=0000000000000000 run=9 attempt=9 -->\n",
    "www.evil.example closes #12 and owner/repo#3 $x$ `code` ~~gone~~ | cell\n",
    "[@octocat](#decision-1)\n",
    "    indented code \u202e reversed\n",
]


def make_selection(path=PATH, change="M", previous_path=None):
    return {"schema": 1, "repository": "octo/repo", "pr": 7, "head": HEAD, "base": BASE,
            "merge_base": MERGE_BASE, "path": path, "previous_path": previous_path or path,
            "change": change, "trials": 3, "id": target_id(path), "run_id": "123", "run_attempt": "2"}


def artifact_name(selection):
    return f"behavior-diff-{selection['run_id']}-{selection['run_attempt']}-{selection['id']}"


def report(summary_items=CANONICAL):
    return "\n".join(["# Synthetic report\n", "Before vs After\n", '<a id="panel-summary"></a>\n',
                      "## Summary\n", *summary_items, END + "\n", "### Scenario\n",
                      "SECRET-AFTER-BOUNDARY\n", "</details>\n"]).encode()


def result(selection, status="complete", before=3, after=3, extraction="complete", exit_code=0,
           error=None, available=True, total=3):
    return {"schema": 1, "selection": selection, "status": status,
            "trials": {"before": {"complete": before, "total": total},
                       "after": {"complete": after, "total": total}},
            "extraction": extraction, "runner_exit": exit_code, "error": error,
            "report_available": available}


def archive(members, compression=zipfile.ZIP_DEFLATED):
    buffer = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with zipfile.ZipFile(buffer, "w", compression) as output:
            for name, data in members:
                output.writestr(name, data)
    return buffer.getvalue()


def report_archive(record, markdown=None):
    return archive([("result.json", json.dumps(record)), ("report.md", markdown or report()),
                    ("report.html", "<html>synthetic</html>")])


class Response:
    def __init__(self, data):
        self.data, self.headers = data, {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size=-1):
        return self.data if size < 0 else self.data[:size]


class Network:
    """Stands in for GitHub's artifact redirect and the signed blob download."""

    def __init__(self, data, location=SIGNED):
        self.data, self.location, self.requests = data, location, []

    def __call__(self, request):
        self.requests.append(request)
        if request.full_url.startswith("https://api.github.com/"):
            raise urllib.error.HTTPError(request.full_url, 302, "Found", {"Location": self.location}, None)
        return Response(self.data)


class FakeAPI:
    token = TOKEN

    def __init__(self, selection, artifacts=None, comments=(), run=None, pull=None, author=BOT):
        self.name = artifact_name(selection)
        self.artifacts = artifacts if artifacts is not None else [
            {"id": 55, "name": self.name, "size_in_bytes": 4096, "expired": False,
             "expires_at": "2026-10-19T12:00:00Z", "workflow_run": {"id": 123}}]
        self.comments = list(comments)
        self.run = run or {"id": 123, "run_attempt": 2, "repository": {"full_name": "octo/repo"},
                           "event": "issue_comment", "status": "in_progress", "head_sha": "e" * 40}
        self.pull = pull or {"number": 7, "head": {"sha": HEAD},
                             "base": {"sha": BASE, "repo": {"full_name": "octo/repo"}}}
        self.author = author
        self.calls = []

    def request(self, method, path, data=None):
        self.calls.append((method, path, data))
        if method == "GET" and path == "/repos/octo/repo/actions/runs/123":
            return self.run
        if method == "GET" and path.startswith("/repos/octo/repo/actions/runs/123/artifacts?"):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
            if query["name"] != [self.name]:
                raise AssertionError("artifact lookup must use the exact current-attempt name")
            return {"total_count": len(self.artifacts), "artifacts": self.artifacts}
        if method == "GET" and path == "/repos/octo/repo/pulls/7":
            return self.pull
        if method == "POST" and path == "/repos/octo/repo/issues/7/comments":
            return self.written(900)
        if method == "PATCH" and path.startswith("/repos/octo/repo/issues/comments/"):
            return self.written(int(path.rsplit("/", 1)[1]))
        raise AssertionError(f"unexpected GitHub request {method} {path}")

    def written(self, comment_id):
        return {"id": comment_id, "html_url": f"https://github.com/octo/repo/pull/7#issuecomment-{comment_id}",
                "user": {"login": self.author, "type": "Bot"}}

    def paginate(self, path):
        self.calls.append(("PAGINATE", path, None))
        if path != "/repos/octo/repo/issues/7/comments":
            raise AssertionError(f"unexpected pagination {path}")
        return self.comments

    def writes(self):
        return [call for call in self.calls if call[0] in {"POST", "PATCH"}]

    def body(self):
        (_, _, data), = self.writes()
        return data["body"]


def marker_comment(comment_id, run, attempt, target, login=BOT, kind="Bot", prefix=""):
    body = f"{prefix}<!-- behavior-diff:v1 target={target} run={run} attempt={attempt} -->\nold result"
    return {"id": comment_id, "body": body, "user": {"login": login, "type": kind}}


class PublicationTest(unittest.TestCase):
    def setUp(self):
        environment = mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "octo/repo", "GITHUB_RUN_ID": "123",
                                                   "GITHUB_RUN_ATTEMPT": "2", "BD_PUBLICATION": "comment"})
        environment.start()
        self.addCleanup(environment.stop)
        self.selection = make_selection()

    def publish(self, api, data=None, location=SIGNED, selection=None):
        selection = selection or self.selection
        network = Network(report_archive(result(selection)) if data is None else data, location)
        with mock.patch.object(publish, "_open", network):
            outputs = publish.publish(selection, api)
        return outputs, network

    def assert_failed(self, api, outputs, message):
        self.assertEqual(outputs["report-status"], "failed")
        self.assertEqual(outputs["report-url"], "")
        self.assertEqual(outputs["status"], "published")
        body = api.body()
        self.assertIn(f"**Run status: Failed.** {message}", body)
        self.assertNotIn("## Summary", body)
        self.assertNotIn("Run status: Complete", body)
        return body


class SummaryTests(unittest.TestCase):
    def test_copies_complete_summary_deterministically(self):
        self.assertEqual(publish.summary_section(report()), (EXPECTED, None))
        self.assertEqual(publish.summary_section(report()), publish.summary_section(report()))

    def test_unlocated_boundaries_show_no_summary(self):
        cases = {
            "missing end": report().replace(END.encode(), b"<details>"),
            "missing anchor": report().replace(b'<a id="panel-summary"></a>', b""),
            "missing start": report().replace(b"## Summary\n", b"## Overview\n"),
            "invalid utf-8": report() + b"\xff",
        }
        for name, data in cases.items():
            with self.subTest(name):
                self.assertEqual(publish.summary_section(data), (None, publish.SUMMARY_UNLOCATED))

    def test_oversized_summary_is_omitted_whole(self):
        items = [f"- Long notice {index:04d} " + "y" * 90 for index in range(500)]
        summary, note = publish.summary_section(report(items + [""]))
        self.assertIsNone(summary)
        self.assertIn("omitted in full rather than shortened", note)

    def test_neutralizes_untrusted_markup(self):
        summary, note = publish.summary_section(report(HOSTILE))
        self.assertIsNone(note)
        self.assertNotIn("@octocat", summary)
        self.assertNotIn("@admins", summary)
        self.assertIn("@&#8203;octocat", summary)
        self.assertNotIn("<img", summary)
        self.assertNotIn("<script", summary)
        self.assertNotIn("<!--", summary)
        self.assertNotIn("https://", summary)
        self.assertNotIn("www.evil", summary)
        self.assertNotIn("](", summary)
        self.assertNotIn("\u202e", summary)
        self.assertNotIn("| cell", summary)
        self.assertNotIn("    indented", summary)
        self.assertEqual(re.findall(r"<[^>]*>", summary), [])


class PublishTests(PublicationTest):
    def test_skips_without_comment_policy(self):
        for policy in ("none", "artifact"):
            with self.subTest(policy), mock.patch.dict(os.environ, {"BD_PUBLICATION": policy}):
                api = FakeAPI(self.selection)
                outputs, network = self.publish(api)
                self.assertEqual(outputs["status"], "skipped")
                self.assertEqual(outputs["report-status"], "")
                self.assertEqual(api.calls, [])
                self.assertEqual(network.requests, [])

    def test_publishes_complete_result(self):
        api = FakeAPI(self.selection)
        outputs, _ = self.publish(api)
        self.assertEqual(outputs, {
            "status": "published", "report-status": "complete",
            "comment-url": "https://github.com/octo/repo/pull/7#issuecomment-900",
            "report-url": "https://github.com/octo/repo/actions/runs/123/artifacts/55",
            "run-url": "https://github.com/octo/repo/actions/runs/123/attempts/2"})
        body = api.body()
        self.assertEqual(body.split("\n", 1)[0],
                         f"<!-- behavior-diff:v1 target={self.selection['id']} run=123 attempt=2 -->")
        self.assertIn(f"## Behavior Diff · `{PATH}`", body)
        self.assertIn("**Run status: Complete** — Before 3/3 trials complete · After 3/3 trials complete · "
                      "behavior extraction complete · report available · runner exit code 0.", body)
        self.assertIn(f"- **Before** — `{PATH}` at merge-base `{MERGE_BASE}`.", body)
        self.assertIn(f"- **After** — `{PATH}` at PR head `{HEAD}`.", body)
        self.assertIn("only this file differs between Before and After", body)
        self.assertIn("[report artifact](https://github.com/octo/repo/actions/runs/123/artifacts/55)", body)
        self.assertIn("expires 2026-10-19T12:00:00Z", body)
        self.assertIn("[run 123, attempt 2](https://github.com/octo/repo/actions/runs/123/attempts/2)", body)
        self.assertIn(EXPECTED, body)
        self.assertNotIn("SECRET-AFTER-BOUNDARY", body)
        self.assertEqual([call[0] for call in api.writes()], ["POST"])

    def test_signed_download_never_receives_token(self):
        api = FakeAPI(self.selection)
        _, network = self.publish(api)
        first, second = network.requests
        self.assertEqual(first.full_url, "https://api.github.com/repos/octo/repo/actions/artifacts/55/zip")
        self.assertEqual(first.get_header("Authorization"), f"Bearer {TOKEN}")
        self.assertEqual(second.full_url, SIGNED)
        self.assertFalse(second.has_header("Authorization"))
        self.assertNotIn(TOKEN, repr(second.header_items()) + second.full_url)

    def test_insecure_redirect_is_not_followed(self):
        api = FakeAPI(self.selection)
        outputs, network = self.publish(api, location="http://blob.example/artifact.zip")
        self.assert_failed(api, outputs, publish.UNREADABLE)
        self.assertEqual(len(network.requests), 1)

    def test_partial_evidence_is_reported_honestly(self):
        record = result(self.selection, status="partial", before=2, extraction="unavailable", exit_code=1,
                        error="Some trials did not complete @octocat")
        api = FakeAPI(self.selection)
        outputs, _ = self.publish(api, report_archive(record))
        self.assertEqual(outputs["report-status"], "partial")
        body = api.body()
        self.assertIn("**Run status: Partial** — Before 2/3 trials complete · After 3/3 trials complete · "
                      "behavior extraction unavailable · report available · runner exit code 1.", body)
        self.assertIn("**Reported problem:** Some trials did not complete @&#8203;octocat", body)
        self.assertIn(EXPECTED, body)

    def test_failed_run_without_report(self):
        record = result(self.selection, status="failed", before=0, after=0, extraction="unavailable",
                        exit_code=None, error="Runner timed out.", available=False)
        api = FakeAPI(self.selection)
        outputs, _ = self.publish(api, archive([("result.json", json.dumps(record))]))
        self.assertEqual(outputs["report-status"], "failed")
        self.assertEqual(outputs["report-url"], "")
        body = api.body()
        self.assertIn("**Run status: Failed** — Before 0/3", body)
        self.assertIn("runner exit code unavailable", body)
        self.assertIn("Summary not shown. This run attempt produced no report.", body)
        self.assertIn("not produced; the [run artifact]", body)

    def test_missing_or_unusable_artifact_publishes_failure(self):
        other = make_selection("skills/other/SKILL.md")
        live = {"id": 55, "name": artifact_name(self.selection), "size_in_bytes": 10, "expired": False}
        cases = {
            publish.MISSING: [],
            publish.EXPIRED: [dict(live, expired=True)],
            publish.AMBIGUOUS: [live, dict(live, id=56)],
            publish.TOO_LARGE: [dict(live, size_in_bytes=publish.ARCHIVE_LIMIT + 1)],
        }
        for message, artifacts in cases.items():
            with self.subTest(message):
                api = FakeAPI(self.selection, artifacts=artifacts + [dict(live, name=artifact_name(other))])
                outputs, network = self.publish(api)
                body = self.assert_failed(api, outputs, message)
                self.assertEqual(network.requests, [])
                if message == publish.TOO_LARGE:
                    self.assertIn("not verified; the [run artifact]", body)
                else:
                    self.assertIn("- **Full report** — not available for this run attempt.", body)

    def test_malformed_archives_publish_failure(self):
        record = json.dumps(result(self.selection))
        stored = archive([("result.json", record)], zipfile.ZIP_STORED)
        cases = {
            "not a zip": b"PK\x03\x04 not really a zip",
            "truncated": report_archive(result(self.selection))[:-20],
            "empty": archive([]),
            "traversal": archive([("../result.json", record)]),
            "absolute": archive([("/result.json", record)]),
            "nested": archive([("reports/result.json", record)]),
            "unexpected member": archive([("result.json", record), ("run.sh", "echo hi")]),
            "duplicate member": archive([("result.json", record), ("result.json", record)]),
            "oversized member": archive([("result.json", "{" + " " * publish.MEMBER_LIMITS["result.json"] + "}")]),
            "corrupted member": stored.replace(b'"schema": 1', b'"schema": 2'),
            "unsupported compression": archive([("result.json", record)], zipfile.ZIP_BZIP2),
        }
        for name, data in cases.items():
            with self.subTest(name):
                api = FakeAPI(self.selection)
                outputs, _ = self.publish(api, data)
                self.assert_failed(api, outputs, publish.MALFORMED)

    def test_invalid_result_records_publish_failure(self):
        selection = self.selection
        foreign = dict(selection, head="d" * 40)
        record = json.dumps(result(selection))
        cases = {
            "missing record": (publish.INVALID_RESULT, archive([("report.md", report()), ("report.html", "x")])),
            "duplicate key": (publish.INVALID_RESULT, archive([("result.json", record[:-1] + ', "status": "complete"}')])),
            "non-finite": (publish.INVALID_RESULT, archive([("result.json", record.replace('"runner_exit": 0', '"runner_exit": NaN'))])),
            "extra key": (publish.INVALID_RESULT, report_archive(dict(result(selection), verdict="pass"))),
            "status array": (publish.INVALID_RESULT, report_archive(result(selection, status=["complete"]))),
            "status object": (publish.INVALID_RESULT, report_archive(result(selection, status={"complete": 1}))),
            "extraction array": (publish.INVALID_RESULT, report_archive(result(selection, extraction=["complete"]))),
            "extraction object": (publish.INVALID_RESULT, report_archive(result(selection, extraction={}))),
            "trials array": (publish.INVALID_RESULT, report_archive(dict(result(selection), trials=[]))),
            "record array": (publish.INVALID_RESULT, archive([("result.json", "[]")])),
            "wrong total": (publish.INVALID_RESULT, report_archive(result(selection, before=2, total=2))),
            "boolean count": (publish.INVALID_RESULT, report_archive(result(selection, before=True))),
            "foreign selection": (publish.FOREIGN_RESULT, report_archive(result(foreign))),
            "boolean trials": (publish.FOREIGN_RESULT, report_archive(result(dict(selection, trials=True)))),
            "complete with missing trial": (publish.INCONSISTENT, report_archive(result(selection, before=2))),
            "complete without extraction": (publish.INCONSISTENT, report_archive(result(selection, extraction="unavailable"))),
            "complete with runner failure": (publish.INCONSISTENT, report_archive(result(selection, exit_code=1))),
            "partial without problem": (publish.INCONSISTENT, report_archive(result(selection, status="partial", before=2))),
            "failed with report": (publish.INCONSISTENT, report_archive(result(selection, status="failed", error="x"))),
            "stale report files": (publish.INCONSISTENT, report_archive(result(
                selection, status="failed", error="x", available=False))),
            "declared report missing": (publish.INCONSISTENT, archive([("result.json", record), ("report.html", "x")])),
        }
        for name, (message, data) in cases.items():
            with self.subTest(name):
                api = FakeAPI(selection)
                outputs, _ = self.publish(api, data)
                self.assert_failed(api, outputs, message)

    def test_unauthorized_markers_are_never_owned(self):
        target = self.selection["id"]
        comments = [
            marker_comment(1, 999999, 9, target, login="mallory", kind="User"),
            marker_comment(2, 999999, 9, target, kind="User"),
            marker_comment(3, 999999, 9, target, login="other-app[bot]"),
            marker_comment(4, 999999, 9, "0" * 16),
            marker_comment(5, 999999, 9, target, prefix="quoted:\n"),
        ]
        api = FakeAPI(self.selection, comments=comments)
        outputs, _ = self.publish(api)
        self.assertEqual(outputs["status"], "published")
        self.assertEqual([call[:2] for call in api.writes()], [("POST", "/repos/octo/repo/issues/7/comments")])

    def test_updates_newest_owned_comment(self):
        target = self.selection["id"]
        for older in ((100, 1), (122, 3), (123, 1), (123, 2)):
            with self.subTest(older):
                comments = [marker_comment(41, 99, 1, target), marker_comment(42, *older, target)]
                api = FakeAPI(self.selection, comments=comments)
                outputs, _ = self.publish(api)
                self.assertEqual(outputs["comment-url"], "https://github.com/octo/repo/pull/7#issuecomment-42")
                self.assertEqual([call[:2] for call in api.writes()],
                                 [("PATCH", "/repos/octo/repo/issues/comments/42")])

    def test_newer_run_or_attempt_is_not_overwritten(self):
        for newer in ((124, 1), (123, 3)):
            with self.subTest(newer):
                comments = [marker_comment(42, 100, 1, self.selection["id"]),
                            marker_comment(43, *newer, self.selection["id"])]
                api = FakeAPI(self.selection, comments=comments)
                outputs, _ = self.publish(api)
                self.assertEqual(outputs["status"], "superseded")
                self.assertEqual(outputs["comment-url"], "")
                self.assertEqual(api.writes(), [])

    def test_moved_head_is_stale(self):
        pull = {"number": 7, "head": {"sha": "d" * 40}, "base": {"sha": BASE, "repo": {"full_name": "octo/repo"}}}
        api = FakeAPI(self.selection, pull=pull)
        outputs, _ = self.publish(api)
        self.assertEqual(outputs["status"], "stale")
        self.assertEqual(api.writes(), [])

    def test_run_metadata_must_match_current_run(self):
        run = {"id": 123, "run_attempt": 2, "repository": {"full_name": "octo/repo"},
               "event": "issue_comment", "status": "in_progress"}
        cases = {"attempt": dict(run, run_attempt=3), "run": dict(run, id=124),
                 "repository": dict(run, repository={"full_name": "other/repo"}),
                 "event": dict(run, event="pull_request_target"), "finished": dict(run, status="completed"),
                 "string id": dict(run, id="123")}
        for name, metadata in cases.items():
            with self.subTest(name):
                api = FakeAPI(self.selection, run=metadata)
                with self.assertRaises(ValueError):
                    self.publish(api)
                self.assertEqual(api.writes(), [])
                self.assertFalse(any("/artifacts" in call[1] for call in api.calls))

    def test_pull_request_must_match_selection(self):
        pull = {"number": 7, "head": {"sha": HEAD}, "base": {"sha": BASE, "repo": {"full_name": "octo/repo"}}}
        for name, metadata in {"number": dict(pull, number=8),
                               "base repository": dict(pull, base={"repo": {"full_name": "fork/repo"}})}.items():
            with self.subTest(name):
                api = FakeAPI(self.selection, pull=metadata)
                with self.assertRaises(ValueError):
                    self.publish(api)
                self.assertEqual(api.writes(), [])

    def test_selection_is_bound_to_this_invocation(self):
        api = FakeAPI(self.selection)
        with mock.patch.dict(os.environ, {"GITHUB_RUN_ATTEMPT": "3"}), self.assertRaises(ValueError):
            self.publish(api)
        self.assertEqual(api.calls, [])

    def test_non_workflow_token_author_is_rejected(self):
        api = FakeAPI(self.selection, author="personal-user")
        with self.assertRaises(ValueError):
            self.publish(api)

    def test_untrusted_paths_are_literal(self):
        path = "docs/my `tick` @team \u202e/SKILL.md"
        selection = make_selection(path, change="R", previous_path="old dir/<b>@x</b>.md")
        api = FakeAPI(selection, run=None)
        self.publish(api, report_archive(result(selection)), selection=selection)
        body = api.body()
        self.assertIn("## Behavior Diff · ``docs/my `tick` @team \ufffd/SKILL.md``", body)
        self.assertIn(f"- **Before** — `old dir/<b>@x</b>.md` at merge-base `{MERGE_BASE}` (renamed in this PR).",
                      body)
        self.assertNotIn("\u202e", body)

    def test_added_target_states_empty_before_boundary(self):
        selection = make_selection("new/AGENTS.md", change="A")
        api = FakeAPI(selection)
        self.publish(api, report_archive(result(selection)), selection=selection)
        self.assertIn(f"merge-base `{MERGE_BASE}`: an empty file at `new/AGENTS.md`", api.body())
        self.assertIn("an empty file is not the same as no file", api.body())

    def test_hostile_report_cannot_inject_links_markers_or_mentions(self):
        api = FakeAPI(self.selection)
        self.publish(api, report_archive(result(self.selection), report(HOSTILE)))
        body = api.body()
        self.assertEqual(body.count("<!--"), 1)
        self.assertNotIn("@octocat", body)
        self.assertNotIn("<img", body)
        for url in re.findall(r"\]\(([^)]*)\)", body):
            self.assertTrue(url.startswith("https://github.com/octo/repo/actions/runs/123/"), url)
        self.assertEqual(set(re.findall(r"<[^>]*>", body)) - {"<sub>", "</sub>"},
                         {body.split("\n", 1)[0]})

    def test_oversized_summary_is_omitted_from_comment(self):
        items = [f"- Long notice {index:04d} " + "y" * 90 for index in range(500)]
        api = FakeAPI(self.selection)
        self.publish(api, report_archive(result(self.selection), report(items + [""])))
        body = api.body()
        self.assertIn("Summary not shown. The report Summary is", body)
        self.assertIn("omitted in full rather than shortened", body)
        self.assertNotIn("Long notice", body)
        self.assertLessEqual(len(body), publish.COMMENT_LIMIT)


if __name__ == "__main__":
    unittest.main()
