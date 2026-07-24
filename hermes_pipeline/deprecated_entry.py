"""Deprecated console-script entry points for the pre-tpo CLI names.

Kept for one release after the TODO-33 rename so existing installs of
pipeline-watch/hermes-pipeline keep working while users migrate to `tpo`.
Remove both this module and their [project.scripts] entries in the
following version bump.
"""
from __future__ import annotations

import sys

from .cli import main


def _deprecated_main(old_name: str) -> int:
    print(
        f"`{old_name}` is deprecated, use `tpo` instead. "
        f"This alias will be removed in a future release.",
        file=sys.stderr,
    )
    try:
        return main()
    except SystemExit as e:
        return e.code if e.code is not None else 0


def pipeline_watch_deprecated() -> int:
    return _deprecated_main("pipeline-watch")


def hermes_pipeline_deprecated() -> int:
    return _deprecated_main("hermes-pipeline")
