#!/usr/bin/env python3
"""DataRec 命令行入口，直接 `python3 datarec.py ...` 即可。"""
import sys

from datarec.cli import main

if __name__ == "__main__":
    sys.exit(main())
