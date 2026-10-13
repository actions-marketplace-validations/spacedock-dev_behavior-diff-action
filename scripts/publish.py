"""Publish one target's Behavior Diff result as a single bot-owned pull request comment.

Runs in the write-scoped publish job: no model credentials, no model calls, no workflow
or repository code execution. Evidence comes only from the current run attempt's artifact,
found by its exact name through the REST API and read in memory with size limits. The
archive is never extracted, and no caller-supplied path or URL is trusted.

Comment identity: the first body line is a hidden marker with the target id and the
run/attempt that wrote it. Only comments authored by ``github-actions[bot]`` can own a
marker, so comment publication requires the workflow ``GITHUB_TOKEN``. A comment written
by a newer run or attempt is never overwritten. Listing and then writing comments is not
atomic: callers MUST serialize publish jobs per repository and pull request (a
``concurrency`` group with ``cancel-in-progress: false``).
"""
import html
import http.client
import io
import json
import re
import string
import struct
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib

from common import NoRedirect, input_value, validate_selection

SERVER = "https://github.com"
API = "https://api.github.com"
BOT_LOGIN = "github-actions[bot]"
RUN_EVENTS = {"pull_request", "issue_comment", "workflow_dispatch"}
ARCHIVE_LIMIT = 64 * 1024 * 1024
ARCHIVE_MEMBERS = {"report.html", "report.md", "result.json"}
REPORT_MEMBERS = {"report.html", "report.md"}
# report.html is linked for readers, never read here.
MEMBER_LIMITS = {"result.json": 64 * 1024, "report.md": 16 * 1024 * 1024}
SUMMARY_LIMIT = 40_000
COMMENT_LIMIT = 65_536
MARKER = re.compile(r"<!-- behavior-diff:v1 target=([0-9a-f]{16}) run=([1-9][0-9]{0,18}) "
                    r"attempt=([1-9][0-9]{0,18}) -->")
TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")

# Boundaries emitted by the pinned core renderer (reporting/render_markdown.py).
SUMMARY_ANCHOR = '<a id="panel-summary"></a>'
SUMMARY_START = "## Summary"
SUMMARY_END = "<details><summary>Full scenario and expected behavior</summary>"

MISSING = "The report artifact for this run attempt was not found."
EXPIRED = "The report artifact for this run attempt has expired."
AMBIGUOUS = "More than one report artifact matched this run attempt."
TOO_LARGE = "The report artifact exceeds the publication size limit."
UNREADABLE = "The report artifact could not be downloaded."
MALFORMED = "The report artifact is not a valid ZIP archive containing only the expected report files."
INVALID_RESULT = "The result record in the report artifact is missing or invalid."
FOREIGN_RESULT = "The result record does not match this selection."
INCONSISTENT = "The result record contradicts itself or the artifact contents."
SUMMARY_UNLOCATED = "The Summary section could not be located in the report."

ESCAPE = re.compile(r"\\([!-/:-@\[-`{-~])")
ENTITY = re.compile(r"&(?:#[0-9]{1,7}|#[xX][0-9A-Fa-f]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});")
FRAGMENT_LINK = re.compile(r"\[((?:\\[!-/:-@\[-`{-~]|[^\\\[\]])*)\]\(#[A-Za-z0-9_-]{1,128}\)")
HEADING = re.compile(r"(#{1,6})[ \t]+(.*)")
LITERAL = set(",;?'\"/%")
BIDI = set("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")


class ArtifactProblem(Exception):
    """A fixed, reader-safe reason why no evidence from this attempt can be shown."""


def _get(value, *keys):
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _same_repository(value, repository):
    return isinstance(value, str) and value.lower() == repository.lower()


def _visible(char):
    if char in BIDI or (unicodedata.category(char) in {"Cc", "Cf", "Cs", "Co"} and char != "\u200d"):
        return "\ufffd"
    return char


