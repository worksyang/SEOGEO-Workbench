#!/usr/bin/env python3
import sys

from web.run import main as run_web
from web.ui import main as run_demo


def main() -> int:
    if not sys.argv[1:]:
        return run_web()
    if sys.argv[1:] == ["--demo"]:
        return run_demo()
    print("用法：python3 run.py [--demo]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
