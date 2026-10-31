#!/usr/bin/env python3
"""Entry point, so that `python ar.py <command>` works from any directory."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "src"))

from ar_agent.cli import main  # noqa: E402

raise SystemExit(main())
