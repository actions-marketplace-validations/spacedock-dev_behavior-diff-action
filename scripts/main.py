#!/usr/bin/env python3
"""Composite Action entrypoint. Errors never echo repository/model output or secrets."""
import json
import os
from pathlib import Path
import sys

from common import GitHub, input_value, integer, output, validate_selection


def supported_environment():
    if (sys.platform != "linux" or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
            or os.environ.get("GITHUB_SERVER_URL") != "https://github.com"
            or os.environ.get("GITHUB_API_URL") != "https://api.github.com"):
        raise ValueError("Only GitHub.com hosted Ubuntu 24.04 runners are supported")
    release = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines() if "=" in line)
    if release.get("ID", "").strip('"') != "ubuntu" or release.get("VERSION_ID", "").strip('"') != "24.04":
        raise ValueError("Use runs-on: ubuntu-24.04")


def main():
    os.umask(0o077)
    try:
        supported_environment()
        mode = input_value("mode")
        if mode == "finalize":
            return 0 if input_value("status") in {"complete", "ready", "skipped", "published", "superseded"} else 1
        if mode not in {"prepare", "run", "publish"}:
            raise ValueError("mode must be prepare, run, or publish")
        if input_value("publication", "none") not in {"none", "artifact", "comment"}:
            raise ValueError("publication must be none, artifact, or comment")
        integer(input_value("retention-days", "7"), "retention-days", 1, 30)
        integer(input_value("timeout-minutes", "45"), "timeout-minutes", 1, 50)
        if mode != "run" and any(os.environ.get(key) for key in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")):
            raise ValueError("Model credentials must be scoped only to the execution job")
        api = GitHub(input_value("github-token"))
        if mode == "prepare":
            from prepare import prepare
            results = prepare(api)
        else:
            selection = validate_selection(json.loads(input_value("selection")))
            if mode == "run":
                from execute import execute
                results = execute(selection, api)
            else:
                from publish import publish
                results = publish(selection, api)
        for key, value in results.items():
            output(key, value)
        return 0
    except Exception as error:
        # Validation/transport errors are authored here; never print subprocess output or tracebacks.
        detail = str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__
        detail = detail.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error::Behavior Diff failed: {detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
