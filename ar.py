#!/usr/bin/env python3
"""入口脚本 —— 让 `python ar.py <命令>` 在任何目录下都能跑。"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "src"))

from ar_agent.cli import main  # noqa: E402

raise SystemExit(main())
