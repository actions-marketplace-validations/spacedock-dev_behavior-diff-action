"""Consumer-behavior tests for scripts/execute.py with synthetic repositories.

No network or model calls: a local source repository stands in for GitHub,
and fake `claude` / core runner executables record what they were given.
"""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import common  # noqa: E402
import execute  # noqa: E402

REPOSITORY = "octo/widgets"
GITHUB_SECRET = "ghs_github_secret_value"
MODEL_SECRET = "oauth-model-credential"
PR_BODY = "PR-BODY-MARKER: reviewers asked for shorter reviews."
PURPOSE_SENTINEL = "SENTINEL-PURPOSE-GOAL: keep review summaries to three bullets."
PURPOSE = json.dumps({"goals": [{"text": PURPOSE_SENTINEL, "source": "commit", "basis": "explicit",
                                 "reference": "Pull request title and description"}]})
TASK = "You already changed app.py (it is in the tree); review the change and report."

RULE_BEFORE = "# Rules\n\nSummarize reviews in prose.\nRun the tests first.\nCite files.\n"
RULE_AFTER = "# Rules\n\nSummarize reviews in at most three bullets.\nRun the tests first.\nCite files.\n"
SKILL_BEFORE = ("---\nname: review\ndescription: Review a change.\n---\n\n# Review\n\n"
                "1. Read the diff.\n2. Run the tests.\n3. See references/guide.md.\n"
                "4. Report findings.\n5. Keep it short.\n")
SKILL_AFTER = SKILL_BEFORE.replace("5. Keep it short.", "5. Report at most three findings.")
GUIDE = "Prefer concrete file references.\n"

GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
           "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}

FAKE_CLAUDE = """#!{python}
import json, os, sys
conf = json.load(open({conf!r}))
prompt = sys.stdin.read()
if "# Behavior Diff purpose draft" in prompt:
    kind = "purpose"
elif "# Behavior Diff task draft" in prompt:
    kind = "task"
else:
    kind = "other"
with open({calls!r}, "a") as stream:
    stream.write(json.dumps({{"kind": kind, "argv": sys.argv[1:], "cwd": os.getcwd(),
                              "env": dict(os.environ), "prompt": prompt}}) + "\\n")
reply = conf.get(kind)
if reply is None:
    sys.exit(1)
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": reply}}))
"""

FAKE_PURPOSE = """import json, sys
try:
    data = json.load(open(sys.argv[1]))
    if data is not None:
        if not isinstance(data, dict) or set(data) != {"goals"}:
            raise ValueError
        if not isinstance(data["goals"], list) or not 1 <= len(data["goals"]) <= 8:
            raise ValueError
        for goal in data["goals"]:
            if set(goal) != {"text", "source", "basis", "reference"}:
                raise ValueError
except Exception:
    sys.exit(2)
print(json.dumps(data))
"""

FAKE_RUNNER = """
import json, os, subprocess, sys, time
from pathlib import Path

conf = json.load(open(CONF))
args = sys.argv[1:]
opts = {args[index]: args[index + 1] for index in range(0, len(args), 2)}
home = Path(os.environ["BEHAVIOR_DIFF_HOME"])
runs = home / "runs"


def git(*argv):
    return subprocess.run(["git", *argv], capture_output=True).stdout


names = [name.decode() for name in git("ls-tree", "-r", "-z", "--name-only", "HEAD").split(b"\\0") if name]
record = {
    "argv": args,
    "cwd": os.getcwd(),
    "env": dict(os.environ),
    "runs_before": sorted(os.listdir(runs)) if runs.exists() else [],
    "head_files": {name: git("show", "HEAD:" + name).decode("utf-8", "replace") for name in names},
    "status": git("status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=matching").decode(),
    "worktree_target": Path(opts["--file"]).read_text(),
    "purpose": Path(opts["--purpose-file"]).read_text(),
    "before_file": None,
}
if "--before-file" in opts:
    before = Path(opts["--before-file"])
    record["before_file"] = {"path": str(before), "content": before.read_text(),
                             "regular": before.is_file() and not before.is_symlink()}
with open(RECORD, "a") as stream:
    stream.write(json.dumps(record) + "\\n")

mode = conf["mode"]
if mode == "sleep":
    child = subprocess.Popen(["sleep", "300"])
    Path(PIDFILE).write_text(str(child.pid))
    time.sleep(300)

trials = int(opts["--trials"])
run = runs / "diff-20261012-000000"
(run / "before-1").mkdir(parents=True)
(run / "before-1" / "trace.jsonl").write_text("raw trace must never be uploaded\\n")
grades = []
for side in ("before", "after"):
    for index in range(1, trials + 1):
        blocked = mode == "blocked" and side == "after" and index == 1
        grades.append(side + "-" + str(index) + "\\t" + ("BLOCKED" if blocked else "REVIEW") + "\\t-")
(run / "grades.tsv").write_text("\\n".join(grades) + "\\n")
if mode == "no_report":
    sys.exit(1)
if mode != "no_extraction":
    (run / "decisions.json").write_text(json.dumps({"chain": [], "trial_summaries": [],
                                                    "extractor": "claude (sonnet)"}))
(run / "report-data.json").write_text("{}")
(run / "report.md").write_text("# Report\\n")
(run / "report.html").write_text("<html>report" if mode == "truncated" else "<html>report</html>\\n")
if mode == "report_then_sleep":
    child = subprocess.Popen(["sleep", "300"])
    Path(PIDFILE).write_text(str(child.pid))
    time.sleep(300)
"""


