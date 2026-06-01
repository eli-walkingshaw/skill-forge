#!/usr/bin/env python3
"""Convenience launcher. Symlink this to somewhere on your PATH."""
import sys
from forge.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
