"""Synthetic repositories and API records; no provider calls or private fixtures."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from common import CORE_REVISION, clean_env, git, safe_path, target_id, validate_selection
from prepare import authorize, changed_files, parse_command, prepare, regular_blob


class API:
    def __init__(self, pr, denied=()):
        self.pr, self.denied = pr, denied

    def request(self, method, path, data=None):
        if path.endswith("/permission"):
            login = path.split("/")[-2]
            return {"permission": "read" if login in self.denied else "write"}
        if path == "/repos/acme/demo/pulls/7":
            return self.pr
        raise AssertionError((method, path))


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.env = patch.dict(os.environ, {
            "GITHUB_REPOSITORY": "acme/demo", "GITHUB_RUN_ID": "101", "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_ACTOR": "maintainer", "GITHUB_TRIGGERING_ACTOR": "maintainer",
            "GITHUB_EVENT_NAME": "workflow_dispatch", "BD_PR": "7", "BD_TRIALS": "3",
            "BD_FAST": "false", "BD_TRUST_POLICY": "explicit", "BD_PUBLICATION": "none",
            "BD_FILE": "", "BD_MAX_FILES": "3", "BD_CORE_REF": "", "RUNNER_TEMP": str(self.root),
            "GITHUB_WORKSPACE": str(self.repo), "BD_GITHUB_TOKEN": "synthetic-token",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        git(["init", "-q"], self.repo)
        self.put("CLAUDE.md", "Before instruction\n")
        self.put("notes.txt", "Base non-target\n")
        self.base = self.commit()
        self.put("CLAUDE.md", "After instruction\n")
        self.head = self.commit()
        os.environ["BD_APPROVED_SHA"] = self.head
        self.pr = {"number": 7, "state": "open", "title": "Synthetic instruction change", "body": "",
                   "head": {"sha": self.head, "repo": {"full_name": "acme/demo"}},
                   "base": {"sha": self.base, "repo": {"full_name": "acme/demo"}}}
        self.event = {"repository": {"full_name": "acme/demo"}}
        self.event_path = self.root / "event.json"
        os.environ["GITHUB_EVENT_PATH"] = str(self.event_path)
        self.event_path.write_text(json.dumps(self.event))

    def put(self, path, text):
        p = self.repo / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def commit(self):
        git(["add", "-A"], self.repo)
        git(["-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.invalid", "commit", "-qm", "fixture"], self.repo)
        return git(["rev-parse", "HEAD"], self.repo).decode().strip()

    def fetch(self, directory, repository, head, base, token):
        shutil.copytree(self.repo, directory)

    def test_manual_approval_is_bound_to_exact_head_and_actor(self):
        pr, path, trials = authorize(API(self.pr), self.event)
        self.assertEqual((pr["head"]["sha"], path, trials), (self.head, "", 3))
        with self.assertRaises(ValueError):
            authorize(API(self.pr, {"maintainer"}), self.event)
        with patch.dict(os.environ, {"BD_APPROVED_SHA": self.base}):
            with self.assertRaises(ValueError):
                authorize(API(self.pr), self.event)
        with patch.dict(os.environ, {"GITHUB_TRIGGERING_ACTOR": "outsider"}):
            with self.assertRaises(ValueError):
                authorize(API(self.pr, {"outsider"}), self.event)

    def test_manual_pr_alias_cannot_bypass_publication_serialization(self):
        with patch.dict(os.environ, {"BD_PR": "007"}):
            with self.assertRaises(ValueError):
                authorize(API(self.pr), self.event)

    def test_fork_comment_requires_writer_and_current_sha(self):
        self.pr["head"]["repo"]["full_name"] = "outside/demo"
        event = {**self.event, "action": "created", "issue": {"number": 7, "pull_request": {}},
                 "comment": {"body": f'/behavior-diff {self.head} "dir with spaces/SKILL.md" fast',
                             "user": {"login": "reviewer"}}}
        event["issue"]["pull_request"] = {"url": "https://api.github.com/repos/acme/demo/pulls/7"}
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "issue_comment"}):
            _, path, trials = authorize(API(self.pr), event)
            self.assertEqual((path, trials), ("dir with spaces/SKILL.md", 1))
            with self.assertRaises(ValueError):
                authorize(API(self.pr, {"reviewer"}), event)
            event["comment"]["body"] = f"/behavior-diff {self.base}"
            with self.assertRaises(ValueError):
                authorize(API(self.pr), event)

    def test_automatic_requires_opt_in_and_rejects_fork_or_moving_base(self):
        event = {**self.event, "action": "opened", "pull_request": json.loads(json.dumps(self.pr))}
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}):
            with self.assertRaises(ValueError):
                authorize(API(self.pr), event)
            with patch.dict(os.environ, {"BD_TRUST_POLICY": "same-repository"}):
                self.assertEqual(authorize(API(self.pr), event)[2], 3)
                event["pull_request"]["base"]["sha"] = self.head
                with self.assertRaises(ValueError):
                    authorize(API(self.pr), event)
                event["pull_request"]["head"]["repo"]["full_name"] = "outside/demo"
                with self.assertRaises(ValueError):
                    authorize(API(self.pr), event)

    def test_privileged_target_event_never_runs(self):
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request_target"}):
            with self.assertRaises(ValueError):
                authorize(API(self.pr), self.event)

    def test_command_parser_rejects_surplus_and_multiline(self):
        for body in ("/behavior-diff", f"/behavior-different {self.head}",
                     f"/behavior-diff {self.head} x y", f"/behavior-diff {self.head}\nfast",
                     f"/behavior-diff {self.head} ../CLAUDE.md", f"/behavior-diff {self.head} 'unterminated"):
            with self.subTest(body=body), self.assertRaises(ValueError):
                parse_command(body)

    def test_selection_uses_git_rename_records_and_old_bytes(self):
        git(["checkout", "-q", self.base], self.repo)
        (self.repo / "CLAUDE.md").rename(self.repo / "AGENTS.md")
        self.put("space dir/SKILL.md", "Added\n")
        self.put("notes.txt", "Unrelated change\n")
        head = self.commit()
        changes = changed_files(self.repo, self.base, head)
        self.assertEqual(changes, [
            {"path": "AGENTS.md", "previous_path": "CLAUDE.md", "change": "R"},
            {"path": "space dir/SKILL.md", "previous_path": "space dir/SKILL.md", "change": "A"}])
        self.assertEqual(regular_blob(self.repo, self.base, "CLAUDE.md"), b"Before instruction\n")

    def test_symlink_and_hostile_path_rejected(self):
        (self.repo / "CLAUDE.md").unlink()
        (self.repo / "CLAUDE.md").symlink_to("notes.txt")
        head = self.commit()
        with self.assertRaises(ValueError):
            regular_blob(self.repo, head, "CLAUDE.md")
        for path in ("/CLAUDE.md", "../CLAUDE.md", "a/../CLAUDE.md", "a//CLAUDE.md", "a\\CLAUDE.md", ".git/CLAUDE.md", "a\nCLAUDE.md"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                safe_path(path)
        self.assertEqual(safe_path("literal [skill]/SKILL.md"), "literal [skill]/SKILL.md")

    def test_prepare_freezes_matrix_and_cleans_scratch(self):
        with patch("prepare.fetch_repository", self.fetch):
            result = prepare(API(self.pr))
        selection = result["matrix"]["include"][0]
        self.assertEqual((result["status"], result["count"], selection["merge_base"]), ("ready", 1, self.base))
        self.assertEqual(selection["path"], "CLAUDE.md")
        self.assertEqual(selection["core_revision"], CORE_REVISION)
        self.assertEqual(list(self.root.glob("behavior-diff-prepare-*")), [])
        with patch.dict(os.environ, {"GITHUB_RUN_ATTEMPT": "2"}):
            with self.assertRaises(ValueError):
                validate_selection(selection)

    def test_core_ref_is_frozen_and_inherited_without_repeating_input(self):
        revision = "d" * 40
        with patch("prepare.fetch_repository", self.fetch), patch.dict(os.environ, {"BD_CORE_REF": revision}):
            selection = prepare(API(self.pr))["matrix"]["include"][0]
        self.assertEqual(validate_selection(selection)["core_revision"], revision)
        with patch.dict(os.environ, {"BD_CORE_REF": CORE_REVISION}):
            with self.assertRaisesRegex(ValueError, "core-ref does not match"):
                validate_selection(selection)

    def test_core_ref_rejects_mutable_and_malformed_refs_before_fetch(self):
        for revision in ("main", "v0.4.6", "abcdef0", "A" * 40, "a" * 64,
                         "a" * 40 + "\n", " " + "a" * 40, "--upload-pack=bad"):
            with self.subTest(revision=revision), patch.dict(os.environ, {"BD_CORE_REF": revision}):
                with patch("prepare.fetch_repository") as fetch, self.assertRaises(ValueError):
                    prepare(API(self.pr))
                fetch.assert_not_called()

    def test_selection_requires_valid_frozen_core(self):
        with patch("prepare.fetch_repository", self.fetch):
            selection = prepare(API(self.pr))["matrix"]["include"][0]
        for revision in (None, "", "main", 123, "d" * 39):
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                validate_selection({**selection, "core_revision": revision})
        legacy = {key: value for key, value in selection.items() if key != "core_revision"}
        legacy["schema"] = 1
        with self.assertRaisesRegex(ValueError, "selection schema"):
            validate_selection(legacy)

    def test_deletion_only_auto_skips_but_explicit_fails(self):
        (self.repo / "CLAUDE.md").unlink()
        head = self.commit()
        self.pr["head"]["sha"] = head
        os.environ["BD_APPROVED_SHA"] = head
        with patch("prepare.fetch_repository", self.fetch):
            with self.assertRaises(ValueError):
                prepare(API(self.pr))
            event = {**self.event, "action": "opened", "pull_request": self.pr}
            self.event_path.write_text(json.dumps(event))
            with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request", "BD_TRUST_POLICY": "same-repository"}):
                self.assertEqual(prepare(API(self.pr))["status"], "skipped")

    def test_changed_membership_and_file_cap_fail_before_execution(self):
        with patch("prepare.fetch_repository", self.fetch), patch.dict(os.environ, {"BD_FILE": "notes.txt"}):
            with self.assertRaises(ValueError):
                prepare(API(self.pr))
        self.put("nested/SKILL.md", "Synthetic\n")
        self.pr["head"]["sha"] = self.commit()
        os.environ["BD_APPROVED_SHA"] = self.pr["head"]["sha"]
        with patch("prepare.fetch_repository", self.fetch), patch.dict(os.environ, {"BD_MAX_FILES": "1"}):
            with self.assertRaises(ValueError):
                prepare(API(self.pr))

    def test_snapshot_helpers_stop_at_execution_deadline(self):
        tools = self.root / "slow-tools"
        tools.mkdir()
        executable = tools / "git"
        executable.write_text("#!/bin/sh\nsleep 5\nexit 0\n")
        executable.chmod(0o700)
        with patch.dict(os.environ, {"PATH": f"{tools}{os.pathsep}{os.environ['PATH']}"}):
            with self.assertRaises(subprocess.TimeoutExpired):
                changed_files(self.repo, self.base, self.head, deadline=time.monotonic() + 0.1)
            with self.assertRaises(subprocess.TimeoutExpired):
                regular_blob(self.repo, self.head, "CLAUDE.md", deadline=time.monotonic() + 0.1)

    def test_model_environment_excludes_credentials_and_command_files(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "write-secret", "CLAUDE_CODE_OAUTH_TOKEN": "model-secret",
                                     "GITHUB_OUTPUT": "/output", "GITHUB_ENV": "/env", "GH_TOKEN": "gh-secret",
                                     "ACTIONS_RUNTIME_TOKEN": "artifact-secret", "BD_GITHUB_TOKEN": "read-secret"}):
            environment = clean_env("/private/home")
        self.assertEqual(environment["HOME"], "/private/home")
        self.assertFalse(any("secret" in value for value in environment.values()))
        self.assertNotIn("GITHUB_ENV", environment)


if __name__ == "__main__":
    unittest.main()