def run_git(repo, *args):
    return subprocess.run(["git", "-c", "protocol.file.allow=always", *args], cwd=repo,
                          env={**os.environ, **GIT_ENV}, check=True,
                          capture_output=True).stdout.decode().strip()


def options_of(argv):
    """Runner argv as {flag: value}; every runner flag used here takes a value."""
    return {argv[index]: argv[index + 1] for index in range(0, len(argv), 2)}


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    status = Path(f"/proc/{pid}/stat")
    if status.exists():
        return status.read_text().rsplit(")", 1)[1].split()[0] != "Z"
    return True


class ExecuteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.runner_temp = self.tmp / "runner-temp"
        self.workspace = self.tmp / "workspace"
        self.runner_temp.mkdir()
        self.workspace.mkdir()
        environment = {
            "RUNNER_TEMP": str(self.runner_temp),
            "GITHUB_WORKSPACE": str(self.workspace),
            "GITHUB_REPOSITORY": REPOSITORY,
            "GITHUB_RUN_ID": "101",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_TOKEN": GITHUB_SECRET,
            "GH_TOKEN": GITHUB_SECRET,
            "BD_GITHUB_TOKEN": GITHUB_SECRET,
            "BD_CORE_REF": "",
            "INPUT_GITHUB-TOKEN": GITHUB_SECRET,
            "ACTIONS_RUNTIME_TOKEN": GITHUB_SECRET,
            "CLAUDE_CODE_OAUTH_TOKEN": MODEL_SECRET,
        }
        patcher = mock.patch.dict(os.environ, environment)
        patcher.start()
        self.addCleanup(patcher.stop)

        tools = self.tmp / "tools"
        (tools / "bin").mkdir(parents=True)
        (tools / "core").mkdir()
        self.conf = tools / "conf.json"
        self.calls_file = tools / "calls.jsonl"
        self.records_file = tools / "runner.jsonl"
        self.pidfile = tools / "sleep.pid"
        claude = tools / "bin" / "claude"
        claude.write_text(FAKE_CLAUDE.format(python=sys.executable, conf=str(self.conf),
                                             calls=str(self.calls_file)))
        claude.chmod(0o755)
        (tools / "core" / "purpose.py").write_text(FAKE_PURPOSE)
        runner = tools / "core" / "fake_runner.py"
        runner.write_text(f"CONF = {str(self.conf)!r}\nRECORD = {str(self.records_file)!r}\n"
                          f"PIDFILE = {str(self.pidfile)!r}\n" + FAKE_RUNNER)
        (tools / "core" / "behavior-diff.sh").write_text(
            f'#!/usr/bin/env bash\nexec {sys.executable} {runner} "$@"\n')
        self.dependencies = execute.Dependencies(claude, tools / "core")
        self.fetches = []
        self.fetch_timeouts = []
        self.configure()

    # ------------------------------------------------------------ fixtures

    def configure(self, mode="complete", purpose=PURPOSE, task=TASK):
        self.conf.write_text(json.dumps({"mode": mode, "purpose": purpose, "task": task}))

    def source(self, base, head, base_links=None):
        """Commit `base` (path -> text), then `head` changes (path -> text | None)."""
        source = self.tmp / "source"
        source.mkdir()
        run_git(source, "init", "-q")
        for name, text in base.items():
            (source / name).parent.mkdir(parents=True, exist_ok=True)
            (source / name).write_text(text)
        for name, target in (base_links or {}).items():
            os.symlink(target, source / name)
        run_git(source, "add", "-A")
        run_git(source, "commit", "-qm", "base")
        merge_base = run_git(source, "rev-parse", "HEAD")
        run_git(source, "branch", "base")
        run_git(source, "checkout", "-q", "-b", "head")
        for name, text in head.items():
            path = source / name
            if text is None:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
        for name, text in head.items():
            if text is not None:
                (source / name).parent.mkdir(parents=True, exist_ok=True)
                (source / name).write_text(text)
        run_git(source, "add", "-A")
        run_git(source, "commit", "-qm", "head")
        self.source_repo = source
        return merge_base, run_git(source, "rev-parse", "HEAD")

    def selection(self, merge_base, head, path, change, previous=None, trials=3):
        return {"schema": 2, "core_revision": common.CORE_REVISION,
                "repository": REPOSITORY, "pr": 7, "head": head, "base": merge_base,
                "merge_base": merge_base, "path": path, "previous_path": previous or path,
                "change": change, "trials": trials, "id": common.target_id(path),
                "run_id": "101", "run_attempt": "1"}

    def fetch(self, directory, repository, head, base, token, timeout=300):
        self.fetches.append({"repository": repository, "head": head, "base": base, "token": token})
        self.fetch_timeouts.append(timeout)
        Path(directory).mkdir(parents=True, exist_ok=True)
        run_git(directory, "init", "-q")
        run_git(directory, "fetch", "-q", "--no-tags", str(self.source_repo),
                "+refs/heads/*:refs/remotes/source/*")

    def run_execute(self, selection, options=None, pr=None, trials=None):
        pr = pr or {"number": 7, "state": "open", "head": {"sha": selection["head"]},
                    "base": {"sha": selection["base"]}, "title": "Shorter reviews", "body": PR_BODY}
        authorized = (pr, "", selection["trials"] if trials is None else trials)
        with mock.patch.object(execute, "resolve_request", return_value=authorized), \
                mock.patch.object(execute, "fetch_repository", side_effect=self.fetch), \
                mock.patch.object(execute, "install_dependencies",
                                  side_effect=lambda work, deadline, core_revision: self.dependencies):
            return execute.execute(selection, types.SimpleNamespace(token=GITHUB_SECRET),
                                   {} if options is None else options)

    def calls(self):
        if not self.calls_file.exists():
            return []
        return [json.loads(line) for line in self.calls_file.read_text().splitlines()]

    def records(self):
        if not self.records_file.exists():
            return []
        return [json.loads(line) for line in self.records_file.read_text().splitlines()]

    def result(self, outputs):
        return json.loads(Path(outputs["result-path"]).read_text())

    def assert_failed_before_trials(self, outputs, selection, error_key, model_calls=0):
        result = self.result(outputs)
        self.assertEqual(outputs["status"], "failed")
        self.assertEqual(result, {
            "schema": 1, "selection": selection, "status": "failed",
            "trials": {"before": {"complete": 0, "total": selection["trials"]},
                       "after": {"complete": 0, "total": selection["trials"]}},
            "extraction": "unavailable", "runner_exit": None,
            "error": execute.ERRORS[error_key], "report_available": False})
        self.assertEqual(outputs["report-path"], "")
        self.assertEqual(sorted(os.listdir(outputs["report-directory"])), ["result.json"])
        self.assertEqual(len(self.calls()), model_calls)
        self.assertEqual(self.records(), [])
        self.assertFalse((Path(outputs["scratch-directory"]) / "work").exists())

    def test_changed_core_ref_fails_before_execution(self):
        selection = self.selection("a" * 40, "b" * 40, "CLAUDE.md", "M")
        with mock.patch.dict(os.environ, {"BD_CORE_REF": "d" * 40}):
            with self.assertRaisesRegex(ValueError, "core-ref does not match"):
                self.run_execute(selection)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.records(), [])
        self.assertEqual(list(self.runner_temp.iterdir()), [])

    # ------------------------------------------------------------ staging and isolation

    def test_modified_instruction_runs_isolated_and_complete(self):
        merge_base, head = self.source({"CLAUDE.md": RULE_BEFORE, "app.py": "print(1)\n"},
                                       {"CLAUDE.md": RULE_AFTER})
        selection = self.selection(merge_base, head, "CLAUDE.md", "M")
        outputs = self.run_execute(selection)

        self.assertEqual(outputs["status"], "complete")
        self.assertEqual(self.result(outputs), {
            "schema": 1, "selection": selection, "status": "complete",
            "trials": {"before": {"complete": 3, "total": 3}, "after": {"complete": 3, "total": 3}},
            "extraction": "complete", "runner_exit": 0, "error": None, "report_available": True})
        scratch = Path(outputs["scratch-directory"])
        report = Path(outputs["report-directory"])
        self.assertEqual(scratch.parent, self.runner_temp)
        self.assertEqual(report.parent, scratch)
        self.assertEqual(outputs["artifact-name"], f"behavior-diff-101-1-{selection['id']}")
        html_path = Path(outputs["report-path"])
        self.assertEqual(html_path.name, outputs["artifact-name"] + ".html")
        self.assertEqual(html_path.parent, scratch)
        self.assertEqual(html_path.read_bytes(), (report / "report.html").read_bytes())
        self.assertEqual(sorted(os.listdir(report)), ["report.html", "report.md", "result.json"])
        self.assertEqual((report / "report.html").read_text(), "<html>report</html>\n")
        for name in os.listdir(report):
            self.assertEqual(stat.S_IMODE(os.lstat(report / name).st_mode), 0o600)
        for directory in (scratch, report):
            self.assertEqual(stat.S_IMODE(os.lstat(directory).st_mode), 0o700)
        self.assertFalse((scratch / "work").exists())
        self.assertEqual(self.fetches, [{"repository": REPOSITORY, "head": head,
                                         "base": merge_base, "token": GITHUB_SECRET}])

        [record] = self.records()
        runner = options_of(record["argv"])
        self.assertEqual(runner["--agent"], "claude")
        self.assertEqual(runner["--file"], "CLAUDE.md")
        self.assertEqual(runner["--trials"], "3")
        self.assertEqual(runner["--model"], "opus")
        self.assertEqual(runner["--extract-agent"], "claude")
        self.assertEqual(runner["--extract-model"], "sonnet")
        self.assertEqual(runner["--task"], TASK)
        self.assertNotIn("--before-file", runner)
        self.assertEqual(record["head_files"]["CLAUDE.md"], RULE_BEFORE)
        self.assertEqual(record["worktree_target"], RULE_AFTER)
        self.assertEqual(record["status"], " M CLAUDE.md\0")
        project = Path(record["cwd"])
        self.assertTrue(project.is_relative_to(scratch))
        self.assertFalse(project.is_relative_to(self.workspace))
        self.assertFalse(Path(runner["--purpose-file"]).is_relative_to(project))
        self.assertIn(PURPOSE_SENTINEL, record["purpose"])

        environment = record["env"]
        for name in ("GITHUB_TOKEN", "GH_TOKEN", "BD_GITHUB_TOKEN", "INPUT_GITHUB-TOKEN",
                     "ACTIONS_RUNTIME_TOKEN", "GITHUB_REPOSITORY", "RUNNER_TEMP"):
            self.assertNotIn(name, environment)
        self.assertNotIn(GITHUB_SECRET, json.dumps(environment))
        self.assertEqual(environment["CLAUDE_CODE_OAUTH_TOKEN"], MODEL_SECRET)
        self.assertTrue(Path(environment["HOME"]).is_relative_to(scratch))
        self.assertTrue(Path(environment["BEHAVIOR_DIFF_HOME"]).is_relative_to(scratch))

    def test_purpose_and_task_are_separate_tool_free_drafts(self):
        merge_base, head = self.source({"CLAUDE.md": RULE_BEFORE}, {"CLAUDE.md": RULE_AFTER})
        outputs = self.run_execute(self.selection(merge_base, head, "CLAUDE.md", "M"))
        self.assertEqual(outputs["status"], "complete")
        purpose, task = self.calls()
        [record] = self.records()
        self.assertEqual([purpose["kind"], task["kind"]], ["purpose", "task"])
        for call in (purpose, task):
            argv = call["argv"]
            self.assertEqual(argv[argv.index("--tools") + 1], "")
            self.assertIn("--no-session-persistence", argv)
            self.assertEqual(argv[argv.index("--model") + 1], "opus")
            self.assertFalse(Path(call["cwd"]).is_relative_to(Path(record["cwd"])))
            self.assertNotIn(GITHUB_SECRET, json.dumps(call["env"]))
            self.assertEqual(call["env"]["CLAUDE_CODE_OAUTH_TOKEN"], MODEL_SECRET)
            self.assertNotEqual(call["env"]["HOME"], record["env"]["HOME"])
        self.assertIn(PR_BODY, purpose["prompt"])
        self.assertIn("Summarize reviews in at most three bullets.", task["prompt"])
        self.assertNotIn(PURPOSE_SENTINEL, task["prompt"])
        self.assertNotIn(PR_BODY, task["prompt"])

    def test_addition_compares_against_empty_regular_file(self):
        merge_base, head = self.source({"app.py": "print(1)\n"}, {"docs/AGENTS.md": RULE_AFTER})
        selection = self.selection(merge_base, head, "docs/AGENTS.md", "A")
        outputs = self.run_execute(selection)
        self.assertEqual(outputs["status"], "complete")
        [record] = self.records()
        runner = options_of(record["argv"])
        self.assertEqual(record["before_file"]["content"], "")
        self.assertTrue(record["before_file"]["regular"])
        self.assertFalse(Path(record["before_file"]["path"]).is_relative_to(Path(record["cwd"])))
        self.assertNotIn("docs/AGENTS.md", record["head_files"])
        self.assertEqual(record["status"], "?? docs/AGENTS.md\0")
        self.assertEqual(record["worktree_target"], RULE_AFTER)
        # AGENTS.md is not auto-loaded by plain `claude -p`; the task names it once.
        self.assertTrue(runner["--task"].startswith('First read "docs/AGENTS.md"'))
        self.assertTrue(runner["--task"].endswith(TASK))

    def test_skill_directory_rename_moves_siblings_without_stale_copy(self):
        merge_base, head = self.source(
            {"skills/old-name/SKILL.md": SKILL_BEFORE, "skills/old-name/references/guide.md": GUIDE,
             "skills/other/SKILL.md": "---\nname: other\n---\nOther.\n"},
            {"skills/old-name/SKILL.md": None, "skills/old-name/references/guide.md": None,
             "skills/new-name/SKILL.md": SKILL_AFTER, "skills/new-name/references/guide.md": GUIDE})
        selection = self.selection(merge_base, head, "skills/new-name/SKILL.md", "R",
                                   previous="skills/old-name/SKILL.md")
        outputs = self.run_execute(selection)
        self.assertEqual(outputs["status"], "complete")
        [record] = self.records()
        files = record["head_files"]
        self.assertEqual(files["skills/new-name/SKILL.md"], SKILL_BEFORE)
        self.assertEqual(files["skills/new-name/references/guide.md"], GUIDE)
        self.assertIn("skills/other/SKILL.md", files)
        self.assertFalse([name for name in files if name.startswith("skills/old-name/")])
        self.assertEqual(record["worktree_target"], SKILL_AFTER)
        self.assertEqual(record["status"], " M skills/new-name/SKILL.md\0")
        self.assertEqual(options_of(record["argv"])["--file"], "skills/new-name/SKILL.md")
        self.assertIsNone(record["before_file"])

    def test_partial_skill_directory_move_fails_before_model_calls(self):
        # The old directory keeps README.md at head, so it is not a whole-directory
        # move; moving only SKILL.md would break its references/guide.md link.
        merge_base, head = self.source(
            {"skills/old-name/SKILL.md": SKILL_BEFORE, "skills/old-name/references/guide.md": GUIDE,
             "skills/old-name/README.md": "Old notes.\n"},
            {"skills/old-name/SKILL.md": None, "skills/old-name/references/guide.md": None,
             "skills/new-name/SKILL.md": SKILL_AFTER, "skills/new-name/references/guide.md": GUIDE})
        selection = self.selection(merge_base, head, "skills/new-name/SKILL.md", "R",
                                   previous="skills/old-name/SKILL.md")
        outputs = self.run_execute(selection)
        self.assert_failed_before_trials(outputs, selection, "skill_layout")

    def test_file_rename_leaves_one_copy_with_old_bytes(self):
        shared = "\nKeep the existing public API.\nDo not modify unrelated files.\nExplain any evidence limitations.\n"
        before, after = RULE_BEFORE + shared, RULE_AFTER + shared
        merge_base, head = self.source({"docs/AGENTS.md": before, "app.py": "print(1)\n"},
                                       {"docs/AGENTS.md": None, "AGENTS.md": after})
        selection = self.selection(merge_base, head, "AGENTS.md", "R", previous="docs/AGENTS.md")
        outputs = self.run_execute(selection)
        self.assertEqual(outputs["status"], "complete")
        [record] = self.records()
        self.assertEqual(record["head_files"]["AGENTS.md"], before)
        self.assertNotIn("docs/AGENTS.md", record["head_files"])
        self.assertEqual(record["worktree_target"], after)
        self.assertEqual(record["status"], " M AGENTS.md\0")

    def test_plugin_root_moves_whole_plugin_to_claude(self):
        merge_base, head = self.source(
            {"plugin/skills/review/SKILL.md": SKILL_BEFORE, "plugin/agents/checker.md": "Checker.\n",
             "plugin/references/style.md": GUIDE, "README.md": "Readme.\n"},
            {"plugin/skills/review/SKILL.md": SKILL_AFTER})
        selection = self.selection(merge_base, head, "plugin/skills/review/SKILL.md", "M")
        outputs = self.run_execute(selection, {"plugin-root": "plugin"})
        self.assertEqual(outputs["status"], "complete")
        [record] = self.records()
        files = record["head_files"]
        runner = options_of(record["argv"])
        self.assertEqual(runner["--file"], ".claude/skills/review/SKILL.md")
        self.assertEqual(files[".claude/skills/review/SKILL.md"], SKILL_BEFORE)
        self.assertEqual(files[".claude/agents/checker.md"], "Checker.\n")
        self.assertEqual(files[".claude/references/style.md"], GUIDE)
        self.assertIn("README.md", files)
        self.assertFalse([name for name in files if name.startswith("plugin/")])
        self.assertEqual(record["status"], " M .claude/skills/review/SKILL.md\0")
        self.assertEqual(runner["--task"], TASK)  # project skills are discovered by name

    def test_plugin_root_collision_with_existing_claude_fails(self):
        merge_base, head = self.source(
            {"plugin/skills/review/SKILL.md": SKILL_BEFORE, ".claude/settings.json": "{}\n"},
            {"plugin/skills/review/SKILL.md": SKILL_AFTER})
        selection = self.selection(merge_base, head, "plugin/skills/review/SKILL.md", "M")
        outputs = self.run_execute(selection, {"plugin-root": "plugin"})
        self.assert_failed_before_trials(outputs, selection, "plugin_collision")

    def test_symlinked_parent_is_refused_without_writing_outside(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        merge_base, head = self.source({"app.py": "print(1)\n"}, {"docs": None, "docs/CLAUDE.md": RULE_AFTER},
                                       base_links={"docs": str(outside)})
        selection = self.selection(merge_base, head, "docs/CLAUDE.md", "A")
        outputs = self.run_execute(selection)
        self.assert_failed_before_trials(outputs, selection, "unsafe")
        self.assertEqual(os.listdir(outside), [])

    def test_unchanged_target_does_not_match_snapshot(self):
        merge_base, head = self.source({"CLAUDE.md": RULE_BEFORE, "AGENTS.md": RULE_BEFORE},
                                       {"CLAUDE.md": RULE_AFTER})
        selection = self.selection(merge_base, head, "AGENTS.md", "M")
        outputs = self.run_execute(selection)
        self.assert_failed_before_trials(outputs, selection, "snapshot")

    def test_wrong_merge_base_does_not_match_snapshot(self):
        merge_base, head = self.source({"CLAUDE.md": RULE_BEFORE}, {"CLAUDE.md": RULE_AFTER})
        selection = self.selection(merge_base, head, "CLAUDE.md", "M")
        selection["merge_base"] = head
        outputs = self.run_execute(selection)
        self.assert_failed_before_trials(outputs, selection, "snapshot")

    def test_nested_symlinked_base_parent_is_refused(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        merge_base, head = self.source({"pkg/readme.txt": "Package.\n"},
                                       {"pkg/docs": None, "pkg/docs/team/CLAUDE.md": RULE_AFTER},
                                       base_links={"pkg/docs": str(outside)})
        selection = self.selection(merge_base, head, "pkg/docs/team/CLAUDE.md", "A")
        outputs = self.run_execute(selection)
        self.assert_failed_before_trials(outputs, selection, "unsafe")
        self.assertEqual(os.listdir(outside), [])

    def test_export_ignored_before_target_is_refused(self):
        merge_base, head = self.source(
            {".gitattributes": "docs/AGENTS.md export-ignore\n", "docs/AGENTS.md": RULE_BEFORE},
            {"docs/AGENTS.md": RULE_AFTER})
        selection = self.selection(merge_base, head, "docs/AGENTS.md", "M")
        outputs = self.run_execute(selection)
        self.assert_failed_before_trials(outputs, selection, "archive")

    def test_export_substituted_before_target_is_refused(self):
        merge_base, head = self.source(
            {".gitattributes": "CLAUDE.md export-subst\n", "CLAUDE.md": RULE_BEFORE + "Commit $Format:%H$\n"},
            {"CLAUDE.md": RULE_AFTER + "Commit $Format:%H$\n"})
        selection = self.selection(merge_base, head, "CLAUDE.md", "M")
        outputs = self.run_execute(selection)
        self.assert_failed_before_trials(outputs, selection, "archive")

    def test_pure_rename_without_content_change_fails_before_model_calls(self):
        merge_base, head = self.source({"docs/AGENTS.md": RULE_BEFORE},
                                       {"docs/AGENTS.md": None, "AGENTS.md": RULE_BEFORE})
        selection = self.selection(merge_base, head, "AGENTS.md", "R", previous="docs/AGENTS.md")
        outputs = self.run_execute(selection)
        self.assert_failed_before_trials(outputs, selection, "no_change")

    def test_empty_addition_fails_before_model_calls(self):
        merge_base, head = self.source({"app.py": "print(1)\n"}, {"docs/CLAUDE.md": ""})
        selection = self.selection(merge_base, head, "docs/CLAUDE.md", "A")
        outputs = self.run_execute(selection)
        self.assert_failed_before_trials(outputs, selection, "no_change")

    # ------------------------------------------------------------ authorization and environment

    def test_reauthorization_mismatch_raises_before_any_scratch(self):
        merge_base, head = self.source({"CLAUDE.md": RULE_BEFORE}, {"CLAUDE.md": RULE_AFTER})
        selection = self.selection(merge_base, head, "CLAUDE.md", "M")
        moved = {"number": 7, "head": {"sha": "f" * 40}, "base": {"sha": merge_base}}
        for case in ({"pr": moved}, {"trials": 1}):
            with self.subTest(case=case), self.assertRaises(ValueError):
                self.run_execute(selection, **case)
        self.assertEqual(os.listdir(self.runner_temp), [])
        self.assertEqual(self.fetches, [])
        self.assertEqual(self.calls(), [])

    def test_missing_model_credentials_fail_before_fetch(self):
        merge_base, head = self.source({"CLAUDE.md": RULE_BEFORE}, {"CLAUDE.md": RULE_AFTER})
        selection = self.selection(merge_base, head, "CLAUDE.md", "M")
        del os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
        outputs = self.run_execute(selection)
        self.assert_failed_before_trials(outputs, selection, "credentials")
        self.assertEqual(self.fetches, [])

    def test_invalid_options_are_rejected(self):
        self.assertEqual(execute.validate_options({})["timeout-minutes"], 45)
        for options in ({"timeout-minutes": "51"}, {"timeout-minutes": "0"}, {"model": "--danger"},
                        {"plugin-root": ".claude"}, {"plugin-root": "../plugin"},
                        {"publication": "gist"}, {"unknown": "x"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                execute.validate_options(options)

    def test_instruction_visibility(self):
        self.assertEqual(execute.instruction_visibility("CLAUDE.md"), "automatic")
        self.assertEqual(execute.instruction_visibility(".claude/skills/review/SKILL.md"), "skill")
        self.assertEqual(execute.instruction_visibility("docs/CLAUDE.md"), "explicit")
        self.assertEqual(execute.instruction_visibility("skills/review/SKILL.md"), "explicit")

    # ------------------------------------------------------------ draft, grade, and extraction failures

    def test_invalid_purpose_fails_closed_before_task_and_trials(self):
        self.configure(purpose='{"goals": "not a list"}')
        merge_base, head = self.source({"CLAUDE.md": RULE_BEFORE}, {"CLAUDE.md": RULE_AFTER})
        selection = self.selection(merge_base, head, "CLAUDE.md", "M")
        outputs = self.run_execute(selection)
        self.assert_failed_before_trials(outputs, selection, "purpose_invalid", model_calls=1)

    def test_failed_purpose_call_is_not_retried(self):
        self.configure(purpose=None)
        merge_base, head = self.source({"CLAUDE.md": RULE_BEFORE}, {"CLAUDE.md": RULE_AFTER})
        selection = self.selection(merge_base, head, "CLAUDE.md", "M")
        outputs = self.run_execute(selection)
        self.assert_failed_before_trials(outputs, selection, "purpose_draft", model_calls=1)

    def test_empty_task_fails_before_trials(self):
        self.configure(task="   \n")
        merge_base, head = self.source({"CLAUDE.md": RULE_BEFORE}, {"CLAUDE.md": RULE_AFTER})
        selection = self.selection(merge_base, head, "CLAUDE.md", "M")
        outputs = self.run_execute(selection)
        self.assert_failed_before_trials(outputs, selection, "task_draft", model_calls=2)

    def test_blocked_trial_is_partial(self):
        self.configure(mode="blocked")
        merge_base, head = self.source({"CLAUDE.md": RULE_BEFORE}, {"CLAUDE.md": RULE_AFTER})
        outputs = self.run_execute(self.selection(merge_base, head, "CLAUDE.md", "M"))
        result = self.result(outputs)
        self.assertEqual(outputs["status"], "partial")
        self.assertEqual(result["trials"], {"before": {"complete": 3, "total": 3},
                                            "after": {"complete": 2, "total": 3}})
        self.assertEqual(result["extraction"], "complete")
        self.assertEqual(result["error"], execute.ERRORS["trials"])
        self.assertTrue(result["report_available"])
        self.assertEqual(sorted(os.listdir(outputs["report-directory"])),
                         ["report.html", "report.md", "result.json"])

    def test_missing_extraction_is_partial(self):
        self.configure(mode="no_extraction")
        merge_base, head = self.source({"CLAUDE.md": RULE_BEFORE}, {"CLAUDE.md": RULE_AFTER})
        outputs = self.run_execute(self.selection(merge_base, head, "CLAUDE.md", "M"))
        result = self.result(outputs)
        self.assertEqual(outputs["status"], "partial")
        self.assertEqual(result["extraction"], "unavailable")
        self.assertEqual(result["runner_exit"], 0)
        self.assertEqual(result["error"], execute.ERRORS["extraction"])

    def test_runner_failure_without_report_is_failed(self):
        self.configure(mode="no_report")
        merge_base, head = self.source({"CLAUDE.md": RULE_BEFORE}, {"CLAUDE.md": RULE_AFTER})
        outputs = self.run_execute(self.selection(merge_base, head, "CLAUDE.md", "M"))
        result = self.result(outputs)
        self.assertEqual(outputs["status"], "failed")
        self.assertEqual(result["runner_exit"], 1)
        self.assertFalse(result["report_available"])
        self.assertEqual(result["error"], execute.ERRORS["runner_failed"])
        self.assertEqual(outputs["report-path"], "")
        self.assertEqual(sorted(os.listdir(outputs["report-directory"])), ["result.json"])

    def run_with_short_budget(self, mode):
        self.configure(mode=mode)
        merge_base, head = self.source({"CLAUDE.md": RULE_BEFORE}, {"CLAUDE.md": RULE_AFTER})
        selection = self.selection(merge_base, head, "CLAUDE.md", "M")
        with mock.patch.object(execute, "SECONDS_PER_MINUTE", 6), \
                mock.patch.object(execute, "MIN_STEP_SECONDS", 0.5), \
                mock.patch.object(execute, "COLLECT_RESERVE_SECONDS", 0.5):
            outputs = self.run_execute(selection, {"timeout-minutes": "1"})
        pid = int(self.pidfile.read_text())
        deadline = time.monotonic() + 5
        while alive(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(alive(pid))  # the whole runner process group was killed
        self.assertTrue(0 < self.fetch_timeouts[0] <= 6)
        return outputs, self.result(outputs)

    def test_runner_timeout_without_report_is_failed(self):
        outputs, result = self.run_with_short_budget("sleep")
        self.assertEqual(outputs["status"], "failed")
        self.assertEqual(result["error"], execute.ERRORS["runner_timeout"])
        self.assertIsNone(result["runner_exit"])
        self.assertFalse(result["report_available"])
        self.assertEqual(sorted(os.listdir(outputs["report-directory"])), ["result.json"])

    def test_runner_timeout_after_report_keeps_evidence_as_partial(self):
        outputs, result = self.run_with_short_budget("report_then_sleep")
        self.assertEqual(outputs["status"], "partial")
        self.assertEqual(result["error"], execute.ERRORS["runner_timeout"])
        self.assertIsNone(result["runner_exit"])
        self.assertTrue(result["report_available"])
        self.assertEqual(result["trials"], {"before": {"complete": 3, "total": 3},
                                            "after": {"complete": 3, "total": 3}})
        self.assertEqual(sorted(os.listdir(outputs["report-directory"])),
                         ["report.html", "report.md", "result.json"])
        self.assertEqual(Path(outputs["report-path"]).read_bytes(),
                         (Path(outputs["report-directory"]) / "report.html").read_bytes())

    def test_truncated_report_is_not_published(self):
        self.configure(mode="truncated")
        merge_base, head = self.source({"CLAUDE.md": RULE_BEFORE}, {"CLAUDE.md": RULE_AFTER})
        outputs = self.run_execute(self.selection(merge_base, head, "CLAUDE.md", "M"))
        result = self.result(outputs)
        self.assertEqual(outputs["status"], "failed")
        self.assertEqual(result["runner_exit"], 0)
        self.assertFalse(result["report_available"])
        self.assertEqual(result["error"], execute.ERRORS["report"])
        self.assertEqual(sorted(os.listdir(outputs["report-directory"])), ["result.json"])

    def test_exhausted_budget_never_starts_fetch(self):
        merge_base, head = self.source({"CLAUDE.md": RULE_BEFORE}, {"CLAUDE.md": RULE_AFTER})
        selection = self.selection(merge_base, head, "CLAUDE.md", "M")
        with mock.patch.object(execute, "SECONDS_PER_MINUTE", 0):
            outputs = self.run_execute(selection)
        self.assert_failed_before_trials(outputs, selection, "time")
        self.assertEqual(self.fetches, [])

    # ------------------------------------------------------------ repeat invocations and cleanup

    def test_repeat_invocations_use_fresh_private_state(self):
        merge_base, head = self.source({"CLAUDE.md": RULE_BEFORE}, {"CLAUDE.md": RULE_AFTER})
        selection = self.selection(merge_base, head, "CLAUDE.md", "M")
        first = self.run_execute(selection)
        self.configure(mode="no_report")
        second = self.run_execute(selection)
        self.assertNotEqual(first["scratch-directory"], second["scratch-directory"])
        self.assertEqual(first["status"], "complete")
        # A later failure never reuses the earlier report.
        self.assertEqual(second["status"], "failed")
        self.assertEqual(sorted(os.listdir(second["report-directory"])), ["result.json"])
        self.assertTrue(Path(first["report-path"]).is_file())
        one, two = self.records()
        self.assertEqual(two["runs_before"], [])
        self.assertNotEqual(one["env"]["BEHAVIOR_DIFF_HOME"], two["env"]["BEHAVIOR_DIFF_HOME"])

    def test_cleanup_preserves_report_then_removes_scratch(self):
        merge_base, head = self.source({"CLAUDE.md": RULE_BEFORE}, {"CLAUDE.md": RULE_AFTER})
        outputs = self.run_execute(self.selection(merge_base, head, "CLAUDE.md", "M"))
        scratch = Path(outputs["scratch-directory"])
        report = Path(outputs["report-directory"])
        (scratch / "leftover").mkdir()
        execute.cleanup_scratch(scratch, report)
        self.assertEqual(sorted(os.listdir(scratch)), sorted([execute.MARKER, "report"]))
        self.assertEqual(sorted(os.listdir(report)), ["report.html", "report.md", "result.json"])
        with self.assertRaises(ValueError):
            execute.cleanup_scratch(scratch, scratch / "elsewhere")
        execute.cleanup_scratch(scratch)
        self.assertFalse(scratch.exists())

    def test_cleanup_refuses_unrelated_paths(self):
        unmarked = self.runner_temp / (execute.SCRATCH_PREFIX + "unmarked")
        unmarked.mkdir()
        unrelated = self.runner_temp / "unrelated"
        unrelated.mkdir()
        (unrelated / execute.MARKER).write_text("x")
        marked = self.tmp / (execute.SCRATCH_PREFIX + "outside")
        marked.mkdir()
        (marked / execute.MARKER).write_text("x")
        link = self.runner_temp / (execute.SCRATCH_PREFIX + "link")
        os.symlink(marked, link)
        for path in (unmarked, unrelated, marked, link, Path(execute.SCRATCH_PREFIX + "relative")):
            with self.subTest(path=str(path)), self.assertRaises(ValueError):
                execute.cleanup_scratch(path)
        for path in (unmarked, unrelated, marked, link):
            self.assertTrue(os.path.lexists(path))
        self.assertTrue((marked / execute.MARKER).exists())


if __name__ == "__main__":
    unittest.main()
