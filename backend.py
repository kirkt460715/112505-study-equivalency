#!/usr/bin/env python3
"""Convenience entry point: ``python backend.py`` starts the JSON backend.

Equivalent to ``python -m studyvisit serve``.  Use ``python backend.py scenario``
to print the canonical fixture report instead.
"""

import sys

from studyvisit.__main__ import main

if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        args = ["serve"]
    raise SystemExit(main(args))
