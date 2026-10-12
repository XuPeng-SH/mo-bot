#!/usr/bin/env python3
"""Source installation entry; shares the CLI installation implementation."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mo_bot.cli import main

if __name__ == "__main__":
    sys.argv.insert(1, "install")
    main()
