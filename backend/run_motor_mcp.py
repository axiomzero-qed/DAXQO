#!/usr/bin/env python3
"""Launch the bundled Motor independent of task cwd and inherited PYTHONPATH."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

if __name__ == "__main__":
    from motor.mcp_server import main
    raise SystemExit(main())
