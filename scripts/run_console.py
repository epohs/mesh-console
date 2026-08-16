#!/usr/bin/env python3
"""
Entry point for the mesh-console terminal interface.

This script exists for:
  - local development convenience
  - running over SSH without installing the package

All real logic lives in mesh_console/ui/__init__.py
"""

import sys
from pathlib import Path

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
  sys.path.insert(0, str(PROJECT_ROOT))

from mesh_console.ui import main

if __name__ == "__main__":
  main()
