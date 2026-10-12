"""Authorize a request before fetching data; freeze exact commit and target identities."""
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import shutil
import urllib.parse

from common import (fetch_repository, git, input_value, integer, private_directory,
                    repository_name, safe_path, sha, target_id, validate_selection)


def boolean(value, name):
    if value not in ("true", "false"):
        raise ValueError(f"{name} must be true or false")
    return value == "true"


def parse_command(body):
    if not isinstance(body, str) or len(body) > 2048 or any(ord(c) < 32 for c in body):
        raise ValueError("Command must be one line")
    try:
        words = shlex.split(body)
    except ValueError:
        raise ValueError("Invalid command quoting") from None
    if len(words) < 2 or words[0] != "/behavior-diff":
        raise ValueError('Use /behavior-diff <40-character-head-sha> ["path"] [fast]')
    approved = sha(words[1])
    rest = words[2:]
    fast = bool(rest and rest[-1] == "fast")
    if fast:
        rest.pop()
    if len(rest) > 1:
        raise ValueError("Unsupported command arguments")
    path = safe_path(rest[0]) if rest else ""
    return approved, path, fast


def require_writer(api, repository, login):
    if not isinstance(login, str) or not login:
        raise ValueError("Missing author identity")
    permission = api.request("GET", f"/repos/{repository}/collaborators/{urllib.parse.quote(login, safe='')}/permission")
    if permission.get("permission") not in {"write", "maintain", "admin"}:
        raise ValueError("Request author and workflow actor must have repository write permission")


def resolve_request(api):
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    return authorize(api, event)


def authorize(api, event, options=None):
    options = options or {}
    get = lambda key, default="": options.get(key, input_value(key, default))
    repository = repository_name(os.environ["GITHUB_REPOSITORY"])
    if event.get("repository", {}).get("full_name") != repository:
        raise ValueError("Event repository identity mismatch")
    name = os.environ["GITHUB_EVENT_NAME"]
    if name not in {"pull_request", "issue_comment", "workflow_dispatch"}:
        raise ValueError("Unsupported event; pull_request_target is deliberately forbidden")
    policy = get("trust-policy", "explicit")
    if policy not in {"explicit", "same-repository"}:
        raise ValueError("Invalid trust-policy")
    publication = get("publication", "none")
    if publication not in {"none", "artifact", "comment"}:
        raise ValueError("Invalid publication policy")
    trials = integer(get("trials", "3"), "trials", 1, 3)
    fast = boolean(get("fast", "false"), "fast")
    requested = get("file")
    approved = get("approved-sha")
    if get("pr") and get("pr") != str(integer(get("pr"), "pr", 1, 2**53 - 1)):
        raise ValueError("pr must use canonical decimal notation without leading zeros")
    if requested:
        safe_path(requested)
    for actor in {os.environ.get("GITHUB_ACTOR"), os.environ.get("GITHUB_TRIGGERING_ACTOR", os.environ.get("GITHUB_ACTOR"))}:
        require_writer(api, repository, actor)
    if name == "pull_request":
        if policy != "same-repository":
            raise ValueError("Automatic PR execution requires trust-policy: same-repository")
        if event.get("action") not in {"opened", "synchronize", "reopened"}:
            raise ValueError("Unsupported pull request activity")
        payload = event.get("pull_request", {})
        if payload.get("head", {}).get("repo", {}).get("full_name") != repository:
            raise ValueError("Fork PRs require an exact-SHA comment or manual approval")
        number = payload.get("number")
        approved = sha(payload.get("head", {}).get("sha"))
    elif name == "issue_comment":
        if event.get("action") != "created" or not event.get("issue", {}).get("pull_request"):
            raise ValueError("Only newly created PR comments can approve a run")
        comment = event.get("comment", {})
        require_writer(api, repository, comment.get("user", {}).get("login"))
        approved, requested, fast = parse_command(comment.get("body"))
        number = event["issue"].get("number")
    else:
        number = get("pr")
        approved = sha(approved)
    number = integer(number, "pr", 1, 2**53 - 1)
    if get("pr") and integer(get("pr"), "pr", 1, 2**53 - 1) != number:
        raise ValueError("Explicit PR does not match the event")
    pr = api.request("GET", f"/repos/{repository}/pulls/{number}")
    if pr.get("number") != number or pr.get("state") != "open" or pr.get("base", {}).get("repo", {}).get("full_name") != repository:
        raise ValueError("PR must be open and target this repository")
    head = sha(pr.get("head", {}).get("sha"))
    sha(pr.get("base", {}).get("sha"))
    if approved != head:
        raise ValueError("Approval/event is stale: approve the current full head SHA")
    if name == "pull_request":
        if pr.get("head", {}).get("repo", {}).get("full_name") != repository:
            raise ValueError("Automatic fork execution forbidden")
        if event["pull_request"].get("base", {}).get("sha") != pr["base"]["sha"]:
            raise ValueError("PR base moved; request a new comparison")
    return pr, requested, 1 if fast else trials


