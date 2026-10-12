#!/usr/bin/env python3
"""Remove execution material while keeping explicitly exposed local reports."""
import os
from pathlib import Path
import sys

from execute import cleanup_scratch

if __name__ == "__main__":
    try:
        scratch = Path(os.environ["BD_SCRATCH"])
        cleanup_scratch(scratch, scratch / "report")
    except Exception:
        print("::error::Private scratch cleanup failed; discard this ephemeral runner.", file=sys.stderr)
        sys.exit(1)
