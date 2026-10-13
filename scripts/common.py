"""Small, credential-conscious primitives shared by the Action's three phases."""
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

CORE_REVISION = "5b325ed40062bb819c574e4172da04f53b53b953"
CLAUDE_VERSION = "2.1.296"


def input_value(name, default=""):
    return os.environ.get("BD_" + name.upper().replace("-", "_"), default)


def integer(value, name, minimum, maximum):
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)):
        raise ValueError(f"{name} must be an integer")
    number = int(value)
    if not minimum <= number <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return number


def sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("Expected a full lowercase 40-character commit SHA")
    return value


def safe_path(value):
    if (not isinstance(value, str) or not value or len(value.encode()) > 1024
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or "\\" in value or value.startswith(("/", "-"))
            or any(p in ("", ".", "..", ".git") for p in value.split("/"))):
        raise ValueError("Unsafe repository-relative path")
    return value


def repository_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise ValueError("Invalid GitHub repository identity")
    if any(p in (".", "..") for p in value.split("/")):
        raise ValueError("Invalid GitHub repository identity")
    return value


def target_id(path):
    return hashlib.sha256(path.encode()).hexdigest()[:16]


def validate_selection(value):
    fields = {"schema", "repository", "pr", "head", "base", "merge_base", "path",
              "previous_path", "change", "trials", "id", "run_id", "run_attempt"}
    if not isinstance(value, dict) or set(value) != fields or value["schema"] != 1:
        raise ValueError("Invalid selection schema")
    repository_name(value["repository"])
    for key in ("head", "base", "merge_base"):
        sha(value[key])
    for key in ("path", "previous_path"):
        safe_path(value[key])
    if PurePosixPath(value["path"]).name not in {"SKILL.md", "AGENTS.md", "CLAUDE.md"}:
        raise ValueError("Unsupported instruction target")
    if value["change"] not in {"A", "M", "R"} or value["id"] != target_id(value["path"]):
        raise ValueError("Invalid selection change or target identity")
    if value["change"] != "R" and value["previous_path"] != value["path"]:
        raise ValueError("Only renames may change target paths")
    for key, maximum in (("pr", 2**53 - 1), ("trials", 3)):
        if type(value[key]) is not int:
            raise ValueError(f"{key} must be a JSON integer")
        integer(value[key], key, 1, maximum)
    for key in ("run_id", "run_attempt"):
        if not isinstance(value[key], str):
            raise ValueError("Run identity must be a string")
        integer(value[key], key, 1, 2**63 - 1)
    for field, env in (("repository", "GITHUB_REPOSITORY"), ("run_id", "GITHUB_RUN_ID"),
                       ("run_attempt", "GITHUB_RUN_ATTEMPT")):
        if value[field] != os.environ.get(env):
            raise ValueError("Selection does not belong to this workflow invocation")
    return value


def output(name, value):
    if not re.fullmatch(r"[a-z][a-z0-9-]*", name):
        raise ValueError("Invalid output name")
    if not isinstance(value, str):
        value = json.dumps(value, separators=(",", ":"))
    if "\n" in value or "\r" in value:
        raise ValueError("Outputs must be single-line values")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        stream.write(f"{name}={value}\n")


def write_private(path, content):
    path = Path(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(content.encode() if isinstance(content, str) else content)


def private_directory(prefix="behavior-diff-"):
    root = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir())).resolve()
    workspace = os.environ.get("GITHUB_WORKSPACE")
    if workspace and root.is_relative_to(Path(workspace).resolve()):
        raise ValueError("RUNNER_TEMP must be outside the comparison workspace")
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=root))
    path.chmod(0o700)
    return path


def clean_env(home=None):
    result = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")
              if key in os.environ}
    result.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                   "GIT_TERMINAL_PROMPT": "0", "GIT_ATTR_NOSYSTEM": "1"})
    if home is not None:
        result["HOME"] = str(home)
    return result


def run_command(argv, cwd=None, env=None, timeout=300, check=True, input=None):
    with subprocess.Popen([str(x) for x in argv], cwd=cwd, env=env if env is not None else clean_env(),
                          stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, start_new_session=True) as process:
        try:
            stdout, stderr = process.communicate(input=input, timeout=timeout)
        except BaseException:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise
    result = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
    if check and result.returncode:
        raise RuntimeError(f"{Path(str(argv[0])).name} failed (exit {result.returncode}); output withheld for privacy")
    return result


def git(args, cwd, env=None, timeout=300, deadline=None):
    if deadline is not None:
        timeout = min(timeout, deadline - time.monotonic())
        if timeout <= 0:
            raise subprocess.TimeoutExpired("git", 0)
    return run_command(["git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.file.allow=never",
                        "-c", "core.quotePath=false", *args], cwd=cwd, env=env, timeout=timeout).stdout


def fetch_repository(directory, repository, head, base, token, timeout=300):
    repository_name(repository)
    sha(head)
    sha(base)
    deadline = time.monotonic() + timeout

    def remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise subprocess.TimeoutExpired("git", timeout)
        return seconds

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    git(["init", "-q"], directory, timeout=remaining())
    env = clean_env()
    if token:
        encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                    "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {encoded}"})
    git(["fetch", "--no-tags", "--", f"https://github.com/{repository}.git", head, base],
        directory, env, timeout=remaining())
    for commit in (head, base):
        if git(["rev-parse", f"{commit}^{{commit}}"], directory, timeout=remaining()).decode().strip() != commit:
            raise ValueError("Fetched commit identity mismatch")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHub:
    def __init__(self, token):
        if not token:
            raise ValueError("github-token is required")
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, method, path, data=None):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("GitHub API path must be relative")
        request = urllib.request.Request("https://api.github.com" + path,
            data=None if data is None else json.dumps(data).encode(), method=method,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "behavior-diff-action",
                     "Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise ValueError("GitHub API response exceeds limit")
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"GitHub API {method} failed (HTTP {exc.code})") from None

    def paginate(self, path):
        result = []
        separator = "&" if "?" in path else "?"
        for page in range(1, 101):
            items = self.request("GET", f"{path}{separator}per_page=100&page={page}")
            if not isinstance(items, list):
                raise ValueError("Expected paginated GitHub list")
            result.extend(items)
            if len(items) < 100:
                return result
        raise ValueError("GitHub pagination exceeds safety limit")