def _text(value):
    """Encode untrusted text as inert Markdown: no HTML, links, mentions or references."""
    out = []
    for index, char in enumerate(value):
        if char == "&":
            out.append("&amp;")
        elif char == "<":
            out.append("&lt;")
        elif char == ">":
            out.append("&gt;")
        elif char == "|":
            out.append("&#124;")
        elif char == "@":
            out.append("@&#8203;")
        elif char == "#":
            out.append("\\#&#8203;")
        elif char == ":":
            out.append(":&#8203;" if value.startswith("//", index + 1) else ":")
        elif char == "." and value[max(0, index - 3):index].lower() == "www":
            out.append("&#8203;\\.")
        elif char in string.punctuation and char not in LITERAL:
            out.append("\\" + char)
        elif char in "\t\u2028\u2029":
            out.append(" ")
        else:
            out.append(_visible(char))
    return "".join(out)


def _code(value):
    """Render an untrusted single-line value as a literal code span."""
    value = "".join(_visible(char) for char in value)
    fence = "`" * (max((len(run) for run in re.findall(r"`+", value)), default=0) + 1)
    pad = " " if value[:1] in {"`", " "} or value[-1:] in {"`", " "} else ""
    return f"{fence}{pad}{value}{pad}{fence}"


def _tokens(line):
    """Split renderer Markdown into decoded text, allowed markup and in-report links."""
    tokens, text, index = [], [], 0

    def flush():
        if text:
            tokens.append(("text", "".join(text)))
            text.clear()

    while index < len(line):
        char = line[index]
        match = None
        if char == "\\":
            match = ESCAPE.match(line, index)
            if match:
                text.append(match.group(1))
        elif char == "&":
            match = ENTITY.match(line, index)
            if match:
                text.append(html.unescape(match.group()))
        elif char == "[":
            match = FRAGMENT_LINK.match(line, index)
            if match:
                flush()
                tokens.append(("link", _tokens(match.group(1))))
        elif line.startswith("**", index):
            flush()
            tokens.append(("markup", "**"))
            index += 2
            continue
        elif line.startswith("<br>", index):
            flush()
            tokens.append(("markup", "<br>"))
            index += 4
            continue
        if match:
            index = match.end()
        else:
            text.append(char)
            index += 1
    flush()
    return tokens


def _render(tokens):
    return "".join(_text(value) if kind == "text" else value if kind == "markup" else _render(value)
                   for kind, value in tokens)


def _summary_line(line):
    """Return safe Markdown for one Summary line, or None for navigation-only lines."""
    line = line.strip(" \t")
    if not line:
        return ""
    heading = HEADING.fullmatch(line)
    prefix = ""
    if heading:
        prefix, line = "#" * max(2, len(heading.group(1))) + " ", heading.group(2)
    elif line.startswith("- "):
        prefix, line = "- ", line[2:]
    tokens = _tokens(line)
    # In-report anchors do not exist in a PR comment; the full report link replaces them.
    if any(kind == "link" for kind, _ in tokens) and all(
            kind == "link" or (kind == "text" and not value.strip(" ·")) for kind, value in tokens):
        return None
    body = _render(tokens).strip()
    return prefix + body if body else ""


def summary_section(report):
    """Return (Markdown, None) for the complete report Summary, or (None, reason)."""
    try:
        lines = report.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n").split("\n")
        start = lines.index(SUMMARY_START)
        end = lines.index(SUMMARY_END, start + 1)
    except (UnicodeDecodeError, ValueError):
        return None, SUMMARY_UNLOCATED
    before = [line for line in lines[:start] if line.strip()]
    if not before or before[-1] != SUMMARY_ANCHOR:
        return None, SUMMARY_UNLOCATED
    out = []
    for line in lines[start:end]:
        rendered = _summary_line(line)
        if rendered is None or (rendered == "" and (not out or out[-1] == "")):
            continue
        out.append(rendered)
    while out and out[-1] == "":
        out.pop()
    if len(out) < 2:
        return None, SUMMARY_UNLOCATED
    body = "\n".join(out)
    if len(body) > SUMMARY_LIMIT:
        return None, (f"The report Summary is {len(body):,} characters after safe escaping, over the "
                      f"{SUMMARY_LIMIT:,}-character comment budget, so it is omitted in full rather than "
                      "shortened.")
    return body, None