def changed_files(directory, before, after, deadline=None):
    raw = git(["diff", "--name-status", "-z", "-M", "--diff-filter=AMR", before, after, "--"],
              directory, deadline=deadline)
    parts = raw.decode("utf-8", errors="strict").split("\0")
    result = []
    index = 0
    while index < len(parts) - 1:
        status = parts[index]
        index += 1
        previous = parts[index]
        index += 1
        path = previous
        if status.startswith("R"):
            path = parts[index]
            index += 1
        if PurePosixPath(path).name not in {"SKILL.md", "AGENTS.md", "CLAUDE.md"}:
            continue
        safe_path(path)
        safe_path(previous)
        result.append({"path": path, "previous_path": previous, "change": status[0]})
    return result


def regular_blob(directory, commit, path, deadline=None):
    # literal pathspec defeats glob metacharacters in perfectly legal file names.
    data = git(["ls-tree", "-z", commit, "--", ":(literal)" + path], directory, deadline=deadline)
    entries = data.split(b"\0")
    if len(entries) != 2 or not entries[0]:
        raise ValueError("Instruction is missing from the selected snapshot")
    metadata, found = entries[0].split(b"\t", 1)
    mode, kind, oid = metadata.split()
    if mode not in {b"100644", b"100755"} or kind != b"blob" or found.decode() != path:
        raise ValueError("Instruction must be a regular file, never a symlink or submodule")
    if int(git(["cat-file", "-s", oid.decode()], directory, deadline=deadline)) > 1024 * 1024:
        raise ValueError("Instruction exceeds the 1 MiB safety limit")
    return git(["cat-file", "blob", oid.decode()], directory, deadline=deadline)


def prepare(api):
    pr, requested, trials = resolve_request(api)
    repository = os.environ["GITHUB_REPOSITORY"]
    head, base = pr["head"]["sha"], pr["base"]["sha"]
    max_files = integer(input_value("max-files", "3"), "max-files", 1, 10)
    scratch = private_directory("behavior-diff-prepare-")
    try:
        repo = scratch / "repository"
        fetch_repository(repo, repository, head, base, input_value("github-token"))
        merge_base = sha(git(["merge-base", base, head], repo).decode().strip())
        candidates = changed_files(repo, merge_base, head)
        if requested:
            candidates = [entry for entry in candidates if entry["path"] == requested]
        if not candidates and (requested or os.environ["GITHUB_EVENT_NAME"] != "pull_request"):
            raise ValueError("Request has no eligible changed instruction files")
        if len(candidates) > max_files:
            raise ValueError("Too many changed instruction files; select one file or raise max-files (maximum 10)")
        selections = []
        for entry in candidates:
            regular_blob(repo, head, entry["path"])
            if entry["change"] != "A":
                regular_blob(repo, merge_base, entry["previous_path"])
            selection = dict(schema=1, repository=repository, pr=pr["number"], head=head,
                base=base, merge_base=merge_base, trials=trials, id=target_id(entry["path"]),
                run_id=os.environ["GITHUB_RUN_ID"], run_attempt=os.environ["GITHUB_RUN_ATTEMPT"], **entry)
            selections.append(validate_selection(selection))
        return {"matrix": {"include": selections}, "count": len(selections), "head": head,
                "pr": pr["number"], "status": "ready" if selections else "skipped"}
    finally:
        shutil.rmtree(scratch)
