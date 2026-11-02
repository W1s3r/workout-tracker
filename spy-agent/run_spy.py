"""Launcher used by the `spy` command (see install.py). Runs Spy from this folder
with whatever Python runs this file, without needing pip or PYTHONPATH."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from spy.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