def _open(request):
    """The only network seam for archive bytes; never follows redirects implicitly."""
    return urllib.request.build_opener(NoRedirect()).open(request, timeout=60)


def _read_bounded(response):
    length = response.headers.get("Content-Length")
    if length and length.isdigit() and int(length) > ARCHIVE_LIMIT:
        raise ArtifactProblem(TOO_LARGE)
    data = response.read(ARCHIVE_LIMIT + 1)
    if len(data) > ARCHIVE_LIMIT:
        raise ArtifactProblem(TOO_LARGE)
    return data


def _download(api, repository, artifact_id):
    request = urllib.request.Request(
        f"{API}/repos/{repository}/actions/artifacts/{artifact_id}/zip",
        headers={"Authorization": f"Bearer {api.token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "behavior-diff-action"})
    location = ""
    try:
        with _open(request) as response:
            return _read_bounded(response)
    except urllib.error.HTTPError as error:
        if error.code in {301, 302, 303, 307, 308} and error.headers is not None:
            location = error.headers.get("Location") or ""
        error.close()
    except (OSError, http.client.HTTPException):
        raise ArtifactProblem(UNREADABLE) from None
    target = urllib.parse.urlsplit(location)
    if target.scheme != "https" or not target.hostname or target.username or target.password:
        raise ArtifactProblem(UNREADABLE)
    # The signed URL authorizes itself; the GitHub token never leaves api.github.com.
    try:
        with _open(urllib.request.Request(location, headers={"User-Agent": "behavior-diff-action"})) as response:
            return _read_bounded(response)
    except (OSError, http.client.HTTPException):
        raise ArtifactProblem(UNREADABLE) from None


def _find_artifact(api, selection, *, html_report=False):
    """Return the one live current-attempt artifact as {id, url, expires, size}."""
    repository, run_id = selection["repository"], selection["run_id"]
    name = f"behavior-diff-{run_id}-{selection['run_attempt']}-{selection['id']}"
    if html_report:
        name += ".html"
    path = (f"/repos/{repository}/actions/runs/{run_id}/artifacts"
            f"?name={urllib.parse.quote(name, safe='')}&per_page=100")
    matches = []
    for page in range(1, 11):
        listing = api.request("GET", f"{path}&page={page}")
        items = _get(listing, "artifacts")
        if not isinstance(items, list):
            raise ValueError("Unexpected GitHub artifact listing")
        matches += [item for item in items if isinstance(item, dict) and item.get("name") == name]
        if len(items) < 100:
            break
    else:
        raise ValueError("GitHub artifact listing exceeds safety limit")
    live = [item for item in matches if item.get("expired") is not True]
    if not live:
        raise ArtifactProblem(EXPIRED if matches else MISSING)
    if len(live) > 1:
        raise ArtifactProblem(AMBIGUOUS)
    item = live[0]
    artifact_id, size, run = item.get("id"), item.get("size_in_bytes"), item.get("workflow_run")
    if (type(artifact_id) is not int or artifact_id < 1 or type(size) is not int or size < 0
            or item.get("expired") is not False
            or (run is not None and _get(run, "id") != int(run_id))):
        raise ArtifactProblem(MALFORMED)
    expires = item.get("expires_at")
    return {"id": artifact_id, "size": size,
            "url": f"{SERVER}/{repository}/actions/runs/{run_id}/artifacts/{artifact_id}",
            "expires": expires if isinstance(expires, str) and TIMESTAMP.fullmatch(expires) else None}


def _read_archive(data):
    """Return (member names, bounded bytes of the members publication reads)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if (not entries or len(set(names)) != len(names) or not set(names) <= ARCHIVE_MEMBERS):
                raise ArtifactProblem(MALFORMED)
            files = {}
            for entry in entries:
                # upload-artifact writes stored or deflated members; nothing else is decoded.
                if (entry.flag_bits & 0x1 or entry.is_dir()
                        or entry.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}):
                    raise ArtifactProblem(MALFORMED)
                limit = MEMBER_LIMITS.get(entry.filename)
                if limit is None:
                    continue
                if entry.file_size > limit:
                    raise ArtifactProblem(MALFORMED)
                with archive.open(entry) as stream:
                    content = stream.read(limit + 1)
                if len(content) > limit:
                    raise ArtifactProblem(MALFORMED)
                files[entry.filename] = content
            return set(names), files
    except (zipfile.BadZipFile, zlib.error, struct.error, EOFError, NotImplementedError, RuntimeError,
            ValueError, OverflowError, OSError):
        raise ArtifactProblem(MALFORMED) from None


def _unique_keys(pairs):
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("Duplicate JSON key")
    return dict(pairs)


def _reject_constant(value):
    raise ValueError(f"Unsupported JSON constant {value}")


def _counts(value, trials):
    if not isinstance(value, dict) or set(value) != {"complete", "total"}:
        return False
    complete, total = value["complete"], value["total"]
    return type(complete) is int and type(total) is int and total == trials and 0 <= complete <= total


def _parse_result(data, selection, names):
    """Validate result.json against the selection and the run's status semantics."""
    if data is None:
        raise ArtifactProblem(INVALID_RESULT)
    try:
        result = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_keys,
                            parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ArtifactProblem(INVALID_RESULT) from None
    keys = {"schema", "selection", "status", "trials", "extraction", "runner_exit", "error",
            "report_available"}
    if not isinstance(result, dict) or set(result) != keys:
        raise ArtifactProblem(INVALID_RESULT)
    trials, error, exit_code = result["trials"], result["error"], result["runner_exit"]
    if (type(result["schema"]) is not int or result["schema"] != 1
            or not isinstance(result["status"], str) or result["status"] not in {"complete", "partial", "failed"}
            or not isinstance(result["extraction"], str)
            or result["extraction"] not in {"complete", "unavailable"}
            or type(result["report_available"]) is not bool
            or not (exit_code is None or (type(exit_code) is int and -1024 <= exit_code <= 1024))
            or not (error is None or (isinstance(error, str) and 0 < len(error) <= 500
                                      and error.isprintable()))
            or not isinstance(trials, dict) or set(trials) != {"before", "after"}
            or not all(_counts(trials[side], selection["trials"]) for side in trials)):
        raise ArtifactProblem(INVALID_RESULT)
    if not isinstance(result["selection"], dict) or (
            json.dumps(result["selection"], sort_keys=True) != json.dumps(selection, sort_keys=True)):
        raise ArtifactProblem(FOREIGN_RESULT)
    finished = (exit_code == 0 and result["extraction"] == "complete"
                and all(trials[side]["complete"] == trials[side]["total"] for side in trials))
    status, available = result["status"], result["report_available"]
    consistent = {
        "complete": finished and available and error is None,
        "partial": available and error is not None,
        "failed": not available and error is not None,
    }[status]
    # Report files exist exactly when the run declares them available; never show a stale report.
    members = REPORT_MEMBERS <= names if available else not names & REPORT_MEMBERS
    if not consistent or not members:
        raise ArtifactProblem(INCONSISTENT)
    return result


def _evidence(api, selection):
    """Collect what this run attempt can honestly show; artifact problems become a failed status."""
    artifact = None
    try:
        artifact = _find_artifact(api, selection)
        if artifact["size"] > ARCHIVE_LIMIT:
            raise ArtifactProblem(TOO_LARGE)
        names, files = _read_archive(_download(api, selection["repository"], artifact["id"]))
        result = _parse_result(files.get("result.json"), selection, names)
    except ArtifactProblem as problem:
        return {"status": "failed", "problem": str(problem), "artifact": artifact, "result": None,
                "summary": None, "note": None, "html_artifact": None, "html_problem": None}
    if result["report_available"]:
        summary, note = summary_section(files["report.md"])
    else:
        summary, note = None, "This run attempt produced no report."
    html_artifact, html_problem = None, None
    if result["report_available"]:
        try:
            html_artifact = _find_artifact(api, selection, html_report=True)
        except ArtifactProblem as problem:
            html_problem = str(problem)
    return {"status": result["status"], "problem": None, "artifact": artifact, "result": result,
            "summary": summary, "note": note, "html_artifact": html_artifact,
            "html_problem": html_problem}


def _status_lines(evidence):
    if evidence["problem"]:
        return [f"**Run status: Failed.** {evidence['problem']} No evidence from this run attempt is shown."]
    result = evidence["result"]
    trials = result["trials"]
    exit_code = result["runner_exit"]
    parts = [f"Before {trials['before']['complete']}/{trials['before']['total']} trials complete",
             f"After {trials['after']['complete']}/{trials['after']['total']} trials complete",
             f"behavior extraction {result['extraction']}",
             "report available" if result["report_available"] else "report unavailable",
             "runner exit code unavailable" if exit_code is None else f"runner exit code {exit_code}"]
    lines = [f"**Run status: {result['status'].capitalize()}** — " + " · ".join(parts) + "."]
    if result["error"]:
        lines.append(f"**Reported problem:** {_text(result['error'])}")
    return lines


def _comment(selection, evidence, run_url):
    path, previous = selection["path"], selection["previous_path"]
    merge_base, head = _code(selection["merge_base"]), _code(selection["head"])
    if selection["change"] == "A":
        before = (f"- **Before** — merge-base {merge_base}: an empty file at {_code(path)}. The target did "
                  "not exist at the merge-base, and an empty file is not the same as no file.")
    elif selection["change"] == "R":
        before = f"- **Before** — {_code(previous)} at merge-base {merge_base} (renamed in this PR)."
    else:
        before = f"- **Before** — {_code(path)} at merge-base {merge_base}."
    artifact, result = evidence["artifact"], evidence["result"]
    expiry = f"; expires {artifact['expires']}" if artifact and artifact["expires"] else ""
    html_artifact = evidence["html_artifact"]
    if html_artifact and result and result["report_available"]:
        html_expiry = f"; expires {html_artifact['expires']}" if html_artifact["expires"] else ""
        report = (f"- **Full report** — [HTML report]({html_artifact['url']}) (single HTML file, no ZIP; "
                  f"requires GitHub sign-in and repository access{html_expiry}). "
                  f"[Evidence ZIP]({artifact['url']}).")
    elif artifact and result and result["report_available"]:
        report = (f"- **Full report** — separate HTML artifact unavailable: {evidence['html_problem']} "
                  f"Download the [evidence ZIP]({artifact['url']}) and open `report.html` "
                  f"(requires repository access{expiry}).")
    elif artifact and result:
        report = (f"- **Full report** — not produced; the [run artifact]({artifact['url']}) holds only the "
                  f"result record (requires repository access{expiry}).")
    elif artifact:
        report = (f"- **Full report** — not verified; the [run artifact]({artifact['url']}) failed the "
                  f"checks above (requires repository access{expiry}).")
    else:
        report = "- **Full report** — not available for this run attempt."
    run = f"- **Workflow run** — [run {selection['run_id']}, attempt {selection['run_attempt']}]({run_url})."
    if evidence["summary"]:
        summary = [evidence["summary"], "",
                   "Comparison references and omitted section links refer to the full report."]
    elif evidence["problem"]:
        summary = []
    else:
        summary = ["## Summary", "", f"Summary not shown. {evidence['note']} See the full report."]
    lines = [
        f"<!-- behavior-diff:v1 target={selection['id']} run={selection['run_id']} "
        f"attempt={selection['run_attempt']} -->",
        f"## Behavior Diff · {_code(path)}",
        "",
        *_status_lines(evidence),
        "",
        before,
        f"- **After** — {_code(path)} at PR head {head}.",
        "- **Scope** — only this file differs between Before and After. Every other file stays at the "
        "merge-base, so other changes in this PR, including files this instruction references, are not "
        "tested.",
        report,
        run,
        "",
        *summary,
        "",
        "---",
        f"<sub>Generated deterministically from this run's result record and Behavior Diff core report "
        f"(selected core {_code(selection['core_revision'])}); no model wrote this comment. Observed trial behavior is "
        "evidence to review, not a pass/fail verdict.</sub>",
    ]
    body = "\n".join(line for index, line in enumerate(lines)
                     if line or (index and lines[index - 1]))
    if len(body) > COMMENT_LIMIT:
        raise ValueError("Comment body exceeds GitHub limit")
    return body


def _verify_run(api, selection):
    """The selection is already bound to this environment; confirm GitHub agrees on the run."""
    run = api.request("GET", f"/repos/{selection['repository']}/actions/runs/{selection['run_id']}")
    # run.head_sha is the workflow commit (the default branch for issue_comment), not the PR head.
    if (not isinstance(run, dict) or type(run.get("id")) is not int or run["id"] != int(selection["run_id"])
            or type(run.get("run_attempt")) is not int or run["run_attempt"] != int(selection["run_attempt"])
            or not _same_repository(_get(run, "repository", "full_name"), selection["repository"])
            or run.get("event") not in RUN_EVENTS or run.get("status") == "completed"):
        raise ValueError("Workflow run metadata does not match this selection")


def _marker_order(comment, target):
    """Return (run, attempt) for a target comment owned by the workflow bot, else None."""
    user = _get(comment, "user")
    body = _get(comment, "body")
    if (_get(user, "login") != BOT_LOGIN or _get(user, "type") != "Bot" or not isinstance(body, str)
            or type(_get(comment, "id")) is not int):
        return None
    match = MARKER.fullmatch(body.split("\n", 1)[0].rstrip("\r"))
    if not match or match.group(1) != target:
        return None
    return int(match.group(2)), int(match.group(3))


def publish(selection, api):
    selection = validate_selection(selection)
    policy = input_value("publication", "none")
    if policy not in {"none", "artifact", "comment"}:
        raise ValueError("publication must be none, artifact, or comment")
    repository, pr = selection["repository"], selection["pr"]
    run_url = f"{SERVER}/{repository}/actions/runs/{selection['run_id']}/attempts/{selection['run_attempt']}"
    outputs = {"status": "skipped", "report-status": "", "comment-url": "", "report-url": "",
               "run-url": run_url}
    if policy != "comment":
        return outputs
    _verify_run(api, selection)
    evidence = _evidence(api, selection)
    outputs["report-status"] = evidence["status"]
    if evidence["html_artifact"]:
        outputs["report-url"] = evidence["html_artifact"]["url"]
    body = _comment(selection, evidence, run_url)

    pull = api.request("GET", f"/repos/{repository}/pulls/{pr}")
    if (not isinstance(pull, dict) or type(pull.get("number")) is not int or pull["number"] != pr
            or not _same_repository(_get(pull, "base", "repo", "full_name"), repository)):
        raise ValueError("Pull request does not match this selection")
    if _get(pull, "head", "sha") != selection["head"]:
        # A result for an older head must not replace or add to the current discussion.
        outputs["status"] = "stale"
        return outputs

    ours = (int(selection["run_id"]), int(selection["run_attempt"]))
    owned = []
    for comment in api.paginate(f"/repos/{repository}/issues/{pr}/comments"):
        order = _marker_order(comment, selection["id"])
        if order is not None:
            owned.append((order, comment["id"]))
    if owned:
        order, comment_id = max(owned)
        if order > ours:
            outputs["status"] = "superseded"
            return outputs
        written = api.request("PATCH", f"/repos/{repository}/issues/comments/{comment_id}", {"body": body})
    else:
        written = api.request("POST", f"/repos/{repository}/issues/{pr}/comments", {"body": body})
    url = _get(written, "html_url")
    if not isinstance(url, str) or not re.fullmatch(r"https://github\.com/[!-~]+", url):
        raise ValueError("Unexpected GitHub comment response")
    if _get(written, "user", "login") != BOT_LOGIN:
        raise ValueError("Comment publication requires the workflow GITHUB_TOKEN")
    outputs["status"] = "published"
    outputs["comment-url"] = url
    return outputs
