"""Absolute privileged entrypoint; also usable with Python's isolated mode."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fucc.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
